# jobs.py - Background Process Manager
import os
import subprocess
import time
import uuid
import signal
from executor import _inject_sudo

LOG_DIR = os.path.expanduser("~/gemini_local_agent/logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Global registry to track running jobs
ACTIVE_JOBS = {}

# Jobs that exited on their own (crash or normal finish): job_id -> info incl. exit code.
# Jobs stopped by the user are NOT recorded here.
FINISHED_JOBS = {}
_MAX_FINISHED = 50


def _close_handle(job):
    try:
        job["file_handle"].close()
    except Exception:
        pass


def _record_finished(jid, job):
    """Move a job that exited by itself into FINISHED_JOBS, remembering its exit code."""
    _close_handle(job)
    FINISHED_JOBS[jid] = {
        "command": job["command"],
        "log_path": job["log_path"],
        "exit_code": job["process"].returncode,
    }
    while len(FINISHED_JOBS) > _MAX_FINISHED:
        FINISHED_JOBS.pop(next(iter(FINISHED_JOBS)))
    ACTIVE_JOBS.pop(jid, None)


def start_background_job(command, cwd=None):
    """Starts a command in the background, pipes output to a log, and checks for instant crashes."""
    if cwd is not None and not os.path.isdir(cwd):
        return f"ERROR: Working directory does not exist: {cwd}"

    job_id = f"job_{uuid.uuid4().hex[:6]}"
    log_path = os.path.join(LOG_DIR, f"{job_id}.log")

    injected_command = _inject_sudo(command)
    f = None

    try:
        # Open the log file
        f = open(log_path, 'w', encoding='utf-8')
        f.write(f"--- STARTING BACKGROUND JOB: {command} ---\n")
        f.flush()

        # Popen runs the process in the background.
        # os.setsid creates a process group so we can kill children cleanly later.
        process = subprocess.Popen(
            injected_command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=f,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            preexec_fn=os.setsid if os.name != 'nt' else None
        )

        # Wait 2 seconds. If a server instantly crashes, we catch it immediately.
        time.sleep(2.0)

        if process.poll() is not None:
            f.close()
            with open(log_path, 'r', encoding='utf-8', errors='replace') as rf:
                log_content = rf.read()
            if process.returncode == 0:
                return f"Job {job_id} finished immediately (Exit Code 0); it is not running in the background.\nLog Output:\n{log_content}"
            return f"ERROR: Job {job_id} crashed immediately (Exit Code {process.returncode}).\nLog Output:\n{log_content}"

        # Job survived the 2-second check; it is officially running.
        ACTIVE_JOBS[job_id] = {
            "process": process,
            "command": command,
            "log_path": log_path,
            "file_handle": f
        }
        return f"Success: Background job started and is running.\nJob ID: {job_id}\nLog File: {log_path}\n(Note: Use read_file on this log path to monitor the server's live output)."

    except Exception as e:
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        # Do not leave an empty/orphan log behind when the process never started
        try:
            if os.path.exists(log_path) and job_id not in ACTIVE_JOBS:
                os.remove(log_path)
        except Exception:
            pass
        return f"ERROR starting background job: {str(e)}"


def stop_background_job(job_id):
    """Kills a background job and all its child processes."""
    if job_id not in ACTIVE_JOBS:
        if job_id in FINISHED_JOBS:
            info = FINISHED_JOBS[job_id]
            return (f"ERROR: Job {job_id} already exited on its own (exit code {info['exit_code']}). "
                    f"Check its log: {info['log_path']}")
        log_path = os.path.join(LOG_DIR, f"{job_id}.log")
        if os.path.exists(log_path):
            return f"ERROR: Job {job_id} is already stopped (process exited)."
        return f"ERROR: Job {job_id} not found."

    job = ACTIVE_JOBS[job_id]
    process = job["process"]

    # Already dead before we got here: report the truth instead of "Successfully stopped".
    if process.poll() is not None:
        code = process.returncode
        _record_finished(job_id, job)
        return (f"NOTE: Job {job_id} had already exited on its own (exit code {code}) "
                f"before the stop request. Check its log: {job['log_path']}")

    try:
        if os.name != 'nt':
            # Kill the entire process group (kills the shell and the actual server command)
            try:
                pgid = os.getpgid(process.pid)
            except ProcessLookupError:
                pgid = None
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # Process ignored SIGTERM: escalate to SIGKILL on the whole group
                try:
                    os.killpg(pgid if pgid is not None else process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        else:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

        _close_handle(job)
        del ACTIVE_JOBS[job_id]
        return f"Successfully stopped background job: {job_id} ({job['command']})"
    except Exception as e:
        return f"ERROR stopping job: {str(e)}"


def list_background_jobs():
    """Lists all active background jobs. Jobs that died silently are moved to a
    'recently exited' section with their exit code instead of vanishing."""
    newly_dead = [jid for jid, job in ACTIVE_JOBS.items() if job["process"].poll() is not None]
    for jid in newly_dead:
        _record_finished(jid, ACTIVE_JOBS[jid])

    parts = []
    if not ACTIVE_JOBS:
        parts.append("No active background jobs.")
    else:
        output = ["--- ACTIVE BACKGROUND JOBS ---"]
        for jid, job in ACTIVE_JOBS.items():
            output.append(f"ID: {jid} | Command: {job['command']}\nLog: {job['log_path']}")
        parts.append("\n\n".join(output))

    if FINISHED_JOBS:
        lines = ["--- RECENTLY EXITED (on their own) ---"]
        for jid, info in list(FINISHED_JOBS.items())[-10:]:
            lines.append(f"ID: {jid} | Exit code: {info['exit_code']} | Command: {info['command']}\nLog: {info['log_path']}")
        parts.append("\n\n".join(lines))

    return "\n\n".join(parts)


def cleanup_all_jobs():
    """Failsafe to kill all jobs when the CLI exits."""
    for jid in list(ACTIVE_JOBS.keys()):
        stop_background_job(jid)
