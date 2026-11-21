"""M5: the short finish line the USER sees when a parallel sub-agent ends.

The Manager already calls on_finish(notice) for every parallel sub, separately from the queue the
model reads. This module turns that notice into a compact line for the user and hands it to a print
function the harness supplies. It never prints on its own, never raises, and nothing here is sent to
the model.
"""
from __future__ import annotations

import re
from typing import Callable

_NOTICE_RE = re.compile(
    r"^\[sub-agent\] (?P<id>\S+) \((?P<role>[^)]*)\) (?P<status>\w+)"
    r"(?: \[(?P<reason>[^\]]*)\])?(?:: (?P<detail>.*?))? - result: (?P<path>.*)$"
)

MAX_LINE = 200


def finish_line(notice: str, max_detail: int = 80) -> str:
    """Compact user-facing line for one finished sub-agent notice."""
    text = (notice or "").strip()
    m = _NOTICE_RE.match(text)
    if not m:
        return text[:MAX_LINE]
    line = "sub-agent %s (%s) %s" % (m.group("id"), m.group("role"), m.group("status"))
    if m.group("reason"):
        line += " [%s]" % m.group("reason")
    detail = (m.group("detail") or "").strip()
    if detail:
        if len(detail) > max_detail:
            detail = detail[: max_detail - 3] + "..."
        line += ": " + detail
    return line + " - result: " + m.group("path").strip()


def make_printer(print_fn: Callable[[str], None]) -> Callable[[str], None]:
    """Build an on_finish callback that shows the finish line through print_fn."""
    def on_finish(notice: str) -> None:
        try:
            print_fn(finish_line(notice))
        except Exception:
            pass
    return on_finish
