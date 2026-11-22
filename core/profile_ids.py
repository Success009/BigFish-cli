"""One place that translates between Chrome profile folders and account numbers.

Chrome names folders in creation order and never reuses numbers (Default, Profile 1 ...
Profile 14), so the folder name drifts away from the account the user knows
(successstorages13@gmail.com is account 13 but lives in the folder "Profile 14").

Everything that shows, logs, sorts or selects a profile should use this module:
  label(folder)            -> "Profile 13"   (account number, from the email)
  account_number(folder)   -> 13
  dir_for_account(13)      -> "Profile 14"   (the real folder)
  ordered_dirs()           -> folders sorted by account number 1..N
  resolve("13") / resolve("profile 13") / resolve("Profile 14") -> folder or None

It only reads Chrome's Local State (cached by file modification time) and has no
imports from the rest of the CLI, so any module can use it safely.
"""
import os
import re
import json

_cache = {}
_UNKNOWN_ORDER = 10 ** 6


def chrome_user_data_dir():
    """The Chrome data folder the CLI uses (sandbox copy inside the project if present)."""
    if os.name == "nt":
        return os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\BigFish")
    return os.path.expanduser("~/.config/bigfish-chrome")


def _info_cache(user_data_dir=None):
    """Chrome's profile.info_cache as a dict. Cached; survives a locked or broken file."""
    udd = user_data_dir or chrome_user_data_dir()
    path = os.path.join(udd, "Local State")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            info = json.load(f).get("profile", {}).get("info_cache", {})
        if not isinstance(info, dict):
            info = {}
    except Exception:
        return cached[1] if cached else {}
    _cache[path] = (mtime, info)
    return info


def account_number_from_email(email):
    """successstorages12@gmail.com -> 12, or None."""
    user = (email or "").split("@")[0]
    m = re.search(r"\d+", user)
    return int(m.group()) if m else None


def email_for_dir(folder, user_data_dir=None):
    if not folder:
        return ""
    meta = _info_cache(user_data_dir).get(folder, {})
    return (meta.get("user_name") or "") if isinstance(meta, dict) else ""


def account_number(folder, user_data_dir=None):
    return account_number_from_email(email_for_dir(folder, user_data_dir))


def label(folder, user_data_dir=None):
    """'Profile 13' for a folder whose account email ends in 13; the folder name if unknown."""
    n = account_number(folder, user_data_dir)
    return "Profile %d" % n if n is not None else (folder or "?")


def _folder_key(folder):
    m = re.search(r"\d+", folder or "")
    return int(m.group()) if m else (0 if folder == "Default" else _UNKNOWN_ORDER)


def ordered_dirs(user_data_dir=None):
    """All known folders sorted by account number (1..N); unknown accounts last."""
    info = _info_cache(user_data_dir)

    def key(folder):
        n = account_number(folder, user_data_dir)
        return (n if n is not None else _UNKNOWN_ORDER, _folder_key(folder), folder)

    return sorted(info.keys(), key=key)


def dir_for_account(number, user_data_dir=None):
    """Folder for account number N, or None."""
    try:
        number = int(number)
    except (TypeError, ValueError):
        return None
    for folder in _info_cache(user_data_dir):
        if account_number(folder, user_data_dir) == number:
            return folder
    return None


def resolve(selector, user_data_dir=None):
    """Turn what a user typed into a folder. Accepts 13, '13', 'profile 13', or an exact folder name."""
    s = str(selector or "").strip()
    if not s:
        return None
    info = _info_cache(user_data_dir)
    m = re.fullmatch(r"(?:profile\s*)?(\d+)", s, flags=re.IGNORECASE)
    if m:
        found = dir_for_account(int(m.group(1)), user_data_dir)
        if found:
            return found
    for folder in info:
        if folder.lower() == s.lower():
            return folder
    return None
