"""The real channel factory for the Manager (plan items A1/A2 follow-up).

Manager calls factory(task, saved_state_or_None) and gets a channel with send(prompt).
This factory:
  * picks the Chrome profile: the saved one on resume, else the least-used free profile for the
    role's provider (subagents.profile_pick), never one already held by a running sub;
  * builds a WebChannel whose profile copy lives at <work_root>/chrome_<task.id>;
  * records every turn's tokens in the shared ledger under that profile and provider;
  * releases the profile when the channel closes;
  * startup_sweep() stops orphan slot Chromes and deletes stale copies (subagents.orphans).
Nothing here imports ui, audio or the main loop. Core modules are imported lazily.
"""
from __future__ import annotations

import os
import threading
from typing import Callable, Dict, Iterable, Optional

from subagents import contracts as C
from subagents import orphans as ORPH
from subagents import profile_pick as PP
from subagents.channel_web import WebChannel


def _default_profiles() -> list:
    PP._tokens()  # puts core/ on sys.path via paths
    from profiles import get_all_available_profiles
    return list(get_all_available_profiles())


def _default_src_dir() -> str:
    PP._tokens()
    from profiles import get_chrome_user_data_dir
    return get_chrome_user_data_dir()


class SubFactory:
    def __init__(self, work_root: str, *, roles_dir: Optional[str] = None,
                 profiles: Optional[Callable[[], Iterable[str]]] = None,
                 src_dir: Optional[str] = None, ledger_file: Optional[str] = None,
                 channel_cls=WebChannel, channel_kwargs: Optional[dict] = None,
                 record: Optional[Callable] = None, start_pause: float = 3.0,
                 mark_limited: Optional[Callable] = None):
        self.work_root = work_root
        self.roles_dir = roles_dir
        self._profiles = profiles or _default_profiles
        self._src_dir = src_dir
        self.ledger_file = ledger_file
        self._channel_cls = channel_cls
        self._channel_kwargs = dict(channel_kwargs or {})
        self._record = record
        self._mark_limited = mark_limited
        self._lock = threading.Lock()
        self._start_gate = threading.Lock()   # browsers start one at a time
        self._start_pause = start_pause
        self._in_use: Dict[str, str] = {}   # sub id -> profile
        os.makedirs(work_root, exist_ok=True)

    def in_use(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._in_use)

    def _record_turn(self, profile, provider, p_tok, r_tok) -> None:
        if self._record is not None:
            self._record(profile, provider, p_tok, r_tok)
            return
        tk = PP._tokens()
        tk.record_turn_tokens(profile_dir=profile, prompt_tokens=p_tok, response_tokens=r_tok,
                              provider=provider, ledger_file=self.ledger_file)

    def _on_rate_limit(self, profile, provider) -> None:
        """A sub hit a provider limit: mark that profile so the picker skips it. Never raises."""
        try:
            if self._mark_limited is not None:
                self._mark_limited(profile, provider)
                return
            tk = PP._tokens()
            kw = {}
            win = tk.window_for(provider)
            if win:
                kw["window_seconds"] = win
            tk.mark_rate_limited(profile, provider, reason="sub-agent rate limit",
                                 ledger_file=self.ledger_file, **kw)
        except Exception:
            pass

    def _release(self, sub_id: str) -> None:
        with self._lock:
            self._in_use.pop(sub_id, None)

    def __call__(self, task, resume=None):
        provider = resume.provider if resume is not None and resume.provider else task.provider
        with self._lock:
            if resume is not None and resume.profile:
                profile = resume.profile
            else:
                taken = [p for sid, p in self._in_use.items() if sid != task.id]
                profile = PP.pick_profile(self._profiles(), taken, provider, ledger_file=self.ledger_file)
            self._in_use[task.id] = profile
        try:
            ch = self._channel_cls(provider, self._src_dir or _default_src_dir(), profile,
                                   self.work_root, task.id,
                                   resume_url=(resume.chat_url if resume is not None else "") or None,
                                   on_turn=self._record_turn, start_gate=self._start_gate,
                                   start_pause=self._start_pause, on_rate_limit=self._on_rate_limit,
                                   **self._channel_kwargs)
        except Exception:
            self._release(task.id)
            raise
        orig_close = ch.close
        sub_id = task.id

        def close():
            try:
                orig_close()
            finally:
                self._release(sub_id)
        ch.close = close
        return ch

    def startup_sweep(self, **kw):
        """Stop orphan slot Chromes and delete stale profile copies. Runs of this process stay."""
        return ORPH.sweep(self.work_root, live=self.in_use().keys(), **kw)
