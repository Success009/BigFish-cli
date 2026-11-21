# prompts.py - Agent Persona and Instructions
import os

_PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.txt")


def _load_system_prompt():
    with open(_PROMPT_FILE, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        raise RuntimeError("core/system_prompt.txt is empty")
    return text


SYSTEM_PROMPT = _load_system_prompt()
