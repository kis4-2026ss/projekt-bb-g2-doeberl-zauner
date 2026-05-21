import asyncio
import os
import sys
import copy
import datetime
import json
import base64

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
POKEMONS_FILE = os.path.join(PROJECT_ROOT, "POKEMONS.md")

# Globaler OpenAI Client (wird bei Bedarf initialisiert)
_openai_client = None

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
            # OpenAI: usage Objekt mit prompt_tokens, completion_tokens, etc.
            usage = response.usage
            input_tokens = getattr(usage, 'prompt_tokens', 0) or 0
            output_tokens = getattr(usage, 'completion_tokens', 0) or 0
            # Cached tokens stecken in prompt_tokens_details
            details = getattr(usage, 'prompt_tokens_details', None)
            cached_tokens = getattr(details, 'cached_tokens', 0) or 0 if details else 0

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


def read_pokemon_context():
    """Liest die aktuell dokumentierten Pokemon und Attacken fuer den Modell-Kontext."""
    try:
        with open(POKEMONS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except FileNotFoundError:
        content = "POKEMONS.md existiert noch nicht."
    except Exception as e:
        content = f"POKEMONS.md konnte nicht gelesen werden: {e}"

    return (
        "Aktuell dokumentierte Pokemon und Attacken aus POKEMONS.md:\n"
        f"{content}\n\n"
        "Wenn du attack_pokemon nutzt, waehle den Slot der gewuenschten Attacke:\n"
        "1 = oben links, 2 = oben rechts, 3 = unten links, 4 = unten rechts."
    )


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
                    task_name: str = "Unbekannt", screenshots_dir: str = None) -> dict:
    """
    Startet einen Agenten-Durchlauf für eine spezifische Aufgabe.

    Args:
        selfhosted: True = Ollama (lokal), False = OpenAI API
        api_key: OpenAI API Key (nur bei selfhosted=False nötig, alternativ OPENAI_API_KEY Env-Var)
    """
    backend = "Ollama (lokal)" if selfhosted else "OpenAI API"
    print(f"\n🚀 Starte Agent mit Modell: '{model_name}' via {backend} (Max Steps: {max_steps})")

    # Initialisiere Token-Tracker und Screenshot-Liste
    total_tokens_used = {"input": 0, "cached": 0, "output": 0, "total": 0}
    saved_screenshots = []
    model_interactions = []

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

    def add_model_interaction(phase, step_number, request_messages, response_payload, token_usage, tools_payload=None):
        model_interactions.append({
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
        })

    if screenshots_dir:
        os.makedirs(screenshots_dir, exist_ok=True)

    messages = [{"role": "system", "content": system_prompt}]
    pokemon_context_message_index = None

    def refresh_pokemon_context():
        nonlocal pokemon_context_message_index
        pokemon_context_message = {"role": "system", "content": read_pokemon_context()}
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

            # Initialer Kickoff
            # Hier vielleicht noch mehr Kontext geben. Instruction file, sonst vll schaß KOntext
            messages.append({
                "role": "user",
                "content": "Bitte starte die Aufgabe. Nutze das Tool 'get_state', um dir als allererstes ein Bild von der Lage zu machen. WICHTIG: Erkläre ab jetzt bei jedem Schritt kurz, was du siehst und WARUM du das nächste Tool nutzt (als reinen Text), bevor du das Tool aufrufst!"
            })

            refresh_pokemon_context()

            steps_taken = 0
            success = False
            finish_reason = "Max steps reached"

            while steps_taken < max_steps:
                steps_taken += 1
                print(f"\n--- Schritt {steps_taken}/{max_steps} ---")
                print(f"🧠 {'Ollama' if selfhosted else 'OpenAI'} ({model_name}) überlegt...")

                refresh_pokemon_context()
                messages = cleanup_history_to_save_context(messages, selfhosted=selfhosted)

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
                        content_text = response_message_dict.get('content', '') or ''
                        tool_calls = response_message_dict.get('tool_calls', []) or []
                    else:
                        resp = _openai_client.chat.completions.create(model=model_name, messages=messages, tools=api_tools)
                        token_usage = _log_tokens(model_name, resp, selfhosted=False, task_name=task_name)
                        add_tokens(token_usage)
                        add_model_interaction("step", steps_taken, request_messages, resp, token_usage, api_tools)
                        choice = resp.choices[0].message
                        # Als serialisierbares Dict speichern
                        asst_msg = {"role": "assistant", "content": choice.content or ""}
                        if choice.tool_calls:
                            asst_msg["tool_calls"] = [
                                {"id": tc.id, "type": "function",
                                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                for tc in choice.tool_calls
                            ]
                        messages.append(asst_msg)
                        content_text = choice.content or ""
                        tool_calls = choice.tool_calls or []

                except Exception as e:
                    print(f"\n❌ API Fehler: {e}")
                    return create_result(False, steps_taken, f"API Error: {e}", messages, selfhosted,
                                         total_tokens_used, saved_screenshots, model_interactions)

                if content_text:
                    print(f"\n🧠 [Gedanken des Modells]: {content_text.strip()}")
                if debug:
                    print(f"\n[DEBUG] Response: {response_message if selfhosted else choice}")

                if not tool_calls:
                    print("⚠️ Modell hat keine Tools aufgerufen.")
                    messages.append({"role": "user", "content": "Du musst handeln! Bitte nutze get_state() oder press_button()."})
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

                        # Benchmark-Signal prüfen → Validierung mit Screenshot
                        if "BENCHMARK_SIGNAL: TASK_COMPLETED" in result_str:
                            # Dieser Schritt zählt NICHT → zurücksetzen
                            steps_taken -= 1
                            print(f"\n🔍 Task-Completed aufgerufen. Starte Validierung mit Screenshot...")

                            if selfhosted:
                                messages.append({"role": "tool", "name": func_name, "content": result_str})
                            else:
                                messages.append({"role": "tool", "tool_call_id": tc_id, "content": result_str})

                            # 1. Screenshot machen über MCP
                            val_image_b64 = None
                            try:
                                validation_screenshot = await session.call_tool("get_state", {})
                                for part in validation_screenshot.content:
                                    if part.type == "image":
                                        val_image_b64 = part.data
                            except Exception as ve:
                                print(f"⚠️ Validierungs-Screenshot fehlgeschlagen: {ve}")

                            if val_image_b64:
                                val_ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                                val_filename = save_screenshot(val_image_b64, "validation", steps_taken)
                                val_label = val_filename or f"mcp_current_state_{val_ts}"

                                # 2. Screenshot dem Modell zur Validierung senden
                                val_prompt = (
                                    f"Du hast behauptet, die Aufgabe abgeschlossen zu haben. Grund: {result_str}\n\n"
                                    "Hier ist ein aktueller Screenshot des Spiels. Prüfe GENAU ob die Aufgabe "
                                    "wirklich erfolgreich erledigt wurde.\n\n"
                                    "Antworte NUR mit einem einzelnen Wort:\n"
                                    "- 'JA' wenn die Aufgabe auf dem Screenshot eindeutig als erledigt erkennbar ist.\n"
                                    "- 'NEIN' wenn die Aufgabe NICHT erledigt ist oder du dir unsicher bist."
                                )

                                if selfhosted:
                                    messages.append({
                                        "role": "user",
                                        "content": val_prompt,
                                        "images": [val_image_b64],
                                        "image_timestamps": [val_label]
                                    })
                                    try:
                                        request_messages = copy.deepcopy(messages)
                                        val_response = ollama.chat(model=model_name, messages=messages)
                                        token_usage = _log_tokens(model_name, val_response, selfhosted=True, task_name=task_name)
                                        add_tokens(token_usage)
                                        add_model_interaction("validation", steps_taken, request_messages, val_response, token_usage)
                                        val_msg = val_response['message']
                                        if isinstance(val_msg, dict):
                                            val_answer = val_msg.get('content', '').strip().upper()
                                            val_msg_copy = val_msg
                                        else:
                                            val_answer = getattr(val_msg, 'content', '').strip().upper()
                                            val_msg_copy = {
                                                "role": getattr(val_msg, "role", "assistant"),
                                                "content": getattr(val_msg, "content", "")
                                            }
                                        messages.append(val_msg_copy)
                                    except Exception as e:
                                        print(f"⚠️ Validierungs-API-Fehler: {e}")
                                        val_answer = "NEIN"
                                        messages.append({"role": "assistant", "content": "NEIN (Fehler bei API)"})
                                else:
                                    messages.append({
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": val_prompt},
                                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{val_image_b64}"}}
                                        ],
                                        "image_timestamps": [val_label]
                                    })
                                    try:
                                        request_messages = copy.deepcopy(messages)
                                        val_resp = _openai_client.chat.completions.create(model=model_name, messages=messages)
                                        token_usage = _log_tokens(model_name, val_resp, selfhosted=False, task_name=task_name)
                                        add_tokens(token_usage)
                                        add_model_interaction("validation", steps_taken, request_messages, val_resp, token_usage)
                                        val_choice = val_resp.choices[0].message
                                        val_answer = val_choice.content.strip().upper()
                                        messages.append({"role": "assistant", "content": val_choice.content or ""})
                                    except Exception as e:
                                        print(f"⚠️ Validierungs-API-Fehler: {e}")
                                        val_answer = "NEIN"
                                        messages.append({"role": "assistant", "content": "NEIN (Fehler bei API)"})

                                print(f"🔍 Validierungs-Ergebnis: {val_answer}")

                                if val_answer.startswith("JA"):
                                    success, finish_reason = True, result_str
                                    print(f"\n🎉 AUFGABE VALIDIERT UND ERFOLGREICH BEENDET!")
                                    return create_result(success, steps_taken, finish_reason, messages, selfhosted,
                                                         total_tokens_used, saved_screenshots, model_interactions)
                                else:
                                    print("❌ Validierung fehlgeschlagen! Aufgabe ist NICHT erledigt. Agent macht weiter.")
                                    messages.append({
                                        "role": "user",
                                        "content": "Die Validierung hat ergeben, dass die Aufgabe NICHT abgeschlossen ist. "
                                                   "Bitte analysiere den aktuellen Screenshot erneut und mache weiter!",
                                        "images": [val_image_b64],
                                        "image_timestamps": [val_label]
                                    } if selfhosted else {
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": "Die Validierung hat ergeben, dass die Aufgabe NICHT abgeschlossen ist. "
                                                                     "Bitte analysiere den aktuellen Screenshot erneut und mache weiter!"},
                                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{val_image_b64}"}}
                                        ],
                                        "image_timestamps": [val_label]
                                    })
                                    continue  # Nächster Schleifendurchlauf (ohne Step-Verbrauch)
                            else:
                                # Kein Screenshot möglich → sicherheitshalber weitermachen
                                print("⚠️ Kein Validierungs-Screenshot möglich. Agent macht weiter.")
                                messages.append({"role": "user", "content": "Validierung konnte nicht durchgeführt werden. Mache weiter mit der Aufgabe."})
                                continue

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
                            if image_b64:
                                messages.append({
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": "Hier ist der aktuelle Screenshot vom Spiel. Analysiere das Bild und treffe deine nächste Entscheidung."},
                                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
                                    ],
                                    "image_timestamps": [ts_label]
                                })

                    except Exception as e:
                        print(f"❌ Fehler bei Tool-Ausführung: {e}")
                        err_str = f"Fehler bei Ausführung: {str(e)}"
                        if selfhosted:
                            messages.append({"role": "tool", "name": func_name, "content": err_str})
                        else:
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": err_str})

                await asyncio.sleep(1)

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
                    "ob das Ziel der Aufgabe vielleicht im allerletzten Schritt doch noch erreicht wurde.\n\n"
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
                        val_resp = _openai_client.chat.completions.create(model=model_name, messages=val_messages)
                        token_usage = _log_tokens(model_name, val_resp, selfhosted=False, task_name=task_name)
                        add_tokens(token_usage)
                        add_model_interaction("final_validation", steps_taken, request_messages, val_resp, token_usage)
                        val_answer = val_resp.choices[0].message.content.strip().upper()
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
    args = parser.parse_args()

    selfhosted = args.selfhosted.lower() == "true"
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
                        debug=args.debug, selfhosted=selfhosted, api_key=args.api_key)

    asyncio.run(run_default())
