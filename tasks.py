#!/usr/bin/env python3
import json
import os
import sys
import argparse
import time
import uuid
from datetime import datetime, date, timedelta

# Auf Unix-Systemen brauchen wir select für den Timer
if os.name != 'nt':
    import select
else:
    import ctypes

# --- KONFIGURATION & KONSTANTEN ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "tasks.json")
LOG_FILE = os.path.join(BASE_DIR, "done_log.txt")
ICAL_FILE = os.path.join(BASE_DIR, "tasks.ics")

def enable_windows_ansi_support():
    if os.name == 'nt':
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    REVERSE = '\033[7m' 
    
    ALT_SCREEN_ENTER = '\033[?1049h'
    ALT_SCREEN_EXIT  = '\033[?1049l'
    HIDE_CURSOR      = '\033[?25l'
    SHOW_CURSOR      = '\033[?25h'

# --- HELPER FUNCTIONS ---

def parse_german_date(date_str):
    if not date_str: return None
    if "-" in date_str and date_str.count("-") == 2:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            return date_str
        except ValueError: pass

    try:
        parts = date_str.strip().split('.')
        today = date.today()
        year = today.year
        if len(parts) == 2:
            day, month = int(parts[0]), int(parts[1])
            if (month, day) < (today.month, today.day): year += 1
        elif len(parts) == 3:
            day, month = int(parts[0]), int(parts[1])
            year_in = int(parts[2])
            year = year_in + 2000 if year_in < 100 else year_in
        else: return None
        return date(year, month, day).strftime("%Y-%m-%d")
    except (ValueError, IndexError): return None

def format_due_date(date_str):
    if not date_str: return ""
    try:
        due_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = date.today()
        delta = (due_date - today).days
        if delta < 0: return f"{Colors.FAIL}Überfällig ({abs(delta)}d){Colors.ENDC}"
        elif delta == 0: return f"{Colors.WARNING}HEUTE{Colors.ENDC}"
        elif delta == 1: return f"{Colors.WARNING}Morgen{Colors.ENDC}"
        elif delta < 7: return f"{Colors.BLUE}In {delta} Tagen{Colors.ENDC}"
        else: return f"{Colors.BLUE}{due_date.strftime('%d.%m.%Y')}{Colors.ENDC}"
    except ValueError: return date_str

def calculate_next_date(date_str, recurrence):
    if not date_str or not recurrence: return None
    try:
        current_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        unit = recurrence[-1].lower()
        val_str = recurrence[:-1]
        
        if not val_str: val = 1
        else:
            try: val = int(val_str)
            except ValueError: val = 1

        if unit == 'd': new_date = current_date + timedelta(days=val)
        elif unit == 'w': new_date = current_date + timedelta(weeks=val)
        else: return None
        return new_date.strftime("%Y-%m-%d")
    except: return None
    
def send_notification(title, message):
    if sys.platform == 'darwin':
        safe_msg = message.replace('"', '\\"')
        safe_title = title.replace('"', '\\"')
        os.system(f"""osascript -e 'display notification "{safe_msg}" with title "{safe_title}" sound name "Glass"'""")
    elif sys.platform.startswith('linux'):
        os.system(f'notify-send "{title}" "{message}"')
    elif os.name == 'nt':
        import winsound
        winsound.MessageBeep()

# --- INPUT HANDLING ---
class InputHandler:
    def __init__(self):
        self.is_windows = os.name == 'nt'
        if self.is_windows:
            import msvcrt
            self.msvcrt = msvcrt

    def get_key(self, timeout=None):
        if self.is_windows:
            start_time = time.time()
            while True:
                if self.msvcrt.kbhit():
                    key = self.msvcrt.getch()
                    if key in (b'\x00', b'\xe0'):
                        key = self.msvcrt.getch()
                        if key == b'H': return 'up'
                        if key == b'P': return 'down'
                    return key.decode('utf-8', 'ignore').lower()
                if timeout is not None:
                    if time.time() - start_time > timeout: return None
                    time.sleep(0.05)
                else: time.sleep(0.05)
        else:
            import tty, termios
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(sys.stdin.fileno())
                rlist, _, _ = select.select([sys.stdin], [], [], timeout)
                if rlist:
                    ch = sys.stdin.read(1)
                    if ch == '\x1b':
                        seq = sys.stdin.read(2)
                        if seq == '[A': return 'up'
                        if seq == '[B': return 'down'
                        return 'esc'
                    return ch
                else: return None
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

# --- CONTEXT MANAGER ---
class AppWindow:
    def __enter__(self):
        enable_windows_ansi_support()
        sys.stdout.write(Colors.ALT_SCREEN_ENTER)
        sys.stdout.write(Colors.HIDE_CURSOR)
        sys.stdout.flush()
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout.write(Colors.ALT_SCREEN_EXIT)
        sys.stdout.write(Colors.SHOW_CURSOR)
        sys.stdout.flush()
        return False

# --- CORE APPLICATION ---
class TodoApp:
    def __init__(self):
        self.tasks = self.load_tasks()
        self.input = InputHandler()
        self.last_deleted = None
        
        # NEU: Aktive Liste
        self.current_list = "Allgemein" 
        
        # Migration alter Daten (Feld "list" hinzufügen)
        if self.sanitize_tasks():
            self.save_tasks()
            
        self.selected_idx = 0
        self.sort_tasks()

    def load_tasks(self):
        if not os.path.exists(DATA_FILE): return []
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: return []

    def sanitize_tasks(self):
        """Stellt sicher, dass alle Tasks ein 'list' Attribut haben"""
        dirty = False
        for task in self.tasks:
            # Migration Datumsformat
            d = task.get('due')
            if d and isinstance(d, str) and '.' in d:
                clean = parse_german_date(d)
                if clean:
                    task['due'] = clean
                    dirty = True
            
            # Migration Liste
            if 'list' not in task:
                task['list'] = "Allgemein"
                dirty = True
            
            if 'priority' not in task: task['priority'] = 1
            if 'recurrence' not in task: task['recurrence'] = None
            
        return dirty

    def save_tasks(self):
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.tasks, f, indent=4)

    # --- LIST MANAGEMENT HELPER ---
    
    def get_visible_tasks(self):
        """Gibt nur Tasks der aktuellen Liste zurück"""
        return [t for t in self.tasks if t.get('list') == self.current_list]

    def get_all_list_names(self):
        """Sammelt alle einzigartigen Listennamen"""
        names = set(t.get('list', 'Allgemein') for t in self.tasks)
        names.add(self.current_list) # Aktuelle Liste immer dazu, auch wenn leer
        return sorted(list(names))

    # --- EXPORT ---

    def export_ical(self):
        try:
            with open(ICAL_FILE, 'w', encoding='utf-8') as f:
                f.write("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//TerminalTodo//DE\n")
                count = 0
                now_stamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
                # Exportiere nur Tasks der aktuellen Liste oder alle? 
                # -> Hier: ALLE exportieren ist sinnvoller für Kalender
                for task in self.tasks:
                    due = task.get('due')
                    if not due: continue
                    dtstart = due.replace("-", "")
                    status = "COMPLETED" if task['done'] else "CONFIRMED"
                    f.write("BEGIN:VEVENT\n")
                    f.write(f"UID:{uuid.uuid4()}\nDTSTAMP:{now_stamp}\n")
                    f.write(f"DTSTART;VALUE=DATE:{dtstart}\nSUMMARY:{task['title']} ({task.get('list')})\n")
                    f.write(f"STATUS:{status}\nEND:VEVENT\n")
                    count += 1
                f.write("END:VCALENDAR\n")
            
            sys.stdout.write(Colors.SHOW_CURSOR)
            self.clear_screen()
            print(f"\n\n  {Colors.GREEN}Export erfolgreich!{Colors.ENDC}")
            print(f"  {Colors.BOLD}{ICAL_FILE}{Colors.ENDC}")
            time.sleep(1.5)
            sys.stdout.write(Colors.HIDE_CURSOR)
        except Exception: pass

    # --- LOGGING ---
    def log_done_task(self, task):
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] [{task.get('list')}] {task['title']}\n")
        except: pass

    def remove_log_entry(self, task):
        if not os.path.exists(LOG_FILE): return
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines: return
            last_line = lines[-1].strip()
            if task['title'] in last_line:
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.writelines(lines[:-1])
        except: pass

    def sort_tasks(self):
        self.tasks.sort(key=lambda x: (
            x['done'], 
            x['due'] if x['due'] else "9999-12-31", 
            -x.get('priority', 1)
        ))

    def get_progress(self):
        visible = self.get_visible_tasks()
        if not visible: return 0.0, 0
        done_count = sum(1 for t in visible if t['done'])
        return (done_count / len(visible)), done_count

    def clear_screen(self):
        if os.name == 'nt': os.system('cls')
        else: sys.stdout.write('\033[2J\033[H')

    def draw_ui(self):
        self.clear_screen()
        
        # Header mit Listenname
        list_display = f"LISTE: {self.current_list.upper()}"
        print(f"{Colors.BLUE}╔{'═'*48}╗{Colors.ENDC}")
        print(f"{Colors.BLUE}║ {Colors.BOLD}{list_display:<46}{Colors.ENDC} {Colors.BLUE}║{Colors.ENDC}")
        print(f"{Colors.BLUE}╚{'═'*48}╝{Colors.ENDC}")
        
        percent, done_count = self.get_progress()
        bar_len = 30
        filled = int(bar_len * percent)
        bar = '█' * filled + '░' * (bar_len - filled)
        col = Colors.FAIL if percent < 0.5 else Colors.GREEN
        print(f"\n   Fortschritt: {col}[{bar}]{Colors.ENDC} {int(percent*100)}%")

        print(f"\n{Colors.UNDERLINE}Deine Aufgaben:{Colors.ENDC}\n")

        visible_tasks = self.get_visible_tasks()

        if not visible_tasks:
            print(f"  {Colors.WARNING}(Liste '{self.current_list}' ist leer){Colors.ENDC}")
            print(f"  {Colors.BLUE}Drücke 'a' zum Hinzufügen{Colors.ENDC}")
        else:
            for i, task in enumerate(visible_tasks):
                checkbox = f"{Colors.GREEN}[✔]{Colors.ENDC}" if task['done'] else f"{Colors.FAIL}[ ]{Colors.ENDC}"
                p = task.get('priority', 1)
                p_str = f"{Colors.FAIL}!!!{Colors.ENDC}" if p==3 else (f"{Colors.WARNING} !!{Colors.ENDC}" if p==2 else "   ")
                
                # Highlight selection
                if i == self.selected_idx:
                    prefix = f"{Colors.BLUE}>>{Colors.ENDC}"
                    l_col = Colors.REVERSE
                else:
                    prefix = "  "
                    l_col = ""
                
                due_str = format_due_date(task.get('due'))
                rec = task.get('recurrence')
                rec_str = f"{Colors.BLUE} ⟳ {rec}{Colors.ENDC}" if rec else ""
                
                print(f"{prefix} {checkbox} {l_col} {p_str} {task['title']:<25} {due_str}{rec_str} {Colors.ENDC}")

        print("\n" + "-" * 50)
        print(f"{Colors.BOLD}Steuerung:{Colors.ENDC} [↑/↓] Nav | [Space] Done | [F]ocus")
        # Neue Steuerung
        print(f"           [L]iste | [A]dd  | [D]el  | [U]ndo")

    def run_focus_mode(self):
        visible = self.get_visible_tasks()
        if not visible: return
        task = visible[self.selected_idx]
        
        sys.stdout.write(Colors.SHOW_CURSOR)
        self.clear_screen()
        print(f"\n{Colors.BLUE}Focus Modus für: {Colors.BOLD}{task['title']}{Colors.ENDC}")
        try:
            inp = input(f"{Colors.BLUE}Dauer in Minuten (Default 25): {Colors.ENDC}").strip()
            minutes = int(inp) if inp else 25
        except ValueError: minutes = 25
        
        sys.stdout.write(Colors.HIDE_CURSOR)
        duration = minutes * 60
        start_time = time.time()
        
        while True:
            elapsed = time.time() - start_time
            remaining = max(0, duration - elapsed)
            m, s = divmod(int(remaining), 60)
            
            self.clear_screen()
            print("\n" * 5)
            print(f"{Colors.BLUE}{'='*50}{Colors.ENDC}")
            print(f"{Colors.BOLD}   F O C U S    M O D E   ({minutes} min){Colors.ENDC}".center(60))
            print(f"{Colors.BLUE}{'='*50}{Colors.ENDC}")
            print("\n" * 3)
            print(f"  {Colors.BOLD}{Colors.UNDERLINE}{task['title']}{Colors.ENDC}".center(60))
            print("\n" * 2)
            t_col = Colors.GREEN if remaining > 60 else Colors.FAIL
            print(f"  {t_col}[ {m:02d}:{s:02d} ]{Colors.ENDC}".center(60))
            print("\n" * 5)
            print(f"{Colors.WARNING}Drücke 'q' oder 'f' zum Beenden{Colors.ENDC}".center(60))
            
            key = self.input.get_key(timeout=0.5)
            if key in ('q', 'f', '\x1b'): break
            if remaining == 0:
                send_notification("Focus beendet!", f"Gut gemacht: {task['title']}")
                for _ in range(3):
                    print('\033[?5h', end='', flush=True); time.sleep(0.3)
                    print('\033[?5l', end='', flush=True); time.sleep(0.3)
                print('\a')
                self.input.get_key() 
                break

    def _prompt(self, text):
        sys.stdout.write(Colors.SHOW_CURSOR)
        print(f"\n{Colors.BLUE}{text}{Colors.ENDC}", end=" ")
        val = input().strip()
        sys.stdout.write(Colors.HIDE_CURSOR)
        return val

    # --- ACTION HANDLERS ---

    def action_manage_lists(self):
        """Menü zum Wechseln/Erstellen/Löschen von Listen"""
        while True:
            self.clear_screen()
            all_lists = self.get_all_list_names()
            print(f"{Colors.BOLD}LISTEN VERWALTUNG{Colors.ENDC}\n")
            print(f"Aktuelle Liste: {Colors.GREEN}{self.current_list}{Colors.ENDC}\n")
            
            print("Verfügbare Listen:")
            for l_name in all_lists:
                prefix = " >" if l_name == self.current_list else "  "
                count = sum(1 for t in self.tasks if t.get('list') == l_name)
                print(f"{prefix} {l_name:<20} ({count} Tasks)")
                
            print("\n[W]echseln/Neu | [U]mbenennen | [L]öschen | [Esc] Zurück")
            
            key = self.input.get_key()
            if key == 'esc' or key == 'q':
                break
            
            elif key == 'w': # Wechseln / Erstellen
                name = self._prompt("Name der Liste (neu oder vorhanden):")
                if name:
                    self.current_list = name
                    self.selected_idx = 0 # Reset Index
                    return # Zurück zur Main UI

            elif key == 'u': # Umbenennen
                new_name = self._prompt(f"Liste '{self.current_list}' umbenennen in:")
                if new_name and new_name != self.current_list:
                    # Alle Tasks der aktuellen Liste updaten
                    for t in self.tasks:
                        if t.get('list') == self.current_list:
                            t['list'] = new_name
                    self.current_list = new_name
                    self.save_tasks()

            elif key == 'l': # Löschen
                confirm = self._prompt(f"Liste '{self.current_list}' und ALLE Tasks löschen? (y/n):")
                if confirm.lower() == 'y':
                    # Tasks filtern (nur die behalten, die NICHT in der aktuellen Liste sind)
                    self.tasks = [t for t in self.tasks if t.get('list') != self.current_list]
                    self.current_list = "Allgemein" # Fallback
                    self.selected_idx = 0
                    self.save_tasks()
                    return

    def action_add(self):
        title = self._prompt("Titel:")
        if title:
            due = parse_german_date(self._prompt("Fällig (DD.MM oder DD.MM.YYYY) [Enter=Nie]:"))
            rec_in = self._prompt("Wiederholung (z.B. '1d', '3d', '1w') [Enter=Nein]:").lower()
            recurrence = None
            if rec_in and (rec_in.endswith('d') or rec_in.endswith('w')):
                recurrence = rec_in
            elif rec_in == 't': recurrence = '1d'

            # NEU: Liste speichern
            self.tasks.append({
                "title": title, 
                "done": False, 
                "priority": 1, 
                "due": due,
                "recurrence": recurrence,
                "list": self.current_list
            })
            self.sort_tasks(); self.save_tasks()

    def action_edit(self):
        visible = self.get_visible_tasks()
        if not visible: return
        # Achtung: Wir müssen das originale Task-Objekt in self.tasks finden
        current_view_task = visible[self.selected_idx]
        
        # Referenz finden
        real_task = next((t for t in self.tasks if t is current_view_task), None)
        if real_task:
            new_t = self._prompt(f"Neuer Titel ({real_task['title']}):")
            if new_t:
                real_task['title'] = new_t
                self.save_tasks()

    def action_delete(self):
        visible = self.get_visible_tasks()
        if not visible: return
        current_view_task = visible[self.selected_idx]
        
        # In self.tasks finden und entfernen
        if current_view_task in self.tasks:
            self.last_deleted = current_view_task
            self.log_done_task(current_view_task)
            self.tasks.remove(current_view_task)
            self.save_tasks()
            
            # Index korrigieren
            new_len = len(self.get_visible_tasks())
            self.selected_idx = max(0, min(self.selected_idx, new_len - 1))

    def action_undo(self):
        if self.last_deleted:
            self.tasks.append(self.last_deleted)
            self.remove_log_entry(self.last_deleted)
            self.last_deleted = None
            self.sort_tasks()
            self.save_tasks()
            sys.stdout.write(Colors.SHOW_CURSOR)
            print(f"\n  {Colors.GREEN}Wiederhergestellt!{Colors.ENDC}")
            time.sleep(0.8)
            sys.stdout.write(Colors.HIDE_CURSOR)

    def run_tui(self):
        with AppWindow():
            while True:
                visible = self.get_visible_tasks()
                if visible: 
                    self.selected_idx = max(0, min(self.selected_idx, len(visible)-1))
                else:
                    self.selected_idx = 0

                self.draw_ui()
                key = self.input.get_key()

                if key in ('up', 'k') and self.selected_idx > 0: self.selected_idx -= 1
                elif key in ('down', 'j') and self.selected_idx < len(visible)-1: self.selected_idx += 1
                
                elif key in (' ', 't') and visible:
                    task = visible[self.selected_idx]
                    task['done'] = not task['done']
                    
                    # Wiederholung Logic
                    if task['done'] and task.get('recurrence') and task.get('due'):
                        new_due = calculate_next_date(task['due'], task['recurrence'])
                        if new_due:
                            new_task = task.copy()
                            new_task['done'] = False
                            new_task['due'] = new_due
                            # WICHTIG: UUID oder Referenz lösen, hier Copy -> OK. 
                            # Liste wird automatisch mitkopiert.
                            self.tasks.append(new_task)
                            sys.stdout.write(Colors.SHOW_CURSOR)
                            print(f"\n  {Colors.GREEN}Neue Aufgabe für {new_due} erstellt!{Colors.ENDC}")
                            time.sleep(0.8)
                            sys.stdout.write(Colors.HIDE_CURSOR)

                    self.sort_tasks(); self.save_tasks()
                    
                elif key in ('1', '2', '3') and visible:
                    visible[self.selected_idx]['priority'] = int(key)
                    self.sort_tasks(); self.save_tasks()
                    
                elif key == 'f': self.run_focus_mode()
                elif key == 'a': self.action_add()
                elif key == 'e': self.action_edit()
                elif key == 'd': self.action_delete()
                elif key == 'x': self.export_ical()
                elif key == 'u': self.action_undo()
                # NEU: List Management
                elif key == 'l': self.action_manage_lists()
                
                elif key in ('q', '\x1b'): break

    def run_cli_add(self, title, prio, due_input):
        enable_windows_ansi_support()
        due = parse_german_date(due_input)
        # CLI fügt standardmäßig zu "Allgemein" hinzu, außer wir bauen Argumente dafür.
        # Hier simpel halten:
        self.tasks.append({
            "title": title, 
            "done": False, 
            "priority": prio, 
            "due": due, 
            "recurrence": None,
            "list": "Allgemein"
        })
        self.sort_tasks(); self.save_tasks()
        print(f"{Colors.GREEN}Task '{title}' gespeichert.{Colors.ENDC}")
    
    def run_list_short(self):
        enable_windows_ansi_support()
        open_tasks = [t for t in self.tasks if not t['done']]
        if not open_tasks: print(f"{Colors.GREEN}Alles erledigt!{Colors.ENDC}"); return
        print(f"{Colors.BOLD}Offene Aufgaben:{Colors.ENDC}")
        for i, t in enumerate(open_tasks):
            p = "!!!" if t.get('priority')==3 else (" !!" if t.get('priority')==2 else "  ")
            due = format_due_date(t.get('due'))
            # Liste mit anzeigen
            lst = f" [{t.get('list', 'Allgemein')}]"
            print(f" {i+1}. {p} {t['title']} {due}{lst}")

def main():
    app = TodoApp()
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_p = subparsers.add_parser("add")
    add_p.add_argument("title")
    add_p.add_argument("-p", "--priority", type=int, choices=[1,2,3], default=1)
    add_p.add_argument("-d", "--due", help="Datum: DD.MM")
    subparsers.add_parser("list-short")
    args = parser.parse_args()
    if args.command == "add": app.run_cli_add(args.title, args.priority, args.due)
    elif args.command == "list-short": app.run_list_short()
    else: app.run_tui()

if __name__ == "__main__":
    main()