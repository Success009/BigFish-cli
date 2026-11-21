# editor.py - Advanced Bulletproof Editing Engine (Three-Mode Edition)
import os
import re
import shutil
import subprocess
import tempfile

def detect_indentation(line):
    """Detects leading spaces or tabs in a given string line."""
    match = re.match(r'^([ \t]*)', line)
    return match.group(1) if match else ""

def apply_smart_indentation(target_block, replace_text):
    """
    Detects the base indentation of the block being replaced,
    and dynamically applies it to the replacement lines if they lack it.
    """
    original_lines = target_block.splitlines()
    if not original_lines:
        return replace_text
        
    base_indent = detect_indentation(original_lines[0])
    replace_lines = replace_text.splitlines()
    if not replace_lines:
        return replace_text
        
    # If the first line of replacement already has indentation,
    # assume the AI is already handling indentation itself.
    first_rep_indent = detect_indentation(replace_lines[0])
    if first_rep_indent:
        return replace_text
        
    # Otherwise, apply the original base indentation to all replacement lines
    indented_lines = []
    for line in replace_lines:
        if line.strip() == "":
            indented_lines.append("") # Keep blank lines clean
        else:
            indented_lines.append(base_indent + line)
            
    # Determine the correct line endings (Windows vs Linux)
    line_ending = "\n"
    if "\r\n" in target_block:
        line_ending = "\r\n"
        
    return line_ending.join(indented_lines)

def apply_smart_indentation_at(content, block, replace_text):
    """Like apply_smart_indentation, but aware of where the match sits in the file.
    If the match starts mid-line after pure indentation, continuation lines of a
    multi-line replacement inherit that line's indent."""
    idx = content.find(block)
    if idx > 0:
        ls = content.rfind("\n", 0, idx) + 1
        prefix = content[ls:idx]
        if prefix and not prefix.strip():
            rl = replace_text.splitlines()
            if len(rl) > 1 and not detect_indentation(rl[0]):
                rest = [l for l in rl[1:] if l.strip()]
                if rest and not all(len(detect_indentation(l)) >= len(prefix) for l in rest):
                    ending = "\r\n" if "\r\n" in content else "\n"
                    out = [rl[0]] + [(prefix + l if l.strip() else "") for l in rl[1:]]
                    return ending.join(out)
    return apply_smart_indentation(block, replace_text)

def find_fuzzy_match(pattern_text, full_content):
    """Fuzzy whitespace matcher. Returns all re.Match objects found in the content."""
    normalized_search = re.sub(r'\s+', ' ', pattern_text.strip())
    escaped_search = re.escape(normalized_search)
    fuzzy_pattern = escaped_search.replace(r'\ ', r'\s*')
    return list(re.finditer(fuzzy_pattern, full_content))

def edit_file(path, search_text=None, replace_text=None, start_line=None, end_line=None, start_string=None, end_string=None):
    """
    Executes precise file edits using line-ranges, substring matching, 
    or landmark-boundary ranges.
    """
    if search_text is not None and not str(search_text).strip():
        return "ERROR: <search> parameter cannot be empty. Please provide the exact text to search and replace."
    try:
        if not os.path.exists(path):
            return f"ERROR: File not found at {path}"
            
        try:
            with open(path, 'r', encoding='utf-8', newline='') as f:
                original_content = f.read()
        except UnicodeDecodeError:
            return (f"ERROR: {path} is not valid UTF-8 text (binary or a different encoding). "
                    f"Editing it would corrupt it, so no changes were made.")
        except PermissionError:
            proc = subprocess.run(["sudo", "-n", "cat", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise PermissionError(f"Sudo read failed: {proc.stderr.decode('utf-8', errors='replace')}")
            original_content = proc.stdout.decode('utf-8', errors='replace')
            
        lines = original_content.splitlines(True)
        edit_start_line_idx = 0
        replace_str = replace_text if replace_text else ""
        if "\r\n" in original_content:
            # Keep CRLF files consistently CRLF
            replace_str = replace_str.replace("\r\n", "\n").replace("\n", "\r\n")
        
        # --- MODE 1: DETERMINISTIC LINE-RANGE EDITING ---
        if start_line is not None and end_line is not None:
            try:
                start_n, end_n = int(start_line), int(end_line)
            except (TypeError, ValueError):
                return "ERROR: <start_line> and <end_line> must be integers."
            if start_n < 1:
                return f"ERROR: start_line must be >= 1 (got {start_n})."
            if start_n > end_n:
                return f"ERROR: start_line ({start_n}) is greater than end_line ({end_n}). No changes made."
            s_idx = start_n - 1
            e_idx = min(len(lines), end_n)
            
            # Detect the dominant line ending style of the surrounding context
            line_ending = "\n"
            if lines:
                sample_line = lines[min(len(lines) - 1, max(0, s_idx - 1))]
                if sample_line.endswith("\r\n"):
                    line_ending = "\r\n"
            
            # Strict range: only start == last line + 1 may append.
            if s_idx > len(lines):
                return (f"ERROR: start_line {start_n} is beyond the end of {path} "
                        f"({len(lines)} lines). Use start_line={len(lines) + 1} to append. "
                        f"No changes made.")
            # Out-of-bounds clamping check
            if s_idx >= len(lines):
                s_idx = len(lines)
                e_idx = len(lines)
                edit_start_line_idx = len(lines)
                
                # If the file is not empty and the last line doesn't end with a newline,
                # append one to prevent trailing inline concatenation.
                if lines and not lines[-1].endswith('\n'):
                    lines[-1] += line_ending
                
            else:
                edit_start_line_idx = s_idx
                
            # Slice assignment natively handles file expansion/shrinkage
            if replace_str and not replace_str.endswith('\n'):
                replace_str += line_ending
                
            lines[s_idx:e_idx] = [replace_str]
            new_content = "".join(lines)
            
        # --- MODE 2: LANDMARK-BOUNDARY RANGE EDITING ---
        elif start_string is not None and end_string is not None:
            # 1. Locate start landmark
            start_matches = find_fuzzy_match(start_string, original_content)
            if not start_matches:
                return f"ERROR: start_string anchor '{start_string[:50]}...' was not found in {path}."
            elif len(start_matches) > 1:
                return f"ERROR: start_string is ambiguous. Matched {len(start_matches)} places in {path}."
                
            start_pos = start_matches[0].start()
            
            # 2. Locate end landmark only after the start landmark ends
            search_from = start_matches[0].end()
            remaining_content = original_content[search_from:]
            end_matches = find_fuzzy_match(end_string, remaining_content)
            if not end_matches:
                return f"ERROR: end_string anchor '{end_string[:50]}...' was not found after the start anchor."
            elif len(end_matches) > 1:
                return f"ERROR: end_string is ambiguous. Matched {len(end_matches)} places after the start anchor."

            # Calculate absolute end position in the full file content
            end_pos = search_from + end_matches[0].end()
            
            exact_match = original_content[start_pos:end_pos]
            char_idx = start_pos
            edit_start_line_idx = original_content.count('\n', 0, char_idx)
            _n_lines = exact_match.count('\n') + 1
            span_note = f"Replaced {_n_lines} line(s) starting at line {edit_start_line_idx + 1} (start anchor through end anchor).\n"
            
            final_replace = apply_smart_indentation_at(original_content, exact_match, replace_str)
            new_content = original_content[:start_pos] + final_replace + original_content[end_pos:]

        # --- MODE 3: STANDARD SUBSTRING MATCHING ---
        elif search_text is not None and replace_text is not None:
            # Try exact match first
            if search_text in original_content:
                exact_match = search_text
                # Reject if search_text is ambiguous (multiple exact occurrences)
                count = original_content.count(exact_match)
                if count > 1:
                    return (f"ERROR: The search text appears {count} times in {path}. "
                            f"Please provide a more unique search block to isolate the edit.")
                char_idx = original_content.find(exact_match)
                edit_start_line_idx = original_content.count('\n', 0, char_idx)
                
                final_replace = apply_smart_indentation_at(original_content, exact_match, replace_str)
                new_content = original_content.replace(exact_match, final_replace, 1)
            else:
                # Fall back to Fuzzy Whitespace Matching
                matches = find_fuzzy_match(search_text, original_content)
                
                if not matches:
                    return f"ERROR: No changes made. The search text '{search_text[:50]}...' was not found in {path}."
                elif len(matches) > 1:
                    match_locations = []
                    for m in matches:
                        line_num = original_content.count('\n', 0, m.start()) + 1
                        match_locations.append(f"Line {line_num}")
                    return (f"ERROR: The search block matches {len(matches)} different locations in this file ({', '.join(match_locations)}).\n"
                            f"Please provide a more unique search block to isolate the edit.")
                else:
                    exact_match = matches[0].group(0)
                    char_idx = matches[0].start()
                    edit_start_line_idx = original_content.count('\n', 0, char_idx)
                    
                    final_replace = apply_smart_indentation_at(original_content, exact_match, replace_str)
                    new_content = original_content.replace(exact_match, final_replace, 1)
        else:
            if search_text is not None and not search_text.strip():
                return "ERROR: <search> parameter cannot be empty. Please provide the exact text to search and replace."
            return "ERROR: Invalid parameters. Provide either <search> & <replace>, <start_string> & <end_string> & <replace>, or <start_line> & <end_line> & <replace>."
            
        if new_content == original_content:
            return f"No changes made to {path}: the replacement is identical to the existing content."

        # Write changes back atomically (temp file + rename), preserving mode and symlinks
        try:
            real_path = os.path.realpath(path)
            tmp_path = None
            try:
                dir_name = os.path.dirname(real_path) or "."
                fd, tmp_path = tempfile.mkstemp(prefix=".bf_edit_", dir=dir_name)
                with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
                    f.write(new_content)
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    shutil.copymode(real_path, tmp_path)
                except OSError:
                    pass
                os.replace(tmp_path, real_path)
                tmp_path = None
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        except PermissionError:
            proc = subprocess.run(["sudo", "-n", "tee", path], input=new_content.encode('utf-8'), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise PermissionError(f"Sudo write failed: {proc.stderr.decode('utf-8', errors='replace')}")
            
        # Contextual feedback loop for verification of small edits (<= 5 lines modified)
        rep_line_count = len(replace_str.splitlines())
        if rep_line_count <= 5:
            new_lines = new_content.splitlines(True)
            ctx_start = max(0, edit_start_line_idx - 15)
            ctx_end = min(len(new_lines), edit_start_line_idx + rep_line_count + 15)
            
            ctx_out = []
            for i in range(ctx_start, ctx_end):
                ctx_out.append(f"{i+1:4} | {new_lines[i]}")
                
            ctx_str = "".join(ctx_out)
            return (f"Successfully edited {path}.\n"
                    f"{locals().get('span_note', '')}"
                    f"--- Verify Your Changes Below ---\n"
                    f"{ctx_str}")
        else:
            return f"Successfully edited large block in {path}.\n{locals().get('span_note', '')}"
            
    except Exception as e:
        return f"ERROR: Editing operation failed. {str(e)}"
