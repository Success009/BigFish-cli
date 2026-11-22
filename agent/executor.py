# executor.py - Anti-Flood, Paginator & Memory-Safe Edition
import subprocess
import os
import time
import sys
import getpass
import base64
import json
import threading
import collections
import signal
import tempfile
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.markup import escape

console = Console()

# Persistent credentials vault location
VAULT_FILE = os.path.expanduser("~/.bigfish_vault.json")
CMD_LOG_FILE = os.path.expanduser("~/gemini_local_agent/last_command_output.txt")

DEFAULT_TIMEOUT = 300.0   # idle seconds (no output) before a command is killed
HARD_CAP = float(os.environ.get('BIGFISH_CMD_MAX', '3600') or 3600)   # absolute wall-clock limit
SPILL_BYTES = 2 * 1024 * 1024  # keep at most ~2MB of output in RAM


def _get_timeout(timeout=None):
    """Resolve timeout: explicit arg > BIGFISH_CMD_TIMEOUT env var > default."""
    if timeout is None:
        env = os.environ.get("BIGFISH_CMD_TIMEOUT")
        if env:
            try:
                timeout = float(env)
            except ValueError:
                timeout = None
    try:
        timeout = float(timeout) if timeout is not None else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return timeout if timeout > 0 else DEFAULT_TIMEOUT


def _open_log():
    """Open the output log for writing. Creates the folder; falls back to a temp file.
    Returns (file_handle, path)."""
    path = CMD_LOG_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return open(path, 'w', encoding='utf-8', errors='replace'), path
    except Exception:
        fd, tmp_path = tempfile.mkstemp(prefix="bigfish_cmd_", suffix=".txt")
        return os.fdopen(fd, 'w', encoding='utf-8', errors='replace'), tmp_path


def load_sudo():
    """Loads and decodes the sudo password from the local vault, falling back safely to hardcoded credentials."""
    pwd = None
    try:
        if os.path.exists(VAULT_FILE):
            with open(VAULT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                encoded_pwd = data.get("sudo_pwd", "")
                if encoded_pwd:
                    pwd = base64.b64decode(encoded_pwd).decode('utf-8')
    except Exception:
        pass

    if pwd and not verify_sudo(pwd):
        pwd = None

    return pwd

def save_sudo(pwd):
    """Encodes and saves the sudo password persistently across all sessions."""
    try:
        encoded_pwd = base64.b64encode(pwd.encode('utf-8')).decode('utf-8')
        with open(VAULT_FILE, 'w', encoding='utf-8') as f:
            json.dump({"sudo_pwd": encoded_pwd}, f)
        os.chmod(VAULT_FILE, 0o600)
    except Exception:
        pass

def set_sudo(pwd):
    """Saves the password only if sudo accepts it. Returns True when it was saved."""
    if not pwd or not verify_sudo(pwd):
        return False
    save_sudo(pwd)
    return True

def clear_sudo():
    """Removes the saved sudo password. Returns True if a vault file was deleted."""
    try:
        if os.path.exists(VAULT_FILE):
            os.remove(VAULT_FILE)
            return True
    except Exception:
        pass
    return False

def verify_sudo(pwd):
    """Pre-flight check: runs a harmless command to verify if the password is correct.
    No shell is involved, so quotes or metacharacters in the password are safe."""
    if not isinstance(pwd, str):
        return False
    try:
        result = subprocess.run(
            ["sudo", "-S", "id"],
            input=pwd + "\n",
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False

def _inject_sudo(command):
    """Intercepts sudo commands, validates credentials, and caches them forever."""
    if command.strip().startswith("sudo "):
        try:
            if subprocess.run("sudo -n true", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return command
        except Exception:
            pass

        pwd = load_sudo()

        # If both vault reading and the failsafe credential verify step fail, prompt the user as a last resort
        if not pwd or not verify_sudo(pwd):
            while True:
                console.print("\n[bold yellow]🔐 Sudo privileges requested. Authentication required.[/bold yellow]")
                pwd = getpass.getpass("Enter sudo password (will be permanently saved securely): ")

                if verify_sudo(pwd):
                    save_sudo(pwd)
                    console.print("[bold green]✔ Password verified and vault secured. Resuming...[/bold green]")
                    break
                else:
                    console.print("[bold red]❌ Incorrect password. Please try again.[/bold red]")

        clean_cmd = command.strip()[5:]
        import shlex
        return f"printf '%s\\n' {shlex.quote(pwd)} | sudo -S {clean_cmd}"

    return command

def _kill_process_group(process):
    """Terminate the shell AND every child it spawned; SIGKILL stragglers."""
    import os, signal, time as _time
    if not hasattr(os, "killpg"):
        process.terminate()
        _time.sleep(0.5)
        process.kill()
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if sig == signal.SIGTERM:
            _time.sleep(0.5)

def execute_command(command, cwd=None, timeout=None):
    """Executes a shell command with a live, transient rolling window UI and dual-layer output pagination.
    Memory-safe: output beyond ~2MB is streamed to the log file and only the tail is kept in RAM."""
    injected_command = _inject_sudo(command)
    timeout_s = _get_timeout(timeout)

    MAX_LINES = 100
    MAX_CHARS = 15000

    full_output = list()          # in-RAM buffer (until spill)
    buffered_chars = 0
    rolling_lines = collections.deque(maxlen=15)
    is_timeout = [False]

    spilled = False
    log_f = None
    log_path = CMD_LOG_FILE
    total_lines = 0
    total_chars = 0
    tail_lines = collections.deque(maxlen=MAX_LINES)
    HEAD_LINES = 30
    head_lines = list()
    tail_text = ""

    short_title = command[:50] + ("..." if len(command) > 50 else "")

    try:
        # Avoid preexec_fn=os.setsid to prevent background deadlocks and session collisions
        with subprocess.Popen(
            injected_command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            bufsize=1,
            universal_newlines=True,
            errors='replace',
            stdin=subprocess.DEVNULL,
            start_new_session=True
        ) as process:

            _why = ['limit']
            last_out = [time.time()]
            def timeout_handler(reason='limit'):
                _why[0] = reason
                is_timeout[0] = True
                try:
                    _kill_process_group(process)
                except Exception:
                    pass

            _stop = threading.Event()
            _t0 = time.time()

            def _watchdog():
                while not _stop.wait(1.0):
                    now = time.time()
                    if now - last_out[0] > timeout_s:
                        timeout_handler('no output for %g s' % timeout_s)
                        return
                    if now - _t0 > HARD_CAP:
                        timeout_handler('hard cap %g s' % HARD_CAP)
                        return

            class _TimerShim:
                def cancel(self):
                    _stop.set()
            timer = _TimerShim()
            threading.Thread(target=_watchdog, daemon=True).start()

            spinner = Spinner("dots", text=f"⚡ Running: {escape(short_title)}", style="cyan")

            try:
                with Live(Group(spinner, Panel("Starting command...", border_style="cyan")), refresh_per_second=10, transient=True) as live:
                    for line in iter(process.stdout.readline, ''):
                        last_out[0] = time.time()
                        total_lines += 1
                        if len(head_lines) < HEAD_LINES:
                            head_lines.append(line.rstrip('\n'))
                        total_chars += len(line)

                        if not spilled:
                            full_output.append(line)
                            buffered_chars += len(line)
                            if buffered_chars > SPILL_BYTES:
                                # Spill everything so far to disk; keep only tails in RAM.
                                spilled = True
                                log_f, log_path = _open_log()
                                joined = "".join(full_output)
                                log_f.write(joined)
                                for l in joined.splitlines():
                                    tail_lines.append(l)
                                tail_text = joined[-MAX_CHARS:]
                                full_output = []
                        else:
                            log_f.write(line)
                            tail_lines.append(line.rstrip('\n'))
                            tail_text = (tail_text + line)[-MAX_CHARS:]

                        rolling_lines.append(line.rstrip('\n')[:500])
                        display_text = "\n".join(rolling_lines) if rolling_lines else "..."
                        live.update(Group(spinner, Panel(escape(display_text), border_style="cyan")))
                process.wait()
            finally:
                timer.cancel()
                if log_f is not None:
                    try:
                        log_f.close()
                    except Exception:
                        pass

        if spilled:
            result_str = None
            lines = list(tail_lines)
        else:
            result_str = "".join(full_output)
            lines = result_str.splitlines()
            total_lines = len(lines)
            total_chars = len(result_str)

        # Dual-Layer Paginator (Lines AND Characters)
        if spilled or total_lines > MAX_LINES or total_chars > MAX_CHARS:
            if not spilled:
                # Save the full output to the log file (folder auto-created, temp fallback)
                f, log_path = _open_log()
                try:
                    f.write(result_str)
                finally:
                    f.close()

            if total_lines > MAX_LINES and (total_chars / max(1, total_lines)) < 500:
                _tail_n = MAX_LINES - HEAD_LINES
                truncated_lines = lines[-_tail_n:]
                start_idx = total_lines - len(truncated_lines)

                numbered_lines = list()
                for i, line in enumerate(head_lines):
                    numbered_lines.append(f"{i + 1:4} | {line}")
                _omitted = max(0, start_idx - len(head_lines))
                numbered_lines.append(f"     ... {_omitted} lines omitted (full text in the saved file) ...")
                for i, line in enumerate(truncated_lines):
                    actual_line_num = start_idx + i + 1
                    numbered_lines.append(f"{actual_line_num:4} | {line}")

                trunc_str = "\n".join(numbered_lines)
                prefix = f"--- COMMAND OUTPUT TRUNCATED ---\nThe command produced {total_lines} lines of output.\nThe FULL output was automatically saved to: {log_path}\n(If you need to read the older logs to find an error, use read_file on that path).\n\n--- FIRST 30 AND LAST 70 LINES ---\n"
                result_str = prefix + trunc_str

            # Massive jumbled single line (e.g. minified JS, giant JSON) or spilled blob
            else:
                tail = tail_text if spilled else result_str[-MAX_CHARS:]
                prefix = f"--- COMMAND OUTPUT TRUNCATED (SINGLE LINE/LARGE BLOB) ---\nThe command produced {total_chars} characters of output.\nThe FULL output was automatically saved to: {log_path}\n(If you need to read the full output, use read_file on that path).\n\n--- LAST 15,000 CHARACTERS ---\n... "
                result_str = prefix + tail

        if is_timeout[0]:
            return f"ERROR: Command timed out ({_why[0]}).\nOutput before timeout:\n{result_str}"

        if process.returncode != 0:
            return f"ERROR (Exit Code {process.returncode}):\n{result_str}"

        return result_str

    except Exception as e:
        return f"PYTHON ERROR: Failed to execute command. {str(e)}"
