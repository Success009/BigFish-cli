# ui.py - The Advanced Interface Engine (Clean Layout Edition)
import os, sys, asyncio, logging, re, datetime
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.markdown import Markdown
from rich.text import Text
from rich.markup import escape 
from rich.table import Table
import audio 
import attachments

try:
    import nest_asyncio
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.application import run_in_terminal
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys
except ImportError:
    print("Installing premium UI libraries 'prompt_toolkit' and 'nest_asyncio'...")
    os.system(f"{sys.executable} -m pip install prompt_toolkit nest_asyncio")
    import nest_asyncio
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.application import run_in_terminal
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

nest_asyncio.apply()
console = Console()
HISTFILE = os.path.expanduser("~/gemini_local_agent/.cli_history")
LAST_AGENT_REPLY = ""

# Map terminal sequences for plain Enter and modified Enter keys (Kitty Keyboard Protocol + xterm)
# Plain Enter in Kitty KKP sends \x1b[13u or \x1b[13;1u -- must map to ControlM (submit)
ANSI_SEQUENCES['\x1b[13u'] = Keys.ControlM    # Plain Enter (Kitty KKP, no modifier)
ANSI_SEQUENCES['\x1b[13;1u'] = Keys.ControlM  # Plain Enter (Kitty KKP, modifier=1 = none)
ANSI_SEQUENCES['\x1b[27;1;13~'] = Keys.ControlM  # Plain Enter (xterm CSI, modifier=1)
# Shift+Enter / Ctrl+Enter → map to Escape+Enter so our newline handler fires
# (c-j now submits, so we can't reuse it for newline-insert)
ANSI_SEQUENCES['\x1b[13;2u'] = (Keys.Escape, Keys.ControlM)   # Shift+Enter (Kitty KKP) → newline via escape+enter path
ANSI_SEQUENCES['\x1b[13;5u'] = (Keys.Escape, Keys.ControlM)   # Ctrl+Enter (Kitty KKP)
ANSI_SEQUENCES['\x1b[13;6u'] = (Keys.Escape, Keys.ControlM)   # Ctrl+Shift+Enter (Kitty KKP)
ANSI_SEQUENCES['\x1b[27;2;13~'] = (Keys.Escape, Keys.ControlM)  # Shift+Enter (xterm)
ANSI_SEQUENCES['\x1b[27;5;13~'] = (Keys.Escape, Keys.ControlM)  # Ctrl+Enter (xterm)
ANSI_SEQUENCES['\x1b[27;6;13~'] = (Keys.Escape, Keys.ControlM)  # Ctrl+Shift+Enter (xterm)

class SlashCommandCompleter(Completer):
    def __init__(self):
        # Slash commands with autocomplete support
        self.commands = ['/new', '/resume', '/continue', '/next', '/transfer', '/model', '/tokens', '/speech', '/exit', '/help', '/remind', '/jobs', '/kill', '/name', '/autotransfer', '/sound', '/subs']
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith('/') and ' ' not in text:
            for cmd in self.commands:
                if cmd.startswith(text.lower()):
                    yield Completion(cmd, start_position=-len(text))
        elif not text.startswith('/') and ' ' not in text and text.strip():
            for cmd in self.commands:
                if cmd.lstrip('/').startswith(text.lower()):
                    yield Completion(cmd, start_position=-len(text))

class TabCyclingManager:
    def __init__(self, commands):
        self.commands = commands
        self.options = []
        self.index = 0

    def handle_tab(self, event):
        buff = event.current_buffer
        text = buff.document.text_before_cursor
        if ' ' in text:
            return
        stripped = text.strip()
        if not stripped:
            return

        # Consecutive tab presses cycle through options
        if self.options and stripped in self.options:
            self.index = (self.index + 1) % len(self.options)
            chosen = self.options[self.index]
            buff.delete_before_cursor(len(stripped))
            buff.insert_text(chosen)
            return

        prefix = stripped.lower()
        if not prefix.startswith('/'):
            prefix_with_slash = '/' + prefix
        else:
            prefix_with_slash = prefix

        matches = [cmd for cmd in self.commands if cmd.startswith(prefix_with_slash)]
        if not matches:
            return

        if len(matches) == 1:
            chosen = matches[0]
            buff.delete_before_cursor(len(stripped))
            buff.insert_text(chosen)
            self.options = []
            return

        # Multiple matches: complete common prefix or cycle first match
        import os
        common = os.path.commonprefix(matches)
        if len(common) > len(prefix_with_slash):
            buff.delete_before_cursor(len(stripped))
            buff.insert_text(common)
            self.options = matches
            self.index = 0
        else:
            self.options = matches
            self.index = 0
            chosen = matches[0]
            buff.delete_before_cursor(len(stripped))
            buff.insert_text(chosen)

tab_manager = TabCyclingManager(SlashCommandCompleter().commands)
bindings = KeyBindings()

# Enter submits prompt — bind ALL enter-like keys so every terminal works.
# c-m = \r (classic Enter), c-j = \n (Kitty sends \n for Enter), enter = alias for c-m
def _submit(event):
    import debuglog as _dlog
    buff = event.current_buffer
    _dlog.log("KEY", f"_submit fired: text={repr(buff.text[:40])}")
    if buff.complete_state and buff.complete_state.current_completion:
        buff.apply_completion(buff.complete_state.current_completion)
    buff.validate_and_handle()
    _dlog.log("KEY", "_submit: validate_and_handle done")

bindings.add('c-m')(_submit)   # \r  — classic Enter
bindings.add('enter')(_submit) # alias for c-m in prompt_toolkit
bindings.add('c-j')(_submit)   # \n  — Kitty / some terminals send \n for Enter

# Newline insertion: Alt+Enter only (Escape then Enter)
# Kitty's Shift+Enter / Ctrl+Enter are remapped to c-j above in ANSI_SEQUENCES — 
# now we need explicit escape sequences for those to insert newlines instead.
@bindings.add('escape', 'c-m')
@bindings.add('escape', 'enter')
def _newline(event):
    """Insert a literal newline (Alt+Enter = multiline)."""
    event.current_buffer.insert_text('\n')

# Native Undo/Redo Key Bindings inside Prompt input typing buffer
@bindings.add('c-z')
def _(event):
    """Triggers prompt_toolkit native undo on current buffer (e.g. undoing accidental paste or delete)."""
    event.current_buffer.undo()

@bindings.add('c-y')
def _(event):
    """Triggers prompt_toolkit native redo on current buffer."""
    event.current_buffer.redo()

@bindings.add('home')
def _(event):
    """Standard Home key: move cursor to beginning of input."""
    event.current_buffer.cursor_position = 0

# Tab: read last reply (or stop if already speaking), else autocomplete slash commands
@bindings.add('tab')
def _(event):
    """Tab: speak last response; if already speaking, stop it. Autocomplete when typing slash commands."""
    buff = event.current_buffer
    text = buff.document.text_before_cursor.strip()
    if not text:
        if audio.is_speaking():
            audio.stop_audio()
            event.app.invalidate()
        else:
            global LAST_AGENT_REPLY
            if LAST_AGENT_REPLY:
                audio.speak(LAST_AGENT_REPLY)
            else:
                run_in_terminal(lambda: console.print("[dim]No reply to read yet.[/dim]"))
    else:
        tab_manager.handle_tab(event)

# ---- Finish-notification shortcuts (see interface/notify.py) ----
# F2 cycles finish action (off -> sound -> raise); F3 cycles the chime and previews it.
def _notify_cfg():
    from config import load_cli_config, save_cli_config
    return load_cli_config, save_cli_config

@bindings.add('f2')
def _(event):
    import notify
    load, save = _notify_cfg()
    new = notify.cycle_action(load, save)
    event.app.invalidate()

@bindings.add('f3')
def _(event):
    import notify
    load, save = _notify_cfg()
    new = notify.cycle_sound(load, save)
    notify.play(new)
    event.app.invalidate()

# Ctrl+P for voice speech cancellation and replay
@bindings.add('c-p')
def _(event):
    """Ctrl+P: Cancel active speech or replay last response."""
    if audio.is_speaking():
        audio.stop_audio()
        event.app.invalidate()
    else:
        global LAST_AGENT_REPLY
        if LAST_AGENT_REPLY:
            event.app.invalidate()
            audio.speak(LAST_AGENT_REPLY)

session = PromptSession(history=FileHistory(HISTFILE), completer=SlashCommandCompleter(), complete_while_typing=True, key_bindings=bindings, lexer=attachments.AttachmentLexer(), multiline=False, refresh_interval=0.3)
attachments.install(bindings, session)

def show_header(provider="Gemini"):
    console.print(Panel.fit("[bold cyan]Big Fish CLI v8.0[/bold cyan]\n[dim]github.com/Success009/BigFish-cli[/dim]", border_style="cyan"))

def show_status(message): console.print(f"[bold yellow]{escape(message)}[/bold yellow]")

def show_capture_notice(message): console.print(f"[red]{escape(message)}[/red]")

def get_spinner(message):
    return console.status(f"[bold yellow]{escape(message)}[/bold yellow]", spinner="dots", spinner_style="cyan")

# ---- Prompt status line -------------------------------------------------
# The status line is part of the prompt message (a callable), so F2/F3 can
# change the finish state and redraw it in place with app.invalidate().
_prompt_info = {"tokens": 0, "provider": "", "source": ""}

def _finish_label():
    """Current finish-notification state for the status line (off/sound/raise/raise+sound)."""
    try:
        import notify
        from config import load_cli_config
        action = notify.get_action(load_cli_config)
        if "sound" in action:
            return f"{action}:{notify.get_sound(load_cli_config)}"
        return action
    except Exception:
        return "?"

def _finish_symbol():
    """Compact finish-alert marker: off / ♪ (sound) / ↑ (raise) / ↑♪ (raise+sound)."""
    try:
        import notify
        from config import load_cli_config
        return {'off': 'off', 'sound': '♪', 'raise': '↑', 'raise+sound': '↑♪'}.get(notify.get_action(load_cli_config), '?')
    except Exception:
        return '?'

def _provider_shown():
    """Provider name for the status line; adds a small marker when the last reply came from page scraping."""
    prov = _prompt_info['provider']
    if prov and _prompt_info.get('source') == 'ui':
        prov = prov + ' - page'
    return prov

def _prompt_message():
    t = _prompt_info['tokens']
    tok = f"{t/1000:.1f}k" if t >= 1000 else str(t)
    parts = [f"⚡ {tok} tokens", _provider_shown(), _finish_symbol()]
    try:
        if audio.is_speaking():
            parts.append('🔊')
    except Exception:
        pass
    line = ' '.join(f'[{p}]' for p in parts if p)
    return [("", "\n"), ("ansibrightblack", line), ("", "\nBig Fish > ")]

def _safe_input():
    """True when the plain input() fallback is requested (--safe flag or BIGFISH_SAFE_INPUT=1)."""
    return "--safe" in sys.argv or os.environ.get("BIGFISH_SAFE_INPUT", "").strip() in ("1", "true", "yes")

def show_prompt(token_count=0, manual_mode=False, provider="DeepSeek", source=""):
    import debuglog as _dlog
    _prompt_info["tokens"] = token_count
    _prompt_info["provider"] = provider
    _prompt_info["source"] = source
    if _safe_input():
        # Plain input(): no PromptSession, no custom key bindings. Ctrl+D leaves the CLI.
        try:
            return input("\nBig Fish (safe) > ")
        except EOFError:
            return "/exit"
    _dlog.log("PROMPT", "show_prompt: calling session.prompt()")
    result = session.prompt(_prompt_message)
    _dlog.log("PROMPT", f"show_prompt: returned {repr(result[:40] if result else result)}")
    return result

def sanitize_formatting_for_display(text):
    """Sanitizes raw text, XML leaks, ANSI codes, and LaTeX math formatting for beautiful Rich Markdown rendering."""
    if not text:
        return ""

    # 0. Strip ANSI escape sequences (e.g. [1m[K, \x1b[2J) that bleed from DeepSeek responses
    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
    text = re.sub(r'\x1b\][^\x07]*\x07', '', text)  # OSC sequences
    text = re.sub(r'\x1b[()][AB012]', '', text)       # charset switches

    # 1. Strip raw XML leaks and termination markers if present in raw string
    # New format: tags inside say text are shown as written; nothing is stripped here.
    text = text.replace('__DONE__', '').strip()
    
    # 2. Transform LaTeX math expressions into clean Unicode Markdown
    text = re.sub(r'\\approx', '≈', text)
    text = re.sub(r'\\times', '×', text)
    text = re.sub(r'\\cdot', '·', text)
    text = re.sub(r'\\pm', '±', text)
    text = re.sub(r'\\le|\\leq', '≤', text)
    text = re.sub(r'\\ge|\\geq', '≥', text)
    text = re.sub(r'\\neq', '≠', text)
    text = re.sub(r'\\mathbf\{([^}]+)\}', r'**\1**', text)
    text = re.sub(r'\\text\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\mathrm\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\mathit\{([^}]+)\}', r'*\1*', text)
    text = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'(\1 / \2)', text)
    
    # Clean up single dollar math wrappers ($expr$) -> expr
    text = re.sub(r'\$([^$\n]+)\$', r'\1', text)
    
    return text

def show_agent_reply(content, auto_speak=True):
    global LAST_AGENT_REPLY
    clean_content = sanitize_formatting_for_display(content)
    LAST_AGENT_REPLY = clean_content 
    console.print(Panel(Markdown(clean_content), title="Big Fish AI", border_style="cyan", expand=False))
    if audio.SPEECH_ENABLED and auto_speak: 
        audio.speak(content)

def reprint_history(history):
    """Muted, greyed-out visual panels to clearly indicate restored history logs."""
    global LAST_AGENT_REPLY
    os.system('cls' if os.name == 'nt' else 'clear')
    show_header()
    for item in history[-20:]: 
        role, content = item.get("role"), item.get("content", "")
        if role == "User": 
            console.print(f"\n[bold cyan]Big Fish >[/bold cyan] {escape(content)}")
        elif role == "AI": 
            show_agent_reply(content, auto_speak=False)
        elif role == "System":
            match = re.match(r"^Ran '([^']+)'(?: on '(.*)')?\. Result:\n?(.*)$", content, re.DOTALL)
            if match:
                tool = match.group(1)
                target = match.group(2) or tool
                output = match.group(3)
                # Same rendering as live output; no dim styling, no per-tool tags.
                if tool == 'read_file':
                    show_file_read(target, output)
                elif tool == 'write_file':
                    show_file_write(target, output)
                elif tool == 'edit_file':
                    show_file_edit(target, output)
                else:
                    show_bash_output(target, output)
            else:
                console.print(f"[dim]{escape(content[:500])}[/dim]")

def show_notice(message, style="yellow"):
    """Prints a styled one-line notice (message is escaped, style is applied)."""
    console.print(f"[bold {style}]{escape(message)}[/bold {style}]")

def _short(text, n=80):
    text = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return text if len(text) <= n else text[: n - 1] + "…"

def _looks_failed(output):
    head = (output or "").lstrip()[:200]
    return head.startswith("ERROR") or "Exit Code" in head

def show_bash_output(command, output, full=False):
    title = Text(f"🔧 Ran: {command}", style="bold magenta")
    if output:
        lines = output.splitlines()
        if len(lines) > 15:
            truncated_text = "\n".join(lines[-15:])
            safe_out = escape(truncated_text) + "\n\n[dim]... (output truncated for UI, AI received full context)[/dim]"
        else:
            safe_out = escape(output)
    else:
        safe_out = "[dim]No output[/dim]"
    console.print(Panel(safe_out, title=title, border_style="magenta", expand=False))

def show_file_read(path, content):
    console.print(Panel(Syntax(content, "python", theme="monokai", line_numbers=True), title=f"📄 Read: {path}", border_style="cyan"))

def show_file_write(path, result):
    console.print(Panel(f"[green]✔[/green] {escape(result)}", title="File System", border_style="green"))

def show_file_edit(path, result):
    console.print(Panel(f"[yellow]✎[/yellow] {escape(result)}", title="File System", border_style="yellow"))

def show_error(message): console.print(f"[bold red]ERROR: {escape(message)}[/bold red]")

def show_token_ledger_table(usage_list, active_profile=None, provider=None):
    prov_title = f"{provider} " if provider else ""
    table = Table(title=f"📊 {prov_title}Token Consumption Ledger", border_style="cyan")
    table.add_column("Profile", style="bold cyan")
    table.add_column("Account", style="white")
    table.add_column("Tokens Sent", justify="right", style="green")
    table.add_column("Tokens Recv", justify="right", style="blue")
    table.add_column("Today (Daily)", justify="right", style="bold yellow")
    table.add_column("Total Tokens", justify="right", style="bold magenta")
    table.add_column("Turns", justify="right", style="dim white")
    table.add_column("Last Active", justify="center", style="dim")

    for item in usage_list:
        p_name = item.get("profile_dir", "")
        is_active = (p_name == active_profile)
        _lbl = _label(p_name, item.get("email", ""))
        p_display = f"{_lbl} *" if is_active else _lbl
        
        last_ts = item.get("last_used_timestamp", 0)
        last_str = "Never"
        if last_ts:
            dt = datetime.datetime.fromtimestamp(last_ts)
            last_str = dt.strftime("%Y-%m-%d %H:%M")

        row_style = "bold" if is_active else None
        table.add_row(
            p_display,
            item.get("email", "(No Email)") or "(No Email)",
            f"{item.get('tokens_sent', 0):,}",
            f"{item.get('tokens_received', 0):,}",
            f"{item.get('daily_tokens', 0):,}",
            f"{item.get('total_tokens', 0):,}",
            str(item.get("session_turns_count", 0)),
            last_str,
            style=row_style
        )
    console.print(table)

def show_provider_usage_report(report, active_profile=None, provider_name=None):
    """/tokens view: every profile for ONE provider, plus provider-wide totals."""
    import time as _time
    name = provider_name or report.get("provider", "").capitalize()
    table = Table(title=f"{name} Token Usage", border_style="cyan")
    table.add_column("#", justify="right", style="bold cyan")
    table.add_column("Profile", style="white")
    table.add_column("Account", style="white")
    table.add_column("Today", justify="right", style="bold yellow")
    table.add_column("Lifetime", justify="right", style="bold magenta")
    table.add_column("Turns", justify="right", style="dim white")
    table.add_column("Last Active", justify="center", style="dim")
    table.add_column("Status", justify="left")

    now = _time.time()
    for r in report.get("rows", []):
        p = r.get("profile_dir", "")
        is_active = (p == active_profile)
        num = r.get("account_number")
        last_ts = r.get("last_used_timestamp", 0)
        last_str = datetime.datetime.fromtimestamp(last_ts).strftime("%m-%d %H:%M") if last_ts else "Never"

        until = r.get("rate_limited_until", 0)
        if until and until > now:
            status = f"[bold red]Limited until {_time.strftime('%H:%M', _time.localtime(until))}[/bold red]"
        elif is_active:
            status = "[bold green]* Active[/bold green]"
        else:
            status = "[green]Ready[/green]"

        table.add_row(
            str(num) if num is not None else "-",
            escape(_label(p, r.get("email", ""))),
            escape(r.get("email", "") or "(No Email)"),
            f"{r.get('daily_tokens', 0):,}",
            f"{r.get('total_tokens', 0):,}",
            str(r.get("turns", 0)),
            last_str,
            status,
            style="bold" if is_active else None,
        )
    console.print(table)

    lines = [
        f"[bold yellow]Today (all {escape(name)} accounts):[/bold yellow]  {report.get('today_total', 0):,} tokens",
        f"[bold magenta]Lifetime (all {escape(name)} accounts):[/bold magenta]  {report.get('lifetime_total', 0):,} tokens",
        f"[dim]Total turns:[/dim]  {report.get('turns_total', 0):,}",
    ]
    un = report.get("unassigned", [])
    if un:
        un_total = sum(u.get("total_tokens", 0) for u in un)
        names = ", ".join(escape(u.get("profile_dir", "?")) for u in un)
        lines.append(f"[dim]Includes {un_total:,} lifetime tokens recorded under a legacy key, not tied to any account: {names}[/dim]")
    console.print(Panel("\n".join(lines), title=f"{escape(name)} Totals", border_style="cyan"))

def _label(profile_dir, email=""):
    """Always the email number (user13 -> Profile 13). The Chrome folder name is only a last resort."""
    try:
        import profile_ids
        return profile_ids.label(profile_dir)
    except Exception:
        return profile_dir or "?"

def show_profile_usage_analysis(usage_list, selected_profile, provider=None):
    """Transfer screen: one row per profile, sorted by account number, with a clear status."""
    import time as _time
    try:
        import tokens
    except Exception:
        tokens = None
    now = _time.time()
    rows = []
    for item in usage_list:
        p = item.get("profile_dir", "")
        email = item.get("email", "") or ""
        if tokens is not None and (not email or email == "(No Email)" or email.endswith(" User")):
            email = tokens.get_profile_email_from_local_state(p) or email
        try:
            import profile_ids
            num = profile_ids.account_number(p)
        except Exception:
            num = None
        if num is None and tokens is not None:
            num = tokens._account_number(email)
        rows.append((num if num is not None else 9999, p, email, item))
    rows.sort(key=lambda r: (r[0], r[1]))
    table = Table(title="Transfer: profile usage", border_style="cyan")
    table.add_column("Profile", style="bold cyan")
    table.add_column("Account", style="white")
    table.add_column("Today", justify="right", style="yellow")
    table.add_column("Status", justify="left")
    for num, p, email, item in rows:
        until = 0
        if tokens is not None and provider:
            try:
                until = tokens.get_rate_limited_until(p, provider)
            except Exception:
                until = 0
        if p == selected_profile:
            status = "[bold green]> Next[/bold green]"
        elif until and until > now:
            status = "[red]Limited until " + _time.strftime("%H:%M", _time.localtime(until)) + "[/red]"
        else:
            status = "[green]Ready[/green]"
        table.add_row(escape(_label(p, email)), escape(email or "(No Email)"),
                      f"{item.get('daily_tokens', 0):,}", status,
                      style="bold" if p == selected_profile else None)
    console.print(table)

def show_help():
    help_text = """
/new        - Start a fresh chat session
/new --carry- Carry context into a new account/chat
/continue   - Pick up and resume from the last response / previous execution state
/model [name]- Switch active agent (g=gemini, d=deepseek) with full context handover
/transfer   - Migrate to least-used profile or specified index with context handover
/tokens     - Token usage for the active provider: every profile, today, lifetime, limits
/resume     - Robust Menu to Load or Delete past sessions
/next       - Instantly switch to the next Chrome profile directory and start a fresh chat
/name <name>- Rename the current session file on disk
/autotransfer - Toggle auto profile switch on rate notice (default OFF; use /transfer manually)
/speech     - Toggle AI auto-voice ON/OFF (Saved)
/remind     - Send the core system prompt to the AI to re-align it
/jobs       - List all active background servers and processes
/subs [stop|clean|clean all] - List sub-agents; stop cancels all running ones, clean removes leftovers
/kill [id]  - Manually terminate a background job (e.g. /kill job_1a2b3c)
/exit       - Quit the CLI

[Tab] - Autocomplete slash commands
[Ctrl+Enter] - Insert new line in prompt
[Ctrl+P] - Instantly cancel active speech or replay last message
    """
    console.print(Panel(help_text.strip(), title="Big Fish Commands", border_style="blue"))
