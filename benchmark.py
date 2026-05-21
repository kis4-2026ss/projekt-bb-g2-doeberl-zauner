import json
import os
import asyncio
import sys
import time
import argparse

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

RESULTS_DIR = "results"

async def main(debug=False, selfhosted=True, api_key=None):
    if not os.path.exists(RESULTS_DIR):
        os.makedirs(RESULTS_DIR)

    try:
        with open("tasks.json", "r", encoding="utf-8") as f:
            tasks_data = json.load(f)
            tasks = tasks_data.get("tasks", [])
    except FileNotFoundError:
        print("Konnte tasks.json nicht finden. Bitte sicherstellen, dass die Datei existiert.")
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

    try:
        for model in models:
            for task in tasks:
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
                
                # Führe den autonomen Agenten aus
                result = await run_agent(model_name=model, system_prompt=prompt, max_steps=max_steps,
                                        debug=debug, selfhosted=selfhosted, api_key=api_key,
                                        task_name=task.get("name", task_id))
                
                # Speichere die Ergebnisse
                task_result = {
                    "model": model,
                    "task_id": task_id,
                    "success": result["success"],
                    "steps_taken": result["steps_taken"],
                    "reason": result["reason"]
                }
                report.append(task_result)
                
                # Log des Durchlaufs detailliert speichern
                model_dir = os.path.join(RESULTS_DIR, model.replace(":", "_"))
                if not os.path.exists(model_dir):
                    os.makedirs(model_dir)
                    
                log_file = os.path.join(model_dir, f"{task_id}_log.json")
                with open(log_file, "w", encoding="utf-8") as f:
                    json.dump(result["log"], f, indent=4, ensure_ascii=False, cls=BenchmarkEncoder)
                    
                print(f"\n>>> TASK BEENDET. Erfolg: {result['success']} in {result['steps_taken']} Schritten.")
                print(f">>> Detailliertes Log gespeichert unter {log_file}")
            
    except KeyboardInterrupt:
        print("\n\n⚠️ BENCHMARK WURDE VOM BENUTZER ABGEBROCHEN (STRG+C)!")
        print("Speichere bisher gesammelte Ergebnisse...")
            
    finally:
        # Am Ende einen maschinenlesbaren Gesamtbericht erstellen, auch bei Abbruch
        if report:
            report_file = os.path.join(RESULTS_DIR, "benchmark_report.json")
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
