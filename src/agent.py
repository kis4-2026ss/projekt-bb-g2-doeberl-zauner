import asyncio
import os
import sys
import copy
import datetime
import json
import base64
import time
import re

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    print("Fehlende Abhängigkeiten! Bitte installiere sie mit:")
    print("pip install mcp")
    sys.exit(1)

# Optionale Imports - werden je nach Backend benötigt
try:
    import ollama
except ImportError:
    ollama = None

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SERVICES_SCRIPT = os.path.join(os.path.dirname(__file__), "services.py")
TOKENS_FILE = os.path.join(PROJECT_ROOT, "TOKENS.txt")
OPENAI_REASONING = {"effort": "low"}
# OPENAI_REASONING = {"effort": "medium"}
# OPENAI_REASONING = {"effort": "high"}
# OPENAI_REASONING = {"effort": "xhigh"}

# none	Latency-critical tasks that do not benefit from any reasoning or multi-chained tool calls. For latency-sensitive use cases with gpt-5.5, we recommend trying low to begin with and then moving to none if required.
#   Common use cases include voice, fast information retrieval, and classification.

# low	Efficient reasoning with a modest latency increase. Ideal for use cases requiring tool-use, planning, search, or multi-step decision making, while optimizing for speed and cost.
#   Common use cases include data analysis, drafting, execution-oriented coding, and customer support / chat assistant workflows.

# medium	When quality and reliability matter, and the task involves planning, complex reasoning, and judgement. Default configuration for most workloads, and a well-balanced point on the pareto curve of latency, performance and cost.
#   Common use cases include agentic coding, research, working with spreadsheets & slides, and delegating long-horizon work.

# high	Hard reasoning, complex debugging, deep planning, and high-value tasks where quality and intelligence matters more than latency. Recommended for complex workflows and agentic tasks.
#   Common use cases include agentic coding, long-horizon research, and knowledge work. Depending on the complexity of the task, evaluate both medium and high.

# xhigh	Deep research, asynchronous workflows and agentic tasks that require very long rollouts. Only use when your evals show a clear benefit that justifies the extra latency and cost.
#   Common use cases include security and code review, enterprise productivity, deeper research tasks, and challenging coding workflows.

# Globaler OpenAI Client (wird bei Bedarf initialisiert)
_openai_client = None


def sanitizeSpeechText(text):
    """Bereitet Modelltext fuer Text-to-Speech auf."""
    if not text:
        return ""
    cleanText = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    cleanText = re.sub(r"`([^`]*)`", r"\1", cleanText)
    cleanText = re.sub(r"[*_#>\[\]{}]", " ", cleanText)
    cleanText = re.sub(r"https?://\S+", "Link", cleanText)
    cleanText = re.sub(r"\s+", " ", cleanText).strip()
    return cleanText[:1200]


class VoiceOutput:
    """Gibt Modelltexte optional ueber die lokale Windows-Sprachausgabe aus."""
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.engine = None
        if self.enabled:
            self.initializeEngine()

    def initializeEngine(self):
        """Initialisiert die lokale Text-to-Speech-Engine."""
        try:
            import win32com.client
            self.engine = win32com.client.Dispatch("SAPI.SpVoice")
        except Exception as e:
            self.enabled = False
            print(f"Warnung: Voice-Ausgabe konnte nicht initialisiert werden: {e}")

    def speakText(self, text):
        """Spricht einen Text, wenn Voice aktiviert ist."""
        if not self.enabled or self.engine is None:
            return
        speechText = sanitizeSpeechText(text)
        if not speechText:
            return
        try:
            self.engine.Rate = 4 # Sprechgeschwindigkeit
            self.engine.Speak(speechText, 1)
        except Exception as e:
            self.enabled = False
            print(f"Warnung: Voice-Ausgabe wurde deaktiviert: {e}")

def _init_openai(api_key=None):
    """Initialisiert den OpenAI Client. Nur bei selfhosted=False nötig."""
    global _openai_client
    if _openai_client is not None:
        return
    try:
        from openai import OpenAI
    except ImportError:
        print("❌ OpenAI Paket nicht installiert! pip install openai")
        sys.exit(1)
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        print("❌ Kein OpenAI API Key! Setze OPENAI_API_KEY oder nutze --api-key.")
        sys.exit(1)
    _openai_client = OpenAI(api_key=key)


# ─────────────────────────────────────────────────────────
#  Token-Logging
# ─────────────────────────────────────────────────────────

def _log_tokens(model_name, response, selfhosted, task_name="Unbekannt"):
    """
    Extrahiert Token-Nutzung aus der API-Antwort und schreibt sie in TOKENS.txt.
    Format: Zeitstempel | Model | Task | InputTokens | CachedTokens | OutputTokens | GesamtTokens
    """
    try:
        if selfhosted:
            # Ollama: prompt_eval_count = Input, eval_count = Output
            input_tokens = response.get('prompt_eval_count', 0) or 0
            output_tokens = response.get('eval_count', 0) or 0
            cached_tokens = 0  # Ollama hat kein Caching-Konzept
        else:
            # OpenAI Responses API: usage Objekt mit input_tokens, output_tokens, etc.
            usage = response.usage
            if isinstance(usage, dict):
                input_tokens = usage.get('input_tokens')
                output_tokens = usage.get('output_tokens')
                details = usage.get('input_tokens_details') or usage.get('prompt_tokens_details')
            else:
                input_tokens = getattr(usage, 'input_tokens', None)
                output_tokens = getattr(usage, 'output_tokens', None)
                details = getattr(usage, 'input_tokens_details', None)
                if details is None:
                    details = getattr(usage, 'prompt_tokens_details', None)
            if input_tokens is None:
                input_tokens = (usage.get('prompt_tokens', 0) if isinstance(usage, dict) else getattr(usage, 'prompt_tokens', 0)) or 0
            if output_tokens is None:
                output_tokens = (usage.get('completion_tokens', 0) if isinstance(usage, dict) else getattr(usage, 'completion_tokens', 0)) or 0
            if details:
                cached_tokens = (details.get('cached_tokens', 0) if isinstance(details, dict) else getattr(details, 'cached_tokens', 0)) or 0
            else:
                cached_tokens = 0

        total_tokens = input_tokens + output_tokens
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        line = f"{timestamp} | {model_name} | {task_name} | Input: {input_tokens} | Cached: {cached_tokens} | Output: {output_tokens} | Gesamt: {total_tokens}\n"

        # Header schreiben falls Datei noch nicht existiert
        write_header = not os.path.exists(TOKENS_FILE)
        with open(TOKENS_FILE, "a", encoding="utf-8") as f:
            if write_header:
                f.write("Zeitstempel              | Model                | Task                 | InputTokens | CachedTokens | OutputTokens | GesamtTokens\n")
                f.write("-" * 130 + "\n")
            f.write(line)

        print(f"📊 Tokens: Input={input_tokens}, Cached={cached_tokens}, Output={output_tokens}, Gesamt={total_tokens}")
        return {
            "input": input_tokens,
            "cached": cached_tokens,
            "output": output_tokens,
            "total": total_tokens
        }
    except Exception as e:
        print(f"⚠️ Token-Logging fehlgeschlagen: {e}")
        return {"input": 0, "cached": 0, "output": 0, "total": 0}


def _save_screenshot_from_b64(image_b64, directory, filename):
    """Decodiert ein Base64-Bild und speichert es als PNG im angegebenen Verzeichnis."""
    try:
        file_path = os.path.join(directory, filename)
        with open(file_path, "wb") as f:
            f.write(base64.b64decode(image_b64))
        return file_path
    except Exception as e:
        print(f"⚠️ Fehler beim Speichern des Screenshots {filename}: {e}")
        return None


def convert_to_serializable(value):
    """Wandelt SDK-Objekte in JSON-kompatible Strukturen um."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, dict):
        return {key: convert_to_serializable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [convert_to_serializable(item) for item in value]
    if isinstance(value, tuple):
        return [convert_to_serializable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: convert_to_serializable(item) for key, item in value.__dict__.items()}
    return value


def create_result(success, steps_taken, reason, messages, selfhosted, total_tokens_used, saved_screenshots, model_interactions):
    """Erstellt die einheitliche Ergebnisstruktur eines Agenten-Durchlaufs."""
    return {
        "success": success,
        "steps_taken": steps_taken,
        "reason": reason,
        "log": get_clean_conversation_log(messages, selfhosted),
        "tokens": total_tokens_used,
        "screenshots": saved_screenshots,
        "model_interactions": model_interactions
    }


def format_task_pokemon_context(task_pokemon):
    """Formatiert die Pokemon- und Attacken-Slots aus der Task-Konfiguration."""
    if not task_pokemon:
        return "In diesem Task wurden keine Pokemon-Slots konfiguriert."

    lines = ["Pokemon-Slots aus der Task-Konfiguration:"]
    for index, pokemon in enumerate(task_pokemon, start=1):
        if not isinstance(pokemon, dict):
            lines.append(f"Pokemon {index}: {pokemon}")
            continue

        pokemon_id = pokemon.get("id", index)
        name = pokemon.get("name", "Unbekannt")
        level = pokemon.get("level")
        attacks = pokemon.get("attacks", pokemon.get("moves", []))
        level_text = f" Lv. {level}" if level is not None else ""
        lines.append(f"Pokemon {pokemon_id}: {name}{level_text}")

        if not attacks:
            lines.append("  Attacken: nicht konfiguriert")
            continue

        for attack_index, attack in enumerate(attacks, start=1):
            if isinstance(attack, dict):
                attack_id = attack.get("id", attack_index)
                attack_name = attack.get("name", "Unbekannt")
                attack_type = attack.get("type")
                attack_text = f"  Attacke {attack_id}: {attack_name}"
                if attack_type:
                    attack_text += f" ({attack_type})"
                lines.append(attack_text)
            else:
                lines.append(f"  Attacke {attack_index}: {attack}")

    lines.append("Pokemon-IDs gehen von 1 bis 6. Attacken-IDs gehen von 1 bis 4.")
    lines.append("attack_pokemon nutzt die Attacken-ID des aktuell aktiven Pokemon als Slot.")
    lines.append("switch_pokemon nutzt die Pokemon-ID als Slot.")
    return "\n".join(lines)


def build_pokemon_context(task_pokemon=None):
    """Erstellt den Pokemon- und Attacken-Kontext aus der Task-Konfiguration."""
    return (
        f"{format_task_pokemon_context(task_pokemon)}\n\n"
        "Wenn du attack_pokemon nutzt, waehle den Slot der gewuenschten Attacke:\n"
        "1 = oben links, 2 = oben rechts, 3 = unten links, 4 = unten rechts.\n"
        "Wenn du switch_pokemon nutzt, waehle den Pokemon-Slot 1 bis 6."
    )


def build_openai_instructions(system_prompt, task_pokemon=None):
    """Erstellt die Instructions fuer die OpenAI Responses API."""
    return f"{system_prompt}\n\n{build_pokemon_context(task_pokemon)}"


def convert_tools_to_responses_tools(api_tools):
    """Wandelt Chat-Completions-Tools in Responses-API-Tools um."""
    responses_tools = []
    for tool in api_tools:
        function_data = tool.get("function", {})
        responses_tools.append({
            "type": "function",
            "name": function_data.get("name"),
            "description": function_data.get("description", ""),
            "parameters": function_data.get("parameters", {"type": "object", "properties": {}})
        })
    return responses_tools


def create_openai_user_input(content, image_b64=None):
    """Erstellt eine User-Message fuer die OpenAI Responses API."""
    if image_b64:
        return {
            "role": "user",
            "content": [
                {"type": "input_text", "text": content},
                {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"}
            ]
        }
    return {"role": "user", "content": content}


def sanitize_response_output_item(item):
    """Entfernt Response-Only-Felder, bevor ein Output-Item wieder als Input gesendet wird."""
    item_type = item.get("type")
    if item_type == "function_call":
        sanitized = {
            "type": "function_call",
            "call_id": item.get("call_id"),
            "name": item.get("name"),
            "arguments": item.get("arguments") or "{}"
        }
        if item.get("id"):
            sanitized["id"] = item.get("id")
        return sanitized

    if item_type == "message":
        return {
            "type": "message",
            "role": item.get("role", "assistant"),
            "content": item.get("content", [])
        }

    if item_type == "reasoning":
        sanitized = {"type": "reasoning"}
        for key in ("id", "summary", "encrypted_content"):
            if key in item and item.get(key) is not None:
                sanitized[key] = item.get(key)
        return sanitized

    return {"type": item_type} if item_type else {}


def get_response_output_items(response):
    """Gibt die Output-Items einer Responses-API-Antwort serialisierbar zurueck."""
    output_items = getattr(response, "output", None) or []
    return convert_to_serializable(output_items)


def get_response_input_items(response):
    """Gibt Responses-Output-Items in API-kompatibler Input-Form zurueck."""
    input_items = []
    for item in get_response_output_items(response):
        sanitized_item = sanitize_response_output_item(item)
        if sanitized_item.get("type"):
            input_items.append(sanitized_item)
    return input_items


def get_response_text(response):
    """Extrahiert Text aus einer Responses-API-Antwort."""
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text

    output_items = get_response_output_items(response)
    text_parts = []
    for item in output_items:
        if item.get("type") != "message":
            continue
        for content_item in item.get("content", []):
            if content_item.get("type") in ("output_text", "text"):
                text_parts.append(content_item.get("text", ""))
    return "\n".join(part for part in text_parts if part)


def get_response_tool_calls(response):
    """Extrahiert Function Calls aus einer Responses-API-Antwort."""
    tool_calls = []
    for item in get_response_output_items(response):
        if item.get("type") != "function_call":
            continue
        tool_calls.append({
            "id": item.get("call_id"),
            "type": "function",
            "function": {
                "name": item.get("name"),
                "arguments": item.get("arguments") or "{}"
            }
        })
    return tool_calls


# ─────────────────────────────────────────────────────────
#  Hilfsfunktionen für Bild-Erkennung in beiden Formaten
# ─────────────────────────────────────────────────────────

def _msg_has_image(msg, selfhosted):
    """Prüft ob eine Nachricht ein Bild enthält (Ollama- oder OpenAI-Format)."""
    if selfhosted:
        return "images" in msg and msg["images"]
    else:
        if isinstance(msg.get("content"), list):
            return any(b.get("type") == "image_url" for b in msg["content"])
    return False


def cleanup_history_to_save_context(messages, selfhosted=True):
    """
    Entfernt alte Bilder aus der Historie, um zu verhindern, dass der RAM/Context-Window explodiert.
    Behält nur das allerletzte Bild.
    """
    image_indices = [i for i, msg in enumerate(messages) if _msg_has_image(msg, selfhosted)]
    if len(image_indices) > 1:
        for i in image_indices[:-1]:
            if selfhosted:
                del messages[i]["images"]
                messages[i]["content"] += " [Altes Bild wurde aus Speichergründen entfernt]"
            else:
                # OpenAI: content ist eine Liste -> Bild-Blöcke entfernen, Text behalten
                text_parts = [b["text"] for b in messages[i]["content"] if b.get("type") == "text"]
                messages[i]["content"] = " ".join(text_parts) + " [Altes Bild wurde aus Speichergründen entfernt]"
    return messages


def get_clean_conversation_log(messages, selfhosted=True):
    """
    Gibt eine bereinigte Kopie der Konversation zurück.
    Base64-Bilder werden durch Zeitstempel-Platzhalter ersetzt.
    """
    clean_messages = []

    for msg in messages:
        # Falls msg kein dict ist (z.B. ollama.Message), in ein dict konvertieren
        if not isinstance(msg, dict):
            if hasattr(msg, "model_dump"):
                msg_dict = msg.model_dump()
            elif hasattr(msg, "dict"):
                msg_dict = msg.dict()
            elif hasattr(msg, "__dict__"):
                msg_dict = dict(msg.__dict__)
            else:
                try:
                    msg_dict = dict(msg)
                except TypeError:
                    msg_dict = {}
                    for attr in ["role", "content", "images", "tool_calls"]:
                        if hasattr(msg, attr):
                            msg_dict[attr] = getattr(msg, attr)
        else:
            msg_dict = msg

        msg_copy = copy.deepcopy(msg_dict)
        timestamps = msg_copy.pop("image_timestamps", [])

        if selfhosted:
            if "images" in msg_copy and msg_copy["images"]:
                msg_copy["images"] = [
                    f"[BILD ENTFERNT FÜR LOG: {timestamps[i] if i < len(timestamps) else 'mcp_current_state_' + datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}]"
                    for i in range(len(msg_copy["images"]))
                ]
        else:
            if isinstance(msg_copy.get("content"), list):
                new_content, img_idx = [], 0
                for block in msg_copy["content"]:
                    if block.get("type") == "image_url":
                        ts = timestamps[img_idx] if img_idx < len(timestamps) else f"mcp_current_state_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
                        new_content.append({"type": "text", "text": f"[BILD ENTFERNT FÜR LOG: {ts}]"})
                        img_idx += 1
                    else:
                        new_content.append(block)
                msg_copy["content"] = new_content

        clean_messages.append(msg_copy)
    return clean_messages


# ─────────────────────────────────────────────────────────
#  Haupt-Agent
# ─────────────────────────────────────────────────────────

async def run_agent(model_name: str, system_prompt: str, max_steps: int,
                    debug: bool = False, selfhosted: bool = True, api_key: str = None,
                    task_name: str = "Unbekannt", screenshots_dir: str = None,
                    task_pokemon: list = None, log_dir: str = None,
                    auto_screenshot: bool = False, voice: bool = False) -> dict:
    """
    Startet einen Agenten-Durchlauf für eine spezifische Aufgabe.

    Args:
        selfhosted: True = Ollama (lokal), False = OpenAI API
        api_key: OpenAI API Key (nur bei selfhosted=False nötig, alternativ OPENAI_API_KEY Env-Var)
    """
    backend = "Ollama (lokal)" if selfhosted else "OpenAI API"
    effective_system_prompt = system_prompt
    if auto_screenshot:
        effective_system_prompt = (
            f"{system_prompt}\n\n"
            "Auto-Screenshot-Modus ist aktiv: get_state steht dir nicht als Tool zur Verfuegung. "
            "Du bekommst vor jeder Entscheidung automatisch einen aktuellen Screenshot und musst diesen analysieren."
        )
    print(f"\n🚀 Starte Agent mit Modell: '{model_name}' via {backend} (Max Steps: {max_steps})")
    voiceOutput = VoiceOutput(enabled=voice)

    # Initialisiere Token-Tracker und Screenshot-Liste
    total_tokens_used = {"input": 0, "cached": 0, "output": 0, "total": 0}
    saved_screenshots = []
    model_interactions = []
    
    model_io_log_file = os.path.join(log_dir, "model_io_log.txt") if log_dir else None
    tokens_file = os.path.join(log_dir, "tokens.json") if log_dir else None

    def write_json_file(file_path, data):
        if not file_path:
            return
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(convert_to_serializable(data), f, indent=4, ensure_ascii=False, default=str)

    def format_message_content(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    parts.append(str(block))
                elif block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    parts.append("[Bild]")
                else:
                    parts.append(str(block))
            return "\n".join(part for part in parts if part)
        return str(content)

    def format_request_for_text_log(request_messages):
        clean_request_messages = get_clean_conversation_log(request_messages, selfhosted)
        lines = []
        for message in clean_request_messages:
            role = message.get("role", "unknown")
            content = format_message_content(message.get("content", ""))
            if message.get("tool_calls"):
                content += f"\nTool Calls: {json.dumps(message.get('tool_calls'), ensure_ascii=False, default=str)}"
            if message.get("images"):
                content += f"\nBilder: {json.dumps(message.get('images'), ensure_ascii=False, default=str)}"
            lines.append(f"[{role}]\n{content}".strip())
        return "\n\n".join(lines)

    def format_response_for_text_log(response_payload):
        response_data = convert_to_serializable(response_payload)
        if isinstance(response_data, dict):
            output_text = response_data.get("output_text")
            if output_text:
                return output_text
            output_items = response_data.get("output")
            if output_items:
                lines = []
                for item in output_items:
                    if item.get("type") == "message":
                        for content_item in item.get("content", []):
                            if content_item.get("type") in ("output_text", "text"):
                                lines.append(content_item.get("text", ""))
                    elif item.get("type") == "function_call":
                        lines.append(
                            "Function Call: "
                            f"{item.get('name')}({item.get('arguments') or '{}'})"
                        )
                if lines:
                    return "\n".join(line for line in lines if line)
            if "message" in response_data:
                message = response_data.get("message") or {}
                content = format_message_content(message.get("content", ""))
                tool_calls = message.get("tool_calls")
                if tool_calls:
                    content += f"\nTool Calls: {json.dumps(tool_calls, ensure_ascii=False, default=str)}"
                return content.strip() or json.dumps(response_data, ensure_ascii=False, default=str)
            choices = response_data.get("choices")
            if choices:
                message = choices[0].get("message", {})
                content = format_message_content(message.get("content", ""))
                tool_calls = message.get("tool_calls")
                if tool_calls:
                    content += f"\nTool Calls: {json.dumps(tool_calls, ensure_ascii=False, default=str)}"
                return content.strip() or json.dumps(response_data, ensure_ascii=False, default=str)
        return json.dumps(response_data, ensure_ascii=False, default=str)

    def append_model_io_text_log(interaction, request_messages, response_payload):
        if not model_io_log_file:
            return
        os.makedirs(log_dir, exist_ok=True)
        with open(model_io_log_file, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"Zeit: {interaction['timestamp']}\n")
            f.write(f"Modell: {interaction['model']}\n")
            f.write(f"Task: {interaction['task']}\n")
            f.write(f"Phase: {interaction['phase']}\n")
            f.write(f"Schritt: {interaction['step']}\n")
            f.write("-" * 80 + "\n")
            f.write("Frage an das Modell:\n")
            f.write(format_request_for_text_log(request_messages))
            f.write("\n" + "-" * 80 + "\n")
            f.write("Antwort des Modells:\n")
            f.write(format_response_for_text_log(response_payload))
            f.write("\n" + "-" * 80 + "\n")
            f.write(f"Tokens: {json.dumps(interaction['tokens'], ensure_ascii=False, default=str)}\n")

    def persist_runtime_logs():
        if not log_dir:
            return
        os.makedirs(log_dir, exist_ok=True)
        write_json_file(tokens_file, total_tokens_used)

    def add_tokens(t_dict):
        if t_dict:
            for k in total_tokens_used:
                total_tokens_used[k] += t_dict.get(k, 0)

    def save_screenshot(image_b64, prefix, step_number):
        if not image_b64 or not screenshots_dir:
            return None
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        filename = f"{prefix}_{step_number}_{timestamp}.png"
        saved_path = _save_screenshot_from_b64(image_b64, screenshots_dir, filename)
        if saved_path:
            saved_screenshots.append(filename)
            return filename
        return None

    async def take_current_screenshot(session, prefix, step_number):
        """Erstellt einen aktuellen Screenshot ueber den MCP-Server."""
        image_b64 = None
        tool_result = await session.call_tool("get_state", {})
        for part in tool_result.content:
            if part.type == "image":
                image_b64 = part.data

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        label = f"mcp_current_state_{timestamp}"
        if image_b64:
            screenshot_filename = save_screenshot(image_b64, prefix, step_number)
            if screenshot_filename:
                label = screenshot_filename
        return image_b64, label

    def append_screenshot_message(content, image_b64, label):
        """Haengt einen Screenshot im passenden Modellformat an die Konversation an."""
        if not image_b64:
            return
        if selfhosted:
            messages.append({
                "role": "user",
                "content": content,
                "images": [image_b64],
                "image_timestamps": [label]
            })
        else:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": content},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
                ],
                "image_timestamps": [label]
            })
            openai_input_items.append(create_openai_user_input(content, image_b64))

    def add_model_interaction(phase, step_number, request_messages, response_payload, token_usage, tools_payload=None):
        interaction = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "model": model_name,
            "task": task_name,
            "phase": phase,
            "step": step_number,
            "request": {
                "messages": get_clean_conversation_log(request_messages, selfhosted),
                "tools": convert_to_serializable(tools_payload)
            },
            "response": convert_to_serializable(response_payload),
            "tokens": token_usage
        }
        model_interactions.append(interaction)
        append_model_io_text_log(interaction, request_messages, response_payload)
        persist_runtime_logs()

    if screenshots_dir:
        os.makedirs(screenshots_dir, exist_ok=True)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    messages = [{"role": "system", "content": effective_system_prompt}]
    pokemon_context_message_index = None

    def refresh_pokemon_context():
        nonlocal pokemon_context_message_index
        pokemon_context_message = {"role": "system", "content": build_pokemon_context(task_pokemon)}
        if pokemon_context_message_index is None:
            messages.append(pokemon_context_message)
            pokemon_context_message_index = len(messages) - 1
        else:
            messages[pokemon_context_message_index] = pokemon_context_message

    if selfhosted and ollama is None:
        print("❌ Ollama nicht installiert! pip install ollama")
        return create_result(False, 0, "Ollama nicht installiert", messages, selfhosted, total_tokens_used,
                             saved_screenshots, model_interactions)
    if not selfhosted:
        _init_openai(api_key)

    server_params = StdioServerParameters(command="python", args=[SERVICES_SCRIPT])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools_response = await session.list_tools()
            api_tools = [{
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.inputSchema}
            } for t in tools_response.tools]
            if auto_screenshot:
                api_tools = [tool for tool in api_tools if tool["function"]["name"] != "get_state"]
            responses_tools = convert_tools_to_responses_tools(api_tools)

            # Initialer Kickoff
            kickoff_text = (
                "Bitte starte die Aufgabe. Du bekommst automatisch vor jeder Entscheidung einen aktuellen Screenshot vom Spiel. "
                "Nutze kein get_state-Tool, sondern analysiere den jeweils mitgegebenen Screenshot. "
                "WICHTIG: Erklaere ab jetzt bei jedem Schritt kurz, was du siehst und WARUM du das naechste Tool nutzt (als reinen Text), bevor du das Tool aufrufst!"
            ) if auto_screenshot else (
                "Bitte starte die Aufgabe. Nutze das Tool 'get_state', um dir als allererstes ein Bild von der Lage zu machen. "
                "WICHTIG: Erklaere ab jetzt bei jedem Schritt kurz, was du siehst und WARUM du das naechste Tool nutzt (als reinen Text), bevor du das Tool aufrufst!"
            )
            # Hier vielleicht noch mehr Kontext geben. Instruction file, sonst vll schaß KOntext
            messages.append({
                "role": "user",
                "content": "Bitte starte die Aufgabe. Nutze das Tool 'get_state', um dir als allererstes ein Bild von der Lage zu machen. WICHTIG: Erkläre ab jetzt bei jedem Schritt kurz, was du siehst und WARUM du das nächste Tool nutzt (als reinen Text), bevor du das Tool aufrufst!"
            })

            messages[-1]["content"] = kickoff_text

            openai_input_items = [
                create_openai_user_input(messages[-1]["content"])
            ]

            refresh_pokemon_context()

            steps_taken = 0
            success = False
            finish_reason = "Max steps reached"

            while steps_taken < max_steps:
                voiceOutput.engine.WaitUntilDone(-1)
                steps_taken += 1
                print(f"\n--- Schritt {steps_taken}/{max_steps} ---")
                print(f"🧠 {'Ollama' if selfhosted else 'OpenAI'} ({model_name}) überlegt...")

                refresh_pokemon_context()
                messages = cleanup_history_to_save_context(messages, selfhosted=selfhosted)

                if auto_screenshot:
                    try:
                        image_b64, image_label = await take_current_screenshot(session, "auto_step", steps_taken)
                        append_screenshot_message(
                            "Hier ist der aktuelle Screenshot vom Spiel. Analysiere das Bild und treffe deine naechste Entscheidung.",
                            image_b64,
                            image_label
                        )
                    except Exception as e:
                        print(f"Warnung: Automatischer Screenshot fehlgeschlagen: {e}")

                # ── API-Aufruf ──
                try:
                    request_messages = copy.deepcopy(messages)
                    if selfhosted:
                        response = ollama.chat(model=model_name, messages=messages, tools=api_tools)
                        token_usage = _log_tokens(model_name, response, selfhosted=True, task_name=task_name)
                        add_tokens(token_usage)
                        add_model_interaction("step", steps_taken, request_messages, response, token_usage, api_tools)
                        response_message = response['message']
                        
                        # In Standard-Dict umwandeln
                        if not isinstance(response_message, dict):
                            if hasattr(response_message, "model_dump"):
                                response_message_dict = response_message.model_dump()
                            elif hasattr(response_message, "dict"):
                                response_message_dict = response_message.dict()
                            else:
                                try:
                                    response_message_dict = dict(response_message)
                                except TypeError:
                                    response_message_dict = {
                                        "role": getattr(response_message, "role", "assistant"),
                                        "content": getattr(response_message, "content", "")
                                    }
                                    if hasattr(response_message, "images"):
                                        response_message_dict["images"] = response_message.images
                                    if hasattr(response_message, "tool_calls"):
                                        response_message_dict["tool_calls"] = response_message.tool_calls
                        else:
                            response_message_dict = response_message
                            
                        messages.append(response_message_dict)
                        persist_runtime_logs()
                        content_text = response_message_dict.get('content', '') or ''
                        tool_calls = response_message_dict.get('tool_calls', []) or []
                    else:
                        resp = _openai_client.responses.create(
                            model=model_name,
                            instructions=build_openai_instructions(effective_system_prompt, task_pokemon),
                            input=copy.deepcopy(openai_input_items),
                            tools=responses_tools,
                            reasoning=OPENAI_REASONING
                        )
                        token_usage = _log_tokens(model_name, resp, selfhosted=False, task_name=task_name)
                        add_tokens(token_usage)
                        add_model_interaction("step", steps_taken, request_messages, resp, token_usage, responses_tools)
                        openai_input_items.extend(get_response_input_items(resp))
                        content_text = get_response_text(resp)
                        tool_calls = get_response_tool_calls(resp)
                        asst_msg = {"role": "assistant", "content": content_text or ""}
                        if tool_calls:
                            asst_msg["tool_calls"] = tool_calls
                        messages.append(asst_msg)
                        persist_runtime_logs()

                except Exception as e:
                    print(f"\n❌ API Fehler: {e}")
                    return create_result(False, steps_taken, f"API Error: {e}", messages, selfhosted,
                                         total_tokens_used, saved_screenshots, model_interactions)

                if content_text:
                    print(f"\n🧠 [Gedanken des Modells]: {content_text.strip()}")
                    voiceOutput.speakText(content_text)
                if debug:
                    print(f"\n[DEBUG] Response: {response_message if selfhosted else resp}")

                if not tool_calls:
                    print("⚠️ Modell hat keine Tools aufgerufen.")
                    retry_text = "Du musst handeln! Bitte nutze eines der verfuegbaren Tools." if auto_screenshot else "Du musst handeln! Bitte nutze get_state() oder press_button()."
                    messages.append({"role": "user", "content": retry_text})
                    if not selfhosted:
                        openai_input_items.append(create_openai_user_input(messages[-1]["content"]))
                    await asyncio.sleep(2)
                    continue

                # ── Tool-Aufrufe verarbeiten ──
                for tc in tool_calls:
                    if selfhosted:
                        func_name = tc['function']['name']
                        func_args = tc['function']['arguments']
                        tc_id = None
                    else:
                        func_name = tc.function.name if hasattr(tc, 'function') else tc['function']['name']
                        raw_args = tc.function.arguments if hasattr(tc, 'function') else tc['function']['arguments']
                        func_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        tc_id = tc.id if hasattr(tc, 'id') else tc.get('id')

                    if auto_screenshot and func_name == "get_state":
                        result_str = "get_state ist im Auto-Screenshot-Modus nicht als Agenten-Tool verfuegbar. Der aktuelle Screenshot wird automatisch bereitgestellt."
                        if selfhosted:
                            messages.append({"role": "tool", "name": func_name, "content": result_str})
                        else:
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": result_str})
                            openai_input_items.append({
                                "type": "function_call_output",
                                "call_id": tc_id,
                                "output": result_str
                            })
                        continue

                    print(f"🛠️ [Führe Tool aus]: {func_name}({func_args})")

                    try:
                        tool_result = await session.call_tool(func_name, func_args)
                        tool_text_result, image_b64 = [], None
                        for part in tool_result.content:
                            if part.type == "text":
                                tool_text_result.append(part.text)
                            elif part.type == "image":
                                image_b64 = part.data
                                tool_text_result.append("Screenshot erfolgreich erstellt.")
                        result_str = "\n".join(tool_text_result)

                        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        ts_label = f"mcp_current_state_{timestamp}"
                        if image_b64:
                            screenshot_filename = save_screenshot(image_b64, "step", steps_taken)
                            if screenshot_filename:
                                ts_label = screenshot_filename

                        # Tool-Ergebnis + ggf. Bild anhängen
                        if selfhosted:
                            messages.append({"role": "tool", "name": func_name, "content": result_str})
                            if image_b64:
                                messages.append({
                                    "role": "user",
                                    "content": "Hier ist der aktuelle Screenshot vom Spiel. Analysiere das Bild und treffe deine nächste Entscheidung.",
                                    "images": [image_b64],
                                    "image_timestamps": [ts_label]
                                })
                        else:
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": result_str})
                            openai_input_items.append({
                                "type": "function_call_output",
                                "call_id": tc_id,
                                "output": result_str
                            })
                            if image_b64:
                                messages.append({
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": "Hier ist der aktuelle Screenshot vom Spiel. Analysiere das Bild und treffe deine nächste Entscheidung."},
                                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
                                    ],
                                    "image_timestamps": [ts_label]
                                })
                                openai_input_items.append(create_openai_user_input(
                                    "Hier ist der aktuelle Screenshot vom Spiel. Analysiere das Bild und treffe deine nÃ¤chste Entscheidung.",
                                    image_b64
                                ))

                    except Exception as e:
                        print(f"❌ Fehler bei Tool-Ausführung: {e}")
                        err_str = f"Fehler bei Ausführung: {str(e)}"
                        if selfhosted:
                            messages.append({"role": "tool", "name": func_name, "content": err_str})
                        else:
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": err_str})
                            openai_input_items.append({
                                "type": "function_call_output",
                                "call_id": tc_id,
                                "output": err_str
                            })

                await asyncio.sleep(2)

            # Max Steps erreicht
            print("\n⏰ Maximale Anzahl an Schritten erreicht. Starte automatische Endvalidierung des letzten Zustands...")
            
            # 1. Screenshot machen über MCP
            val_image_b64 = None
            try:
                validation_screenshot = await session.call_tool("get_state", {})
                for part in validation_screenshot.content:
                    if part.type == "image":
                        val_image_b64 = part.data
            except Exception as ve:
                print(f"⚠️ Endvalidierungs-Screenshot fehlgeschlagen: {ve}")

            if val_image_b64:
                val_filename = save_screenshot(val_image_b64, "final_validation", steps_taken)
                # 2. Screenshot dem Modell zur Validierung senden
                val_prompt = (
                    "Die maximale Anzahl an Schritten wurde erreicht. Bitte prüfe den aktuellen Screenshot, "
                    "ob das Ziel der Aufgabe vielleicht im allerletzten Schritt doch noch erreicht wurde. "
                    "Sei dabei so pessimistisch wie möglich!\n\n"
                    f"Aufgabenstellung (System Prompt):\n{system_prompt}\n\n"
                    "Antworte NUR mit einem einzelnen Wort:\n"
                    "- 'JA' wenn das Ziel auf dem Screenshot eindeutig als erreicht erkennbar ist.\n"
                    "- 'NEIN' wenn das Ziel NICHT erreicht ist oder du dir unsicher bist."
                )
                val_ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                val_label = val_filename or f"mcp_current_state_{val_ts}"
                val_messages = list(messages)

                if selfhosted:
                    val_messages.append({
                        "role": "user",
                        "content": val_prompt,
                        "images": [val_image_b64],
                        "image_timestamps": [val_label]
                    })
                    try:
                        request_messages = copy.deepcopy(val_messages)
                        val_response = ollama.chat(model=model_name, messages=val_messages)
                        token_usage = _log_tokens(model_name, val_response, selfhosted=True, task_name=task_name)
                        add_tokens(token_usage)
                        add_model_interaction("final_validation", steps_taken, request_messages, val_response, token_usage)
                        val_answer = val_response['message'].get('content', '').strip().upper()
                        persist_runtime_logs()
                    except Exception as e:
                        print(f"⚠️ Endvalidierungs-API-Fehler: {e}")
                        val_answer = "NEIN"
                else:
                    val_messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": val_prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{val_image_b64}"}}
                        ],
                        "image_timestamps": [val_label]
                    })
                    try:
                        request_messages = copy.deepcopy(val_messages)
                        val_input_items = copy.deepcopy(openai_input_items)
                        val_input_items.append(create_openai_user_input(val_prompt, val_image_b64))
                        val_resp = _openai_client.responses.create(
                            model=model_name,
                            instructions=build_openai_instructions(effective_system_prompt, task_pokemon),
                            input=val_input_items,
                            reasoning=OPENAI_REASONING
                        )
                        token_usage = _log_tokens(model_name, val_resp, selfhosted=False, task_name=task_name)
                        add_tokens(token_usage)
                        add_model_interaction("final_validation", steps_taken, request_messages, val_resp, token_usage)
                        val_answer = get_response_text(val_resp).strip().upper()
                        persist_runtime_logs()
                    except Exception as e:
                        print(f"⚠️ Endvalidierungs-API-Fehler: {e}")
                        val_answer = "NEIN"

                print(f"🔍 Endvalidierungs-Ergebnis: {val_answer}")

                if val_answer.startswith("JA"):
                    success = True
                    finish_reason = "Task im letzten Schritt abgeschlossen (durch automatische Endvalidierung bestätigt)"
                    print(f"\n🎉 ENDVALIDIERUNG ERFOLGREICH! Aufgabe im letzten Schritt gelöst.")
                    
                    if selfhosted:
                        messages.append({
                            "role": "user",
                            "content": "Automatische Endvalidierung erfolgreich.",
                            "images": [val_image_b64],
                            "image_timestamps": [val_label]
                        })
                    else:
                        messages.append({
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Automatische Endvalidierung erfolgreich."},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{val_image_b64}"}}
                            ],
                            "image_timestamps": [val_label]
                        })
                    
                    return create_result(True, steps_taken, finish_reason, messages, selfhosted,
                                         total_tokens_used, saved_screenshots, model_interactions)

            print("\n⏰ Endvalidierung nicht erfolgreich oder kein Screenshot möglich. Abbruch.")
            return create_result(False, steps_taken, finish_reason, messages, selfhosted,
                                 total_tokens_used, saved_screenshots, model_interactions)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Autonomer Pokemon Agent")
    parser.add_argument("--debug", action="store_true", help="Aktiviere detaillierte Modell-Ausgaben")
    parser.add_argument("--selfhosted", default="true", help="'true' = Ollama lokal, 'false' = OpenAI API (default: true)")
    parser.add_argument("--api-key", default=None, help="OpenAI API Key (alternativ: OPENAI_API_KEY Env-Var)")
    parser.add_argument("--model", default=None, help="Modellname (default: llama3.2-vision / gpt-4o)")
    parser.add_argument("--autoScreenShot", default="Off", choices=["On", "Off", "on", "off"],
                        help="'On' = Screenshot wird automatisch vor jeder Agentenentscheidung erstellt, 'Off' = Agent nutzt get_state selbst (default: Off)")
    parser.add_argument("--Voice", default="Off", choices=["On", "Off", "on", "off"],
                        help="'On' = Modellgedanken werden per Text-to-Speech gesprochen, 'Off' = keine Sprachausgabe (default: Off)")
    args = parser.parse_args()

    selfhosted = args.selfhosted.lower() == "true"
    auto_screenshot = args.autoScreenShot.lower() == "on"
    voice = args.Voice.lower() == "on"
    default_model = "llama3.2-vision" if selfhosted else "gpt-4o" # UMAENDERN AUF DIE RICHTIGEN DEFAULT MODELLE! WARUM ZUM FICK EIN VISIO MODEL?!??!!??!
    model = args.model or default_model

    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    async def run_default():
        prompt = "Du bist ein Pokémon-Agent. Nutze deine Tools zum Spielen."
        try:
            agents_file = os.path.join(PROJECT_ROOT, "Agents.md")
            with open(agents_file, "r", encoding="utf-8") as f:
                prompt = f.read()
        except FileNotFoundError:
            pass
        await run_agent(model_name=model, system_prompt=prompt, max_steps=50,
                        debug=args.debug, selfhosted=selfhosted, api_key=args.api_key,
                        auto_screenshot=auto_screenshot,
                        voice=voice)

    asyncio.run(run_default())
