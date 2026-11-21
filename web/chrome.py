"""Split out of main.py (unchanged logic)."""
import os, time, subprocess
import tools
from browser import WebAgentController, get_terminal_window_id, restore_terminal_focus
from config import load_cli_config
from profiles import get_chrome_user_data_dir

def is_chrome_cdp_alive():
    """Fast non-blocking probe to verify if Chrome DevTools Protocol port is alive."""
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:9222/json/version")
        with urllib.request.urlopen(req, timeout=0.3) as resp:
            return resp.status == 200
    except Exception:
        return False

def launch_chrome_process(profile_dir, url=None):
    """Spawns Google Chrome with remote debugging in an insulated process group."""
    prof_flag = f'--profile-directory="{profile_dir}"' if profile_dir else ""
    url_suffix = f' "{url}"' if url else ""
    user_data_dir = get_chrome_user_data_dir()
    extra_flags = "--disable-session-crashed-bubble --hide-crash-restore-bubble"
    
    if os.name == 'nt':
        chrome_command = f"start chrome --window-size=500,1040 --window-position=780,0 --remote-debugging-port=9222 --user-data-dir=\"{user_data_dir}\" {prof_flag} {extra_flags}{url_suffix}"
    else:
        chrome_command = f"google-chrome --window-size=500,1040 --window-position=780,0 --remote-debugging-port=9222 --user-data-dir=\"{user_data_dir}\" {prof_flag} {extra_flags}{url_suffix}"
        
    return subprocess.Popen(
        chrome_command, 
        shell=True, 
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, 
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid if os.name != 'nt' else None
    )

def restart_browser(chrome_process, controller, profile_dir, url=None, provider=None):
    term_win_id = get_terminal_window_id()
    if controller:
        try: controller.close()
        except: pass
    if chrome_process and chrome_process.poll() is None:
        try: chrome_process.terminate()
        except: pass
        time.sleep(0.3)
        try: chrome_process.kill()
        except: pass
    
    # Clean up any existing stale Chrome debugging instances to free up port and locks
    if os.name == 'nt':
        try:
            subprocess.run(
                'powershell -Command "Get-CimInstance Win32_Process -Filter \\"CommandLine like \'%remote-debugging-port=9222%\'\\" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"',
                shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except: pass
    else:
        try:
            subprocess.run("pkill -9 -f remote-debugging-port=9222", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass

    # Fast wait for port to release
    for _ in range(10):
        if not is_chrome_cdp_alive():
            break
        time.sleep(0.1)

    chrome_process = launch_chrome_process(profile_dir, url=url)

    # Fast poll until CDP is alive instead of fixed sleeping
    for _ in range(30):
        if is_chrome_cdp_alive():
            break
        time.sleep(0.2)
    
    restore_terminal_focus(term_win_id)
    
    # Recreate fresh controller instance matching target provider
    target_provider = provider or (controller.provider if (controller and hasattr(controller, 'provider')) else "gemini")
    controller = WebAgentController(provider=target_provider)
    try:
        controller.auto_transfer_on_rate_limit = load_cli_config().get('auto_transfer_on_rate_limit', True)
    except Exception:
        pass
    controller.connect(target_url=url)
    tools.set_browser_controller(controller)
    restore_terminal_focus(term_win_id)
    return chrome_process, controller
