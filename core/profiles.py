"""Split out of main.py (unchanged logic)."""
import os, re, json, subprocess
import session
import tokens

def get_current_chrome_profile_dir():
    user_data_dir = get_chrome_user_data_dir()
    local_state_path = os.path.join(user_data_dir, "Local State")
    
    try:
        with open(local_state_path, 'r', encoding='utf-8') as f:
            local_state = json.load(f)
        return local_state.get('profile', {}).get('last_used', 'Default')
    except Exception:
        return 'Default'

def get_chrome_profile_dir_by_email(email):
    """Programmatically queries Chrome profile info cache to find the exact directory containing email."""
    if not email or email == "Unknown Profile":
        return None
        
    user_data_dir = get_chrome_user_data_dir()
    local_state_path = os.path.join(user_data_dir, "Local State")
        
    try:
        with open(local_state_path, 'r', encoding='utf-8') as f:
            local_state = json.load(f)
        info_cache = local_state.get('profile', {}).get('info_cache', {})
        
        # Match email to its registered profile directory folder
        for profile_dir, meta in info_cache.items():
            if meta.get('user_name', '').lower() == email.lower():
                return profile_dir
    except Exception:
        pass
    
    return None

def get_running_chrome_profile_dir():
    """Detects which Chrome profile directory is currently running on the remote debugging port."""
    try:
        if os.name != 'nt':
            out = subprocess.check_output("ps aux | grep 'remote-debugging-port=9222' | grep 'profile-directory'", shell=True, text=True, stdin=subprocess.DEVNULL)
            match = re.search(r'--profile-directory=(?:"([^"]+)"|([^\s]+))', out)
            if match:
                return match.group(1) or match.group(2)
        else:
            out = subprocess.check_output(
                'powershell -Command "Get-CimInstance Win32_Process -Filter \\"CommandLine like \'%remote-debugging-port=9222%\'\\" | Select-Object -ExpandProperty CommandLine"',
                shell=True, text=True
            )
            match = re.search(r'--profile-directory=(?:"([^"]+)"|([^\s]+))', out)
            if match:
                return match.group(1) or match.group(2)
    except Exception:
        pass
    return None

def get_profile_email_for_dir(profile_dir):
    """Gets the registered account email for a profile directory."""
    if not profile_dir:
        return ""
    email = tokens.get_profile_email_from_local_state(profile_dir, user_data_dir=get_chrome_user_data_dir())
    if not email:
        email = tokens.get_profile_email_from_local_state(profile_dir)
    return email or ""

def profile_label_for_dir(profile_dir):
    return tokens.profile_label(profile_dir, get_profile_email_for_dir(profile_dir))

def get_all_available_profiles():
    """Indexes and lists all active profile folders registered in Chrome's info_cache, sorted strictly by email numbers."""
    user_data_dir = get_chrome_user_data_dir()
    local_state_path = os.path.join(user_data_dir, "Local State")
    
    profiles_map = dict()
    try:
        with open(local_state_path, 'r', encoding='utf-8') as f:
            local_state = json.load(f)
        info_cache = local_state.get('profile', {}).get('info_cache', {})
        for profile_dir, meta in info_cache.items():
            email = meta.get('user_name', '')
            profiles_map[profile_dir] = email
    except Exception:
        pass
        
    # Fallback to scanning standard directory list if Local State is locked
    if not profiles_map:
        if os.path.exists(user_data_dir):
            try:
                for item in os.listdir(user_data_dir):
                    if (item.startswith("Profile ") or item == "Default") and os.path.isdir(os.path.join(user_data_dir, item)):
                        profiles_map[item] = ""
            except Exception:
                pass
            
    # Sorting key function based strictly on sequential email digit numbers (e.g. user7@example.com -> 7)
    def profile_email_sort_key(profile_dir):
        email = profiles_map.get(profile_dir, '')
        username = email.split('@')[0] if '@' in email else email
        match = re.search(r'\d+', username)
        if match:
            return int(match.group())
            
        # Fallback to folder number if no email number is found
        num_match = re.search(r'\d+', profile_dir)
        if num_match:
            return int(num_match.group())
            
        if profile_dir == "Default":
            return 0
        return 9999
        
    try:
        sorted_profiles = sorted(list(profiles_map.keys()), key=profile_email_sort_key)
    except Exception:
        sorted_profiles = sorted(list(profiles_map.keys()))
        
    return sorted_profiles

def get_chrome_user_data_dir():
    """Resolves and targets the exact Chrome sandboxed user data folder inside your project workspace."""
    if os.name == 'nt':
        return os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\BigFish")

    # Dedicated Chrome profile folder (Chrome blocks remote debugging on its default folder)
    return os.path.expanduser("~/.config/bigfish-chrome")

def get_latest_session(provider=None):
    """Returns the filename of the active session JSON file, falling back to most recently modified."""
    return session.get_latest_session(provider=provider)
