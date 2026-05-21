# Autonomous Pokémon Agent Framework

Dieses Projekt ist im Rahmen des KIS-Semesters (Künstliche Intelligente Systeme) entstanden. Es bietet ein Framework, um autonome KI-Agenten (Large Language Models) Pokémon spielen zu lassen. 

Der Kern des Projekts ist ein **Benchmarking-Framework**, mit dem verschiedene lokale Ollama-Modelle (insbesondere Vision-Modelle) anhand vordefinierter Aufgaben in Pokémon (z. B. Smaragd-Edition) evaluiert werden können. Das Framework bindet den Emulator direkt an die KI an, versorgt sie mit Screenshots und führt ihre Tastenbefehle aus.

## Voraussetzungen

### 1. Python-Pakete
Stelle sicher, dass du Python 3 installiert hast. Öffne dein Terminal und installiere die benötigten Abhängigkeiten:

```bash
pip install ollama mcp pyautogui pygetwindow pydirectinput Pillow pywin32
```

### 2. Ollama
Du benötigst eine lokale Installation von [Ollama](https://ollama.com/).
Lade dir das Modell herunter, das du testen möchtest. Da der Agent Screenshots auswertet, benötigst du ein **Vision-Modell**, das auch Tool-Calling (MCP-Server)unterstützt (z. B. `llama3.2-vision`):

```bash
ollama run llama3.2-vision
```

### 3. Emulator
Das Skript ist primär für Windows ausgelegt.
Starte deinen Gameboy Advance Emulator (z.B. **mGBA**) und lade dein Pokémon-Spiel (z.B. Smaragd-Edition).

**Wichtig:** Das Skript sucht standardmäßig nach einem Fenster, dessen Titel mit `"mGBA - Pokemon - Smaragd"` beginnt. Du kannst dies am Ende der Datei `emulator_controller.py` anpassen.

**Achtung** Wenn man zwei Bildschirme benuetzt, muss der Emulator auf dem Hauptbildschirm laufen. Sonst bekommt man nur schwarze Screenshots.

---

## Projektstruktur

*   **`emulator_controller.py`**: Die Hardware-Brücke. Sucht das Emulator-Fenster, macht Screenshots (inkl. Archivierung im `BackUp_Ordner`) und sendet echte Tastatur-/Maus-Befehle (via `pydirectinput`).
*   **`services.py`**: Der MCP-Server. Er stellt dem LLM standardisierte Werkzeuge (Tools) wie `press_button()`, `get_state()`, `load_savestate()` und `task_completed()` zur Verfügung.
*   **`agent.py`**: Der eigenständige KI-Agent. Er verbindet sich mit dem MCP-Server, spricht mit Ollama, hält das Context-Window sauber und iteriert in einer Schleife, bis eine Aufgabe gelöst oder das Limit erreicht ist.
*   **`benchmark.py`**: Das Orchestrierungs-Skript. Es iteriert über definierte Modelle und Aufgaben, lädt Savestates und speichert am Ende detaillierte Berichte im Ordner `results/`.
*   **`tasks.json`**: Die Konfigurationsdatei für das Benchmarking. Hier legst du fest, welche Aufgaben getestet werden.
*   **`Agents.md`**: Der System-Prompt für den Agenten (sein "Gehirn").
*   **`POKEMONS.md`**: Eine Markdown-Datei, die vom Agenten gepflegt werden soll, um den Team-Status und gefangene Pokémon zu dokumentieren.

---

## Verwendung

### 1. Vorbereitung im Spiel (Savestates)
Damit der Benchmark fair ist, starten die Agenten immer von exakt definierten Spielständen (Savestates).
Bereite in deinem Emulator die entsprechenden Slots vor (Speichern meist mit `Umschalt + F1`, Laden mit `F1`):
BeispielE:
*   **Slot 1 (F1)**: Z. B. direkt vor einem Pokémon Center.
*   **Slot 2 (F2)**: Z. B. im hohen Gras auf Route 102.
*   **Slot 3 (F3)**: Z. B. direkt vor einem Arenaleiter.

Diese Slots sind direkt in der `tasks.json` mit der jeweiligen Aufgabe verknüpft!

### 2. Einen Benchmark-Lauf starten
Möchtest du testen, wie gut ein Modell die Aufgaben meistert?
1. Öffne die Datei `benchmark.py`.
2. Trage oben im Array `MODELS` die Ollama-Modelle ein, die du vergleichen möchtest (z.B. `["llama3.2-vision"]`).
3. Führe das Skript im Terminal aus:

```bash
python benchmark.py
```

Das Skript wird nun:
1. Den Emulator in den Vordergrund holen.
2. Den richtigen Savestate laden (z.B. F1 drücken).
3. Den Agenten starten.
4. Den Erfolg prüfen und detaillierte Logs sowie einen Report in den Ordner `results/` speichern.

### 3. Den Agenten "Free-Roam" spielen lassen
Wenn du nicht benchmarken, sondern dem Agenten einfach zuschauen möchtest, wie er frei spielt:
Führe einfach den Agenten direkt aus:

```bash
python agent.py
```
*(Stelle sicher, dass du vorher im Spiel bist, er wird keinen Savestate laden, sondern einfach loslegen).*

---

## Troubleshooting

*   **Der Emulator wird nicht gefunden:** Prüfe, ob in `emulator_controller.py` der `EMULATOR_TITLE` mit deinem tatsächlichen Fensternamen übereinstimmt. Das Skript sucht standardmäßig nach den ersten 20 Zeichen.
*   **Die Tastendrücke kommen nicht an:** Manche Emulatoren müssen als Administrator gestartet werden, damit `pydirectinput` funktioniert. Klicke einmal manuell in das Emulator-Fenster, bevor du das Skript startest.
*   **Das Modell ruft keine Tools auf:** Nicht jedes Ollama-Modell beherrscht Function Calling gut. Wenn das Modell nur redet und nichts drückt, wechsle in `benchmark.py` / `agent.py` zu einem fähigeren Modell.
*   **Out of Memory (OOM):** Vision-Modelle brauchen viel VRAM. Wenn der PC abstürzt oder sehr langsam wird, nutze ein kleineres Modell oder schließe andere VRAM-intensive Programme.
