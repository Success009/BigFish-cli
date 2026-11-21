"""subagents.runner - the sub-agent loop (plan items R2, R5-R10, R11 hook, R13 saving).

One Runner executes one task against one channel (anything with send(prompt) -> reply text).
It never imports ui, audio, elevation or executor. The real browser channel comes in phase 5;
tests use subagents.fake.FakeChannel.

Per turn: send prompt, get reply, then exactly one of
  - summary block and no tool call -> finish "done" (the HARNESS writes result.md)
  - summary block AND tool call    -> reject, nothing runs, counts as a bad reply
  - tool call(s)                   -> run through the Toolbox, feed results back
  - nothing usable                 -> bad format: send the format back, count the streak
Stops: bad-format streak, 3 identical tool calls in a row (loop), token hard stop (partial result),
very high turn cap, rate limit from the channel, cancel (checked before and after each send).
A valid reply resets the bad-format streak. A valid summary wins even past the hard stop.

result.md is written BEFORE state.json is removed. A crash in between leaves both, and the
start-up scan must treat "result exists" as finished.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
from collections import deque
from typing import List, Optional, Tuple

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
import paths  # noqa: E402,F401  (only edits sys.path; imports nothing else)
import parser as P  # noqa: E402  (core/parser.py)

from subagents import contracts as C  # noqa: E402
from subagents import prompts as PR  # noqa: E402
from subagents.toolbox import Toolbox  # noqa: E402


class ChannelError(Exception):
    """The model channel failed (page crashed, browser closed, ...)."""


class RateLimited(ChannelError):
    """The provider reported a usage limit. The run fails with reason rate_limit."""


def est_tokens(text: str) -> int:
    """Same heuristic as core/tokens.calculate_tokens (length / 3.8). An estimate, not a count."""
    return math.ceil(len(text) / 3.8) if text else 0


_OPEN_RE = re.compile(r"<" + C.SUMMARY_TAG + r"\s*>", re.IGNORECASE)
_CLOSE_RE = re.compile(r"</" + C.SUMMARY_TAG + r"\s*>", re.IGNORECASE)


_BODY_TAGS = ("write", "sh", "edit", "bg_start")


def _mask_new_bodies(text: str) -> str:
    """Blank the inside of write/sh/edit/bg_start bodies (same length, newlines kept), so a
    summary tag quoted inside a body never counts. Line based, like the parser: a body opens
    at a line that starts with its tag and ends at a line equal to its closing tag (same label)."""
    lt, gt = chr(60), chr(62)
    out = []
    closer = None
    for line in text.split("\n"):
        if closer is None:
            out.append(line)
            m = re.match("^" + lt + "(" + "|".join(_BODY_TAGS) + r")(?::([A-Za-z0-9_]+))?(?=[ \t/" + gt + "])", line)
            if m and not re.search("/[ \t]*" + gt + "[ \t]*$", line):
                label = (":" + m.group(2)) if m.group(2) else ""
                closer = lt + "/" + m.group(1) + label + gt
        else:
            if line.rstrip() == closer:
                out.append(line)
                closer = None
            else:
                out.append("x" * len(line))
    return "\n".join(out)


def extract_summary(reply: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (summary_text, None), (None, problem) or (None, None) when there is no summary tag.

    The search runs on a copy where file-content bodies and CDATA are masked, so a summary tag
    quoted inside a tool call's content never counts. Slices come from the ORIGINAL reply."""
    try:
        masked = _mask_new_bodies(reply)
    except Exception:
        masked = reply
    if len(masked) != len(reply):
        masked = reply
    om = _OPEN_RE.search(masked)
    if not om:
        return None, None
    cm = _CLOSE_RE.search(masked, om.end())
    if not cm:
        return None, "the summary block is not closed"
    body = reply[om.end():cm.start()].strip()
    if body.startswith("<![CDATA[") and body.endswith("]]>"):
        body = body[9:-3].strip()
    if not body:
        return None, "the summary block is empty"
    return body, None


class Runner:
    def __init__(self, task: C.Task, role_prompt: str, channel, toolbox: Toolbox,
                 runs_dir: Optional[str] = None, cancel: Optional[threading.Event] = None,
                 clock=time.time, resume: Optional[C.SavedState] = None):
        self.task = task.validate()
        self.role_prompt = role_prompt
        self.channel = channel
        self.toolbox = toolbox
        self.runs_dir = runs_dir
        self.cancel = cancel or threading.Event()
        self.clock = clock
        self.started = clock()
        self.turns = 0
        self.tokens = 0
        self.streak = 0
        self.wrapup_sent = False
        self.recent = deque(maxlen=3)
        self.files_read: List[str] = []
        self.last_reply = ""
        self.resume = resume
        if resume is not None:
            self.turns, self.tokens = resume.turns, resume.tokens_used
            self.streak, self.wrapup_sent = resume.bad_format_streak, resume.wrapup_sent

    # ---- files ----
    def _log(self, who: str, text: str) -> None:
        p = C.transcript_path(self.task.id, self.runs_dir)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("===== %s (turn %d, ~%d tokens) =====\n%s\n\n" % (who, self.turns, self.tokens, text))

    def _save_state(self) -> None:
        st = C.SavedState(
            id=self.task.id, status=C.RUNNING, provider=self.task.provider,
            profile=str(getattr(self.channel, "profile", "") or ""),
            chat_url=str(getattr(self.channel, "chat_url", "") or ""),
            tokens_used=self.tokens, turns=self.turns, wrapup_sent=self.wrapup_sent,
            bad_format_streak=self.streak, updated_at=self.clock())
        C.write_atomic(C.state_path(self.task.id, self.runs_dir), st.to_json())

    def _finish(self, status: str, reason: str, detail: str, summary: str,
                partial: bool = False, unfinished: Optional[List[str]] = None) -> C.Result:
        res = C.Result(
            id=self.task.id, role=self.task.role, status=status, reason=reason, detail=detail,
            summary=summary, files_changed=list(self.toolbox.files_changed),
            unfinished=list(unfinished or []), partial=partial, tokens_used=self.tokens,
            turns=self.turns, started_at=self.started, finished_at=self.clock())
        C.write_atomic(C.result_path(self.task.id, self.runs_dir), res.render())  # result first
        try:
            os.remove(C.state_path(self.task.id, self.runs_dir))                  # then drop state
        except OSError:
            pass
        return res

    def _partial_summary(self, headline: str) -> str:
        lines = [headline, ""]
        if self.files_read:
            lines.append("Files read: " + ", ".join(self.files_read[:30]))
        if self.toolbox.files_changed:
            lines.append("Files changed: " + ", ".join(self.toolbox.files_changed[:30]))
        last = " ".join(self.last_reply.split())[:1500]
        if last:
            lines += ["", "Last reply from the sub-agent (cut):", last]
        return "\n".join(lines)

    def _fail(self, reason: str, detail: str) -> C.Result:
        return self._finish(C.FAILED, reason, detail,
                            self._partial_summary("Sub-agent stopped: " + detail),
                            partial=True, unfinished=["task not finished"])

    def _cancelled(self) -> C.Result:
        d = "cancelled after %d turns" % self.turns
        return self._finish(C.CANCELLED, C.R_CANCELLED, d,
                            self._partial_summary("Sub-agent " + d + "."),
                            partial=True, unfinished=["task not finished"])

    # ---- prompt helpers ----
    def _with_wrapup(self, text: str) -> str:
        if self.tokens >= self.task.soft_tokens and not self.wrapup_sent:
            self.wrapup_sent = True
            text += "\n\n" + PR.wrapup_text()
        return text

    def _bad(self, problem: str):
        """Count a bad reply. Returns (Result, None) when the streak limit is hit, else (None, prompt)."""
        self.streak += 1
        if self.streak >= self.task.bad_format_limit:
            return self._fail(C.BAD_FORMAT, "%d invalid replies in a row (last: %s)" % (self.streak, problem)), None
        return None, self._with_wrapup(PR.bad_format_prompt(problem))

    # ---- main loop ----
    def run(self) -> C.Result:
        try:
            tp = C.task_path(self.task.id, self.runs_dir)
            if not os.path.exists(tp):
                C.write_atomic(tp, self.task.to_json())
            return self._loop()
        except Exception as e:  # a harness bug must end the run cleanly, not leave it "running"
            return self._fail(C.ERROR, "runner error: %s: %s" % (type(e).__name__, e))

    def _loop(self) -> C.Result:
        prompt = PR.resume_prompt(self.task) if self.resume else PR.first_prompt(self.role_prompt, self.task)
        while True:
            if self.cancel.is_set():
                return self._cancelled()
            if self.turns >= self.task.turn_cap:
                return self._fail(C.BUDGET, "turn cap %d reached" % self.task.turn_cap)
            self._log("PROMPT", prompt)
            try:
                reply = self.channel.send(prompt)
            except RateLimited as e:
                return self._fail(C.RATE_LIMIT, str(e) or "provider rate limit")
            except Exception as e:
                return self._fail(C.ERROR, "channel error: %s: %s" % (type(e).__name__, e))
            if self.cancel.is_set():
                return self._cancelled()
            reply = reply if isinstance(reply, str) else ""
            self.turns += 1
            self.tokens += est_tokens(prompt) + est_tokens(reply)
            self.last_reply = reply
            self._log("REPLY", reply)

            parsed = P.smart_parse_ai_output(reply) if reply.strip() else "PARSE_ERROR"
            tools = [] if isinstance(parsed, str) else list(parsed[0]["tools"])
            summary, problem = extract_summary(reply)

            if summary is not None and not tools:
                return self._finish(C.DONE, "", "finished", summary)
            if self.tokens >= self.task.hard_tokens:
                return self._fail(C.BUDGET, "token hard stop at ~%d tokens" % self.tokens)

            if summary is not None and tools:
                res, prompt = self._bad("summary and tool call in one reply")
                if res:
                    return res
                prompt = self._with_wrapup(PR.summary_with_tools_prompt())
            elif problem:
                res, prompt = self._bad(problem)
                if res:
                    return res
            elif not tools:
                res, prompt = self._bad("no tool call and no summary block")
                if res:
                    return res
            else:
                self.streak = 0
                blocks = []
                seen = set()
                unique = []
                for t in tools:
                    key = t["tool"] + json.dumps(t["parameters"], sort_keys=True, default=str)
                    if key in seen:
                        continue
                    seen.add(key)
                    unique.append(t)
                dropped = len(tools) - len(unique)
                for t in unique:
                    name, params = t["tool"], t["parameters"]
                    sig = name + json.dumps(params, sort_keys=True, default=str)
                    self.recent.append(sig)
                    if len(self.recent) == 3 and len(set(self.recent)) == 1:
                        return self._fail(C.LOOP, "the same tool call was made 3 times in a row (%s)" % name)
                    out = self.toolbox.execute(name, params)
                    if name == "read_file" and params.get("path") and params["path"] not in self.files_read:
                        self.files_read.append(str(params["path"]))
                    self._log("TOOL " + name, out)
                    blocks.append("Tool '%s' executed. Result:\n%s\n" % (name, out))
                if dropped:
                    blocks.append("Note: %d identical repeated tool call(s) in your last reply were ignored. Send each call once.\n" % dropped)
                prompt = self._with_wrapup(PR.results_prompt(blocks))
            self._save_state()
