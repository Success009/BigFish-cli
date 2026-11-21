"""subagents.prompts - text the runner sends to a sub-agent.

PLACEHOLDER WORDING: plan items P2-P4 will replace and tune these texts. The runner depends
only on the function names and on the tag name SUMMARY_TAG, so wording can change freely.
Tags are built from fragments so no literal tool tag sits in this source file.
"""
from __future__ import annotations

from subagents.contracts import SUMMARY_TAG, Task

LT = "<"


def T(name: str, body: str) -> str:
    return LT + name + ">" + body + LT + "/" + name + ">"


SUMMARY_OPEN = LT + SUMMARY_TAG + ">"
SUMMARY_CLOSE = LT + "/" + SUMMARY_TAG + ">"


# NEWFMT: sub-agent format help in the one-tag-per-tool syntax
def _tag(name: str, attrs: str = "", body: str = "") -> str:
    if body:
        return chr(60) + name + (" " + attrs if attrs else "") + chr(62) + body + chr(60) + "/" + name + chr(62)
    return chr(60) + name + (" " + attrs if attrs else "") + "/" + chr(62)


def format_help(allowed=None) -> str:
    """Tag help for a sub-agent. allowed: tool names from the role (None shows the read tags)."""
    allowed = set(allowed or ("read_file", "list_dir", "search_text"))
    lines = [
        "How to reply:",
        "- A tool call is one tag that starts at the beginning of a line. You may send several tags in one reply; they run in order. Do not repeat an identical call.",
        "- Reading tags:",
        _tag("read", 'path="src/a.py" lines="1-40"') + "   (also find=\"text\" around=\"3\", or all=\"true\")",
        _tag("ls", 'path="src"'),
        _tag("grep", 'dir="src" text="TODO"') + "   (plain text, one line at a time, no regex)",
    ]
    if "write_file" in allowed or "edit_file" in allowed or "run_command" in allowed:
        lines.append("- Working tags (only inside your working folder):")
    if "write_file" in allowed:
        lines.append(_tag("write", 'path="a.txt"', "\nfile text\n"))
    if "edit_file" in allowed:
        lines.append("edit: use old/new blocks, or start=\"..\" end=\"..\" attributes, or lines=\"10-20\". An edit that matches nothing or several places fails and changes nothing.")
    if "rename_file" in allowed:
        lines.append(_tag("mv", 'from="a.py" to="b.py"'))
    if "delete_file" in allowed:
        lines.append(_tag("rm", 'path="old.py"'))
    if "run_command" in allowed:
        lines.append(_tag("sh", "", "python3 -m py_compile a.py"))
    lines += [
        "- Body tags (sh, write, edit) need a closing tag. Line tags (read, ls, grep, mv, rm) end with a slash and a closing angle bracket and have nothing after them.",
        "- When the task is finished, send ONE reply that holds your final report inside a "
        + SUMMARY_OPEN + " ... " + SUMMARY_CLOSE + " block and contains NO tool call.",
        "- Never put a summary block and a tool call in the same reply.",
        "- A reply with no tool call and no summary block is invalid.",
        "- You cannot ask the user questions. If you are blocked, say so in the summary.",
        "",
    ]
    return "\n".join(lines)


def write_help() -> str:
    return (
        "To create or change a file, use write or edit with a path relative to your working folder:\n"
        + _tag("write", 'path="relative/new_file.txt"', "\nfile text here\n") + "\n"
        + chr(60) + 'edit path="a.py"' + chr(62) + "\n"
        + chr(60) + "old" + chr(62) + "\ntext to find\n" + chr(60) + "/old" + chr(62) + "\n"
        + chr(60) + "new" + chr(62) + "\nreplacement\n" + chr(60) + "/new" + chr(62) + "\n"
        + chr(60) + "/edit" + chr(62) + "\n"
        "Do not create files with shell redirects. There is no git repository here, so do not run git. "
        "Do not look at or touch anything outside your working folder."
    )


def first_prompt(role_prompt: str, task: Task) -> str:
    parts = [role_prompt.strip(), "", format_help(task.allowed_tools),
             "Allowed tools: " + ", ".join(sorted(task.allowed_tools))]
    if task.workdir:
        parts.append("Your working folder (the only place you may write or run commands): " + task.workdir)
        parts.append("It holds copies of the files in scope, at the same relative paths. Edit them there.")
        parts.append(write_help())
    if task.scope:
        parts.append("Files or folders in scope: " + ", ".join(task.scope))
    parts.append("Keep your final summary under %d characters." % task.summary_cap)
    parts += ["", "TASK:", task.goal.strip()]
    return "\n".join(parts)


def resume_prompt(task: Task) -> str:
    return (
        "This task was interrupted and is being resumed in a new session. Earlier tool results "
        "are not visible to you. Re-check anything you need, then continue. Do not redo finished work "
        "if the files show it is done.\n\n" + format_help() + "\nTASK:\n" + task.goal.strip()
    )


def results_prompt(blocks: list) -> str:
    return "Tool Results:\n" + "\n".join(blocks)


def bad_format_prompt(problem: str) -> str:
    return "Your last reply was invalid: " + problem + ".\n" + format_help()


def summary_with_tools_prompt() -> str:
    return (
        "Your last reply held a summary AND a tool call. Nothing was run. Do not summarize until you have "
        "finished the task. If you are finished, send only the summary block. Otherwise send only the tool call."
    )


def wrapup_text() -> str:
    return (
        "TOKEN BUDGET NEARLY USED. Finish quickly: stop exploring and send your summary block now, "
        "with what you found so far and what is unfinished."
    )


def main_agent_section() -> str:
    """Text for the MAIN agent's prompt (plan item P1). Tool names are fixed: no sys_ prefix."""
    return (
        "SUB-AGENTS (helper agents)\n"
        "Tools: spawn_subagent (role, brief, scope (required for a worker), optional write_paths, optional wait), "
        "list_subagents, cancel_subagent (id).\n"
        "write_paths (optional, a list of repo-relative files or folders): when set, a worker can write only there; "
        "changes anywhere else are refused, and left out of the diff with a note.\n"
        "Roles: explore (read-only: answers questions about code), worker (edits inside its own working folder).\n"
        "Use a sub-agent for work that is long, narrow and has a clear finish, such as finding where something "
        "is handled across many files. Do not use one for a single command, or for work that needs your back-and-forth.\n"
        "A good brief states: the goal, the files in scope, what done looks like, what must not be touched, "
        "and the shape of the answer. A vague brief is the usual reason a sub-agent fails. The sub-agent cannot ask you questions.\n"
        "Modes: by default the call returns an id at once and you keep working; when the sub-agent ends you get a "
        "one-line notice with the path of its result file, which you read with your normal read tool when you choose. "
        "With wait set, you pause and the summary comes straight back.\n"
        "A worker edits a private copy of the files in its scope. Its changes are in a diff file next to its result file; "
        "nothing is applied to your files automatically. Review the diff, run the tests, then apply it yourself from the repository root with patch -p1.\n"
        "Never trust a result blindly. Check a claim against the code before you act on it, and for a worker read "
        "its diff and run the tests before you keep its changes.\n"
    )
