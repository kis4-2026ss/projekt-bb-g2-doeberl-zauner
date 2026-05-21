import os
import logging
from mcp.server.fastmcp import FastMCP, Image
import emulator_controller
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Konfiguriere das Logging so, dass es in eine Datei schreibt und NICHT nach stdout.
# Standard-Output (stdout) wird zwingend von MCP für die JSON-Kommunikation gebraucht!
logging.basicConfig(
    filename=os.path.join(PROJECT_ROOT, 'mcp_server.log'),
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Erstelle den MCP Server
mcp = FastMCP("PokemonEmulator")

# Mapping von logischen "Spiele-Buttons" auf echte Tastaturtasten.
# Standard für mGBA: z=A, x=B, enter=Start, backspace=Select, Pfeiltasten für Bewegung.
BUTTON_MAPPING = {
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "a": "x",
    "b": "z",
    "start": "enter",
    "select": "backspace"
}

# Übernehme die Konfiguration aus dem emulator_controller (PID/Title)
USE_PID = emulator_controller.USE_PID
TARGET = emulator_controller.EMULATOR_PID if USE_PID else emulator_controller.EMULATOR_TITLE


@mcp.tool()
def press_button(button: str, duration: float = 0.1) -> str:
    """
    Drückt eine Taste auf der Tastatur, um den Charakter im Pokemon-Emulator zu steuern.
    
    Erlaubte Werte für button: 'up', 'down', 'left', 'right', 'a', 'b', 'start', 'select'.
    Nutze 'a' zum Interagieren und Bestätigen, 'b' zum Abbrechen oder Laufen.
    """
    button = button.lower()
    if button not in BUTTON_MAPPING:
        return f"Fehler: Unbekannter Button '{button}'. Erlaubte Werte sind: {list(BUTTON_MAPPING.keys())}"
    
    real_key = BUTTON_MAPPING[button]
    logging.info(f"Tool Aufruf: press_button('{button}'). Mapped zu echter Taste: '{real_key}'")
    
    # Sende Input an Emulator
    emulator_controller.send_keyboard_input(TARGET, real_key, is_pid=USE_PID, duration=duration)
    
    return f"Erfolg: Taste '{button}' wurde im Spiel gedrückt."


@mcp.tool()
def get_state() -> Image:
    """
    Macht einen Screenshot vom aktuellen Zustand des Emulators und gibt das Bild zurück.
    Rufe dieses Tool auf, wenn du sehen musst, wo du dich befindest, welche Pokémon 
    angezeigt werden oder welcher Text gerade auf dem Bildschirm steht.
    """
    screenshot_path = os.path.join(PROJECT_ROOT, "mcp_current_state.png")
    logging.info("Tool Aufruf: get_state(). Erstelle Screenshot...")
    
    success = emulator_controller.take_screenshot(TARGET, is_pid=USE_PID, output_filename=screenshot_path)
    
    if not success or not os.path.exists(screenshot_path):
        raise RuntimeError("Fehler: Konnte keinen Screenshot vom Emulator erstellen. Läuft der Emulator?")
        
    try:
        with open(screenshot_path, "rb") as f:
            image_data = f.read()
        logging.info("Screenshot erfolgreich gelesen und wird als Image zurückgegeben.")
        return Image(data=image_data, format="png")
    except Exception as e:
        logging.error(f"Fehler beim Lesen des Bildes: {e}")
        raise RuntimeError(f"Screenshot konnte nicht in den MCP-Stream geladen werden: {e}")

@mcp.tool()
def load_savestate(slot: int) -> str:
    """
    Lädt einen Savestate (Spielstand) aus dem Emulator, um eine Aufgabe von vorne zu beginnen.
    Erlaubte Werte für slot: 1 bis 10. (Drückt F1 bis F10)
    """
    if not isinstance(slot, int) or not (1 <= slot <= 10):
        return "Fehler: Slot muss eine Zahl zwischen 1 und 10 sein."
    key = f"f{slot}"
    logging.info(f"Tool Aufruf: load_savestate({slot}). Drücke Taste '{key}'")
    emulator_controller.send_keyboard_input(TARGET, key, is_pid=USE_PID, duration=0.2)
    return f"Savestate aus Slot {slot} wurde geladen (Taste {key} gedrückt)."

@mcp.tool()
def task_completed(reason: str) -> str:
    """
    Rufe dieses Tool auf, wenn du durch Analyse des Bildes sicher bist, dass du das vorgegebene Ziel der Aufgabe erreicht hast.
    Erkläre kurz den Grund. Beispiel: task_completed('Habe das Pokemon erfolgreich gefangen.')
    WICHTIG: Das beendet die aktuelle Aufgabe erfolgreich!
    """
    logging.info(f"Tool Aufruf: task_completed. Grund: {reason}")
    return f"BENCHMARK_SIGNAL: TASK_COMPLETED. Grund: {reason}"

@mcp.tool()
def get_pokemon_log() -> str:
    """
    Liest den aktuellen Inhalt der POKEMONS.md Datei.
    Nutze dies, um zu sehen, welche Pokémon aktuell im Team oder in der Box dokumentiert sind.
    """
    try:
        pokemon_file = os.path.join(PROJECT_ROOT, "POKEMONS.md")
        with open(pokemon_file, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "Fehler: Die Datei POKEMONS.md existiert noch nicht."
    except Exception as e:
        return f"Fehler beim Lesen der POKEMONS.md: {e}"

@mcp.tool()
def update_pokemon_log(content: str) -> str:
    """
    Überschreibt den Inhalt der POKEMONS.md Datei mit dem übergebenen Text.
    Nutze dies, um dein Team zu aktualisieren (Level-Ups, neue Attacken, Entwicklungen) 
    oder neue Pokémon in die Datei aufzunehmen.
    Lies am besten zuerst den aktuellen Zustand mit get_pokemon_log(), ändere den Text 
    und schreibe ihn mit dieser Funktion zurück.
    """
    try:
        pokemon_file = os.path.join(PROJECT_ROOT, "POKEMONS.md")
        with open(pokemon_file, "w", encoding="utf-8") as f:
            f.write(content)
        logging.info("Tool Aufruf: update_pokemon_log. POKEMONS.md wurde überschrieben.")
        return "Erfolg: POKEMONS.md wurde erfolgreich aktualisiert."
    except Exception as e:
        logging.error(f"Fehler beim Überschreiben von POKEMONS.md: {e}")
        return f"Fehler beim Aktualisieren der POKEMONS.md: {e}"

@mcp.tool()
def add_pokemon(name: str, level: int, location: str, moves: str = "Unbekannt") -> str:
    """
    Hängt ein neues Pokémon an die POKEMONS.md Datei an.
    Parameter:
    - name: Name des Pokémon (z.B. 'Flemmli')
    - level: Aktuelles Level als Zahl (z.B. 5)
    - location: Wo es erhalten/gefangen wurde (z.B. 'Route 101')
    - moves: Bekannte Attacken als kommagetrennter String (z.B. 'Kratzer, Heuler')
    """
    entry = f"\n- **{name}** (Lv. {level}) | Ort: {location} | Attacken: {moves}"
    try:
        pokemon_file = os.path.join(PROJECT_ROOT, "POKEMONS.md")
        with open(pokemon_file, "a", encoding="utf-8") as f:
            f.write(entry)
        logging.info(f"Tool Aufruf: add_pokemon. {name} zu POKEMONS.md hinzugefügt.")
        return f"Erfolg: {name} wurde der POKEMONS.md hinzugefügt."
    except Exception as e:
        logging.error(f"Fehler beim Anhängen an POKEMONS.md: {e}")
        return f"Fehler beim Anhängen an POKEMONS.md: {e}"


@mcp.tool()
def attack_pokemon(slot: int) -> str:
    """
    Fuehrt eine Attacke im Pokemon-Kampf aus.

    WICHTIG: Rufe diese Funktion NUR auf, wenn du dich im Kampf-Hauptmenue befindest,
    also wenn rechts unten die vier Optionen 'KAMPF', 'BEUTEL', 'POKEMON', 'FLUCHT' sichtbar sind!
    Wenn du dir unsicher bist, nutze zuerst get_state() um den Bildschirm zu pruefen.

    Der Agenten-Kontext enthaelt die aktuell dokumentierten Pokemon und Attacken aus POKEMONS.md.
    Waehle daraus die sinnvollste Attacke und uebergib nur den passenden Slot.
    Die Funktion drueckt automatisch 'KAMPF', navigiert zum gewaehlten Attacken-Slot und bestaetigt.

    Parameter:
    - slot: Position der Attacke im Attacken-Menue (1 bis 4):
        1 = oben links
        2 = oben rechts
        3 = unten links
        4 = unten rechts
    """
    if not isinstance(slot, int) or not (1 <= slot <= 4):
        return f"Fehler: Slot muss 1, 2, 3 oder 4 sein. Erhalten: {slot}"

    logging.info(f"Tool Aufruf: attack_pokemon(slot={slot})")

    emulator_controller.send_keyboard_input(TARGET, BUTTON_MAPPING["a"], is_pid=USE_PID, duration=0.15)
    time.sleep(0.4)

    emulator_controller.send_keyboard_input(TARGET, "up", is_pid=USE_PID, duration=0.1)
    time.sleep(0.1)
    emulator_controller.send_keyboard_input(TARGET, "left", is_pid=USE_PID, duration=0.1)
    time.sleep(0.1)

    if slot == 2:
        emulator_controller.send_keyboard_input(TARGET, "right", is_pid=USE_PID, duration=0.1)
        time.sleep(0.1)
    elif slot == 3:
        emulator_controller.send_keyboard_input(TARGET, "down", is_pid=USE_PID, duration=0.1)
        time.sleep(0.1)
    elif slot == 4:
        emulator_controller.send_keyboard_input(TARGET, "right", is_pid=USE_PID, duration=0.1)
        time.sleep(0.1)
        emulator_controller.send_keyboard_input(TARGET, "down", is_pid=USE_PID, duration=0.1)
        time.sleep(0.1)

    emulator_controller.send_keyboard_input(TARGET, BUTTON_MAPPING["a"], is_pid=USE_PID, duration=0.15)

    logging.info(f"Attacke in Slot {slot} ausgefuehrt.")
    return f"Erfolg: Attacke in Slot {slot} wurde ausgefuehrt."

if __name__ == "__main__":
    # Startet den MCP Server. Er lauscht nun auf stdin/stdout.
    logging.info("Starte MCP Server 'PokemonEmulator'...")
    mcp.run()
