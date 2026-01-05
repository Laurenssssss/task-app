#!/usr/bin/env python3
import json
import os
import sys
import argparse
import time
import uuid
import glob
import shutil
import textwrap
import re
from datetime import datetime, date, timedelta

# Auf Unix-Systemen brauchen wir select für den Timer
if os.name != 'nt':
    import select
else:
    import ctypes

# --- KONFIGURATION & KONSTANTEN ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_FILE = os.path.join(BASE_DIR, "done_log.txt")
ICAL_FILE = os.path.join(BASE_DIR, "tasks.ics")

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

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
    GREY = '\033[90m'
    
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    YELLOW = '\033[93m'
    WHITE = '\033[97m'
    RED = '\033[91m'
    
    ALT_SCREEN_ENTER = '\033[?1049h'
    ALT_SCREEN_EXIT  = '\033[?1049l'
    HIDE_CURSOR      = '\033[?25l'
    SHOW_CURSOR      = '\033[?25h'

# --- HELPER FUNCTIONS ---

def strip_ansi(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

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
        self.input = InputHandler()
        self.current_list_name = "ALLE" 
        self.virtual_all_mode = True

        old_file = os.path.join(BASE_DIR, "tasks.json")
        new_file = os.path.join(DATA_DIR, "tasks.json")
        if os.path.exists(old_file) and not os.path.exists(new_file):
            try: os.rename(old_file, new_file)
            except: pass

        self.tasks = self.load_current_context()
        self.last_deleted = None
        self.selected_idx = 0
        self.sort_tasks()

    def get_list_file_path(self, name):
        safe_name = "".join([c for c in name if c.isalnum() or c in (' ', '-', '_')]).strip()
        if not safe_name: safe_name = "default"
        return os.path.join(DATA_DIR, f"{safe_name}.json")

    def _read_file(self, path):
        if not os.path.exists(path): return []
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for task in data:
                    if 'priority' not in task: task['priority'] = 1
                    if 'due' not in task: task['due'] = None
                    if 'recurrence' not in task: task['recurrence'] = None
                    d = task.get('due')
                    if d and isinstance(d, str) and '.' in d:
                        clean = parse_german_date(d)
                        if clean: task['due'] = clean
                return data
        except: return []

    def load_current_context(self):
        if self.current_list_name == "ALLE":
            self.virtual_all_mode = True
            all_tasks = []
            files = glob.glob(os.path.join(DATA_DIR, "*.json"))
            for fpath in files:
                lname = os.path.splitext(os.path.basename(fpath))[0]
                tasks = self._read_file(fpath)
                for t in tasks:
                    t['_origin'] = lname
                all_tasks.extend(tasks)
            return all_tasks
        else:
            self.virtual_all_mode = False
            return self._read_file(self.get_list_file_path(self.current_list_name))

    def save_tasks(self):
        if self.virtual_all_mode:
            tasks_by_origin = {}
            files = glob.glob(os.path.join(DATA_DIR, "*.json"))
            for fpath in files:
                lname = os.path.splitext(os.path.basename(fpath))[0]
                tasks_by_origin[lname] = []

            for task in self.tasks:
                origin = task.get('_origin', 'tasks')
                save_task = task.copy()
                if '_origin' in save_task: del save_task['_origin']
                
                if origin not in tasks_by_origin:
                    tasks_by_origin[origin] = []
                tasks_by_origin[origin].append(save_task)
            
            for lname, t_list in tasks_by_origin.items():
                self._write_file(self.get_list_file_path(lname), t_list)
        else:
            self._write_file(self.get_list_file_path(self.current_list_name), self.tasks)

    def _write_file(self, path, tasks):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(tasks, f, indent=4)

    def get_all_lists(self):
        files = glob.glob(os.path.join(DATA_DIR, "*.json"))
        names = [os.path.splitext(os.path.basename(f))[0] for f in files]
        if "tasks" not in names: names.insert(0, "tasks")
        names = sorted(list(set(names)))
        if "tasks" in names:
            names.remove("tasks")
            names.insert(0, "tasks")
        return names

    def export_ical(self):
        try:
            with open(ICAL_FILE, 'w', encoding='utf-8') as f:
                f.write("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//TerminalTodo//DE\n")
                now_stamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
                for task in self.tasks:
                    due = task.get('due')
                    if not due: continue
                    dtstart = due.replace("-", "")
                    status = "COMPLETED" if task['done'] else "CONFIRMED"
                    f.write("BEGIN:VEVENT\n")
                    f.write(f"UID:{uuid.uuid4()}\nDTSTAMP:{now_stamp}\n")
                    f.write(f"DTSTART;VALUE=DATE:{dtstart}\nSUMMARY:{task['title']}\n")
                    f.write(f"STATUS:{status}\nEND:VEVENT\n")
                f.write("END:VCALENDAR\n")
            
            sys.stdout.write(Colors.SHOW_CURSOR)
            self.clear_screen()
            print(f"\n\n  {Colors.GREEN}Export erfolgreich!{Colors.ENDC}")
            time.sleep(1.0)
            sys.stdout.write(Colors.HIDE_CURSOR)
        except Exception: pass

    def log_done_task(self, task):
        try:
            origin = task.get('_origin', self.current_list_name)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] [{origin}] {task['title']}\n")
        except: pass

    def sort_tasks(self):
        self.tasks.sort(key=lambda x: (
            x['done'], 
            x['due'] if x['due'] else "9999-12-31", 
            -x.get('priority', 1)
        ))

    def get_progress(self):
        if not self.tasks: return 0.0, 0
        done_count = sum(1 for t in self.tasks if t['done'])
        return (done_count / len(self.tasks)), done_count

    def clear_screen(self):
        if os.name == 'nt': os.system('cls')
        else: sys.stdout.write('\033[2J\033[H')

    def get_current_color_map(self):
        unique_origins = sorted(list(set(t.get('_origin', 'tasks') for t in self.tasks)))
        palette = [Colors.CYAN, Colors.MAGENTA, Colors.YELLOW, Colors.BLUE, Colors.GREEN, Colors.RED, Colors.WHITE, Colors.GREY]
        mapping = {}
        for i, name in enumerate(unique_origins):
            mapping[name] = palette[i % len(palette)]
        return mapping

    def draw_ui(self):
        self.clear_screen()
        try:
            term_width = shutil.get_terminal_size((80, 24)).columns
        except:
            term_width = 80
        
        # --- KOMPAKTHEIT ---
        # Wir begrenzen die Breite auf 90 Zeichen, um es kompakt zu halten.
        # Wenn das Terminal kleiner ist, nehmen wir die Terminalbreite.
        MAX_COMPACT_WIDTH = 90
        render_width = min(term_width, MAX_COMPACT_WIDTH)

        if self.virtual_all_mode:
            list_display = "ALLE AUFGABEN"
            header_col = Colors.HEADER
        else:
            list_display = f"LISTE: {self.current_list_name.upper()}"
            header_col = Colors.BLUE

        padding = render_width - len(list_display) - 4
        if padding < 0: padding = 0
        left_pad = padding // 2
        right_pad = padding - left_pad

        print(f"{Colors.BLUE}╔{'═'*(render_width-2)}╗{Colors.ENDC}")
        print(f"{Colors.BLUE}║{' '*left_pad}{Colors.BOLD}{header_col}{list_display}{Colors.ENDC}{Colors.BLUE}{' '*right_pad}║{Colors.ENDC}")
        print(f"{Colors.BLUE}╚{'═'*(render_width-2)}╝{Colors.ENDC}")
        
        percent, done_count = self.get_progress()
        bar_len = min(30, render_width - 20)
        filled = int(bar_len * percent)
        bar = '█' * filled + '░' * (bar_len - filled)
        col = Colors.FAIL if percent < 0.5 else Colors.GREEN
        print(f"\n   Fortschritt: {col}[{bar}]{Colors.ENDC} {int(percent*100)}%")

        print(f"\n{Colors.UNDERLINE}Deine Aufgaben:{Colors.ENDC}\n")

        color_map = self.get_current_color_map()

        if not self.tasks:
            print(f"  {Colors.WARNING}(Liste ist leer){Colors.ENDC}")
        else:
            for i, task in enumerate(self.tasks):
                is_selected = (i == self.selected_idx)
                
                # --- LINKER BLOCK (Prefix, Checkbox, Prio) ---
                # ">> [ ] !!! " -> ca 11 Zeichen
                checkbox = f"{Colors.GREEN}[✔]{Colors.ENDC}" if task['done'] else f"{Colors.FAIL}[ ]{Colors.ENDC}"
                p = task.get('priority', 1)
                p_str = f"{Colors.FAIL}!!!{Colors.ENDC}" if p==3 else (f"{Colors.WARNING} !!{Colors.ENDC}" if p==2 else "   ")
                prefix = f"{Colors.BLUE}>>{Colors.ENDC}" if is_selected else "  "
                l_col = Colors.REVERSE if is_selected else ""
                
                # --- RECHTER BLOCK (Meta-Daten) ---
                origin_str = ""
                origin_len = 0
                if self.virtual_all_mode:
                    origin = task.get('_origin', '?')
                    o_col = color_map.get(origin, Colors.GREY)
                    origin_str = f" {o_col}[{origin}]{Colors.ENDC}"
                    origin_len = len(origin) + 3 # Leerzeichen + []

                rec = task.get('recurrence')
                rec_str = ""
                rec_len = 0
                if rec:
                    rec_str = f"{Colors.BLUE} ⟳ {rec}{Colors.ENDC}"
                    rec_len = len(rec) + 3 # Leerzeichen + Symbol + Text

                due_colored = format_due_date(task.get('due'))
                due_clean = strip_ansi(due_colored)
                
                # Fixe Breite fürs Datum
                TARGET_DUE_WIDTH = 14
                padding_needed = max(0, TARGET_DUE_WIDTH - len(due_clean))
                due_final_str = (" " * padding_needed) + due_colored
                
                # Länge des rechten Blocks (ohne ANSI)
                right_block_visual_len = TARGET_DUE_WIDTH + rec_len + origin_len
                
                # --- LAYOUT BERECHNUNG ---
                left_block_len = 11 
                
                # Verfügbarer Platz für Titel & Füller
                # -4 ist ein Sicherheitsabstand, damit kein Umbruch passiert
                avail_space = render_width - left_block_len - right_block_visual_len - 4
                if avail_space < 10: avail_space = 10 

                title = task['title']
                wrapped_lines = textwrap.wrap(title, width=avail_space)
                if not wrapped_lines: wrapped_lines = [""]

                # Erste Zeile
                line1_txt = wrapped_lines[0]
                
                # Füller mit Punkten (Leader dots)
                gap_len = avail_space - len(line1_txt)
                if gap_len < 0: gap_len = 0
                
                filler = f"{Colors.GREY}{'.' * (gap_len + 1)}{Colors.ENDC}"

                print(f"{prefix} {checkbox} {l_col} {p_str} {line1_txt} {filler} {due_final_str}{rec_str}{origin_str} {Colors.ENDC}")
                
                # Weitere Zeilen (werden eingerückt)
                if len(wrapped_lines) > 1:
                    for extra_line in wrapped_lines[1:]:
                        pad_left = "           " # 11 Spaces
                        if is_selected:
                             # Füllen, damit der Balken gut aussieht, aber nur bis render_width
                             remaining = render_width - len(pad_left) - len(extra_line) - 2
                             filler_right = " " * max(0, remaining)
                             print(f"{pad_left} {l_col} {extra_line}{filler_right}{Colors.ENDC}")
                        else:
                             print(f"{pad_left} {extra_line}")

        # Linie unten auch an render_width anpassen
        print("\n" + "-" * (render_width-2))
        print(f"{Colors.BOLD}Steuerung:{Colors.ENDC} [↑/↓] Nav | [Space] Check | [V]iew (Details)")
        print(f"           [A]dd | [E]dit | [D]el | [L]isten | [F]ocus")

    def action_view_details(self):
        if not self.tasks: return
        task = self.tasks[self.selected_idx]
        
        self.clear_screen()
        term_width = shutil.get_terminal_size((80, 24)).columns
        # Auch hier begrenzen
        render_width = min(term_width, 90)
        
        print("\n" * 2)
        print(f"{Colors.BLUE}╔{'═'*(render_width-2)}╗{Colors.ENDC}")
        title_lines = textwrap.wrap(task['title'], width=render_width-6)
        
        for line in title_lines:
            print(f"{Colors.BLUE}║  {Colors.BOLD}{line.center(render_width-6)}{Colors.ENDC}  {Colors.BLUE}║{Colors.ENDC}")
        
        print(f"{Colors.BLUE}╠{'═'*(render_width-2)}╣{Colors.ENDC}")
        
        status = "ERLEDIGT" if task['done'] else "OFFEN"
        s_col = Colors.GREEN if task['done'] else Colors.FAIL
        
        origin = task.get('_origin', self.current_list_name)
        color_map = self.get_current_color_map()
        o_col = color_map.get(origin, Colors.GREY)
        
        prio_map = {1: "Normal", 2: "Hoch", 3: "Kritisch"}
        prio = prio_map.get(task.get('priority', 1), "Normal")
        
        details = [
            f"Status:      {s_col}{status}{Colors.ENDC}",
            f"Fällig am:   {task.get('due', 'Kein Datum')}",
            f"Liste:       {o_col}{origin}{Colors.ENDC}",
            f"Priorität:   {prio}",
            f"Wiederholung: {task.get('recurrence', '-')}"
        ]
        
        for det in details:
            print(f"{Colors.BLUE}║  {det:<{render_width+13}}  {Colors.BLUE}║{Colors.ENDC}") 
            
        print(f"{Colors.BLUE}╚{'═'*(render_width-2)}╝{Colors.ENDC}")
        
        print(f"\n{Colors.GREY}Drücke eine beliebige Taste zum Zurückkehren...{Colors.ENDC}")
        self.input.get_key()

    def run_focus_mode(self):
        if not self.tasks: return
        task = self.tasks[self.selected_idx]
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
                print('\a')
                self.input.get_key() 
                break

    def _prompt(self, text):
        sys.stdout.write(Colors.SHOW_CURSOR)
        print(f"\n{Colors.BLUE}{text}{Colors.ENDC}", end=" ")
        val = input().strip()
        sys.stdout.write(Colors.HIDE_CURSOR)
        return val

    def action_add(self):
        title = self._prompt("Titel:")
        if title:
            due = parse_german_date(self._prompt("Fällig (DD.MM oder DD.MM.YYYY) [Enter=Nie]:"))
            rec_in = self._prompt("Wiederholung (z.B. '1d', '3d', '1w') [Enter=Nein]:").lower()
            recurrence = None
            if rec_in and (rec_in.endswith('d') or rec_in.endswith('w')):
                recurrence = rec_in
            elif rec_in == 't': recurrence = '1d'
            
            new_task = {
                "title": title, 
                "done": False, 
                "priority": 1, 
                "due": due,
                "recurrence": recurrence
            }
            if self.virtual_all_mode:
                new_task['_origin'] = "tasks"
            
            self.tasks.append(new_task)
            self.sort_tasks()
            self.save_tasks()

    def action_edit(self):
        if not self.tasks: return
        new_t = self._prompt(f"Neuer Titel ({self.tasks[self.selected_idx]['title']}):")
        if new_t:
            self.tasks[self.selected_idx]['title'] = new_t
            self.save_tasks()

    def action_delete(self):
        if not self.tasks: return
        task = self.tasks[self.selected_idx]
        self.last_deleted = task
        self.log_done_task(task)
        self.tasks.pop(self.selected_idx)
        self.save_tasks()
        self.selected_idx = max(0, min(self.selected_idx, len(self.tasks)-1))

    def action_undo(self):
        if self.last_deleted:
            self.tasks.append(self.last_deleted)
            self.last_deleted = None
            self.sort_tasks()
            self.save_tasks()
            sys.stdout.write(Colors.SHOW_CURSOR)
            print(f"\n  {Colors.GREEN}Wiederhergestellt!{Colors.ENDC}")
            time.sleep(0.8)
            sys.stdout.write(Colors.HIDE_CURSOR)

    def run_list_selection(self):
        while True:
            real_lists = self.get_all_lists()
            menu_items = ["ALLE"] + real_lists
            try:
                if self.current_list_name == "ALLE": sel_idx = 0
                else: sel_idx = menu_items.index(self.current_list_name)
            except ValueError: sel_idx = 1

            while True:
                self.clear_screen()
                print(f"{Colors.BLUE}╔{'═'*48}╗{Colors.ENDC}")
                print(f"{Colors.BLUE}║{Colors.BOLD}            LISTEN AUSWAHL                      {Colors.ENDC}{Colors.BLUE}║{Colors.ENDC}")
                print(f"{Colors.BLUE}╚{'═'*48}╝{Colors.ENDC}")
                print("\n")
                
                for i, item in enumerate(menu_items):
                    display_name = f"[ {item} ]" if item == "ALLE" else item
                    prefix = f"{Colors.BLUE}>>{Colors.ENDC}" if i == sel_idx else "  "
                    style = Colors.REVERSE if i == sel_idx else ""
                    mark = "*" if (item == self.current_list_name) else " "
                    print(f" {prefix} {style} {mark} {display_name:<35} {Colors.ENDC}")

                print("\n" + "-" * 50)
                print(f" [Enter] Öffnen | [N]eu | [Esc] Abbrechen")
                
                key = self.input.get_key()
                if key in ('up', 'k'): sel_idx = max(0, sel_idx - 1)
                elif key in ('down', 'j'): sel_idx = min(len(menu_items) - 1, sel_idx + 1)
                elif key == 'n':
                    new_name = self._prompt("Name der neuen Liste:")
                    if new_name:
                        safe = "".join([c for c in new_name if c.isalnum()]).strip()
                        if safe:
                            self.current_list_name = safe
                            self.tasks = []
                            self.save_tasks()
                            return
                    break
                elif key in ('\r', '\n', ' '):
                    self.current_list_name = menu_items[sel_idx]
                    self.tasks = self.load_current_context()
                    self.selected_idx = 0
                    self.sort_tasks()
                    return
                elif key in ('esc', 'q', 'l'): return

    def run_tui(self):
        with AppWindow():
            while True:
                if self.tasks: self.selected_idx = max(0, min(self.selected_idx, len(self.tasks)-1))
                self.draw_ui()
                key = self.input.get_key()

                if key in ('up', 'k') and self.selected_idx > 0: self.selected_idx -= 1
                elif key in ('down', 'j') and self.selected_idx < len(self.tasks)-1: self.selected_idx += 1
                elif key in (' ', 't') and self.tasks:
                    task = self.tasks[self.selected_idx]
                    task['done'] = not task['done']
                    if task['done'] and task.get('recurrence') and task.get('due'):
                        new_due = calculate_next_date(task['due'], task['recurrence'])
                        if new_due:
                            new_task = task.copy()
                            new_task['done'] = False
                            new_task['due'] = new_due
                            if '_origin' in task: new_task['_origin'] = task['_origin']
                            self.tasks.append(new_task)
                            sys.stdout.write(Colors.SHOW_CURSOR)
                            print(f"\n  {Colors.GREEN}Neue Aufgabe für {new_due} erstellt!{Colors.ENDC}")
                            time.sleep(0.8)
                            sys.stdout.write(Colors.HIDE_CURSOR)
                    self.sort_tasks(); self.save_tasks()
                elif key in ('1', '2', '3') and self.tasks:
                    self.tasks[self.selected_idx]['priority'] = int(key)
                    self.sort_tasks(); self.save_tasks()
                elif key == 'f': self.run_focus_mode()
                elif key == 'a': self.action_add()
                elif key == 'e': self.action_edit()
                elif key == 'd': self.action_delete()
                elif key == 'x': self.export_ical()
                elif key == 'u': self.action_undo()
                elif key == 'l': self.run_list_selection()
                elif key == 'v': self.action_view_details()
                elif key in ('q', '\x1b'): break

    def run_cli_add(self, title, prio, due_input):
        enable_windows_ansi_support()
        due = parse_german_date(due_input)
        fpath = self.get_list_file_path("tasks")
        tasks = self._read_file(fpath)
        tasks.append({"title": title, "done": False, "priority": prio, "due": due, "recurrence": None})
        self._write_file(fpath, tasks)
        print(f"{Colors.GREEN}Task '{title}' in 'tasks' gespeichert.{Colors.ENDC}")
    
    def run_list_short(self):
        enable_windows_ansi_support()
        self.tasks = self.load_current_context()
        open_tasks = [t for t in self.tasks if not t['done']]
        print(f"Liste: {Colors.HEADER}{self.current_list_name}{Colors.ENDC}")
        if not open_tasks: print(f"{Colors.GREEN}Alles erledigt!{Colors.ENDC}"); return
        for i, t in enumerate(open_tasks):
            p = "!!!" if t.get('priority')==3 else (" !!" if t.get('priority')==2 else "  ")
            due = format_due_date(t.get('due'))
            rec = f" ⟳ {t.get('recurrence')}" if t.get('recurrence') else ""
            origin = f" [{t.get('_origin')}]" if self.virtual_all_mode else ""
            print(f" {i+1}. {p} {t['title']} {due}{rec}{origin}")

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