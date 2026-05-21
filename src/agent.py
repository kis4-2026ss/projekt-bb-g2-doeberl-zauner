import asyncio
import os
import sys
import copy
import datetime
import json

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

SERVICES_SCRIPT = os.path.join(os.path.dirname(__file__), "services.py")
TOKENS_FILE = os.path.join(os.path.dirname(__file__), "TOKENS.txt")

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
    except Exception as e:
        print(f"⚠️ Token-Logging fehlgeschlagen: {e}")


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
        msg_copy = copy.deepcopy(msg)
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
                    task_name: str = "Unbekannt") -> dict:
    """
    Startet einen Agenten-Durchlauf für eine spezifische Aufgabe.

    Args:
        selfhosted: True = Ollama (lokal), False = OpenAI API
        api_key: OpenAI API Key (nur bei selfhosted=False nötig, alternativ OPENAI_API_KEY Env-Var)
    """
    backend = "Ollama (lokal)" if selfhosted else "OpenAI API"
    print(f"\n🚀 Starte Agent mit Modell: '{model_name}' via {backend} (Max Steps: {max_steps})")

    if selfhosted and ollama is None:
        print("❌ Ollama nicht installiert! pip install ollama")
        return {"success": False, "steps_taken": 0, "reason": "Ollama nicht installiert", "log": []}
    if not selfhosted:
        _init_openai(api_key)

    server_params = StdioServerParameters(command="python", args=[SERVICES_SCRIPT])
    messages = [{"role": "system", "content": system_prompt}]

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

            steps_taken = 0
            success = False
            finish_reason = "Max steps reached"

            while steps_taken < max_steps:
                steps_taken += 1
                print(f"\n--- Schritt {steps_taken}/{max_steps} ---")
                print(f"🧠 {'Ollama' if selfhosted else 'OpenAI'} ({model_name}) überlegt...")

                messages = cleanup_history_to_save_context(messages, selfhosted=selfhosted)

                # ── API-Aufruf ──
                try:
                    if selfhosted:
                        response = ollama.chat(model=model_name, messages=messages, tools=api_tools)
                        _log_tokens(model_name, response, selfhosted=True, task_name=task_name)
                        response_message = response['message']
                        messages.append(response_message)
                        content_text = response_message.get('content', '') if isinstance(response_message, dict) else getattr(response_message, 'content', '')
                        tool_calls = response_message.get('tool_calls', []) if isinstance(response_message, dict) else getattr(response_message, 'tool_calls', [])
                    else:
                        resp = _openai_client.chat.completions.create(model=model_name, messages=messages, tools=api_tools)
                        _log_tokens(model_name, resp, selfhosted=False, task_name=task_name)
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
                    return {"success": False, "steps_taken": steps_taken, "reason": f"API Error: {e}",
                            "log": get_clean_conversation_log(messages, selfhosted)}

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

                        # Benchmark-Signal prüfen
                        if "BENCHMARK_SIGNAL: TASK_COMPLETED" in result_str:
                            success, finish_reason = True, result_str
                            print(f"\n🎉 AUFGABE ERFOLGREICH BEENDET: {result_str}")
                            if selfhosted:
                                messages.append({"role": "tool", "name": func_name, "content": result_str})
                            else:
                                messages.append({"role": "tool", "tool_call_id": tc_id, "content": result_str})
                            return {"success": success, "steps_taken": steps_taken, "reason": finish_reason,
                                    "log": get_clean_conversation_log(messages, selfhosted)}

                        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        ts_label = f"mcp_current_state_{timestamp}"

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
            print("\n⏰ Maximale Anzahl an Schritten erreicht. Abbruch.")
            return {"success": False, "steps_taken": steps_taken, "reason": finish_reason,
                    "log": get_clean_conversation_log(messages, selfhosted)}


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
            with open("Agents.md", "r", encoding="utf-8") as f:
                prompt = f.read()
        except FileNotFoundError:
            pass
        await run_agent(model_name=model, system_prompt=prompt, max_steps=50,
                        debug=args.debug, selfhosted=selfhosted, api_key=args.api_key)

    asyncio.run(run_default())
