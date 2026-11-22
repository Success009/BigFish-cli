# main.py - Big Fish Core Logic (Self-Healing Multi-Agent Edition)
import subprocess, os, signal, time, sys, re, json
import paths  # noqa: sets up sub-folder imports
import attachments
import tools, ui, jobs, audio, tokens
import adapters
from browser import (
    WebAgentController, AIStudioController, BrowserClosedError,
    get_terminal_window_id, restore_terminal_focus
)
from executor import execute_command
import prompts
import parser
import session
import pyperclip  # Required for copying manual mode payload
import debuglog as dlog
from config import CLI_CONFIG_FILE, load_cli_config, save_cli_config
from profiles import (get_current_chrome_profile_dir, get_chrome_profile_dir_by_email, get_running_chrome_profile_dir,
    get_profile_email_for_dir, profile_label_for_dir, get_all_available_profiles, get_chrome_user_data_dir, get_latest_session)
import profile_ids
from handover import load_session_by_filename, compile_handover_packet
from elevation import (needs_elevation_confirm, _elevated_register, _elevated_prompt, _elevated_resolve,
    get_project_root, is_sandboxed_path)
from chrome import is_chrome_cdp_alive, launch_chrome_process, restart_browser

os.makedirs("projects", exist_ok=True)

def build_system_prompt(provider):
    """The system prompt is the same for every provider. Sub-agent text and
    provider addenda are not appended: skills carry that knowledge on demand."""
    return prompts.SYSTEM_PROMPT

def has_url(text): return bool(re.search(r'https?://\S+', text))
def get_url(controller):
    try: return controller.page.url
    except: return None
def get_tool_feedback(tool_name, tool_output): return tool_output




def estimate_tokens(history):
    history_text = "\n".join([f"{item['role']}: {item['content']}" for item in history])
    return tokens.calculate_tokens(prompts.SYSTEM_PROMPT + "\n" + history_text)





























def _start_subagents(main_profile_dir):
    """Build the sub-agent manager. Imported lazily so the default CLI never loads subagents/."""
    from subagents.manager import Manager
    from subagents.factory import SubFactory
    from subagents.finishline import make_printer
    work_root = os.path.join(os.path.expanduser("~/gemini_local_agent"), "subagents_work")
    def _profiles():
        return [p for p in get_all_available_profiles() if p != main_profile_dir]
    factory = SubFactory(work_root, profiles=_profiles)
    mgr = Manager(factory, source_root=os.getcwd(), on_finish=make_printer(ui.show_status))
    try:
        factory.startup_sweep()
    except Exception:
        pass
    return mgr

def _run_subagent_tool(mgr, tool, params):
    """Handle spawn_subagent / list_subagents / cancel_subagent. Never raises."""
    if mgr is None:
        return "ERROR: Sub-agents failed to start in this session. Tell the user to check the start-up error and restart the CLI."
    try:
        if tool == 'list_subagents':
            return mgr.list_text()
        if tool == 'cancel_subagent':
            return mgr.cancel(str(params.get('id', '')).strip())
        role = str(params.get('role', '')).strip()
        brief = str(params.get('brief', '')).strip()
        scope = params.get('scope')
        if isinstance(scope, str):
            scope = [s.strip() for s in re.split(r'[,\n]', scope) if s.strip()]
        wait = str(params.get('wait', '')).strip().lower() in ('1', 'true', 'yes')
        write_paths = params.get('write_paths')
        if isinstance(write_paths, str):
            write_paths = [s.strip() for s in re.split(r'[,\n]', write_paths) if s.strip()]
        sid, text = mgr.spawn(role, brief, scope=scope or None, wait=wait, write_paths=write_paths or None)
        if text is not None:
            return text
        return f"Started sub-agent {sid}. You will get a one-line notice when it finishes."
    except Exception as e:
        return f"ERROR: {e}"

def main():
    dlog.init()
    dlog.log("INIT", "BigFish CLI started")
    active_provider = load_cli_config().get("last_active_provider", "gemini")
    # CLI override: `python3 main.py --provider deepseek` forces the starting provider.
    if "--provider" in sys.argv:
        try:
            _pi = sys.argv.index("--provider")
            if _pi + 1 < len(sys.argv):
                _cli_provider = sys.argv[_pi + 1].strip().lower()
                if _cli_provider in adapters.list_available_adapters():
                    active_provider = _cli_provider
        except Exception:
            pass
    ui.show_header(adapters.get_adapter(active_provider).display_name)
    chrome_process, controller = None, None
    sub_mgr = None  # sub-agent manager; stays None only if start-up failed
    chat_history, current_session_file = list(), None
    cumulative_session_history = list()
    is_new_chat = True
    first_prompt = None
    carried_history_str = ""
    last_known_url = adapters.get_adapter(active_provider).default_url
    # Persistent Manual Mode loaded from CLI config
    MANUAL_MODE = False  # Manual Mode removed
    AUTO_TRANSFER_ON_RATE_LIMIT = load_cli_config().get("auto_transfer_on_rate_limit", True)
    current_active_profile_dir = "Default"  # Initialize early to prevent UnboundLocalError in finally block
    last_ctrl_c_time = 0  # Sequential click timer for force-exit
    pending_auto_prompt = None  # Holds auto-continuation prompt for post-transfer states
    pending_elevated = {}  # command_id -> {command, cwd, created, kind} awaiting confirm_command
    force_session_update = False  # Triggers dynamic session profile overwrite on launches following a transfer
    pending_transfer = None # set after a transfer; the old session record is kept until it is confirmed
    PENDING_CONFIRM_RESPONSES = 1 # responses on a real chat URL needed to commit a transfer
    def _skip_session_save(*args, **kwargs):
        return args[6] # keep the existing session file name and write nothing
    # DeepSeek has no persistent system prompt slot — track whether it has been injected in THIS conversation.
    # Set to True only AFTER a successful response is received, so a failed send retries injection automatically.
    deepseek_sys_injected = False
    # DeepSeek has no persistent system prompt slot — inject on FIRST turn of every process run
    AI_CWD = os.getcwd()
    
    try:
        # Determine the most recently modified active conversation matching active_provider to auto-resume
        latest_filename = get_latest_session(provider=active_provider)
        url, filename, preview, loaded_history, loaded_cwd, loaded_email, loaded_profile_dir = None, None, None, list(), os.getcwd(), "Unknown Profile", "Default"
        
        if latest_filename:
            res = load_session_by_filename(latest_filename)
            url, filename, preview, loaded_history, loaded_cwd, loaded_email, loaded_profile_dir = res
            
        adapter = adapters.get_adapter(active_provider)
        if url and loaded_profile_dir:
            resolved_dir = get_chrome_profile_dir_by_email(loaded_email) or loaded_profile_dir
            current_active_profile_dir = resolved_dir if resolved_dir else loaded_profile_dir
            initial_url = url
            is_new_chat = False
            current_session_file, first_prompt = filename, preview
            chat_history = loaded_history
            cumulative_session_history = list(loaded_history)
            AI_CWD = loaded_cwd
            try:
                os.chdir(loaded_cwd)
            except Exception:
                pass
            session.set_active_session_file(filename)
            # Resumed chat already holds the system prompt in its history: do not re-send it.
            if loaded_history:
                deepseek_sys_injected = True
        else:
            # Fallback to standard startup if no session file exists
            current_active_profile_dir = get_current_chrome_profile_dir()
            initial_url = adapter.default_url if os.name != 'nt' else None
        
        # Try connecting to an already-running Chrome first (preserves state after transfers/restarts)
        controller = None
        chrome_process = None
        if is_chrome_cdp_alive():
            try:
                controller = WebAgentController(provider=active_provider)
                try:
                    controller.auto_transfer_on_rate_limit = load_cli_config().get('auto_transfer_on_rate_limit', True)
                except Exception:
                    pass
                controller.connect(timeout=5, target_url=initial_url)
                tools.set_browser_controller(controller)
            except Exception:
                controller = None
        
        # Verify that connected Chrome matches the desired account profile (for BOTH Gemini and DeepSeek!)
        if controller and controller.is_connected():
            running_profile = get_running_chrome_profile_dir()
            profile_mismatch = (
                (running_profile and current_active_profile_dir and running_profile != current_active_profile_dir)
                or (active_provider != "deepseek" and loaded_email and loaded_email != "Unknown Profile"
                    and controller.get_current_profile() != "Unknown Profile"
                    and controller.get_current_profile().lower() != loaded_email.lower())
            )
            if profile_mismatch:
                # Profile mismatch on running Chrome - restart with the target profile
                with ui.get_spinner(f"🔄 Switching Chrome to {profile_label_for_dir(current_active_profile_dir)} [{loaded_email}]..."):
                    chrome_process, controller = restart_browser(None, controller, current_active_profile_dir, url=initial_url, provider=active_provider)

        # Only kill and restart Chrome if we couldn't connect to an existing instance
        if not controller or not controller.is_connected():
            with ui.get_spinner(f"Connecting to {adapter.display_name}..."):
                chrome_process, controller = restart_browser(None, None, current_active_profile_dir, url=initial_url, provider=active_provider)
            
        # Guarantee the browser is actively showing initial_url (and not lingering on blank new_chat)
        if controller and controller.is_connected() and initial_url and "new_chat" not in initial_url and initial_url != adapter.default_url:
            target_prompt_id = adapter.extract_prompt_id(initial_url)
            if initial_url not in controller.page.url and (not target_prompt_id or target_prompt_id not in controller.page.url):
                with ui.get_spinner("Loading chat URL in browser..."):
                    controller.load_chat(initial_url)
            
        ui.show_status("Agent is ready. Type /help for commands.")
        try:
            sub_mgr = _start_subagents(current_active_profile_dir)
            ui.show_status("Sub-agents ready (/subs to list, /subs stop to cancel all).")
        except Exception as _se:
            sub_mgr = None
            ui.show_error(f"Sub-agents failed to start: {_se}")
        if not is_new_chat:
            ui.show_status(f"✔ Automatically resumed most recent session: {current_session_file}")
            if chat_history:
                ui.reprint_history(chat_history)
        
        _notify_pending = False
        while True:
            try:
                # If there is a pending auto-prompt queued from a `/transfer` or `/model`, use it directly
                if pending_auto_prompt:
                    user_input = pending_auto_prompt
                    pending_auto_prompt = None
                else:
                    if _notify_pending:
                        _notify_pending = False
                        try:
                            import notify as _notify
                            _notify.on_turn_finished(load_cli_config)
                        except Exception:
                            pass
                    user_input = ui.show_prompt(estimate_tokens(chat_history), manual_mode=MANUAL_MODE, provider=adapters.get_adapter(active_provider).display_name, source=getattr(controller, "last_source", ""))
                
                # Skip empty inputs immediately to prevent looping blank strings
                if not user_input or not user_input.strip():
                    continue
                dlog.log("INPUT", repr(user_input[:80]))
                _notify_pending = not user_input.lstrip().startswith('/')
                
                if user_input.strip().lower() in ['continue', '/continue']:
                    ui.show_status("⏩ Resuming from last state...")
                    user_input = "Please continue directly from your last response or tool execution without repeating previous text. Complete any pending steps or proceed to the next objective."
                elif user_input.startswith('/'):
                    parts = user_input.split(' ', 1)
                    cmd = parts[0].lower()
                    arg = parts[1].strip() if len(parts) > 1 else ""

                    if cmd == '/exit': break
                    elif cmd == '/sound':
                        import notify as _notify
                        _a = arg.lower().strip()
                        if _a in _notify.SOUNDS:
                            save_cli_config({'finish_sound': _a, 'finish_action': 'sound'})
                            ui.show_status(f'Finish action: sound ({_a})')
                            _notify.play(_a)
                        elif _a in ('off', 'raise', 'raise+sound'):
                            save_cli_config({'finish_action': _a})
                            ui.show_status(f'Finish action: {_a}')
                        elif _a == 'on':
                            save_cli_config({'finish_action': 'sound'})
                            ui.show_status('Finish action: sound')
                        else:
                            ui.show_status(f'Finish action: {_notify.get_action(load_cli_config)}, sound: {_notify.get_sound(load_cli_config)}. '
                                           'Usage: /sound ' + '|'.join(list(_notify.SOUNDS) + ['on', 'raise', 'raise+sound', 'off']) + '  (F2 = cycle action, F3 = cycle sound)')
                        continue
                    elif cmd == '/sudo':
                        import executor as _ex
                        _a = arg.strip()
                        if _a.lower() == 'clear':
                            _ex.clear_sudo()
                            ui.show_status('Saved sudo password removed.')
                        else:
                            if not _a:
                                import getpass
                                _a = getpass.getpass('Sudo password: ')
                            if _ex.set_sudo(_a):
                                ui.show_status('Sudo password verified and saved.')
                            else:
                                ui.show_error('Wrong sudo password. Nothing was saved.')
                        continue
                    elif cmd == '/help': ui.show_help(); continue
                    elif cmd == '/speech':
                        ui.show_status(f"🔊 Auto-Speech is now {'ON' if audio.toggle_speech() else 'OFF'} (Saved)."); continue
                    elif cmd == '/remind':
                        ui.show_status("📝 Injecting System Prompt Reminder into the chat...")
                        user_input = f"SYSTEM REMINDER - Please read and acknowledge your core operational instructions:\n\n{build_system_prompt(active_provider)}"
                    elif cmd in ('/autotransfer', '/auto-transfer'):
                        AUTO_TRANSFER_ON_RATE_LIMIT = not AUTO_TRANSFER_ON_RATE_LIMIT
                        save_cli_config({"auto_transfer_on_rate_limit": AUTO_TRANSFER_ON_RATE_LIMIT})
                        try:
                            controller.auto_transfer_on_rate_limit = AUTO_TRANSFER_ON_RATE_LIMIT
                        except Exception:
                            pass
                        ui.show_status(
                            f"Auto-Transfer on rate notice is now "
                            f"{'ON' if AUTO_TRANSFER_ON_RATE_LIMIT else 'OFF'} (Saved across restarts)."
                        )
                        if not AUTO_TRANSFER_ON_RATE_LIMIT:
                            ui.show_status("Rate notices will only pause and keep waiting. Use /transfer to switch profiles manually.")
                        continue
                    elif cmd == '/subs':
                        _sa = arg.strip().lower()
                        if _sa == 'stop':
                            try:
                                if sub_mgr is None:
                                    ui.show_status("Sub-agents are not running.")
                                else:
                                    _n = sub_mgr.disable_all()
                                    sub_mgr.enable()
                                    ui.show_status(f"Cancelled {_n} running sub-agent(s). Spawning is still available.")
                            except Exception as _se:
                                ui.show_error(f"stop failed: {_se}")
                        elif _sa in ('clean', 'clean all'):
                            try:
                                ui.show_status(((lambda r: "\n".join(map(str, r)) if isinstance(r, (list, tuple)) else str(r))(sub_mgr.clean(0 if _sa == 'clean all' else None)) or "Nothing to clean.") if sub_mgr is not None else "Sub-agents are off.")
                            except Exception as _se:
                                ui.show_error(f"clean failed: {_se}")
                        else:
                            ui.show_bash_output("Sub-agents", _run_subagent_tool(sub_mgr, 'list_subagents', {}), full=True)
                        continue
                    elif cmd == '/jobs':
                        ui.show_bash_output("Background Jobs", jobs.list_background_jobs(), full=True)
                        continue
                    elif cmd == '/kill':
                        if arg:
                            ui.show_status(jobs.stop_background_job(arg))
                        else:
                            ui.show_error("Provide a job ID: /kill job_xxx")
                        continue
                    elif cmd == '/name':
                        if not arg:
                            ui.show_error("Usage: /name <new_session_name>")
                            continue
                        if not current_session_file:
                            ui.show_error("No active session to rename. Start a conversation first.")
                            continue
                        try:
                            clean_name = "".join(c for c in arg if c.isalnum() or c in ('_', '-')).strip()
                            if not clean_name:
                                ui.show_error("Invalid name. Use alphanumeric characters, dashes, or underscores.")
                                continue
                            new_filename = f"{clean_name}.json"
                            old_path = os.path.join(session.SESSION_DIR, current_session_file)
                            new_path = os.path.join(session.SESSION_DIR, new_filename)
                            if os.path.exists(old_path):
                                with open(old_path, 'r', encoding='utf-8') as f:
                                    data = json.load(f)
                                
                                orig_preview = data.get("preview", "")
                                if ":" in orig_preview:
                                    orig_preview = orig_preview.split(":", 1)[1].strip()
                                data["preview"] = f"{clean_name}: {orig_preview}"
                                
                                # Cleanly write updated preview inside old file path before moving it
                                with open(old_path, 'w', encoding='utf-8') as f:
                                    json.dump(data, f, indent=4)
                                    
                                if os.path.exists(new_path):
                                    os.remove(new_path)  # Cleanly remove existing target to prevent overwrite lock conflicts
                                    
                                os.rename(old_path, new_path)
                                current_session_file = new_filename
                                first_prompt = f"{clean_name}: {orig_preview}"  # Sync in-memory first prompt preview
                                ui.show_status(f"Session successfully renamed and updated to: {new_filename}")
                            else:
                                ui.show_error("Current session file not found on disk.")
                        except Exception as e:
                            ui.show_error(f"Failed to rename session: {e}")
                        continue
                    elif cmd == '/new':
                        carry_context = "--carry" in user_input.lower()
                        context_dump = ""
                        transfer_file = os.path.join(AI_CWD, ".context_transfer.txt")
                        
                        if os.path.exists(transfer_file):
                            try:
                                with open(transfer_file, 'r', encoding='utf-8') as f:
                                    transfer_content = f.read()
                                carried_history_str = f"=== TRANSFERRED PROJECT CONTEXT ===\n{transfer_content}\n===================================\n\n"
                                os.remove(transfer_file)
                            except Exception:
                                carried_history_str = ""
                        else:
                            carried_history_str = ""
                            
                        if carry_context and chat_history:
                            context_dump = "\n".join([f"{item['role']}: {item['content']}" for item in chat_history])
                            carried_history_str += f"=== PREVIOUS CHAT CONTEXT ===\n{context_dump}\n===========================\n\n"
                            
                        if not carried_history_str and not carry_context:
                            AI_CWD = os.getcwd() 
                            
                        controller.start_new_chat()
                        is_new_chat, current_session_file, first_prompt = True, None, None
                        chat_history = list()
                        
                        os.system('cls' if os.name == 'nt' else 'clear')
                        ui.show_header(adapters.get_adapter(active_provider).display_name)
                        
                        if carried_history_str:
                            ui.show_status(f"Started a fresh {adapters.get_adapter(active_provider).display_name} chat (Context & CWD Carried Over: {AI_CWD}).")
                        else:
                            ui.show_status(f"Started a fresh {adapters.get_adapter(active_provider).display_name} chat.")
                        continue
                    elif cmd in ('/model', '/agent'):
                        target_model = arg.strip().lower() if arg else ""
                        available_adapters = adapters.list_available_adapters()
                        if not target_model:
                            # Cycle through providers: gemini -> deepseek -> gemini
                            target_model = adapters.get_next_provider(active_provider)
                        elif target_model not in available_adapters:
                            if target_model in ("d", "ds", "deep-seek", "deepseek"): target_model = "deepseek"
                            elif target_model in ("g", "google", "ai_studio", "aistudio", "gemini"): target_model = "gemini"
                            else:
                                ui.show_error(f"Unknown model '{arg}'. Use: g (gemini), d (deepseek). Available: {', '.join(available_adapters)}")
                                continue

                        if target_model == active_provider:
                            ui.show_status(f"ℹ Already using {adapters.get_adapter(active_provider).display_name}.")
                            continue

                        try: audio.stop_audio()
                        except Exception: pass

                        ui.show_status(f"📝 Compiling Reasoning & Action Handover (RAH) packet for model switch...")
                        transfer_content = compile_handover_packet(
                            chat_history,
                            get_all_available_profiles(),
                            current_active_profile_dir,
                            current_session_file=current_session_file,
                            cumulative_history=cumulative_session_history
                        )
                        try:
                            with open(os.path.join(AI_CWD, ".context_transfer.txt"), 'w', encoding='utf-8') as f:
                                f.write(transfer_content)
                        except Exception: pass

                        old_provider = active_provider
                        active_provider = target_model
                        save_cli_config({"last_active_provider": active_provider})
                        new_adapter = adapters.get_adapter(active_provider)
                        target_url = new_adapter.default_url

                        ui.show_status(f"🔄 Switching agent from {adapters.get_adapter(old_provider).display_name} to {new_adapter.display_name}...")
                        with ui.get_spinner(f"Loading {new_adapter.display_name} ({target_url})..."):
                            chrome_process, controller = restart_browser(chrome_process, controller, current_active_profile_dir, url=target_url, provider=active_provider)

                        is_new_chat = True
                        chat_history = list()
                        deepseek_sys_injected = False  # New conversation — re-inject system prompt on first turn
                        carried_history_str = f"=== TRANSFERRED PROJECT CONTEXT ===\n{transfer_content}\n===================================\n\n"
                        pending_auto_prompt = f"Please review the transferred context blueprint above. You are now running on {new_adapter.display_name}. First, read and inspect the relevant code files to ground yourself in the actual codebase state on disk. Then, continue your work toward the active objective using strict XML message and tool formatting."
                        force_session_update = True
                        pending_transfer = [0] # old session record is kept until this transfer is confirmed
                        os.system('cls' if os.name == 'nt' else 'clear')
                        ui.show_header(new_adapter.display_name)
                        ui.show_status(f"✔ Switched active agent to {new_adapter.display_name} with context handover.")
                        continue
                    elif cmd == '/resume':
                        res = session.resume_session()
                        url, filename, preview, loaded_history, loaded_cwd, loaded_email, loaded_profile_dir = res
                        if filename:
                            target_provider = getattr(res, 'provider', None) or active_provider
                            if target_provider != active_provider:
                                active_provider = target_provider
                                save_cli_config({"last_active_provider": active_provider})
                            
                            target_adapter = adapters.get_adapter(active_provider)
                            if not url:
                                url = target_adapter.default_url

                            if controller:
                                controller.switch_adapter(active_provider)

                            # Profile Resolution for BOTH Gemini and DeepSeek
                            resolved_profile_dir = get_chrome_profile_dir_by_email(loaded_email) or loaded_profile_dir
                            if not resolved_profile_dir:
                                resolved_profile_dir = loaded_profile_dir

                            session.set_active_session_file(filename)

                            running_profile = get_running_chrome_profile_dir() or current_active_profile_dir
                            needs_browser_restart = (
                                not controller 
                                or not controller.is_connected()
                                or (resolved_profile_dir and resolved_profile_dir != running_profile)
                            )
                            if needs_browser_restart:
                                target_pdir = resolved_profile_dir or current_active_profile_dir
                                with ui.get_spinner(f"🔄 Switching Chrome to {profile_label_for_dir(target_pdir)} [{loaded_email}]..."):
                                    chrome_process, controller = restart_browser(chrome_process, controller, target_pdir, url=url, provider=active_provider)
                                    current_active_profile_dir = target_pdir
                                ui.show_status(f"✔ Browser ready with {profile_label_for_dir(target_pdir)} [{loaded_email}]")
                            else:
                                with ui.get_spinner("Loading chat URL in browser..."):
                                    controller.load_chat(url)

                            # Ensure the page is actually on the target prompt URL
                            if url and controller and controller.is_connected():
                                target_prompt_id = target_adapter.extract_prompt_id(url)
                                if url not in controller.page.url and (not target_prompt_id or target_prompt_id not in controller.page.url):
                                    with ui.get_spinner("Loading chat URL in browser..."):
                                        controller.load_chat(url)
                            is_new_chat = False
                            last_known_url = url
                            # Resumed chat already holds the system prompt: do not re-send it.
                            if loaded_history:
                                deepseek_sys_injected = True
                            current_session_file, first_prompt = filename, preview
                            chat_history = loaded_history
                            cumulative_session_history = list(loaded_history)
                            AI_CWD = loaded_cwd
                            try:
                                os.chdir(loaded_cwd)
                            except Exception:
                                pass 
                            os.system('cls' if os.name == 'nt' else 'clear'); ui.show_header(target_adapter.display_name); ui.show_status(f"Resumed session: {filename}")
                            if chat_history:
                                ui.reprint_history(chat_history)
                        else:
                            os.system('cls' if os.name == 'nt' else 'clear'); ui.show_header(adapters.get_adapter(active_provider).display_name)
                        continue
                    elif cmd == '/next':
                        target_adapter = adapters.get_adapter(active_provider)
                        target_provider_url = target_adapter.default_url

                        available_profiles = get_all_available_profiles()
                        if len(available_profiles) <= 1:
                            ui.show_error("Only one Chrome profile found. Cannot switch.")
                            continue

                        next_profile_dir = None
                        if arg:
                            _sel = profile_ids.resolve(arg, get_chrome_user_data_dir())
                            if _sel and _sel in available_profiles:
                                next_profile_dir = _sel
                            else:
                                ui.show_error(f"Invalid profile selector '{arg}'. Use an account number from 1 to {len(available_profiles)}, for example /transfer 13.")
                                continue
                        else:
                            try:
                                curr_idx = available_profiles.index(current_active_profile_dir)
                            except ValueError:
                                curr_idx = 0
                            next_idx = (curr_idx + 1) % len(available_profiles)
                            next_profile_dir = available_profiles[next_idx]

                        target_email = "Unknown Email"
                        try:
                            local_state_path = os.path.join(get_chrome_user_data_dir(), "Local State")
                            with open(local_state_path, 'r', encoding='utf-8') as f:
                                local_state = json.load(f)
                            meta = local_state.get('profile', {}).get('info_cache', {}).get(next_profile_dir, {})
                            target_email = meta.get('user_name', 'No Email')
                        except Exception:
                            pass

                        try:
                            disp_num = available_profiles.index(next_profile_dir) + 1
                        except ValueError:
                            disp_num = "?"

                        ui.show_status(f"🔄 Switching directly to {profile_label_for_dir(next_profile_dir)} [{target_email}]...")
                        chrome_process, controller = restart_browser(chrome_process, controller, next_profile_dir, url=target_provider_url, provider=active_provider)
                        current_active_profile_dir = next_profile_dir
                        is_new_chat, current_session_file, first_prompt = True, None, None
                        chat_history = list()
                        last_known_url = target_provider_url
                        deepseek_sys_injected = False
                        os.system('cls' if os.name == 'nt' else 'clear')
                        ui.show_header(target_adapter.display_name)
                        ui.show_status(f"✔ Switched to {profile_label_for_dir(current_active_profile_dir)} ({target_adapter.display_name}).")
                        continue
                    elif cmd == '/transfer':
                        target_adapter = adapters.get_adapter(active_provider)
                        target_provider_url = target_adapter.default_url

                        # Stop any active TTS playback immediately (Priority Level 2)
                        try:
                            audio.stop_audio()
                        except Exception:
                            pass
                        ui.show_status("📝 Compiling Reasoning & Action Handover (RAH) packet...")
                        transfer_content = compile_handover_packet(
                            chat_history, 
                            get_all_available_profiles(), 
                            current_active_profile_dir,
                            current_session_file=current_session_file,
                            cumulative_history=cumulative_session_history
                        )
                        try:
                            with open(os.path.join(AI_CWD, ".context_transfer.txt"), 'w', encoding='utf-8') as f:
                                f.write(transfer_content)
                        except Exception:
                            pass

                        available_profiles = get_all_available_profiles()
                        if len(available_profiles) <= 1:
                            ui.show_error("Only one Chrome profile found. Cannot switch.")
                            continue
                            
                        next_profile_dir = None
                        
                        # Handle specific profile target argument (like /transfer 3)
                        if arg:
                            _sel = profile_ids.resolve(arg, get_chrome_user_data_dir())
                            if _sel and _sel in available_profiles:
                                next_profile_dir = _sel
                            else:
                                ui.show_error(f"Invalid profile selector '{arg}'. Use an account number from 1 to {len(available_profiles)}, for example /transfer 13.")
                                continue
                        else:
                            # Intelligent Least-Used Transfer Routing
                            usage_summary = tokens.get_profiles_usage_summary(available_profiles, provider=active_provider)
                            next_profile_dir = tokens.pick_transfer_target(available_profiles, current_active_profile_dir, provider=active_provider)
                            if not next_profile_dir:
                                ui.show_error("No other usable profile to transfer to (the others are signed out or rate limited). Pass a number to force one, e.g. /transfer 2.")
                                continue
                            ui.show_profile_usage_analysis(usage_summary, next_profile_dir)
                            
                        # Resolve display index for logging
                        try:
                            disp_num = available_profiles.index(next_profile_dir) + 1
                        except ValueError:
                            disp_num = "?"
                            
                        ui.show_status(f"🔄 Switching directly to {profile_label_for_dir(next_profile_dir)}...")
                        chrome_process, controller = restart_browser(chrome_process, controller, next_profile_dir, url=target_provider_url, provider=active_provider)
                        current_active_profile_dir = next_profile_dir
                        
                        # Reset chat states but PRESERVE current_session_file and first_prompt to inherit in-place
                        is_new_chat = True
                        chat_history = list()
                        deepseek_sys_injected = False  # New conversation — re-inject system prompt on first turn
                        
                        # Store the compiled packet in the in-memory carried context
                        carried_history_str = f"=== TRANSFERRED PROJECT CONTEXT ===\n{transfer_content}\n===================================\n\n"
                        
                        # Queue an automatic continuation turn instructing the model to inspect code first
                        pending_auto_prompt = "Please review the transferred context blueprint above. First, read and inspect the relevant code files to ground yourself in the actual codebase state on disk. Then, continue your work toward the active objective using strict XML message and tool formatting."
                        
                        # Force session profile and URL overwrite on the very next startup turn auto-save
                        force_session_update = True
                        pending_transfer = [0] # old session record is kept until this transfer is confirmed
                        last_known_url = target_provider_url
                        
                        ui.show_status(f"✔ Successfully switched to {profile_label_for_dir(current_active_profile_dir)} ({target_adapter.display_name}). Context loaded and ready to continue!")
                        continue
                    elif cmd == '/tokens':
                        available_profiles = get_all_available_profiles()
                        report = tokens.get_provider_usage_report(available_profiles, active_provider)
                        active_prof = get_running_chrome_profile_dir() or current_active_profile_dir
                        ui.show_provider_usage_report(report, active_profile=active_prof, provider_name=adapters.get_adapter(active_provider).display_name)
                        continue
                    elif cmd == '/reload':
                        import importlib
                        importlib.reload(parser)
                        importlib.reload(tools)
                        importlib.reload(prompts)
                        ui.show_status("✔ Successfully reloaded parser, tools, and prompts modules in memory.")
                        continue
                    else: 
                        if cmd not in ['/remind', '/name', '/continue']:
                            ui.show_error("Unknown command.")
                            continue

                if not first_prompt: first_prompt = user_input
                
                preview = (user_input[:47] + '...') if len(user_input) > 50 else user_input
                ui.show_status(f"⚡ Processing prompt: \"{preview}\"")
                
                # Append User prompt to local history immediately so it is captured
                chat_history.append({"role": "User", "content": user_input})
                cumulative_session_history.append({"role": "User", "content": user_input})
                chat_history = chat_history[-500:]
                cumulative_session_history = cumulative_session_history[-1000:]
                
                # Auto-sync active profile if user manually switches profile inside Chrome
                current_profile = controller.get_current_profile(profile_dir=current_active_profile_dir) if controller else "Unknown Profile"
                if current_profile in ("Unknown Profile", "DeepSeek User", "", None):
                    fb = get_profile_email_for_dir(current_active_profile_dir)
                    if fb: current_profile = fb

                resolved_dir = get_chrome_profile_dir_by_email(current_profile)
                if resolved_dir and resolved_dir != current_active_profile_dir:
                    current_active_profile_dir = resolved_dir

                # Auto-save immediately using resolved profile directories
                current_url = controller.get_current_url()
                # Commit a pending transfer as soon as the new chat has a real URL, even when the last turn used no tools
                if pending_transfer is not None and session.is_real_chat_url(current_url):
                    pending_transfer = None
                    force_session_update = True
                    ui.show_status('Transfer confirmed. Session record updated.')
                is_valid_save_state = current_url and "new_chat" not in current_url and current_profile not in ("Unknown Profile", "")

                current_session_file = (_skip_session_save if pending_transfer is not None else session.save_current_session)(
                    first_prompt, 
                    current_url, 
                    chat_history, 
                    AI_CWD, 
                    current_profile, 
                    current_active_profile_dir, 
                    current_session_file,
                    force_update=force_session_update,
                    provider=active_provider
                )
                
                # Only reset force_session_update once we have successfully written the fresh, non-new_chat details
                if is_valid_save_state and force_session_update and pending_transfer is None:
                    force_session_update = False

                _clean_input, _attach_paths = attachments.extract(user_input)
                if _attach_paths:
                    user_input = _clean_input or "Please look at the attached file(s)."
                    if controller:
                        controller.pending_attachments = list(_attach_paths)
                if is_new_chat:
                    base_prompt = f"{build_system_prompt(active_provider)}\n\n{carried_history_str}=== USER REQUEST ===\n{user_input}"
                    is_new_chat = False
                    carried_history_str = ""
                    deepseek_sys_injected = True
                elif active_provider == "deepseek" and not deepseek_sys_injected:
                    # DeepSeek has no persistent system prompt slot — inject once per conversation.
                    # On resumed sessions (bf restart), this fires on the FIRST turn.
                    # Flag is only set to True AFTER a successful send (see below), so if send
                    # fails the next turn will retry the injection automatically.
                    base_prompt = f"{build_system_prompt(active_provider)}\n\n=== USER REQUEST ===\n{user_input}"
                else:
                    base_prompt = user_input
                
                parse_retry_count = 0
                dead_turn_count = 0
                _dead_transfer = False
                while True: 
                    try:
                        start_time = time.time()
                        turn_marker = f"[SYS_MARKER_{int(time.time()*1000)}]"
                        
                        try:
                            dlog.log("TURN", f"send prompt len={len(base_prompt)} marker={turn_marker[:20]}")
                            with ui.get_spinner(f"Waiting for response... (CWD: {AI_CWD})"):
                                raw_response = controller.send_prompt_and_get_response(base_prompt, turn_marker, auto_submit=True)
                            dlog.log("TURN", f"recv raw_response len={len(raw_response) if raw_response else 0}")
                        except BrowserClosedError:
                            dlog.log("TURN", "BrowserClosedError caught")
                            raw_response = None
                            
                        # Handle the self-healing bypass event cleanly
                        if raw_response == "PERMISSION_DENIED_RELOADED":
                            ui.show_error("Session refreshed dynamically to clear permission state. Resubmitting turn...")
                            continue

                        # Dead turn: the browser returned nothing (prompt never registered, no stream).
                        # After 2 in a row, reuse the transfer branch instead of re-sending the prompt.
                        if raw_response == "":
                            dead_turn_count += 1
                            if dead_turn_count >= 2 and active_provider != "deepseek":
                                dlog.log("RATE", "dead turns reached 2; routing to transfer")
                                raw_response = "RATE_LIMIT_DETECTED"
                                _dead_transfer = True
                                dead_turn_count = 0
                        elif raw_response:
                            dead_turn_count = 0
                        
                        # =====================================================================
                        # PRIORITY LEVEL 2: SESSION TRANSFER & RE-ROUTING (Priority: 80)
                        # Triggered on quota exhaustion or rate limits.
                        # Actions:
                        #   1. Cancel any active TTS speech threads immediately.
                        #   2. Compile atomic Handover Blueprint packet.
                        #   3. Restart browser on least-used profile.
                        #   4. Purge inner execution queue and BREAK out of inner turn loop.
                        #   5. Queue ONLY the clean continuation prompt for the next cycle.
                        # =====================================================================
                        if raw_response == "RATE_LIMIT_DETECTED":
                            if active_provider == "deepseek":
                                ui.show_status("ℹ Rate notice observed; waiting for DeepSeek generation to continue (auto-transfer voided on DeepSeek)...")
                                time.sleep(1.5)
                                continue

                            # Auto-transfer is OPT-IN. Default off: just keep
                            # waiting — the model often just needs more time, and
                            # a false positive should never switch profiles.
                            if not AUTO_TRANSFER_ON_RATE_LIMIT:
                                # Safety net only. browser.py already keeps
                                # polling when auto-transfer is OFF, so this is
                                # unreachable in normal operation. Never resend.
                                ui.show_status(
                                    "Rate notice observed but auto-transfer is OFF. "
                                    "Not resending. Use /transfer to switch profiles manually."
                                )
                                break

                            ui.show_error("⚠️ Rate limit hit on current profile! Initiating Priority-2 Auto-Transfer...")
                            
                            # 1. Stop audio / speech immediately so old speech doesn't bleed into new session
                            try:
                                audio.stop_audio()
                            except Exception:
                                pass
                            
                            available_profiles = get_all_available_profiles()
                            if len(available_profiles) <= 1:
                                ui.show_error("Only one Chrome profile found. Cannot auto-transfer. Wait for rate limit to reset.")
                                break
                                
                            transfer_content = compile_handover_packet(
                                chat_history, 
                                available_profiles, 
                                current_active_profile_dir,
                                current_session_file=current_session_file,
                                cumulative_history=cumulative_session_history
                            )
                            try:
                                with open(os.path.join(AI_CWD, ".context_transfer.txt"), 'w', encoding='utf-8') as f:
                                    f.write(transfer_content)
                            except Exception:
                                pass

                            # Remember WHICH provider is limited on WHICH account, for 5 hours.
                            # Other providers on this same account are not affected.
                            try:
                                if _dead_transfer:
                                    raise RuntimeError("dead-turn transfer: not marking profile rate limited")
                                _reset_secs = tokens.RATE_LIMIT_WINDOW_SECONDS
                                try:
                                    _rs = controller.adapter.read_limit_reset_seconds(controller.page)
                                    if _rs:
                                        _reset_secs = _rs
                                        dlog.log("RATE", f"banner reset in {_rs}s")
                                except Exception as _e2:
                                    dlog.log("RATE", f"reset time read failed: {_e2}")
                                _until = tokens.mark_rate_limited(
                                    current_active_profile_dir, active_provider,
                                    reason="auto-transfer: rate limit confirmed",
                                    window_seconds=_reset_secs)
                                dlog.log("RATE", f"marked {active_provider}/{current_active_profile_dir} limited until {time.strftime('%H:%M', time.localtime(_until))}")
                            except Exception as _e:
                                dlog.log("RATE", f"mark_rate_limited failed: {_e}")

                            next_profile_dir = tokens.pick_transfer_target(available_profiles, current_active_profile_dir, provider=active_provider)
                            if not next_profile_dir:
                                ui.show_error("No other usable profile to transfer to (the others are signed out or rate limited). Stopped. Try again after a limit resets.")
                                break
                                
                            usage_summary = tokens.get_profiles_usage_summary(available_profiles, provider=active_provider)
                            ui.show_profile_usage_analysis(usage_summary, next_profile_dir)
                            
                            try:
                                disp_num = available_profiles.index(next_profile_dir) + 1
                            except ValueError:
                                disp_num = "?"
                                
                            target_url = adapters.get_adapter(active_provider).default_url
                            ui.show_status(f"🔄 Auto-switching to {profile_label_for_dir(next_profile_dir)}...")
                            chrome_process, controller = restart_browser(chrome_process, controller, next_profile_dir, url=target_url, provider=active_provider)
                            current_active_profile_dir = next_profile_dir
                            _dead_transfer = False
                            
                            # Reset turn history buffers for clean new chat
                            is_new_chat = True
                            chat_history = list()
                            deepseek_sys_injected = False
                            carried_history_str = f"=== TRANSFERRED PROJECT CONTEXT ===\n{transfer_content}\n===================================\n\n"
                            pending_auto_prompt = "Please review the transferred context blueprint above. First, read and inspect the relevant code files to ground yourself in the actual codebase state on disk. Then, continue your work toward the active objective using strict XML message and tool formatting."
                            force_session_update = True
                            pending_transfer = [0] # old session record is kept until this transfer is confirmed
                            last_known_url = target_url
                            ui.show_status(f"✔ Auto-transferred to {profile_label_for_dir(current_active_profile_dir)}. Resuming single clean continuation prompt!")
                            
                            # CRITICAL: BREAK out of inner turn execution loop to prevent re-sending old outputs or duplicate prompts
                            break
                            
                        if raw_response is None: 
                            ui.show_error("Browser disconnected or chat box not found. Attempting Auto-Recovery...")
                            try:
                                with ui.get_spinner("🔄 Reopening Browser and restoring session..."):
                                    chrome_process, controller = restart_browser(chrome_process, controller, current_active_profile_dir, url=last_known_url, provider=active_provider)
                                    controller.load_chat(last_known_url)
                                continue
                            except Exception as e:
                                ui.show_error(f"Auto-Recovery failed: {e}")
                                break
                        
                        # Measure turn token usage and update persistent ledger
                        sniffed_in = controller.get_last_prompt_tokens() if controller else None
                        prompt_tokens = sniffed_in if (sniffed_in and sniffed_in > 0) else tokens.calculate_tokens(base_prompt)
                        response_tokens = tokens.calculate_tokens(raw_response) if raw_response else 0
                        
                        current_profile = controller.get_current_profile() if controller else "Unknown Profile"
                        tokens.record_turn_tokens(
                            profile_dir=current_active_profile_dir,
                            prompt_tokens=prompt_tokens,
                            response_tokens=response_tokens,
                            email=current_profile if current_profile != "Unknown Profile" else "",
                            provider=active_provider
                        )
                        ui.show_status(f"⏱️ Response generated in {time.time() - start_time:.1f}s | 📊 Tokens: ~{prompt_tokens:,} in, ~{response_tokens:,} out")
                        
                        if getattr(controller, "last_source", "") == "ui":
                            ui.show_capture_notice("Not a network capture: this reply was scraped from the page (details in ui_fallback.log)")
                        current_url = controller.get_current_url()
                        if current_url: last_known_url = current_url
                        # Cut-off guard: a reply that ends inside a tool call is never run.
                        _tr_text = raw_response.split(turn_marker, 1)[-1] if (raw_response and turn_marker and turn_marker in raw_response) else raw_response
                        if _tr_text and parser.detect_truncation(_tr_text):
                            parse_retry_count += 1
                            dlog.log("CUTOFF", f"reply cut off inside tool call (attempt {parse_retry_count})")
                            if parse_retry_count >= 3:
                                ui.show_error("Last reply was cut off 3 times, command not run. Back at the prompt.")
                                parse_retry_count = 0
                                break
                            ui.show_notice("Last reply was cut off, command not run. Asking the model to resend that step.", "yellow")
                            base_prompt = f"{build_system_prompt(active_provider)}\n\n[SYSTEM NOTE: Your last reply was cut off and nothing ran. Try again.]\n\n=== USER REQUEST ===\n{user_input if 'user_input' in dir() else ''}"
                            continue
                        actions = parser.smart_parse_ai_output(raw_response, turn_marker)
                        
                        if not actions or actions in ["RATE_LIMIT", "PARSE_ERROR"]:
                            parse_retry_count += 1
                            if parse_retry_count >= 3:
                                ui.show_error(f"AI failed to format correctly after {parse_retry_count} retries. Suppressing raw XML dump.")
                                if raw_response:
                                    display_text = raw_response
                                    if turn_marker and turn_marker in display_text:
                                        display_text = display_text.split(turn_marker, 1)[-1].strip()
                                    _msg_only = ""
                                    if not re.search(r"^[ \t]*</?[a-z_]+", display_text, re.MULTILINE):
                                        _msg_only = display_text.strip()[:1500]
                                    if not _msg_only:
                                        _lt, _gt = chr(60), chr(62)
                                        _says = re.findall(_lt + "say" + _gt + "(.*?)" + _lt + "/say" + _gt, display_text, re.DOTALL)
                                        _msg_only = "\n\n".join(x.strip() for x in _says if x.strip())[:1500]
                                        _pe = "; ".join(getattr(parser, "LAST_PARSE_ERRORS", []) or [])[:400]
                                        if _pe:
                                            _msg_only = (_msg_only + "\n\n" if _msg_only else "") + "(Reply not run, parse error: " + _pe + ")"
                                    if not _msg_only:
                                        _msg_only = "[model produced malformed output; raw tag text suppressed to avoid leaking internal tool syntax]"
                                    if _msg_only:
                                        ui.show_agent_reply(_msg_only)
                                        chat_history.append({"role": "AI", "content": _msg_only})
                                        cumulative_session_history.append({"role": "AI", "content": _msg_only})
                                parse_retry_count = 0
                                break
                            ui.show_error(f"AI format invalid or empty response. Retry {parse_retry_count}/3...")
                            _perr = "; ".join(getattr(parser, "LAST_PARSE_ERRORS", []) or ["unknown format error"])[:600]
                            _rtxt = raw_response or ""
                            if turn_marker and turn_marker in _rtxt:
                                _rtxt = _rtxt.split(turn_marker, 1)[-1]
                            _rl = _rtxt.split("\n")
                            _mm = re.search(r"line (\d+)", _perr)
                            if _mm:
                                _k = int(_mm.group(1)) - 1
                                _excerpt = "\n".join(_rl[max(0, _k - 2):_k + 4])[:700]
                            else:
                                _excerpt = _rtxt[-500:]
                            base_prompt = (f"{build_system_prompt(active_provider)}\n\n"
                                f"SYSTEM ERROR (Attempt {parse_retry_count}/3): nothing ran because your last reply failed to parse.\n"
                                f"Problem: {_perr}\n"
                                "Rules: every tag starts at the beginning of a line; body tags (say, sh, write, edit, bg_start) need a closing tag; "
                                "line tags (read, ls, cd, rm, mv, grep, web, bg_list, bg_stop, confirm) end with a slash and a closing angle bracket and have nothing after them.\n"
                                f"Lines of your last reply around the problem:\n{_excerpt or 'empty'}\n\nNow try again correctly.\n\n"
                                f"=== USER REQUEST ===\n{user_input if 'user_input' in dir() else ''}")
                            continue
                        
                        # Successful parse — mark provider system prompt as delivered for this conversation
                        if active_provider == "deepseek" and not deepseek_sys_injected:
                            deepseek_sys_injected = True
                        
                        if not isinstance(actions, list): actions = [actions]
                        
                        status = "continue"
                        has_talked = False
                        combined_feedback = ""

                        # Expire stale pending elevated commands (10 minute window)
                        _now = time.time()
                        for _cid in [k for k, v in list(pending_elevated.items()) if _now - v.get("created", 0) > 600]:
                            pending_elevated.pop(_cid, None)

                        for action_obj in actions:
                            status = action_obj.get("status", status)
                            
                            if action_obj.get("message"):
                                msg = action_obj.get("message")
                                try:
                                    msg = adapters.get_adapter(active_provider).post_process_message(msg)
                                except Exception:
                                    pass
                                ui.show_agent_reply(msg)
                                chat_history.append({"role": "AI", "content": msg})
                                cumulative_session_history.append({"role": "AI", "content": msg})
                                chat_history = chat_history[-500:]
                                cumulative_session_history = cumulative_session_history[-1000:]
                                has_talked = True
                            
                            if "tools" in action_obj:
                                tools_to_run = action_obj.get("tools", list())
                            else:
                                tools_to_run = [action_obj] 
                            
                            try:
                                import websearch as _ws
                                _web_items = [(_t.get('parameters', {}).get('query', ''), _t.get('parameters', {}).get('level', 'summary')) for _t in tools_to_run if _t.get('tool') == 'web_search']
                                if len(_web_items) > 1:
                                    _ws.prefetch(_web_items, fallback=lambda _q: tools.web_search(_q, 5))
                            except Exception:
                                pass
                            for tool_call in tools_to_run:
                                tool = tool_call.get('tool', '').strip()
                                params = tool_call.get('parameters', dict())
                                
                                raw_path = params.get('path') or ''
                                abs_path = ""
                                if raw_path:
                                    if str(raw_path).startswith('~'):
                                        abs_path = os.path.expanduser(str(raw_path))
                                    elif os.path.isabs(str(raw_path)):
                                        abs_path = str(raw_path)
                                    else:
                                        abs_path = os.path.abspath(os.path.join(AI_CWD, str(raw_path)))

                                if tool != 'change_directory' and raw_path and not is_sandboxed_path(raw_path, abs_path, AI_CWD):
                                    tool_output = f"ERROR: Path traversal denied. Path '{raw_path}' escapes the project root ({AI_CWD})."
                                    ui.show_error(tool_output)
                                    feedback = get_tool_feedback(tool, tool_output)
                                    entry_content = f"Ran '{tool}' on '{raw_path}'. Result:\n{feedback}"
                                    chat_history.append({"role": "System", "content": entry_content})
                                    cumulative_session_history.append({"role": "System", "content": entry_content})
                                    combined_feedback += f"Tool '{tool}' executed. Result:\n{feedback}\n\n"
                                    continue

                                tool_output = ""
                                if tool == 'change_directory':
                                    if os.path.isdir(abs_path):
                                        AI_CWD = abs_path
                                        try:
                                            os.chdir(abs_path)
                                        except Exception:
                                            pass
                                        tool_output = f"Success: Changed working directory to {AI_CWD}"
                                    else:
                                        tool_output = f"ERROR: Directory not found: {abs_path}"
                                    ui.show_status(tool_output)
                                elif tool == 'confirm_command':
                                    _ccid = (params.get('command_id') or params.get('id') or '').strip()
                                    _dec = (params.get('decision') or '').strip()
                                    tool_output, _ran = _elevated_resolve(pending_elevated, _ccid, _dec, AI_CWD)
                                    ui.show_status(tool_output.splitlines()[0] if tool_output else 'confirmation handled')
                                elif tool == 'run_command': 
                                    cmd_str = params.get('command', '')
                                    if needs_elevation_confirm(cmd_str):
                                        _cid = _elevated_register(pending_elevated, cmd_str, AI_CWD, kind="command")
                                        tool_output = _elevated_prompt(_cid, cmd_str, kind="command")
                                        ui.show_status(f"\U0001f510 Confirmation requested ({_cid}): {cmd_str[:60]}")
                                    else:
                                        tool_output = execute_command(cmd_str, cwd=AI_CWD)
                                        ui.show_bash_output(cmd_str, tool_output)
                                elif tool == 'run_interactive': 
                                    tool_output = "ERROR: run_interactive has been removed for stability. You MUST use run_command for all shell executions."
                                    ui.show_error(tool_output)
                                elif tool == 'read_file': 
                                    s_line = int(params.get('start_line')) if params.get('start_line') else None
                                    e_line = int(params.get('end_line')) if params.get('end_line') else None
                                    s_term = params.get('search_term') or params.get('search')
                                    surr = int(params.get('surrounding_lines')) if params.get('surrounding_lines') else 3
                                    r_all = str(params.get('read_all', '')).strip().lower() == 'true'
                                    tool_output = tools.read_file(abs_path, s_line, e_line, s_term, surr, read_all=r_all)
                                    ui.show_file_read(abs_path, tool_output)
                                elif tool == 'write_file':
                                    content_val = params.get('content')
                                    if not abs_path:
                                        tool_output = "ERROR: write_file requires a valid 'path' parameter — got empty or missing path."
                                        ui.show_error(tool_output)
                                    elif content_val is None:
                                        tool_output = "ERROR: write_file requires a 'content' parameter."
                                        ui.show_error(tool_output)
                                    else:
                                        tool_output = tools.write_file(abs_path, content_val)
                                        ui.show_file_write(abs_path, tool_output)
                                elif tool == 'edit_file': 
                                    search_val = params.get('search') if 'search' in params else params.get('search_text')
                                    if 'search' in params and (search_val is None or not str(search_val).strip()):
                                        search_val = ""
                                    replace_val = params.get('replace', "") if 'replace' in params else (params.get('replace_text', "") if 'replace_text' in params else None)
                                    
                                    s_line = int(params.get('start_line')) if params.get('start_line') else None
                                    e_line = int(params.get('end_line')) if params.get('end_line') else None
                                    s_str = params.get('start_string')
                                    e_str = params.get('end_string')
                                    tool_output = tools.edit_file(abs_path, search_val, replace_val, s_line, e_line, s_str, e_str)
                                    ui.show_file_edit(abs_path, tool_output)
                                elif tool == 'create_dir':
                                    tool_output = tools.create_directory(abs_path)
                                    ui.show_file_write(abs_path, tool_output)
                                elif tool == 'delete_file':
                                    if not abs_path:
                                        tool_output = "ERROR: delete_file requires a valid 'path' parameter — got empty or missing path."
                                        ui.show_error(tool_output)
                                    else:
                                        tool_output = tools.delete_item(abs_path)
                                        ui.show_file_write(abs_path, tool_output)
                                elif tool == 'rename_file':
                                    raw_new_path = params.get('new_path') or ''
                                    abs_new_path = ""
                                    if raw_new_path:
                                        if str(raw_new_path).startswith('~'):
                                            abs_new_path = os.path.expanduser(str(raw_new_path))
                                        elif os.path.isabs(str(raw_new_path)):
                                            abs_new_path = str(raw_new_path)
                                        else:
                                            abs_new_path = os.path.abspath(os.path.join(AI_CWD, str(raw_new_path)))
                                    if raw_new_path and not is_sandboxed_path(raw_new_path, abs_new_path, AI_CWD):
                                        tool_output = f"ERROR: Path traversal denied. Destination '{raw_new_path}' escapes the project root ({AI_CWD})."
                                        ui.show_error(tool_output)
                                        feedback = get_tool_feedback(tool, tool_output)
                                        entry_content = f"Ran '{tool}' on '{raw_new_path}'. Result:\n{feedback}"
                                        chat_history.append({"role": "System", "content": entry_content})
                                        cumulative_session_history.append({"role": "System", "content": entry_content})
                                        combined_feedback += f"Tool '{tool}' executed. Result:\n{feedback}\n\n"
                                        continue
                                    tool_output = tools.rename_item(abs_path, abs_new_path)
                                    ui.show_file_edit(f"{abs_path} -> {abs_new_path}", tool_output)
                                elif tool == 'list_dir': 
                                    target_dir = abs_path if abs_path else AI_CWD
                                    tool_output = tools.list_directory(target_dir)
                                    ui.show_bash_output(f"ls {target_dir}", tool_output)
                                elif tool == 'search_text': 
                                    target_dir = abs_path if abs_path else AI_CWD
                                    tool_output = tools.grep_search(target_dir, params.get('search_text'))
                                    ui.show_bash_output(f"grep '{params.get('search_text')}' in {target_dir}", tool_output)
                                elif tool == 'write_to_scratchpad': 
                                    tool_output = tools.write_to_scratchpad(params.get('content', ''), params.get('mode', 'a'))
                                    ui.show_status(tool_output)
                                elif tool == 'start_background_job':
                                    cmd_str = params.get('command', '')
                                    if needs_elevation_confirm(cmd_str):
                                        _cid = _elevated_register(pending_elevated, cmd_str, AI_CWD, kind="background")
                                        tool_output = _elevated_prompt(_cid, cmd_str, kind="background")
                                        ui.show_status(f"\U0001f510 Confirmation requested ({_cid}): {cmd_str[:60]}")
                                    else:
                                        tool_output = jobs.start_background_job(cmd_str, cwd=AI_CWD)
                                        ui.show_bash_output(f"Start Background Job", tool_output)
                                elif tool == 'stop_background_job':
                                    jid = params.get('job_id') or params.get('id') or params.get('job') or params.get('command')
                                    if not jid:
                                        tool_output = "ERROR: stop_background_job requires a 'job_id' parameter. Example: <job_id>job_abc123</job_id>"
                                        ui.show_error(tool_output)
                                    else:
                                        tool_output = jobs.stop_background_job(str(jid).strip())
                                        ui.show_bash_output(f"Stop Job {jid}", tool_output)
                                elif tool == 'list_background_jobs':
                                    tool_output = jobs.list_background_jobs()
                                    ui.show_bash_output("List Background Jobs", tool_output)
                                elif tool == 'web_search':
                                    query = params.get('query', '')
                                    num_res = int(params.get('num_results', 5))
                                    try:
                                        import websearch as _ws
                                        tool_output = _ws.web_search(query, params.get('level', 'summary'), fallback=lambda _q: tools.web_search(_q, num_res))
                                    except Exception:
                                        tool_output = tools.web_search(query, num_res)
                                    ui.show_bash_output(f"Web Search: {query}", tool_output)
                                elif tool == 'load_skill':
                                    skill_name = params.get('skill', params.get('name', 'list'))
                                    tool_output = tools.load_skill(skill_name)
                                    ui.show_bash_output(f"Load Skill: {skill_name}", tool_output)
                                elif tool in ('spawn_subagent', 'list_subagents', 'cancel_subagent'):
                                    tool_output = _run_subagent_tool(sub_mgr, tool, params)
                                    ui.show_bash_output(f"Sub-agent: {tool}", tool_output)
                                else: 
                                    tool_output = f"ERROR: Unknown tool '{tool}'."
                                    ui.show_error(tool_output)

                                feedback = get_tool_feedback(tool, tool_output)
                                target_info = abs_path if abs_path else (cmd_str if tool in ['run_command', 'start_background_job'] else (query if tool == 'web_search' else ''))
                                if target_info:
                                    entry_content = f"Ran '{tool}' on '{target_info}'. Result:\n{feedback}"
                                else:
                                    entry_content = f"Ran '{tool}'. Result:\n{feedback}"
                                chat_history.append({"role": "System", "content": entry_content})
                                cumulative_session_history.append({"role": "System", "content": entry_content})
                                chat_history = chat_history[-500:]
                                cumulative_session_history = cumulative_session_history[-1000:]
                                combined_feedback += f"Tool '{tool}' executed. Result:\n{feedback}\n\n"
                        
                        current_profile = controller.get_current_profile(profile_dir=current_active_profile_dir) if controller else "Unknown Profile"
                        if current_profile in ("Unknown Profile", "DeepSeek User", "", None):
                            fb = get_profile_email_for_dir(current_active_profile_dir)
                            if fb: current_profile = fb

                        # Auto-sync active profile dir if profile changes during execution loops
                        resolved_dir = get_chrome_profile_dir_by_email(current_profile)
                        if resolved_dir and resolved_dir != current_active_profile_dir:
                            current_active_profile_dir = resolved_dir

                        current_url = controller.get_current_url()
                        is_valid_save_state = current_url and "new_chat" not in current_url and current_profile not in ("Unknown Profile", "")
                        if pending_transfer is not None and session.is_real_chat_url(current_url):
                            pending_transfer[0] += 1
                            if pending_transfer[0] >= PENDING_CONFIRM_RESPONSES:
                                pending_transfer = None
                                ui.show_status(f'Transfer confirmed on {profile_label_for_dir(current_active_profile_dir)}. Session record updated.')

                        current_session_file = (_skip_session_save if pending_transfer is not None else session.save_current_session)(
                            first_prompt, 
                            current_url, 
                            chat_history, 
                            AI_CWD, 
                            current_profile, 
                            current_active_profile_dir, 
                            current_session_file,
                            force_update=force_session_update,
                            provider=active_provider
                        )
                        
                        # Only reset force_session_update once we have successfully written the fresh, non-new_chat details
                        if is_valid_save_state and force_session_update and pending_transfer is None:
                            force_session_update = False
                        
                        if sub_mgr is not None and combined_feedback and status != "complete":
                            try:
                                _notes = sub_mgr.drain_notices()
                            except Exception:
                                _notes = []
                            if isinstance(_notes, str):
                                _notes = [_notes] if _notes.strip() else []
                            if _notes:
                                combined_feedback += "Sub-agent notices:\n" + "\n".join(str(n) for n in _notes) + "\n\n"
                        if status == "complete" or not combined_feedback: break 
                        elif combined_feedback:
                            base_prompt = f"System Info: Current Working Directory is {AI_CWD}\n\nTool Results:\n{combined_feedback}"
                        
                    except KeyboardInterrupt:
                        # Sequential click detection inside execution loops to cleanly break and return to the main input prompt
                        dlog.log("ABORT", "turn loop interrupted by user")
                        ui.show_notice("\n⏹ Stopped. Back at the prompt.", "red")
                        if controller:
                            try:
                                controller.stop_generation()
                            except (Exception, KeyboardInterrupt):
                                pass
                        break 
            except (KeyboardInterrupt, EOFError):
                # Sequential multi-press Ctrl+C to force-exit the entire CLI cleanly
                dlog.log("ABORT", "outer loop Ctrl+C caught")
                curr_time = time.time()
                if curr_time - last_ctrl_c_time < 1.5:
                    dlog.log("ABORT", "force exit CLI")
                    ui.show_notice("\n⏹ Exiting CLI...", "red")
                    break
                else:
                    last_ctrl_c_time = curr_time
                    if controller:
                        try:
                            controller.stop_generation()
                        except KeyboardInterrupt:
                            dlog.log("ABORT", "Ctrl+C during stop_generation, force exit")
                            ui.show_notice("\n⏹ Exiting CLI...", "red")
                            break
                        except Exception:
                            pass
                    ui.show_notice("\n⏹ Stopped. Press Ctrl+C again quickly to exit, or type /exit.", "yellow")
                    continue
    except BrowserClosedError: ui.show_error("Browser closed.")
    finally:
        if sub_mgr is not None:
            try: sub_mgr.disable_all()
            except (Exception, KeyboardInterrupt): pass
        try: jobs.cleanup_all_jobs()
        except: pass
        try:
            current_profile = controller.get_current_profile(profile_dir=current_active_profile_dir) if (controller and controller.is_connected()) else "Unknown Profile"
            if current_profile in ("Unknown Profile", "DeepSeek User", "", None):
                fb = get_profile_email_for_dir(current_active_profile_dir)
                if fb: current_profile = fb
        except (Exception, KeyboardInterrupt):
            current_profile = get_profile_email_for_dir(current_active_profile_dir) or "Unknown Profile"
        try:
            current_url = get_url(controller) if (controller and controller.is_connected()) else None
        except (Exception, KeyboardInterrupt):
            current_url = None
        
        # Auto-sync final profile directory context on close
        resolved_dir = get_chrome_profile_dir_by_email(current_profile)
        if resolved_dir and resolved_dir != current_active_profile_dir:
            current_active_profile_dir = resolved_dir

        # Commit an unconfirmed transfer on exit when the new chat already has a real URL
        if pending_transfer is not None and session.is_real_chat_url(current_url):
            pending_transfer = None
            force_session_update = True
        current_session_file = (_skip_session_save if pending_transfer is not None else session.save_current_session)(
            first_prompt, 
            current_url, 
            chat_history, 
            AI_CWD, 
            current_profile, 
            current_active_profile_dir, 
            current_session_file,
            force_update=force_session_update,
            provider=active_provider
        )
        if pending_transfer is not None:
            ui.show_status('Transfer was never confirmed - your previous session record was left unchanged.')
        elif current_session_file: ui.show_status(f'Session saved to {current_session_file}')
        if controller:
            try: controller.close()
            except: pass
        if chrome_process:
            if os.name == 'nt':
                try: subprocess.run(f"taskkill /F /PID {chrome_process.pid} /T", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except: pass
            else:
                try: chrome_process.terminate()
                except: pass
                time.sleep(0.5)
                try: chrome_process.kill()
                except: pass
        ui.show_status("Cleanup complete.")

if __name__ == "__main__":
    main()
