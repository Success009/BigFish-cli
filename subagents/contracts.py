"""subagents.contracts - file formats, statuses and defaults for the sub-agent system.

Read docs/SUBAGENTS_PLAN.md first. This module is pure Python: it imports nothing from
the rest of the project, so it can never pull in ui/audio (they start threads at import).

On-disk layout, one folder per run:  subagents/runs/<id>/
    task.json        the job (written at spawn, never changed)
    state.json       live state for crash recovery (deleted when the run completes)
    result.md        final result, written by the HARNESS (the sub-agent never names a path)
    transcript.log   full transcript, never sent to the main agent

Formats: task.json and state.json are JSON (machine files). result.md is plain text that the
main agent reads with its normal read tool. Role files are TOML (tomllib, stdlib).
Any corrupt or truncated file raises ContractError, never a bare KeyError or ValueError.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
import tomllib
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

SCHEMA_VERSION = 1

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(ROOT_DIR, "runs")
ROLES_DIR = os.path.join(ROOT_DIR, "roles")

# ---- defaults (every one can be overridden per role file) -------------------------------
MAX_CONCURRENT = 3
BAD_FORMAT_LIMIT = 3          # consecutive bad replies before failing with bad_format
SUMMARY_CAP_CHARS = 4000      # inline summary cap for sequential mode
SOFT_TOKENS = 100_000         # wrap-up message is sent here
HARD_TOKENS = 150_000         # hard stop here (partial result written by the harness)
TURN_CAP = 500                # backstop only; loop detection and tokens are the real limits
KEEP_FINISHED = 20            # cleanup keeps this many finished runs

# ---- statuses and transitions -------------------------------------------------------------
QUEUED, RUNNING, DONE, FAILED, CANCELLED, NEEDS_RESUME = (
    "queued", "running", "done", "failed", "cancelled", "needs_resume")
STATUSES = (QUEUED, RUNNING, DONE, FAILED, CANCELLED, NEEDS_RESUME)
TERMINAL = frozenset({DONE, FAILED, CANCELLED})
TRANSITIONS: Dict[str, frozenset] = {
    QUEUED: frozenset({RUNNING, CANCELLED, FAILED}),
    RUNNING: frozenset({DONE, FAILED, CANCELLED, NEEDS_RESUME}),
    NEEDS_RESUME: frozenset({RUNNING, CANCELLED, FAILED}),
    DONE: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}

# ---- failure reasons (fixed set) ----------------------------------------------------------
BUDGET, RATE_LIMIT, BAD_FORMAT, LOOP, R_CANCELLED, CRASHED, ERROR = (
    "budget", "rate_limit", "bad_format", "loop", "cancelled", "crashed", "error")
REASONS = (BUDGET, RATE_LIMIT, BAD_FORMAT, LOOP, R_CANCELLED, CRASHED, ERROR)

# ---- tools a role may be given (whitelist) ------------------------------------------------
# Anything not listed here is refused when a role file loads. That also makes recursion
# (a sub-agent spawning sub-agents), web access, background jobs and elevated commands
# impossible to grant by editing a role file. Adding a tool here is a code change on purpose.
KNOWN_TOOLS = frozenset({
    "read_file", "list_dir", "search_text",
    "write_file", "edit_file", "create_dir",
    "delete_file", "rename_file", "run_command",
})
WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "create_dir",
    "delete_file", "rename_file", "run_command",
})

SUMMARY_TAG = "summary"   # the finish tag a sub-agent uses (see plan: how a sub-agent finishes)


class ContractError(ValueError):
    """A file or value does not match its contract."""


# ---- ids and paths ------------------------------------------------------------------------
_ID_RE = re.compile(r"^sub_[0-9a-f]{6,12}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def new_id() -> str:
    return "sub_" + secrets.token_hex(3)


def validate_id(sub_id) -> str:
    if not isinstance(sub_id, str) or not _ID_RE.match(sub_id):
        raise ContractError("bad sub-agent id: %r" % (sub_id,))
    return sub_id


def run_dir(sub_id: str, runs_dir: Optional[str] = None) -> str:
    return os.path.join(runs_dir or RUNS_DIR, validate_id(sub_id))


def task_path(sub_id, runs_dir=None):
    return os.path.join(run_dir(sub_id, runs_dir), "task.json")


def state_path(sub_id, runs_dir=None):
    return os.path.join(run_dir(sub_id, runs_dir), "state.json")


def result_path(sub_id, runs_dir=None):
    return os.path.join(run_dir(sub_id, runs_dir), "result.md")


def transcript_path(sub_id, runs_dir=None):
    return os.path.join(run_dir(sub_id, runs_dir), "transcript.log")


def work_path(sub_id, runs_dir=None):
    """A worker's private working folder (copy of its scope)."""
    return os.path.join(run_dir(sub_id, runs_dir), "work")


def base_path(sub_id, runs_dir=None):
    """Pristine copy of the scope, used only to build the diff."""
    return os.path.join(run_dir(sub_id, runs_dir), "base")


def diff_path(sub_id, runs_dir=None):
    """Unified diff of a worker's changes, written by the harness when the run ends."""
    return os.path.join(run_dir(sub_id, runs_dir), "changes.diff")


def write_atomic(path: str, text: str) -> None:
    """Write via temp file and rename, so a crash never leaves a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _one_line(s) -> str:
    return " ".join(str(s).split())


def _pos_int(d: dict, key: str, default: int) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ContractError("%s must be a positive integer, got %r" % (key, v))
    return v


def _str_list(d: dict, key: str) -> List[str]:
    v = d.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ContractError("%s must be a list of strings" % key)
    return list(v)


def _check_tools(tools: List[str]) -> None:
    bad = [t for t in tools if t not in KNOWN_TOOLS]
    if bad:
        raise ContractError("tools not allowed for sub-agents: %s" % ", ".join(sorted(bad)))


# ---- C1 task ------------------------------------------------------------------------------
@dataclass
class Task:
    id: str
    role: str
    provider: str
    goal: str                          # the brief written by the main agent
    allowed_tools: List[str]
    scope: List[str] = field(default_factory=list)   # files or dirs the brief is about
    write_paths: List[str] = field(default_factory=list)   # repo-relative paths a worker may write; empty = anywhere in its folder
    workdir: str = ""                  # assigned directory (writers are confined here)
    wait: bool = False                 # True = sequential mode
    soft_tokens: int = SOFT_TOKENS
    hard_tokens: int = HARD_TOKENS
    turn_cap: int = TURN_CAP
    bad_format_limit: int = BAD_FORMAT_LIMIT
    summary_cap: int = SUMMARY_CAP_CHARS
    created_at: float = 0.0
    schema: int = SCHEMA_VERSION

    def validate(self) -> "Task":
        validate_id(self.id)
        if not isinstance(self.role, str) or not _NAME_RE.match(self.role):
            raise ContractError("bad role name: %r" % (self.role,))
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ContractError("provider must be a non-empty string")
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ContractError("goal (the brief) must not be empty")
        if not isinstance(self.allowed_tools, list) or not all(isinstance(t, str) for t in self.allowed_tools):
            raise ContractError("allowed_tools must be a list of strings")
        _check_tools(self.allowed_tools)
        if not isinstance(self.scope, list) or not all(isinstance(s, str) for s in self.scope):
            raise ContractError("scope must be a list of strings")
        if not isinstance(self.write_paths, list) or not all(isinstance(s, str) for s in self.write_paths):
            raise ContractError("write_paths must be a list of strings")
        if not isinstance(self.workdir, str):
            raise ContractError("workdir must be a string")
        if set(self.allowed_tools) & WRITE_TOOLS and not self.workdir:
            raise ContractError("a role with write or run tools needs a workdir")
        for k in ("soft_tokens", "hard_tokens", "turn_cap", "bad_format_limit", "summary_cap"):
            v = getattr(self, k)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ContractError("%s must be a positive integer, got %r" % (k, v))
        if self.soft_tokens >= self.hard_tokens:
            raise ContractError("soft_tokens must be below hard_tokens")
        if self.schema != SCHEMA_VERSION:
            raise ContractError("unsupported task schema: %r" % (self.schema,))
        return self

    def to_json(self) -> str:
        return json.dumps(asdict(self.validate()), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Task":
        try:
            d = json.loads(text)
        except (ValueError, TypeError) as e:
            raise ContractError("task file is not valid JSON: %s" % e)
        if not isinstance(d, dict):
            raise ContractError("task file must hold a JSON object")
        known = {f for f in cls.__dataclass_fields__}
        extra = set(d) - known
        if extra:
            raise ContractError("unknown task fields: %s" % ", ".join(sorted(extra)))
        for req in ("id", "role", "provider", "goal", "allowed_tools"):
            if req not in d:
                raise ContractError("task file missing field: %s" % req)
        try:
            return cls(**d).validate()
        except TypeError as e:
            raise ContractError("task file malformed: %s" % e)


# ---- C2 result ----------------------------------------------------------------------------
@dataclass
class Result:
    id: str
    role: str
    status: str                        # done | failed | cancelled
    reason: str = ""                   # one of REASONS when failed/cancelled, "" when done
    detail: str = ""                   # one line
    summary: str = ""
    files_changed: List[str] = field(default_factory=list)
    unfinished: List[str] = field(default_factory=list)
    partial: bool = False              # True when written by the harness after a hard stop
    tokens_used: int = 0
    turns: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    def validate(self) -> "Result":
        validate_id(self.id)
        if not isinstance(self.role, str) or not _NAME_RE.match(self.role):
            raise ContractError("bad role name: %r" % (self.role,))
        if self.status not in TERMINAL:
            raise ContractError("result status must be done, failed or cancelled, got %r" % (self.status,))
        if self.status == DONE and self.reason:
            raise ContractError("a done result has no reason code")
        if self.status != DONE and self.reason not in REASONS:
            raise ContractError("a %s result needs a reason from %s" % (self.status, ", ".join(REASONS)))
        if self.status == CANCELLED and self.reason != R_CANCELLED:
            raise ContractError("a cancelled result must use reason 'cancelled'")
        return self

    def render(self) -> str:
        """The text the main agent reads. Header first, so a quick read shows the outcome."""
        self.validate()
        lines = [
            "# Sub-agent result",
            "id: " + self.id,
            "role: " + self.role,
            "status: " + self.status,
            "reason: " + (self.reason or "-"),
            "detail: " + _one_line(self.detail),
            "partial: " + ("yes" if self.partial else "no"),
            "tokens: %d" % self.tokens_used,
            "turns: %d" % self.turns,
            "started: %s" % _fmt_time(self.started_at),
            "finished: %s" % _fmt_time(self.finished_at),
            "",
            "## Summary",
            self.summary.strip(),
            "",
            "## Files changed",
        ]
        lines += ["- " + _one_line(x) for x in self.files_changed] or ["(none)"]
        lines += ["", "## Unfinished"]
        lines += ["- " + _one_line(x) for x in self.unfinished] or ["(none)"]
        return "\n".join(lines) + "\n"

    @classmethod
    def parse(cls, text: str) -> "Result":
        if not isinstance(text, str) or not text.startswith("# Sub-agent result\n"):
            raise ContractError("result file does not start with the result header")
        # Section markers: the LAST Unfinished and Files-changed headings are the real ones,
        # so a summary that itself contains such a heading cannot confuse the parser.
        i_s = text.find("\n## Summary\n")
        i_u = text.rfind("\n## Unfinished\n")
        i_f = text.rfind("\n## Files changed\n", 0, i_u) if i_u != -1 else -1
        if i_s == -1 or i_u == -1 or i_f == -1 or not (i_s < i_f < i_u):
            raise ContractError("result file is truncated or missing a section")
        head = {}
        for ln in text[:i_s].splitlines()[1:]:
            if ": " in ln:
                k, v = ln.split(": ", 1)
                head[k.strip()] = v.strip()
        for req in ("id", "role", "status", "reason", "detail", "partial", "tokens", "turns"):
            if req not in head:
                raise ContractError("result header missing field: %s" % req)
        summary = text[i_s + len("\n## Summary\n"):i_f].strip()
        files = _parse_items(text[i_f + len("\n## Files changed\n"):i_u])
        unfinished = _parse_items(text[i_u + len("\n## Unfinished\n"):])
        try:
            return cls(
                id=head["id"], role=head["role"], status=head["status"],
                reason="" if head["reason"] == "-" else head["reason"],
                detail=head["detail"], summary=summary,
                files_changed=files, unfinished=unfinished,
                partial=(head["partial"] == "yes"),
                tokens_used=int(head["tokens"]), turns=int(head["turns"]),
                started_at=_parse_time(head.get("started", "-")),
                finished_at=_parse_time(head.get("finished", "-")),
            ).validate()
        except ValueError as e:
            if isinstance(e, ContractError):
                raise
            raise ContractError("result header has a bad number: %s" % e)

    def inline_summary(self, cap: int = SUMMARY_CAP_CHARS) -> str:
        """Summary for sequential mode. Over the cap: truncate and point at the file."""
        s = self.summary.strip()
        if len(s) <= cap:
            return s
        return s[:cap].rstrip() + "\n[summary truncated; full result: %s]" % result_path(self.id)


def _parse_items(block: str) -> List[str]:
    out = []
    for ln in block.splitlines():
        ln = ln.strip()
        if ln.startswith("- "):
            out.append(ln[2:])
    return out


def _fmt_time(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) if t else "-"


def _parse_time(s: str) -> float:
    if not s or s == "-":
        return 0.0
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0.0


# ---- C3 saved state (crash recovery) -------------------------------------------------------
@dataclass
class SavedState:
    id: str
    status: str
    provider: str
    profile: str = ""                  # browser profile dir used by this sub-agent
    chat_url: str = ""                 # reopened on resume
    tokens_used: int = 0
    turns: int = 0
    wrapup_sent: bool = False
    bad_format_streak: int = 0
    updated_at: float = 0.0
    schema: int = SCHEMA_VERSION

    def validate(self) -> "SavedState":
        validate_id(self.id)
        if self.status not in STATUSES:
            raise ContractError("bad status: %r" % (self.status,))
        if self.status in TERMINAL:
            raise ContractError("state files exist only for unfinished runs, got %r" % (self.status,))
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ContractError("provider must be a non-empty string")
        for k in ("tokens_used", "turns", "bad_format_streak"):
            v = getattr(self, k)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ContractError("%s must be a non-negative integer" % k)
        if self.schema != SCHEMA_VERSION:
            raise ContractError("unsupported state schema: %r" % (self.schema,))
        return self

    def to_json(self) -> str:
        return json.dumps(asdict(self.validate()), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "SavedState":
        try:
            d = json.loads(text)
        except (ValueError, TypeError) as e:
            raise ContractError("state file is not valid JSON: %s" % e)
        if not isinstance(d, dict):
            raise ContractError("state file must hold a JSON object")
        extra = set(d) - set(cls.__dataclass_fields__)
        if extra:
            raise ContractError("unknown state fields: %s" % ", ".join(sorted(extra)))
        for req in ("id", "status", "provider"):
            if req not in d:
                raise ContractError("state file missing field: %s" % req)
        try:
            return cls(**d).validate()
        except TypeError as e:
            raise ContractError("state file malformed: %s" % e)


# ---- C4 transitions -----------------------------------------------------------------------
def can_transition(old: str, new: str) -> bool:
    return new in TRANSITIONS.get(old, frozenset())


def check_transition(old: str, new: str) -> None:
    if old not in TRANSITIONS or new not in STATUSES:
        raise ContractError("unknown status in transition %r -> %r" % (old, new))
    if not can_transition(old, new):
        raise ContractError("illegal transition %s -> %s" % (old, new))


# ---- C6 notice line -----------------------------------------------------------------------
def format_notice(sub_id: str, role: str, status: str, reason: str = "", detail: str = "",
                  runs_dir: Optional[str] = None) -> str:
    """One line for the main agent: id, role, status, result path (derived from the id)."""
    if status not in TERMINAL:
        raise ContractError("notices are only for finished runs, got %r" % (status,))
    path = result_path(sub_id, runs_dir)
    head = "[sub-agent] %s (%s) %s" % (sub_id, role, status)
    if status != DONE:
        head += " [%s]" % (reason or ERROR)
        if detail:
            head += ": " + _one_line(detail)[:160]
    return head + " - result: " + path


# ---- C7 role file (TOML) ------------------------------------------------------------------
@dataclass
class Role:
    name: str
    provider: str                      # the ONLY place a provider name appears
    tools: List[str]
    prompt: str
    soft_tokens: int = SOFT_TOKENS
    hard_tokens: int = HARD_TOKENS
    turn_cap: int = TURN_CAP
    bad_format_limit: int = BAD_FORMAT_LIMIT
    summary_cap: int = SUMMARY_CAP_CHARS

    @property
    def writes(self) -> bool:
        return bool(set(self.tools) & WRITE_TOOLS)


def parse_role(name: str, text: str) -> Role:
    if not _NAME_RE.match(name or ""):
        raise ContractError("bad role name: %r" % (name,))
    try:
        d = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ContractError("role file is not valid TOML: %s" % e)
    known = {"name", "provider", "tools", "prompt", "soft_tokens", "hard_tokens",
             "turn_cap", "bad_format_limit", "summary_cap"}
    extra = set(d) - known
    if extra:
        raise ContractError("unknown role fields: %s" % ", ".join(sorted(extra)))
    if d.get("name", name) != name:
        raise ContractError("role name %r does not match its file name %r" % (d.get("name"), name))
    provider = d.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ContractError("role needs a provider")
    prompt = d.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ContractError("role needs a prompt")
    tools = _str_list(d, "tools")
    if not tools:
        raise ContractError("role needs at least one tool")
    _check_tools(tools)
    role = Role(
        name=name, provider=provider.strip(), tools=tools, prompt=prompt,
        soft_tokens=_pos_int(d, "soft_tokens", SOFT_TOKENS),
        hard_tokens=_pos_int(d, "hard_tokens", HARD_TOKENS),
        turn_cap=_pos_int(d, "turn_cap", TURN_CAP),
        bad_format_limit=_pos_int(d, "bad_format_limit", BAD_FORMAT_LIMIT),
        summary_cap=_pos_int(d, "summary_cap", SUMMARY_CAP_CHARS),
    )
    if role.soft_tokens >= role.hard_tokens:
        raise ContractError("soft_tokens must be below hard_tokens")
    return role


def load_role(name: str, roles_dir: Optional[str] = None) -> Role:
    if not _NAME_RE.match(name or ""):
        raise ContractError("bad role name: %r" % (name,))
    path = os.path.join(roles_dir or ROLES_DIR, name + ".toml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ContractError("cannot read role %r: %s" % (name, e))
    return parse_role(name, text)


def make_task(role: Role, goal: str, scope: Optional[List[str]] = None, workdir: str = "",
              wait: bool = False, sub_id: Optional[str] = None, now: Optional[float] = None,
              write_paths: Optional[List[str]] = None) -> Task:
    """Build a validated Task from a role's defaults plus the main agent's brief."""
    return Task(
        id=sub_id or new_id(), role=role.name, provider=role.provider, goal=goal,
        allowed_tools=list(role.tools), scope=list(scope or []), write_paths=list(write_paths or []), workdir=workdir, wait=wait,
        soft_tokens=role.soft_tokens, hard_tokens=role.hard_tokens, turn_cap=role.turn_cap,
        bad_format_limit=role.bad_format_limit, summary_cap=role.summary_cap,
        created_at=time.time() if now is None else now,
    ).validate()
