import json
import os
import asyncio
import sys
import time
import datetime
import argparse
import keyboard

from agent import run_agent
import emulator_controller

class BenchmarkEncoder(json.JSONEncoder):
    """
    Stellt sicher, dass verschachtelte Objekte (wie 'Message' von Ollama)
    korrekt in JSON umgewandelt werden können.
    """
    def default(self, o):
        if hasattr(o, "model_dump"):
            return o.model_dump()
        if hasattr(o, "dict"):
            return o.dict()
        if hasattr(o, "__dict__"):
            return o.__dict__
        try:
            return dict(o)
        except TypeError:
            return str(o)

# Definiere hier, welche Modelle gegeneinander antreten sollen
# Selfhosted (Ollama) Modelle:
MODELS_OLLAMA = [
    "gemma4:e4b", 
    # Weitere Modelle zum Testen einkommentieren:
    # "llava:13b",
    # "qwen2.5-vl"
]

# OpenAI Modelle:
MODELS_OPENAI = [ # Geld In, Cached, out
    # "gpt-5.4-nano-2026-03-17", # 0,2 0,02 1,25
    # Weitere OpenAI Modelle zum Testen einkommentieren:
    # "gpt-5-2025-08-07" # 1,25 0,13 10,0
    # "gpt-5-mini-2025-08-07", # 0,25 0,03 2,0
    "gpt-5-nano-2025-08-07" #  0,05 0,01 0,4
]

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RESULTS_BASE_DIR = os.path.join(PROJECT_ROOT, "results")

async def main(debug=False, selfhosted=True, api_key=None):
    # Erstelle einen Unterordner mit Zeitstempel für jeden Benchmark-Durchlauf
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = os.path.join(RESULTS_BASE_DIR, timestamp)
    os.makedirs(run_dir, exist_ok=True)
    print(f"📁 Ergebnisse werden gespeichert in: {run_dir}")

    try:
        tasks_file = os.path.join(PROJECT_ROOT, "tasks.json")
        with open(tasks_file, "r", encoding="utf-8") as f:
            tasks_data = json.load(f)
            tasks = tasks_data.get("tasks", [])
    except FileNotFoundError:
        print(f"Konnte tasks.json nicht unter {tasks_file} finden. Bitte sicherstellen, dass die Datei existiert.")
        return

    models = MODELS_OLLAMA if selfhosted else MODELS_OPENAI
    backend = "Ollama (lokal)" if selfhosted else "OpenAI API"

    print("="*60)
    print(f"🏆 POKEMON KI-BENCHMARK FRAMEWORK 🏆")
    print(f"Backend: {backend}")
    print(f"Modelle zu testen: {models}")
    print(f"Anzahl Tasks: {len(tasks)}")
    print("="*60)

    report = []

    cancel_requested = False

    def on_ctrl_x():
        nonlocal cancel_requested
        cancel_requested = True
        print("\n\n🛑 ABBRUCH-ANFORDERUNG ERFASST (Strg+X)!")
        print("Der aktuelle Task wird noch beendet, danach stoppt der Benchmark und speichert die Ergebnisse.\n")

    try:
        keyboard.add_hotkey('ctrl+x', on_ctrl_x)
    except Exception as e:
        print(f"⚠️ Warnung: Strg+X Hotkey konnte nicht registriert werden: {e}")

    try:
        for model in models:
            if cancel_requested:
                break
            for task in tasks:
                if cancel_requested:
                    break
                task_id = task["id"]
                slot = task["savestate_slot"]
                max_steps = task["max_steps"]
                prompt = task["system_prompt"]
                
                print(f"\n\n{'='*60}")
                print(f">>> STARTE BENCHMARK: Modell '{model}' | Task '{task['name']}' <<<")
                print(f"{'='*60}")
                
                # Lade den Savestate VOR dem Start des Agenten
                key = f"f{slot}"
                print(f"Bereite Emulator vor: Lade Savestate aus Slot {slot} (Taste {key})...")
                
                # Greife auf die Konfiguration von emulator_controller zu
                use_pid = getattr(emulator_controller, 'USE_PID', False)
                target = emulator_controller.EMULATOR_PID if use_pid else emulator_controller.EMULATOR_TITLE
                
                emulator_controller.send_keyboard_input(target, key, is_pid=use_pid, duration=0.2)
                print("Warte kurz, bis das Spiel geladen ist...")
                time.sleep(2) # Wartezeit, damit der Savestate sicher geladen ist
                model_dir = os.path.join(run_dir, model.replace(":", "_"))
                task_dir = os.path.join(model_dir, task_id)
                screenshots_dir = os.path.join(task_dir, "screenshots")
                os.makedirs(screenshots_dir, exist_ok=True)
                
                # Führe den autonomen Agenten aus
                result = await run_agent(model_name=model, system_prompt=prompt, max_steps=max_steps,
                                        debug=debug, selfhosted=selfhosted, api_key=api_key,
                                        task_name=task.get("name", task_id),
                                        screenshots_dir=screenshots_dir)
                
                # Speichere die Ergebnisse
                log_file = os.path.join(task_dir, "conversation_log.json")
                interactions_file = os.path.join(task_dir, "model_interactions.json")
                tokens_file = os.path.join(task_dir, "tokens.json")
                task_result_file = os.path.join(task_dir, "result.json")

                task_result = {
                    "model": model,
                    "task_id": task_id,
                    "task_name": task.get("name", task_id),
                    "success": result["success"],
                    "steps_taken": result["steps_taken"],
                    "reason": result["reason"],
                    "tokens": result.get("tokens", {"input": 0, "cached": 0, "output": 0, "total": 0}),
                    "screenshots": result.get("screenshots", []),
                    "files": {
                        "conversation_log": log_file,
                        "model_interactions": interactions_file,
                        "tokens": tokens_file,
                        "result": task_result_file,
                        "screenshots_dir": screenshots_dir
                    }
                }
                report.append(task_result)
                
                with open(log_file, "w", encoding="utf-8") as f:
                    json.dump(result["log"], f, indent=4, ensure_ascii=False, cls=BenchmarkEncoder)

                with open(interactions_file, "w", encoding="utf-8") as f:
                    json.dump(result.get("model_interactions", []), f, indent=4, ensure_ascii=False, cls=BenchmarkEncoder)

                with open(tokens_file, "w", encoding="utf-8") as f:
                    json.dump(result.get("tokens", {}), f, indent=4, ensure_ascii=False, cls=BenchmarkEncoder)

                with open(task_result_file, "w", encoding="utf-8") as f:
                    json.dump(task_result, f, indent=4, ensure_ascii=False, cls=BenchmarkEncoder)
                    
                print(f"\n>>> TASK BEENDET. Erfolg: {result['success']} in {result['steps_taken']} Schritten.")
                print(f">>> Detailliertes Log gespeichert unter {log_file}")
            
    except KeyboardInterrupt:
        print("\n\n⚠️ BENCHMARK WURDE VOM BENUTZER ABGEBROCHEN (STRG+C)!")
        print("Speichere bisher gesammelte Ergebnisse...")
            
    finally:
        try:
            keyboard.remove_hotkey(on_ctrl_x)
        except Exception:
            pass

        # Am Ende einen maschinenlesbaren Gesamtbericht erstellen, auch bei Abbruch
        if report:
            report_file = os.path.join(run_dir, "benchmark_report.json")
            with open(report_file, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=4, ensure_ascii=False, cls=BenchmarkEncoder)
                
            print("\n\n" + "="*60)
            print("🏁 BENCHMARK ABGESCHLOSSEN / BEENDET 🏁")
            print(f"Vollständiger Report gespeichert unter: {report_file}")
            print("Zusammenfassung:")
            
            # Kleine Übersicht ausgeben
            for r in report:
                status = "✅ ERFOLG" if r["success"] else "❌ FEHLSCHLAG"
                print(f"[{r['model']}] {r['task_id']}: {status} ({r['steps_taken']} Schritte)")
        else:
            print("\nBenchmark abgebrochen, bevor eine Aufgabe beendet wurde.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pokemon KI-Benchmark")
    parser.add_argument("--debug", action="store_true", help="Aktiviere Debug-Ausgaben (Rohdaten vom Modell)")
    parser.add_argument("--selfhosted", default="true",
                        help="'true' = Ollama lokal, 'false' = OpenAI API (default: true)")
    parser.add_argument("--api-key", default=None,
                        help="OpenAI API Key (alternativ: OPENAI_API_KEY Umgebungsvariable)")
    args = parser.parse_args()

    selfhosted = args.selfhosted.lower() == "true"

    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(main(debug=args.debug, selfhosted=selfhosted, api_key=args.api_key))
    except KeyboardInterrupt:
        pass # Verhindert unschöne Tracebacks beim Beenden
