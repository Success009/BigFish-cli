"""Pick the Chrome profile for a new sub-agent (plan item A1 follow-up).

Rule: among the profiles not already used by a running sub, skip those rate limited for this
provider, then take the least used one under that provider's own usage window (Claude 5 h,
Gemini 24 h, see core/tokens.py window_for). Usage is read from the same ledger the main
agent writes, so sub-agent turns and main-agent turns count together.
The main agent's profile is NOT excluded: the main agent is mostly Claude, and a sub on another
provider does not share a live session with it.
"""
from __future__ import annotations

from typing import Iterable, List, Optional


class PickError(RuntimeError):
    """No profile can be given to a new sub-agent."""


def _tokens():
    try:
        import paths  # noqa: F401  (adds core/ to sys.path when run from the repo root)
    except Exception:
        pass
    import tokens
    return tokens


def pick_profile(profiles: Iterable[str], in_use: Iterable[str], provider: str, *,
                 ledger: Optional[dict] = None, ledger_file: Optional[str] = None,
                 now_ts: Optional[float] = None) -> str:
    import time
    profiles = list(profiles)
    tk = _tokens()
    now = time.time() if now_ts is None else now_ts
    taken = set(in_use)
    free: List[str] = [p for p in profiles if p not in taken]
    if not free:
        raise PickError("no free profile: all %d are in use by running sub-agents" % len(profiles))
    if ledger is None:
        ledger = tk.load_ledger(ledger_file)
    prov = (provider or "gemini").lower()
    prov_dict = ledger.get("providers", {}).get(prov, {})
    legacy = ledger.get("profiles", {}) if prov == "gemini" else {}
    win = tk.window_for(prov)

    def usage(p):
        return prov_dict.get(p) or legacy.get(p) or {}

    ok = [p for p in free if not tk.is_rate_limited(p, prov, ledger=ledger, now_ts=now)]
    if not ok:
        free.sort(key=lambda p: (tk.get_rate_limited_until(p, prov, ledger=ledger, now_ts=now), p))
        return free[0]
    ok.sort(key=lambda p: (tk.get_effective_daily_tokens(usage(p), now, window_seconds=win),
                           usage(p).get("total_tokens", 0), p))
    return ok[0]
