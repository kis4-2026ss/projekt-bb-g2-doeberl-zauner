import time
import os
import datetime
import logging

logger = logging.getLogger(__name__)

try:
    import pyautogui
    import pygetwindow as gw
    import pydirectinput
    import win32gui
    import win32process
except ImportError:
    logger.error("Fehlende Bibliotheken. Bitte installiere sie mit:")
    logger.error("pip install pyautogui pygetwindow pydirectinput Pillow pywin32")
    exit()

def get_window_title_by_pid(pid):
    """Findet den Titel des Hauptfensters eines Prozesses anhand seiner PID."""
    def callback(hwnd, hwnds):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
            if found_pid == pid:
                hwnds.append(hwnd)
        return True

    hwnds = []
    win32gui.EnumWindows(callback, hwnds)
    
    if hwnds:
        return win32gui.GetWindowText(hwnds[0])
    return None

def find_pid_by_title_prefix(title_prefix):
    """Sucht nach einem Fenster, dessen Titel mit title_prefix beginnt (erste 10 Zeichen), und gibt dessen PID zurück."""
    found_pids = []
    prefix = title_prefix[:20].lower() # Die ersten 20 Buchstaben weil da die Frames sich immer behindert aendern

    def callback(hwnd, hwnds):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title and title.lower().startswith(prefix):
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                hwnds.append(pid)
        return True

    win32gui.EnumWindows(callback, found_pids)
    
    if found_pids:
        return found_pids[0]
    return None

def get_emulator_window(target, is_pid=False):
    """Sucht nach dem Emulator-Fenster anhand des Titels oder der PID."""
    if is_pid:
        window_title = get_window_title_by_pid(target)
        if not window_title:
            logger.error(f"Fehler: Kein sichtbares Fenster für PID {target} gefunden.")
            return None
    else:
        window_title = target

    windows = gw.getWindowsWithTitle(window_title)
    if not windows:
        logger.error(f"Fehler: Fenster mit dem Titel '{window_title}' nicht gefunden.")
        if not is_pid:
            logger.info("Offene Fenster:")
            for w in gw.getAllTitles():
                if w.strip():
                    logger.info(f" - {w}")
        return None
    
    return windows[0]

def take_screenshot(target, is_pid=False, output_filename="emulator_screenshot.png"):
    """Macht einen Screenshot vom Emulator-Fenster und speichert ihn ab."""
    win = get_emulator_window(target, is_pid)
    if not win:
        return False

    try:
        if not win.isActive:
            win.activate()
            time.sleep(0.5)

        time.sleep(1) # Sonst oft mal ein schaß Screenshot wo der Angriff gerade noch läuft oder text noch geschrieben wird
        region = (win.left, win.top, win.width, win.height)
        screenshot = pyautogui.screenshot(region=region)
        
        # 1. Normalen Screenshot speichern (z.B. für MCP)
        screenshot.save(output_filename)
        logger.info(f"Screenshot erfolgreich gespeichert unter: {output_filename}")
        
        # 2. Backup mit Zeitstempel speichern
        backup_dir = "BackUp_Ordner"
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
            
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base_name = os.path.basename(output_filename)
        name, ext = os.path.splitext(base_name)
        backup_filename = os.path.join(backup_dir, f"{name}_{timestamp}{ext}")
        
        screenshot.save(backup_filename)
        logger.info(f"Screenshot archiviert unter: {backup_filename}")
        
        return True

    except Exception as e:
        logger.error(f"Fehler beim Erstellen des Screenshots: {e}")
        return False

def send_keyboard_input(target, key, is_pid=False, duration=0.1):
    """Sendet einen Tastaturanschlag an den Emulator."""
    win = get_emulator_window(target, is_pid)
    if not win:
        return

    if not win.isActive:
        win.activate()
        time.sleep(0.2)

    logger.info(f"Drücke Taste: '{key}'")
    pydirectinput.keyDown(key)
    time.sleep(duration)
    pydirectinput.keyUp(key)

def send_mouse_click(target, x_offset, y_offset, is_pid=False):
    """Klickt mit der Maus auf eine bestimmte Position im Emulator."""
    win = get_emulator_window(target, is_pid)
    if not win:
        return

    if not win.isActive:
        win.activate()
        time.sleep(0.2)

    target_x = win.left + x_offset
    target_y = win.top + y_offset

    logger.info(f"Klicke auf relative Position ({x_offset}, {y_offset}) -> absolut ({target_x}, {target_y})")
    pydirectinput.moveTo(target_x, target_y)
    pydirectinput.click()

# --- KONFIGURATION ---
# Wir lagern die Konfiguration aus dem if __name__ == '__main__' Block aus,
# damit sie auch bei Import (z.B. durch services.py) verfügbar ist.
EMULATOR_TITLE = "mGBA - Pokemon - Smaragd-Edition" 

# Wir suchen automatisch nach der PID anhand der ersten 10 Buchstaben des Titels
EMULATOR_PID = find_pid_by_title_prefix(EMULATOR_TITLE)

if EMULATOR_PID:
    USE_PID = True
else:
    USE_PID = False
    logger.warning(f"WICHTIG: Kein offenes Fenster gefunden, das mit '{EMULATOR_TITLE[:10]}' beginnt!")

if __name__ == "__main__":
    # Wenn direkt aufgerufen, logge in die Konsole
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    target = EMULATOR_PID if USE_PID else EMULATOR_TITLE
    logger.info(f"Suche nach Emulator (Suche per PID aktiv: {USE_PID}): '{target}'...\n")
    
    # 1. Screenshot machen
    take_screenshot(target, is_pid=USE_PID, output_filename="pokemon_screen.png")
    
    time.sleep(1)
    
    # 2. Tastatur-Input senden
    send_keyboard_input(target, 'up', is_pid=USE_PID, duration=0.2)
    time.sleep(0.5)
    
    # 3. Maus-Input senden
    send_mouse_click(target, 150, 250, is_pid=USE_PID)
