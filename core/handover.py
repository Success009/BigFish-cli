"""Split out of main.py (unchanged logic)."""
import os, re, json
import session
from profiles import get_latest_session

def load_session_by_filename(filename):
    """Loads and parses a session JSON file directly by filename."""
    session_dir = session.SESSION_DIR
    filepath = os.path.join(session_dir, filename)
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
        url = data.get("url")
        preview = data.get("preview", "")
        history = data.get("history", list())
        cwd = data.get("cwd", os.getcwd())
        email = data.get("email", "Unknown Profile")
        profile_dir = data.get("profile_dir", "Default")
        return url, filename, preview, history, cwd, email, profile_dir
    except Exception:
        return None, None, None, list(), os.getcwd(), "Unknown Profile", "Default"

def compile_handover_packet(chat_history, available_profiles=None, current_active_profile_dir=None, current_session_file=None, cumulative_history=None):
    """
    Programmatically compiles a rich, high-density Reasoning & Action Handover (RAH) packet
    (30,000 to 50,000 tokens) from active and historical session context.
    Prevents model hallucinations by:
    1. Explicitly separating AI agent role from system harness (prohibiting fake system outputs).
    2. Mandating the one-tag-per-tool syntax from the system prompt.
    3. Instructing the model to inspect code files before continuing blindly.
    4. Providing concise action records for tools (no context-poisoning raw stdout dumps).
    5. Retaining full user instructions and model reasoning across transfers.
    """
    all_turns = []
    seen_sigs = set()

    def add_turn(t):
        if not isinstance(t, dict):
            return
        role = t.get("role")
        content = t.get("content", "")
        if role not in ["User", "AI", "System"] or not content:
            return
        sig = (role, content[:120], content[-120:], len(content))
        if sig not in seen_sigs:
            seen_sigs.add(sig)
            all_turns.append({"role": role, "content": content})

    # 1. Backfill from saved session file if available
    target_session = current_session_file or get_latest_session()
    if target_session:
        try:
            _, _, _, saved_hist, _, _, _ = load_session_by_filename(target_session)
            if saved_hist:
                for item in saved_hist:
                    add_turn(item)
        except Exception:
            pass

    # 2. Add cumulative session history if available
    if cumulative_history:
        for item in cumulative_history:
            add_turn(item)

    # 3. Add active chat history
    if chat_history:
        for item in chat_history:
            add_turn(item)

    # 4. Detect file-system modifications, read files, and commands across all turns
    modified_files = set()
    inspected_files = set()
    executed_commands = []

    for turn in all_turns:
        role = turn.get("role")
        content = turn.get("content", "")
        if role == "System":
            m_tool = re.match(r"Ran '([^']+)'(?:\s+on\s+'([^']+)'|\s+\(([^)]+)\))?\.?\s*Result:\s*\n?(.*)", content, re.DOTALL)
            tool = m_tool.group(1).strip() if m_tool else ""
            explicit_target = (m_tool.group(2) or m_tool.group(3) or "").strip() if m_tool else ""
            body = m_tool.group(4).strip() if m_tool else content

            # Check modified files
            if tool in ["write_file", "edit_file"] or "Successfully edited" in content or "Successfully wrote" in content:
                m_path = re.search(r"Successfully (?:wrote \d+ characters to|edited) ([^\s\n]+)", content)
                p = explicit_target or (m_path.group(1).rstrip('.') if m_path else "")
                if p:
                    modified_files.add(p)

            # Check inspected files
            if tool in ["read_file", "search_text"] or "read_file" in content:
                p = explicit_target
                if not p:
                    m_p = re.search(r"(?:to|in|of|path:?)\s+([/\w\.-]+)", body)
                    if m_p: p = m_p.group(1).rstrip('.')
                if p:
                    inspected_files.add(p)

            # Check commands
            if tool == "run_command" or "run_command" in content:
                cmd_txt = explicit_target
                if not cmd_txt:
                    first_line = body.split("\n")[0].strip()
                    cmd_txt = first_line[:80]
                if cmd_txt and cmd_txt not in executed_commands:
                    executed_commands.append(cmd_txt)

    # 5. Build clean, structured ledger entries
    def format_entry(turn, is_recent_action=False):
        role = turn.get("role")
        content = turn.get("content", "").strip()

        if role == "User":
            return f"[User Request]:\n{content}"

        elif role == "AI":
            if len(content) > 6000:
                compressed_ai = content[:3500] + "\n... [Middle of a long reply collapsed] ...\n" + content[-2000:]
                return f"[Previous Model Explanation & Intent]:\n{compressed_ai}"
            return f"[Previous Model Explanation & Intent]:\n{content}"

        elif role == "System":
            m = re.match(r"Ran '([^']+)'(?:\s+on\s+'(.*?)'|\s+\(([^)]+)\))?\.?\s*Result:\s*\n?(.*)", content, re.DOTALL)
            if m:
                tool = m.group(1).strip()
                explicit_target = m.group(2) or m.group(3) or ""
                result_body = m.group(4).strip()
            else:
                tool = "Action"
                explicit_target = ""
                result_body = content

            target = explicit_target
            outcome = ""

            if tool in ["write_file", "edit_file"]:
                m_path = re.search(r"Successfully (?:wrote \d+ characters to|edited) ([^\s\n]+)", result_body)
                if m_path:
                    target = m_path.group(1).rstrip('.')
                first_line = result_body.split("\n")[0].strip()
                outcome = first_line if first_line else f"Successfully modified {target}"
            elif tool == "read_file":
                if not target:
                    m_path = re.search(r"(?:to|in|of|path:?)\s+([/\w\.-]+)", result_body)
                    if m_path: target = m_path.group(1).rstrip('.')
                outcome = f"Read and inspected code in {target if target else 'file'}"
            elif tool == "run_command":
                lines = [l.strip() for l in result_body.split("\n") if l.strip()]
                outcome = " | ".join(lines[:2])[:200]
            else:
                first_line = result_body.split("\n")[0].strip()
                outcome = first_line[:200]

            entry = f"[Action Executed by Previous Model]:\n- Tool: `{tool}`\n- Target / Command: `{target if target else 'N/A'}`\n- Outcome: {outcome}"

            if is_recent_action and result_body and len(result_body) > len(outcome):
                snippet = result_body[:450].strip()
                entry += f"\n- Output Snippet:\n```\n{snippet}\n```"

            return entry

        return f"[{role}]:\n{content}"

    # 6. Budget packing: collect turns backwards up to ~25,000 tokens (~100,000 chars, max 20k-30k)
    selected_turns = []
    total_chars = 0
    MAX_CHAR_BUDGET = 100000

    for i, turn in enumerate(reversed(all_turns)):
        is_recent_action = (i < 5)
        formatted = format_entry(turn, is_recent_action=is_recent_action)
        entry_len = len(formatted) + 4

        if total_chars + entry_len > MAX_CHAR_BUDGET:
            break

        selected_turns.append(formatted)
        total_chars += entry_len

    selected_turns.reverse()

    # Anchor the initial project objective if cut off
    if all_turns:
        first_user = next((t for t in all_turns if t.get("role") == "User"), None)
        if first_user:
            first_user_formatted = format_entry(first_user, is_recent_action=False)
            if first_user_formatted not in selected_turns:
                selected_turns.insert(0, f"=== INITIAL PROJECT GOAL ===\n{first_user_formatted}\n... [Earlier intermediate history omitted for token budget] ...")

    dialogue_ledger_str = "\n\n".join(selected_turns)

    mod_str = ', '.join(sorted(modified_files)) if modified_files else 'None'
    ins_str = ', '.join(sorted(inspected_files)[-15:]) if inspected_files else 'None'
    cmd_str = ', '.join(executed_commands[-10:]) if executed_commands else 'None'

    transfer_text = f"""=== MODEL CONTEXT TRANSFER & HANDOVER BLUEPRINT ===

CRITICAL ROLE & PROTOCOL DEFINITIONS (SYSTEM DIRECTIVES):
1. YOU ARE THE AI AGENT, NOT THE SYSTEM HARNESS:
   - You are the reasoning and coding assistant. You are NOT the execution environment, terminal, or test harness.
   - You MUST NEVER output simulated system responses, fake tool execution outputs, or command output blocks.
   - When you request a tool call, the host execution harness will physically run it on the system and provide the genuine output back to you in the next turn.

2. OUTPUT SYNTAX (ONE TAG PER TOOL):
   - Follow the system prompt. Your reply is a sequence of tags, each starting at the beginning of a line.
   - Talk to the user with <say>...</say>. Run commands with <sh>...</sh>.
   - Body tags have a closing tag. Line tags such as read, ls and grep end with a slash and a closing angle bracket.
   - Stop after your last tag. Never simulate multi-turn conversations or write tool results yourself.

3. INSPECT RELEVANT CODE BEFORE CONTINUING (NO BLIND EXECUTION):
   - Review the chronological dialogue ledger below to understand what the user requested and what the previous model was doing.
   - Do NOT guess or blindly continue modifying files without knowing their current state on disk.
   - Before writing or editing code, use a read tag to inspect the key files that the previous model touched or was working on, so that you are fully grounded in the actual codebase on disk.
   - Once grounded, proceed with confidence to complete the user's objective.

=== FIRST THING TO READ ===
If IMMEDIATE_OBJECTIVES_AND_PROCESSES.md exists in the project root, read it first: it holds the goal, the user's decisions and the checklist. Continue from its first unchecked item.

=== FILE ACTION SUMMARY (Active Session) ===
- Modified Files: {mod_str}
- Inspected / Read Files: {ins_str}
- Recent Commands Run: {cmd_str}

=== DIALOGUE LEDGER ===
{dialogue_ledger_str}
"""
    return transfer_text
