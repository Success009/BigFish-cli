# tokens.py - Per-Account Token Accounting & Intelligent Routing Engine
import os
import sys
import json
import math
import time
import datetime
import tempfile

DEFAULT_LEDGER_FILE = os.path.expanduser("~/gemini_local_agent/account_token_ledger.json")

def calculate_tokens(text):
    """
    Standard LLM character-to-token ratio heuristic:
    ceil(len(text) / 3.8) for mixed natural language and source code.
    """
    if not text:
        return 0
    return math.ceil(len(str(text)) / 3.8)

def get_next_daily_reset_timestamp(now_ts=None):
    """
    Calculates unix epoch timestamp for the upcoming midnight (00:00:00 local time).
    """
    if now_ts is None:
        now_ts = time.time()
    dt = datetime.datetime.fromtimestamp(now_ts)
    next_midnight = (dt + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(next_midnight.timestamp())

def load_ledger(ledger_file=None):
    """
    Loads token ledger JSON from disk. Returns empty template if file missing or corrupt.
    """
    path = ledger_file or DEFAULT_LEDGER_FILE
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and "profiles" in data:
                return data
        except Exception:
            pass
            
    return {
        "version": "1.0",
        "profiles": {}
    }

def save_ledger(data, ledger_file=None):
    """
    Persists token ledger atomically via a tempfile to prevent corruption.
    """
    path = ledger_file or DEFAULT_LEDGER_FILE
    dir_path = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_path, exist_ok=True)
    
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix="ledger_", suffix=".tmp")
    try:
        with open(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise

def init_profile_entry(email=""):
    """
    Creates a new blank profile tracking entry conforming to the ledger schema.
    """
    return {
        "email": email or "",
        "tokens_sent": 0,
        "tokens_received": 0,
        "total_tokens": 0,
        "daily_tokens": 0,
        "daily_reset_timestamp": get_next_daily_reset_timestamp(),
        "last_used_timestamp": 0,
        "session_turns_count": 0
    }

def check_and_apply_daily_reset(profile_data, now_ts=None):
    """
    Checks if 24h daily quota interval has elapsed.
    Resets daily_tokens to 0 and advances daily_reset_timestamp if needed.
    """
    if now_ts is None:
        now_ts = time.time()
        
    reset_ts = profile_data.get("daily_reset_timestamp", 0)
    if now_ts >= reset_ts:
        profile_data["daily_tokens"] = 0
        profile_data["daily_reset_timestamp"] = get_next_daily_reset_timestamp(now_ts)
        return True
    return False

def get_profile_email_from_local_state(profile_dir, user_data_dir=None):
    """
    Queries Chrome's Local State file to discover registered email for a profile directory.
    """
    if user_data_dir is None:
        user_data_dir = os.path.expanduser("~/.config/bigfish-chrome")
            
    local_state_path = os.path.join(user_data_dir, "Local State")
    try:
        if os.path.exists(local_state_path):
            with open(local_state_path, 'r', encoding='utf-8') as f:
                local_state = json.load(f)
            return local_state.get('profile', {}).get('info_cache', {}).get(profile_dir, {}).get('user_name', '')
    except Exception:
        pass
    return ""

def record_turn_tokens(profile_dir, prompt_tokens, response_tokens, email="", ledger_file=None, provider="gemini"):
    """
    Records input and output token consumption for a single turn in the ledger,
    tracking segregated per-provider accounts and keeping DeepSeek and Gemini independent.
    """
    ledger = load_ledger(ledger_file)
    prov_key = (provider or "gemini").lower()
    
    now_ts = time.time()
    sent = int(prompt_tokens)
    received = int(response_tokens)
    turn_total = sent + received

    # Structure per-provider accounts in ledger
    providers = ledger.setdefault("providers", {})
    prov_dict = providers.setdefault(prov_key, {})

    # Every provider (gemini, claude, deepseek, future ones) keys by the real Chrome
    # profile directory so accounts stay isolated per provider. The "DeepSeek" key
    # only exists as a legacy bucket from before per-profile tracking.
    target_profile = profile_dir
    if target_profile not in prov_dict:
        if email:
            resolved_email = email
        elif prov_key == "deepseek" and target_profile == "DeepSeek":
            resolved_email = "DeepSeek User"  # legacy single-bucket key
        else:
            resolved_email = get_profile_email_from_local_state(target_profile) or (
                "" if prov_key == "gemini" else prov_key.capitalize() + " User")
        prov_dict[target_profile] = init_profile_entry(resolved_email)

    prov_entry = prov_dict[target_profile]
    if email and (not prov_entry.get("email") or prov_entry.get("email") in ("Unknown Profile", "(No Email)")):
        prov_entry["email"] = email

    check_and_apply_daily_reset(prov_entry, now_ts)
    if prov_key == "claude" and now_ts - float(prov_entry.get("last_used_timestamp", 0) or 0) >= CLAUDE_WINDOW_SECONDS:
        prov_entry["daily_tokens"] = 0  # idle for 5h: start a fresh tally
    # Claude usage window: the counter covers 5 hours from the first token after a reset.
    if prov_key == "claude" and int(prov_entry.get("daily_tokens", 0)) == 0:
        prov_entry["daily_reset_timestamp"] = int(now_ts + CLAUDE_WINDOW_SECONDS)
    prov_entry["tokens_sent"] = prov_entry.get("tokens_sent", 0) + sent
    prov_entry["tokens_received"] = prov_entry.get("tokens_received", 0) + received
    prov_entry["total_tokens"] = prov_entry["tokens_sent"] + prov_entry["tokens_received"]
    prov_entry["daily_tokens"] = prov_entry.get("daily_tokens", 0) + turn_total
    prov_entry["last_used_timestamp"] = int(now_ts)
    prov_entry["session_turns_count"] = prov_entry.get("session_turns_count", 0) + 1

    # Keep ledger["profiles"] synchronized strictly for Gemini and backward compatibility
    profiles = ledger.setdefault("profiles", {})
    if prov_key == "gemini":
        if profile_dir not in profiles:
            profiles[profile_dir] = init_profile_entry(email or get_profile_email_from_local_state(profile_dir))
        entry = profiles[profile_dir]
        check_and_apply_daily_reset(entry, now_ts)
        entry["tokens_sent"] = entry.get("tokens_sent", 0) + sent
        entry["tokens_received"] = entry.get("tokens_received", 0) + received
        entry["total_tokens"] = entry["tokens_sent"] + entry["tokens_received"]
        entry["daily_tokens"] = entry.get("daily_tokens", 0) + turn_total
        entry["last_used_timestamp"] = int(now_ts)
        entry["session_turns_count"] = entry.get("session_turns_count", 0) + 1
        if email and not entry.get("email"):
            entry["email"] = email
        ret_entry = entry
    else:
        ret_entry = prov_entry

    save_ledger(ledger, ledger_file)
    return ret_entry

def get_effective_daily_tokens(profile_data, now_ts=None, window_seconds=None):
    """
    Returns daily_tokens taking into account if daily_reset_timestamp has elapsed.
    """
    if now_ts is None:
        now_ts = time.time()
    # Sliding window (Claude): usage counts as 0 once window_seconds have passed since the last turn.
    if window_seconds and now_ts - float(profile_data.get("last_used_timestamp", 0) or 0) >= window_seconds:
        return 0
    if now_ts >= profile_data.get("daily_reset_timestamp", 0):
        return 0
    return profile_data.get("daily_tokens", 0)


# ---------------------------------------------------------------------------
# Per-account, per-provider rate-limit tracking.
# A limit on provider X for profile Y says nothing about provider Z on profile Y.
# ---------------------------------------------------------------------------
CLAUDE_WINDOW_SECONDS = 5 * 60 * 60  # Claude usage counter window
GEMINI_WINDOW_SECONDS = 24 * 60 * 60  # Gemini usage window is 24 hours
PROVIDER_WINDOW_SECONDS = {"claude": CLAUDE_WINDOW_SECONDS, "gemini": GEMINI_WINDOW_SECONDS}


def window_for(provider):
    """Sliding usage window in seconds for a provider, or None when it only uses the daily reset."""
    return PROVIDER_WINDOW_SECONDS.get((provider or "").lower())

RATE_LIMIT_WINDOW_SECONDS = 5 * 60 * 60  # Claude's usage window resets every 5 hours


def _bucket_key(profile_dir, provider):
    prov = (provider or "gemini").lower()
    return prov, profile_dir


def mark_rate_limited(profile_dir, provider, window_seconds=RATE_LIMIT_WINDOW_SECONDS,
                      reason="", ledger_file=None, now_ts=None):
    """
    Records that `provider` is rate limited on `profile_dir` until now + window.
    Only that provider's bucket is touched. Returns the unix time the limit ends.
    """
    now_ts = time.time() if now_ts is None else now_ts
    prov, key = _bucket_key(profile_dir, provider)
    ledger = load_ledger(ledger_file)
    prov_dict = ledger.setdefault("providers", {}).setdefault(prov, {})
    entry = prov_dict.get(key)
    if entry is None:
        if prov == "gemini":
            email = get_profile_email_from_local_state(key)
        else:
            email = prov.capitalize() + " User"
        entry = init_profile_entry(email)
        prov_dict[key] = entry
    entry["rate_limited_at"] = int(now_ts)
    entry["rate_limited_until"] = int(now_ts + window_seconds)
    entry["rate_limit_reason"] = str(reason)[:200]
    save_ledger(ledger, ledger_file)
    return entry["rate_limited_until"]


def clear_rate_limit(profile_dir, provider, ledger_file=None):
    """Manually clears a stored limit for one provider on one profile."""
    prov, key = _bucket_key(profile_dir, provider)
    ledger = load_ledger(ledger_file)
    entry = ledger.get("providers", {}).get(prov, {}).get(key)
    if not entry:
        return False
    for k in ("rate_limited_at", "rate_limited_until", "rate_limit_reason"):
        entry.pop(k, None)
    save_ledger(ledger, ledger_file)
    return True


def get_rate_limited_until(profile_dir, provider, ledger=None, ledger_file=None, now_ts=None):
    """Returns the unix time the limit ends, or 0 if not currently limited."""
    now_ts = time.time() if now_ts is None else now_ts
    prov, key = _bucket_key(profile_dir, provider)
    if ledger is None:
        ledger = load_ledger(ledger_file)
    entry = ledger.get("providers", {}).get(prov, {}).get(key) or {}
    until = entry.get("rate_limited_until", 0) or 0
    return until if until > now_ts else 0


def is_rate_limited(profile_dir, provider, ledger=None, ledger_file=None, now_ts=None):
    return get_rate_limited_until(profile_dir, provider, ledger, ledger_file, now_ts) > 0


def get_least_used_profile(available_profiles, current_profile, ledger=None, ledger_file=None, provider=None):
    """
    Intelligent routing algorithm: selects candidate with lowest daily token consumption.
    Tie-breaks on lowest lifetime total tokens. Excludes active profile directory.

    With `provider` set (e.g. "claude"), routing is per provider:
      * profiles currently rate limited for THAT provider are skipped,
      * usage is read from that provider's own ledger bucket,
      * if every candidate is limited, the one whose limit ends soonest is returned.
    Without `provider`, the legacy Gemini-mirror behaviour is unchanged.
    """
    candidates = [p for p in available_profiles if p != current_profile]
    if not candidates:
        return current_profile if available_profiles else None

    if ledger is None:
        ledger = load_ledger(ledger_file)

    now_ts = time.time()

    if provider is None:
        profiles_dict = ledger.get("profiles", {})
        candidates.sort(key=lambda p: (
            get_effective_daily_tokens(profiles_dict.get(p, {}), now_ts),
            profiles_dict.get(p, {}).get("total_tokens", 0)
        ))
        return candidates[0]

    prov = provider.lower()
    prov_dict = ledger.get("providers", {}).get(prov, {})
    legacy = ledger.get("profiles", {}) if prov == "gemini" else {}

    def usage(p):
        return prov_dict.get(p) or legacy.get(p) or {}

    _win = window_for(prov)
    free = [p for p in candidates if not is_rate_limited(p, prov, ledger=ledger, now_ts=now_ts)]
    if free:
        # Preferred: the next profile after the current one (wrapping around)
        # that has not been used today. Otherwise fall through to lowest usage.
        _order = list(available_profiles)
        if current_profile in _order:
            _i = _order.index(current_profile)
            _order = _order[_i + 1:] + _order[:_i]
        for _p in _order:
            if _p in free and get_effective_daily_tokens(usage(_p), now_ts, window_seconds=_win) == 0:
                return _p
        free.sort(key=lambda p: (
            get_effective_daily_tokens(usage(p), now_ts, window_seconds=_win),
            usage(p).get("total_tokens", 0),
            p
        ))
        return free[0]

    candidates.sort(key=lambda p: get_rate_limited_until(p, prov, ledger=ledger, now_ts=now_ts))
    return candidates[0]

def get_usable_profiles(available_profiles, provider=None, exclude=None, ledger=None, ledger_file=None,
                        now_ts=None, email_of=None):
    """Profiles that can take a switch right now: not `exclude`, signed in (Gemini), and not rate
    limited for this provider. If no profile has a readable account at all (Local State missing),
    the signed-in check is skipped instead of rejecting everything."""
    now_ts = time.time() if now_ts is None else now_ts
    prov = (provider or "gemini").lower()
    if ledger is None:
        ledger = load_ledger(ledger_file)
    names = list(available_profiles or [])
    pool = [p for p in names if p != exclude]
    if prov == "gemini":
        lookup = email_of or get_profile_email_from_local_state
        mails = {p: lookup(p) for p in names}
        if any(mails.values()):
            pool = [p for p in pool if mails.get(p)]
    return [p for p in pool if not is_rate_limited(p, prov, ledger=ledger, now_ts=now_ts)]


def pick_transfer_target(available_profiles, current_profile, provider=None, ledger=None, ledger_file=None,
                         now_ts=None, email_of=None):
    """Next profile for a transfer, or None when no other usable profile exists (there is no other
    profile, the others are signed out, or every one of them is rate limited)."""
    now_ts = time.time() if now_ts is None else now_ts
    prov = (provider or "gemini").lower()
    if ledger is None:
        ledger = load_ledger(ledger_file)
    usable = get_usable_profiles(available_profiles, prov, exclude=current_profile, ledger=ledger,
                                 now_ts=now_ts, email_of=email_of)
    if not usable:
        return None
    ordered = [p for p in (available_profiles or []) if p in usable or p == current_profile]
    return get_least_used_profile(ordered, current_profile, ledger=ledger, provider=prov)


def get_profiles_usage_summary(available_profiles=None, ledger=None, ledger_file=None, provider=None):
    """
    Returns structured list of usage stats across available profiles.
    If provider is 'deepseek', reports DeepSeek account usage strictly isolated from Chrome profiles.
    If provider is 'gemini' or None, reports Gemini Chrome profiles.
    """
    if ledger is None:
        ledger = load_ledger(ledger_file)
        
    now_ts = time.time()
    prov_key = provider.lower() if provider else None
    
    # Any explicit non-Gemini provider (deepseek, claude, future) is reported from
    # its own isolated provider bucket. Gemini (or no provider) keeps the legacy
    # Chrome-profile-based path below for backward compatibility.
    if prov_key and prov_key != "gemini":
        prov_profiles = ledger.get("providers", {}).get(prov_key, {})
        if not prov_profiles:
            placeholder_dir = "DeepSeek" if prov_key == "deepseek" else prov_key
            placeholder_email = "DeepSeek User" if prov_key == "deepseek" else prov_key.capitalize() + " User"
            init_profile_entry(placeholder_email)
            return [{
                "profile_dir": placeholder_dir,
                "email": placeholder_email,
                "daily_tokens": 0,
                "total_tokens": 0,
                "tokens_sent": 0,
                "tokens_received": 0,
                "last_used_timestamp": 0,
                "session_turns_count": 0
            }]
        summary = []
        for p, data in prov_profiles.items():
            entry = dict(data)
            check_and_apply_daily_reset(entry, now_ts)
            summary.append({
                "profile_dir": p,
                "email": entry.get("email") or (prov_key.capitalize() + " User"),
                "daily_tokens": get_effective_daily_tokens(entry, now_ts, window_seconds=window_for(prov_key)),
                "total_tokens": entry.get("total_tokens", 0),
                "tokens_sent": entry.get("tokens_sent", 0),
                "tokens_received": entry.get("tokens_received", 0),
                "last_used_timestamp": entry.get("last_used_timestamp", 0),
                "session_turns_count": entry.get("session_turns_count", 0)
            })
        return summary

    profiles_dict = ledger.get("profiles", {})
    gem_dict = ledger.get("providers", {}).get("gemini", {})
    target_profiles = available_profiles or list(profiles_dict.keys()) or ["Default"]
    
    summary = []
    for p in target_profiles:
        data = (gem_dict.get(p) if gem_dict else None) or profiles_dict.get(p)
        if data is None:
            email = get_profile_email_from_local_state(p)
            entry = init_profile_entry(email)
        else:
            entry = dict(data)
            if not entry.get("email") or entry.get("email") in ("DeepSeek User", "(No Email)", "Unknown Profile"):
                entry["email"] = get_profile_email_from_local_state(p)
                
        daily = get_effective_daily_tokens(entry, now_ts)
        total = entry.get("total_tokens", 0)
        sent = entry.get("tokens_sent", 0)
        received = entry.get("tokens_received", 0)
        turns = entry.get("session_turns_count", 0)
        last_used = entry.get("last_used_timestamp", 0)
        email = entry.get("email", "")
        
        summary.append({
            "profile_dir": p,
            "email": email or "(No Email)",
            "daily_tokens": daily,
            "total_tokens": total,
            "tokens_sent": sent,
            "tokens_received": received,
            "last_used_timestamp": last_used,
            "session_turns_count": turns
        })
        
    return summary


# ---------------------------------------------------------------------------
# /tokens report: every profile for ONE provider, plus provider-wide totals.
# ---------------------------------------------------------------------------
_PLACEHOLDER_EMAILS = ("", "Unknown Profile", "(No Email)", "DeepSeek User")


def _real_email(entry_email, profile_dir):
    """Returns a real account email, resolving it from Chrome's Local State when
    the stored one is only a placeholder like 'Claude User'."""
    e = entry_email or ""
    if e and e not in _PLACEHOLDER_EMAILS and not e.endswith(" User"):
        return e
    return get_profile_email_from_local_state(profile_dir) or ""


def _account_number(email):
    """user12@example.com -> 12 (the number the user thinks in)."""
    import re
    user = (email or "").split("@")[0]
    m = re.search(r"\d+", user)
    return int(m.group()) if m else None


def profile_label(profile_dir, email=""):
    """Serial display name: user13@example.com -> 'Profile 13'. Falls back to the Chrome folder name."""
    n = _account_number(email)
    return "Profile %d" % n if n is not None else (profile_dir or "?")


def get_provider_usage_report(available_profiles, provider, ledger=None, ledger_file=None, now_ts=None):
    """
    Returns {"provider", "rows", "unassigned", "today_total", "lifetime_total", "turns_total"}.
    rows: one per Chrome profile (all of them, used or not), in the given order.
    unassigned: ledger entries for this provider that match no current profile
    (counted in the totals so nothing is hidden).
    """
    now_ts = time.time() if now_ts is None else now_ts
    if ledger is None:
        ledger = load_ledger(ledger_file)
    prov = (provider or "gemini").lower()
    bucket = ledger.get("providers", {}).get(prov, {})
    legacy = ledger.get("profiles", {}) if prov == "gemini" else {}

    def entry_for(p):
        return bucket.get(p) or legacy.get(p) or {}

    rows = []
    names = list(available_profiles or [])

    for p in names:
        e = entry_for(p)
        email = _real_email(e.get("email"), p)
        rows.append({
            "profile_dir": p,
            "email": email or "(No Email)",
            "account_number": _account_number(email),
            "daily_tokens": get_effective_daily_tokens(e, now_ts, window_seconds=window_for(prov)) if e else 0,
            "total_tokens": e.get("total_tokens", 0),
            "turns": e.get("session_turns_count", 0),
            "last_used_timestamp": e.get("last_used_timestamp", 0),
            "rate_limited_until": get_rate_limited_until(p, prov, ledger=ledger, now_ts=now_ts),
        })

    unassigned = []
    known = set(names)
    for p, e in bucket.items():
        if p not in known:
            unassigned.append({
                "profile_dir": p,
                "daily_tokens": get_effective_daily_tokens(e, now_ts, window_seconds=window_for(prov)),
                "total_tokens": e.get("total_tokens", 0),
                "turns": e.get("session_turns_count", 0),
            })

    everything = rows + unassigned
    return {
        "provider": prov,
        "rows": rows,
        "unassigned": unassigned,
        "today_total": sum(r["daily_tokens"] for r in everything),
        "lifetime_total": sum(r["total_tokens"] for r in everything),
        "turns_total": sum(r.get("turns", 0) for r in everything),
    }
