"""Split out of main.py (unchanged logic)."""
import os, time
import ui, jobs
from executor import execute_command

def needs_elevation_confirm(cmd: str) -> bool:
    """Return True if a shell command needs model confirmation before execution.
    Triggers on: explicit sudo, dangerous rm on system paths, writes to system paths.
    Allowlisted: read-only sudo probes (sudo -n, sudo -l, sudo -v)."""
    import re as _re
    c = cmd.strip()
    cl = c.lower()
    # Allowlist: harmless sudo probes — no-op checks, capability listing
    if _re.match(r'sudo\s+(-[nlvV]+\s*)+(\s*true|&&|$|\|)', c):
        return False
    # Explicit sudo usage
    if _re.search(r'(?:^|[;&|`$(\s])sudo\s', c):
        return True
    # Dangerous rm targeting system directories (not user home subdirs)
    _sys_paths = r'/(?:etc|usr|boot|bin|sbin|lib|lib64|proc|sys|run|snap)(?:/|$)'
    # Strip harmless output redirections to /dev/null before analyzing the command
    c_scan = _re.sub(r'\d*>>?\s*/dev/null|&>>?\s*/dev/null', '', c)
    # Match only genuine force/recursive flags (-r, -rf, -Rf, --recursive, --force)
    _force_flag = r'(?:^|\s)(?:-[rRfF]{1,3}|--recursive|--force)(?=\s|$)'
    # Match rm only at a command boundary (start, or after ; & | && || $()
    _rm_cmd = r'(?:^|[;&|`$(]\s*|\s)rm\s'
    if _re.search(_rm_cmd, c_scan) and _re.search(_force_flag, c_scan):
        # Only flag if target is a system path or bare ~/ (home root)
        if _re.search(_sys_paths, c_scan):
            return True
        if _re.search(r'(?:^|\s)~/?(?:\s|$)', c_scan):  # bare ~ or ~/
            return True
        if _re.search(r'(?:^|\s)/(?:\s|$)', c_scan):    # bare /
            return True
    # Writes to protected system directories
    if _re.search(r'(?:>|tee)\s*/(?:etc|usr|boot|bin|sbin|lib|var/lib)/', c):
        return True
    # Direct chmod/chown on system paths
    if _re.search(r'\b(?:chmod|chown)\b.*\s+/(?:etc|usr|boot|bin|sbin|lib)', cl):
        return True
    return False

def _elevated_register(pending, cmd, cwd, kind="command"):
    cid = "ec_" + os.urandom(3).hex()
    pending[cid] = {"command": cmd, "cwd": cwd, "created": time.time(), "kind": kind}
    return cid

def _elevated_prompt(cid, cmd, kind="command"):
    how = "start as a background job" if kind == "background" else "run synchronously"
    return (
        "ELEVATED COMMAND - CONFIRMATION REQUIRED\n"
        "command_id: " + cid + "\n"
        "command: " + cmd + "\n"
        "mode: " + kind + "\n\n"
        "This command was NOT executed. It requires elevated privileges (sudo) or targets a protected system path.\n"
        "To approve (it will " + how + "), reply with this tag:\n"
        "  <confirm id=\"" + cid + "\" decision=\"approve\"/>\n"
        "To deny and discard it, use decision=\"deny\" in the same tag.\n"
        "Do NOT re-issue the original command; approval runs the stored copy."
    )

def _elevated_resolve(pending, cid, decision, default_cwd):
    entry = pending.get(cid)
    if not entry:
        return ("ERROR: Unknown or already-used command_id '" + cid + "'. No command executed. Pending ids: " + (", ".join(pending.keys()) or "none"), False)
    if time.time() - entry.get("created", 0) > 600:
        pending.pop(cid, None)
        return ("ERROR: command_id '" + cid + "' expired after 10 minutes and was discarded. No command executed.", False)
    d = (decision or "").strip().lower()
    if d in ("approve", "confirm", "yes", "run"):
        cmd = entry["command"]
        cwd = entry.get("cwd") or default_cwd
        kind = entry.get("kind", "command")
        pending.pop(cid, None)
        if kind == "background":
            out = jobs.start_background_job(cmd, cwd=cwd)
            ui.show_bash_output("Start Background Job (approved)", out)
        else:
            out = execute_command(cmd, cwd=cwd)
            ui.show_bash_output(cmd, out)
        return ("Elevated command (" + cid + ") APPROVED and executed.\nCommand: " + cmd + "\nResult:\n" + str(out), True)
    if d in ("deny", "cancel", "no", "abort"):
        cmd = entry["command"]
        pending.pop(cid, None)
        return ("Elevated command (" + cid + ") DENIED and discarded. Command was: " + cmd, False)
    return ("ERROR: Invalid decision '" + decision + "'. Use 'approve' or 'deny'. Command " + cid + " remains pending.", False)

def get_project_root(cwd: str) -> str:
    """Finds the root directory of the current project/workspace."""
    curr = os.path.abspath(cwd)
    proj_marker = os.path.sep + "projects" + os.path.sep
    if proj_marker in curr:
        parts = curr.split(proj_marker, 1)
        subparts = parts[1].split(os.path.sep)
        if subparts:
            return os.path.join(parts[0], "projects", subparts[0])
    p = curr
    while p and p != os.path.dirname(p):
        if os.path.exists(os.path.join(p, ".git")):
            return p
        p = os.path.dirname(p)
    return curr

def is_sandboxed_path(raw_path: str, resolved_path: str, cwd: str) -> bool:
    """If raw_path contains '..', ensure it does not escape the current working directory / project root."""
    if not raw_path:
        return True
    path_str = str(raw_path)
    if '..' in path_str:
        target_cwd = os.path.realpath(cwd or os.getcwd())
        prefix = target_cwd if target_cwd.endswith(os.path.sep) else target_cwd + os.path.sep
        target = os.path.realpath(resolved_path)
        if target != target_cwd and not target.startswith(prefix):
            return False
    return True
