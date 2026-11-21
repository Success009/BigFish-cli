# session.py - URL, History, CWD & Profile Session Manager (Immutable Edition)
import os
import json
import time
import ui
import adapters

SESSION_DIR = os.path.expanduser("~/gemini_local_agent/sessions")
os.makedirs(SESSION_DIR, exist_ok=True)
CLI_CONFIG_FILE = os.path.expanduser("~/gemini_local_agent/.cli_config.json")

class SessionTuple(tuple):
    """
    Subclasses 7-tuple (url, filename, preview, history, cwd, email, profile_dir)
    to maintain 100% backward compatibility for unpacks while exposing .provider attribute.
    """
    def __new__(cls, values, provider="gemini"):
        obj = super().__new__(cls, values)
        obj.provider = provider
        return obj

def load_cli_config():
    if os.path.exists(CLI_CONFIG_FILE):
        try:
            with open(CLI_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cli_config(updates):
    try:
        cfg = load_cli_config()
        cfg.update(updates)
        os.makedirs(os.path.dirname(CLI_CONFIG_FILE), exist_ok=True)
        with open(CLI_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

def set_active_session_file(filename):
    """Immediately marks a session file as the active session, updating its mtime and persisting to config."""
    if not filename:
        return
    filepath = os.path.join(SESSION_DIR, filename)
    if os.path.exists(filepath):
        try:
            os.utime(filepath, None)
        except Exception:
            pass
    save_cli_config({"last_active_session": filename})

def compact_history(chat_history):
    """
    Trims giant outputs (like reading 5MB files or verbose build logs) in the session history.
    This prevents file bloat and memory lag while keeping the top and bottom metadata.
    """
    compacted = []
    for item in chat_history:
        role = item.get("role")
        content = item.get("content", "")
        
        # Max character threshold before we truncate for session storage
        MAX_CONTENT_LEN = 3000
        
        if len(content) > MAX_CONTENT_LEN:
            truncated_content = (
                content[:1200] + 
                f"\n\n[... Truncated {len(content) - 2400} characters for terminal display and session storage performance ...]\n\n" + 
                content[-1200:]
            )
            compacted.append({"role": role, "content": truncated_content})
        else:
            compacted.append({"role": role, "content": content})
    return compacted

def get_latest_session(provider=None):
    """Returns the filename of the active session JSON file, falling back to most recently modified.
    If provider is specified, filters only for sessions matching that provider."""
    try:
        if not os.path.exists(SESSION_DIR):
            return None
        
        # Check explicitly configured last_active_session first
        cfg = load_cli_config()
        last_active = cfg.get("last_active_session")
        if last_active and os.path.exists(os.path.join(SESSION_DIR, last_active)):
            if not provider:
                return last_active
            try:
                with open(os.path.join(SESSION_DIR, last_active), 'r') as f:
                    sdata = json.load(f)
                sprov = adapters.infer_provider(sdata)
                if sprov == provider:
                    return last_active
            except Exception:
                pass

        sessions = [f for f in os.listdir(SESSION_DIR) if f.endswith('.json')]
        if not sessions:
            return None
        
        sessions_with_time = list()
        for s in sessions:
            path = os.path.join(SESSION_DIR, s)
            if provider:
                try:
                    with open(path, 'r') as f:
                        sdata = json.load(f)
                    sprov = adapters.infer_provider(sdata)
                    if sprov != provider:
                        continue
                except Exception:
                    continue
            sessions_with_time.append((s, os.path.getmtime(path)))
            
        if not sessions_with_time:
            return None

        # Sort by modification time descending
        sessions_with_time.sort(key=lambda x: x[1], reverse=True)
        return sessions_with_time[0][0]
    except Exception:
        return None

def load_session_by_filename(filename):
    """Loads and parses a session JSON file directly by filename."""
    session_dir = SESSION_DIR
    filepath = os.path.join(session_dir, filename)
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
        url = data.get("url")
        preview = data.get("preview", "")
        history = data.get("history", list())
        cwd = data.get("cwd", os.getcwd())
        email = data.get("email", "Unknown Profile")
        profile_dir = data.get("profile_dir", "Default")
        if email in ("DeepSeek User", "Unknown Profile", "", None):
            try:
                import tokens
                res_email = tokens.get_profile_email_from_local_state(profile_dir)
                if res_email:
                    email = res_email
            except Exception:
                pass
        provider = data.get("provider")
        if not provider:
            provider = adapters.infer_provider({"provider": None, "url": url})
        if url and "/404" in url:
            url = None
        return SessionTuple((url, filename, preview, history, cwd, email, profile_dir), provider=provider)
    except Exception:
        return SessionTuple((None, None, None, list(), os.getcwd(), "Unknown Profile", "Default"), provider="gemini")

def is_real_chat_url(url):
    if not url or not isinstance(url, str):
        return False
    low = url.strip().lower()
    if not low.startswith('http'):
        return False
    if 'new_chat' in low or '/404' in low:
        return False
    base = low.split('?')[0].split('#')[0].rstrip('/')
    generic = {'https://chat.deepseek.com', 'http://chat.deepseek.com', 'https://chat.deepseek.com/new'}
    try:
        for _an in adapters.list_available_adapters():
            _du = adapters.get_adapter(_an).default_url
            if _du:
                generic.add(_du.split('?')[0].rstrip('/').lower())
    except Exception:
        pass
    return not (base in generic or base.endswith('/new'))


def save_current_session(first_prompt, current_url, chat_history, current_cwd, profile_email, profile_dir, filename=None, force_update=False, provider=None):
    """Saves the session. Locks the URL, Email, and Profile ID unless force_update is explicitly requested."""
    # Auto-derive first prompt from history if not explicitly provided
    if not first_prompt and chat_history:
        for msg in chat_history:
            if msg.get("role") in ("User", "user") and msg.get("content"):
                first_prompt = msg["content"].strip()
                break

    if not force_update and (not current_url or "new_chat" in current_url or "/404" in current_url or not first_prompt): 
        return filename
        
    existing_url = None
    existing_email = None
    existing_profile_dir = None
    existing_timestamp = None
    existing_provider = None
    
    # Immutability Check: Read the existing file to preserve the original URL, Accounts, and Creation Timestamp.
    if filename:
        filepath = os.path.join(SESSION_DIR, filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    existing_url = data.get("url")
                    existing_email = data.get("email")
                    existing_profile_dir = data.get("profile_dir")
                    existing_timestamp = data.get("timestamp")
                    existing_provider = data.get("provider")
            except: pass
            
    # Resolve provider
    if not provider:
        if force_update:
            provider = load_cli_config().get("last_active_provider", "gemini")
        elif existing_provider:
            provider = existing_provider
        else:
            provider = adapters.infer_provider(
                {"provider": None, "url": current_url},
                default=load_cli_config().get("last_active_provider", "gemini"),
            )

    # Allow overriding locked keys specifically on context transfers to inherit session profiles in-place.
    # An existing URL is considered a generic landing page (not a specific persistent chat) if:
    # 1. It is DeepSeek root (e.g. https://chat.deepseek.com or https://chat.deepseek.com/)
    # 2. It contains "new_chat" (Gemini)
    _generic_landing_urls = set()
    try:
        for _an in adapters.list_available_adapters():
            _du = adapters.get_adapter(_an).default_url
            if _du:
                _generic_landing_urls.add(_du.rstrip("/").lower())
    except Exception:
        pass
    # DeepSeek roots and any adapter's default landing page count as generic
    _generic_landing_urls.update({
        "https://chat.deepseek.com", "http://chat.deepseek.com",
        "https://chat.deepseek.com/new", "https://chat.deepseek.com/",
    })
    is_generic_landing = bool(existing_url and (
        existing_url.rstrip("/").lower() in _generic_landing_urls
        or "new_chat" in existing_url.lower()
    ))
    has_specific_session_url = bool(current_url and (
        "/a/chat/s/" in current_url.lower()
        or (not current_url.rstrip("/").lower().endswith("deepseek.com") and "new_chat" not in current_url.lower())
    ))

    url_needs_update = force_update or not existing_url or (is_generic_landing and has_specific_session_url)
    final_url = current_url if url_needs_update else existing_url
    final_profile_dir = profile_dir if (force_update or not existing_profile_dir) else existing_profile_dir

    # Resolve email: replace placeholder "DeepSeek User" or "Unknown Profile" with real profile email
    raw_email = profile_email
    if raw_email in ("DeepSeek User", "Unknown Profile", "", None):
        try:
            import tokens
            res_email = tokens.get_profile_email_from_local_state(final_profile_dir)
            if res_email:
                raw_email = res_email
        except Exception:
            pass

    email_needs_update = (
        force_update 
        or not existing_email 
        or existing_email in ("Unknown Profile", "DeepSeek User", "")
    )
    final_email = raw_email if email_needs_update else existing_email
    if final_email in ("DeepSeek User", "Unknown Profile", "", None):
        try:
            import tokens
            res_email = tokens.get_profile_email_from_local_state(final_profile_dir)
            if res_email:
                final_email = res_email
        except Exception:
            pass

    final_timestamp = existing_timestamp if existing_timestamp else time.time()
    final_provider = provider if (force_update or not existing_provider) else existing_provider
    
    if not filename:
        timestamp_str = time.strftime("%Y-%m-%d_%H-%M-%S")
        candidate = f"session_{timestamp_str}.json"
        counter = 1
        while os.path.exists(os.path.join(SESSION_DIR, candidate)):
            candidate = f"session_{timestamp_str}_{counter}.json"
            counter += 1
        filename = candidate
        
    filepath = os.path.join(SESSION_DIR, filename)
    
    # Compress the active chat history to maintain compact session sizes on disk
    final_history = compact_history(chat_history)
    
    session_data = {
        "url": final_url,
        "preview": (first_prompt[:60] + '...') if len(first_prompt) > 60 else first_prompt,
        "history": final_history,
        "cwd": current_cwd,
        "email": final_email if final_email else "Unknown Profile",
        "profile_dir": final_profile_dir if final_profile_dir else "Default",
        "timestamp": final_timestamp,
        "provider": final_provider
    }
    
    try:
        with open(filepath, 'w') as f: 
            # Using indent=2 makes the file far more space-efficient than indent=4
            json.dump(session_data, f, indent=2)
        set_active_session_file(filename)
        return filename
    except Exception: 
        return filename

def resume_session():
    """Restores the environment using a premium Vim-style interactive selection menu with spacebar preview, panning, and search."""
    import sys
    import shutil
    from rich.panel import Panel
    from rich.table import Table
    from rich.markup import escape
    
    trash = []
    selected_idx = 0
    scroll_offset = 0
    search_query = ""
    provider_filter = None  # None (all) or a provider name from the adapter registry
    status_msg = ""
    status_color = "green"
    
    def get_key():
        """Reads a single keypress from standard input (cross-platform)."""
        if os.name == 'nt':
            import msvcrt
            ch = msvcrt.getch()
            if ch == b'\x03':  # Ctrl+C
                raise KeyboardInterrupt()
            if ch in (b'\r', b'\n'):
                return 'enter'
            elif ch == b' ':
                return 'space'
            elif ch == b'\xe0':
                ch2 = msvcrt.getch()
                if ch2 == b'H': return 'up'
                elif ch2 == b'P': return 'down'
                elif ch2 == b'S': return 'delete'
                elif ch2 == b'I': return 'pageup'
                elif ch2 == b'Q': return 'pagedown'
                elif ch2 == b'7': return 'home'
                elif ch2 == b'O': return 'end'
                return None
            raw = ch.decode('utf-8', errors='ignore')
            return raw
        else:
            import tty
            import termios
            import select
            
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1)
                if ch == '\x03':  # Ctrl+C
                    raise KeyboardInterrupt()
                if ch == '\x04': return 'ctrl_d'
                if ch == '\x15': return 'ctrl_u'
                if ch == ' ':
                    return 'space'
                if ch == '\x1b':
                    r, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if r:
                        ch2 = sys.stdin.read(1)
                        if ch2 == '[':
                            r, _, _ = select.select([sys.stdin], [], [], 0.05)
                            if r:
                                ch3 = sys.stdin.read(1)
                                if ch3 == 'A': return 'up'
                                elif ch3 == 'B': return 'down'
                                elif ch3 == '5':
                                    select.select([sys.stdin], [], [], 0.05); sys.stdin.read(1)
                                    return 'pageup'
                                elif ch3 == '6':
                                    select.select([sys.stdin], [], [], 0.05); sys.stdin.read(1)
                                    return 'pagedown'
                                elif ch3 in ('H', '1'):
                                    if ch3 == '1': select.select([sys.stdin], [], [], 0.05); sys.stdin.read(1)
                                    return 'home'
                                elif ch3 in ('F', '4'):
                                    if ch3 == '4': select.select([sys.stdin], [], [], 0.05); sys.stdin.read(1)
                                    return 'end'
                                elif ch3 == '3':
                                    select.select([sys.stdin], [], [], 0.05); sys.stdin.read(1)
                                    return 'delete'
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                
            if ch in ('\r', '\n'):
                return 'enter'
            return ch

    def render_chat_preview_hold(filename, data):
        """Displays the exact loaded chat view while Space key is held down, returning upon release."""
        history = data.get("history", []) if data else []
        ui.reprint_history(history)
        
        # Poll stdin to detect key release (holding Space auto-repeats ' ' in raw mode)
        if os.name != 'nt':
            import tty
            import termios
            import select
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while True:
                    r, _, _ = select.select([sys.stdin], [], [], 0.18)
                    if not r:
                        break
                    ch = sys.stdin.read(1)
                    if ch != ' ':
                        break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        else:
            get_key()

    while True:
        all_meta = []
        all_files = [f for f in os.listdir(SESSION_DIR) if f.endswith('.json')]
        for filename in all_files:
            filepath = os.path.join(SESSION_DIR, filename)
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                ts = data.get("timestamp")
                if not ts:
                    ts = os.path.getmtime(filepath)
                all_meta.append((filename, ts, data))
            except:
                try: ts = os.path.getmtime(filepath)
                except: ts = 0
                all_meta.append((filename, ts, None))
        
        # Sort by creation timestamp ascending (exact original order)
        all_meta.sort(key=lambda x: x[1])
        
        # Apply search filter if active
        if search_query:
            q = search_query.lower()
            sessions_meta = [
                s for s in all_meta
                if q in s[0].lower() or (s[2] and (q in s[2].get("preview", "").lower() or q in s[2].get("email", "").lower()))
            ]
        else:
            sessions_meta = all_meta

        # Apply provider filter if active
        if provider_filter:
            sessions_meta = [
                s for s in sessions_meta
                if s[2] and (
                    adapters.infer_provider(s[2]).lower() == provider_filter.lower()
                )
            ]
        
        if not all_meta and not trash:
            ui.show_status("No past sessions found.")
            return None, None, None, [], os.getcwd(), "Unknown Profile", "Default"
            
        total_sessions = len(sessions_meta)
        if total_sessions > 0:
            selected_idx = max(0, min(selected_idx, total_sessions - 1))
        else:
            selected_idx = 0

        # Dynamic pagination math strictly capped to fit inside terminal height without scrolling
        term_height = shutil.get_terminal_size().lines
        max_visible_rows = max(3, term_height - 11)

        if selected_idx < scroll_offset:
            scroll_offset = selected_idx
        elif selected_idx >= scroll_offset + max_visible_rows:
            scroll_offset = selected_idx - max_visible_rows + 1

        # Fast screen refresh without subprocess fork flicker
        sys.stdout.write("\033[H\033[J")
        sys.stdout.flush()
        
        instructions = (
            "[bold cyan]j[/bold cyan]/[bold cyan]k[/bold cyan] or [bold cyan]↑[/bold cyan]/[bold cyan]↓[/bold cyan] : Nav  |  "
            "[bold cyan]p[/bold cyan] : Agent Filter  |  "
            "[bold cyan]/[/bold cyan] : Search  |  "
            "[bold yellow]Space[/bold yellow] : Preview  |  "
            "[bold green]Enter[/bold green] : Load  |  "
            "[bold red]d[/bold red] : Del  |  "
            "[bold magenta]r[/bold magenta] : Rename"
        )
        ui.console.print(Panel(instructions, title="[bold cyan]⚡ Big Fish Session Manager[/bold cyan]", border_style="cyan"))
        
        if search_query:
            ui.console.print(f"[bold yellow]🔍 Filter: '{search_query}' ({total_sessions} match(es)) | Press 'c' to clear filter[/bold yellow]")

        if provider_filter:
            _opts = " / ".join(["ALL"] + [p.upper() for p in adapters.list_available_adapters()])
            ui.console.print(f"[bold magenta]🏷 Agent Filter: '{provider_filter.upper()}' ({total_sessions} match(es)) | Press 'p' to cycle ({_opts})[/bold magenta]")

        if sessions_meta:
            table = Table(expand=True, border_style="cyan")
            table.add_column("", width=3, justify="center", no_wrap=True)
            table.add_column("#", width=4, justify="right", no_wrap=True)
            table.add_column("Session File", style="bold green", width=25, no_wrap=True)
            table.add_column("Agent", style="bold magenta", width=10, no_wrap=True)
            table.add_column("Account (Email)", style="yellow", width=24, no_wrap=True)
            table.add_column("Turns", width=6, justify="center", style="dim cyan", no_wrap=True)
            table.add_column("Last Preview", style="dim white", no_wrap=True)
            
            visible_slice = sessions_meta[scroll_offset : scroll_offset + max_visible_rows]
            for idx_offset, (filename, ts, data) in enumerate(visible_slice):
                actual_idx = scroll_offset + idx_offset
                is_selected = (actual_idx == selected_idx)
                indicator = " >" if is_selected else ""
                row_style = "bold cyan" if is_selected else ""
                
                preview = "Unknown Session"
                email = "Unknown Profile"
                turn_count = "0"
                provider_tag = "Gemini"
                if data:
                    preview = data.get("preview", "")
                    email = data.get("email", "")
                    pdir = data.get("profile_dir", "Default")
                    if email in ("DeepSeek User", "Unknown Profile", "", None):
                        try:
                            import tokens
                            res_email = tokens.get_profile_email_from_local_state(pdir)
                            if res_email:
                                email = res_email
                        except Exception:
                            pass
                    turn_count = str(len(data.get("history", [])))
                    raw_p = adapters.infer_provider(data)
                    try:
                        provider_tag = adapters.get_adapter(raw_p).display_name
                    except Exception:
                        provider_tag = raw_p.capitalize()
                
                preview_clean = preview.replace('\n', ' ').strip()
                if len(preview_clean) > 40:
                    preview_clean = preview_clean[:40] + "..."

                table.add_row(
                    indicator,
                    str(actual_idx + 1),
                    filename,
                    provider_tag,
                    email,
                    turn_count,
                    preview_clean,
                    style=row_style
                )
            ui.console.print(table)
            
            if total_sessions > max_visible_rows:
                ui.console.print(f"[dim text]Showing {scroll_offset + 1}-{min(scroll_offset + max_visible_rows, total_sessions)} of {total_sessions} sessions (Use J/K or PageUp/PageDown to pan pages)[/dim text]")
        else:
            ui.console.print("[dim]No sessions found matching your search.[/dim]")
            
        if trash:
            ui.console.print(f"\n[dim yellow]({len(trash)} backup session(s) in trash. Press 'u' to undo deletion)[/dim yellow]")
            
        if status_msg:
            ui.console.print(f"\n[{status_color}]{status_msg}[/{status_color}]")
            status_msg = ""
            
        key = get_key()
        
        if key == 'q':
            return SessionTuple((None, None, None, [], os.getcwd(), "Unknown Profile", "Default"), provider="gemini")
            
        elif key == 'space':
            if sessions_meta:
                filename, _, data = sessions_meta[selected_idx]
                render_chat_preview_hold(filename, data)
                
        elif key in ('j', 'down'):
            if sessions_meta:
                selected_idx = (selected_idx + 1) % total_sessions
                
        elif key in ('k', 'up'):
            if sessions_meta:
                selected_idx = (selected_idx - 1) % total_sessions

        elif key in ('J', 'ctrl_d', 'pagedown'):
            if sessions_meta:
                selected_idx = min(total_sessions - 1, selected_idx + max_visible_rows)

        elif key in ('K', 'ctrl_u', 'pageup'):
            if sessions_meta:
                selected_idx = max(0, selected_idx - max_visible_rows)

        elif key in ('g', 'home'):
            selected_idx = 0

        elif key in ('G', 'end'):
            if sessions_meta:
                selected_idx = total_sessions - 1

        elif key == '/':
            ui.console.print("\n[bold yellow]🔍 Enter search filter: [/bold yellow]", end="")
            sys.stdout.flush()
            try:
                search_query = input().strip()
                selected_idx = 0
                scroll_offset = 0
            except KeyboardInterrupt:
                search_query = ""

        elif key == 'c':
            search_query = ""
            selected_idx = 0
            scroll_offset = 0
            status_msg = "Filter cleared."
            status_color = "bold yellow"

        elif key == 'p':
            # Cycle: ALL -> each registered provider -> ALL. Driven by the
            # adapter registry so new providers (e.g. claude) appear automatically.
            _cycle = [None] + adapters.list_available_adapters()
            try:
                _i = _cycle.index(provider_filter)
            except ValueError:
                _i = 0
            provider_filter = _cycle[(_i + 1) % len(_cycle)]
            selected_idx = 0
            scroll_offset = 0
            filter_label = provider_filter.upper() if provider_filter else "ALL"
            status_msg = f"Agent filter: {filter_label}"
            status_color = "bold magenta"
                
        elif key in ('\r', '\n', 'enter'):
            if sessions_meta:
                filename, _, data = sessions_meta[selected_idx]
                if data:
                    url = data.get("url")
                    preview = data.get("preview", "")
                    history = data.get("history", [])
                    cwd = data.get("cwd", os.getcwd())
                    email = data.get("email", "Unknown Profile")
                    profile_dir = data.get("profile_dir", "Default")
                    
                    provider = data.get("provider")
                    if not provider:
                        provider = adapters.infer_provider({"provider": None, "url": url})
                    if not url:
                        url = adapters.get_adapter(provider).default_url

                    set_active_session_file(filename)
                    save_cli_config({"last_active_provider": provider})
                    return SessionTuple((url, filename, preview, history, cwd, email, profile_dir), provider=provider)
                    
        elif key in ('d', 'delete'):
            if sessions_meta:
                filename, _, _ = sessions_meta[selected_idx]
                ui.console.print(f"\n[bold red]Are you sure you want to delete '{filename}'? (y/N): [/bold red]", end="")
                sys.stdout.flush()
                
                conf_key = get_key()
                if conf_key and conf_key.lower() == 'y':
                    orig_path = os.path.join(SESSION_DIR, filename)
                    trash_path = orig_path + ".bak"
                    try:
                        if os.path.exists(trash_path):
                            os.remove(trash_path)
                        os.rename(orig_path, trash_path)
                        trash.append({"original_path": orig_path, "trash_path": trash_path})
                        cfg = load_cli_config()
                        if cfg.get("last_active_session") == filename:
                            save_cli_config({"last_active_session": None})
                        status_msg = f"✔ Moved '{filename}' to trash."
                        status_color = "bold green"
                    except Exception as e:
                        status_msg = f"ERROR: Delete failed. {e}"
                        status_color = "bold red"
                else:
                    status_msg = "Deletion cancelled."
                    status_color = "bold yellow"
                    
        elif key == 'u':
            if trash:
                item = trash.pop()
                try:
                    os.rename(item['trash_path'], item['original_path'])
                    status_msg = f"✔ Restored session: {os.path.basename(item['original_path'])}"
                    status_color = "bold green"
                except Exception as e:
                    status_msg = f"ERROR: Restore failed. {e}"
                    status_color = "bold red"
            else:
                status_msg = "Trash is empty."
                status_color = "bold yellow"
                
        elif key == 'r':
            if sessions_meta:
                filename, _, _ = sessions_meta[selected_idx]
                ui.console.print(f"\n[bold yellow]Renaming session '{filename}'[/bold yellow]")
                ui.console.print("Enter new name (alphanumeric, dashes, underscores only): ", end="")
                sys.stdout.flush()
                try:
                    new_name = input().strip()
                except KeyboardInterrupt:
                    status_msg = "Rename cancelled."
                    status_color = "bold yellow"
                    continue
                    
                clean_name = "".join(c for c in new_name if c.isalnum() or c in ('_', '-')).strip()
                if clean_name:
                    new_filename = f"{clean_name}.json"
                    old_path = os.path.join(SESSION_DIR, filename)
                    new_path = os.path.join(SESSION_DIR, new_filename)
                    
                    if os.path.exists(new_path):
                        status_msg = "ERROR: A session with that name already exists."
                        status_color = "bold red"
                    else:
                        try:
                            with open(old_path, 'r', encoding='utf-8') as f:
                                data = json.load(f)
                            
                            orig_preview = data.get("preview", "")
                            if ":" in orig_preview:
                                orig_preview = orig_preview.split(":", 1)[1].strip()
                            data["preview"] = f"{clean_name}: {orig_preview}"
                            
                            with open(old_path, 'w', encoding='utf-8') as f:
                                json.dump(data, f, indent=2)
                                
                            os.rename(old_path, new_path)
                            cfg = load_cli_config()
                            if cfg.get("last_active_session") == filename:
                                save_cli_config({"last_active_session": new_filename})
                            status_msg = f"✔ Renamed '{filename}' to '{new_filename}'"
                            status_color = "bold green"
                        except Exception as e:
                            status_msg = f"ERROR: Rename failed. {e}"
                            status_color = "bold red"
                else:
                    status_msg = "Invalid name. Rename cancelled."
                    status_color = "bold yellow"
