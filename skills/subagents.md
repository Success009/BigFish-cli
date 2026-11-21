# Skill: Sub-agents (helper agents)

Use a sub-agent for work that is long, narrow and has a clear finish: finding where something is handled across many files, or making one bounded edit in isolation. Do NOT use one for a single command, a quick read, or anything that needs back-and-forth. A sub-agent cannot ask you or the user questions.

Tags below use round brackets so this file cannot run anything. In a real reply use angle brackets, at the start of a line.

## Tags
Start one (body tag, the brief is the body):
(spawn_subagent role="explore" scope="core,main.py" wait="true")
Brief text here.
(/spawn_subagent)

- role (required): explore or worker.
  - explore: read-only. Reads, lists and searches. Answers questions about code.
  - worker: edits a private copy of the files in scope. Can read, write, edit, mv, rm and sh inside that copy.
- scope: comma-separated files or folders. Required for a worker.
- write_paths: optional comma-separated repo-relative files or folders. A worker can then write only there. Other changes are refused and left out of the diff with a note.
- wait="true": you pause and the summary comes straight back. Without it you get an id at once and keep working.
- An empty brief is a parse error.

List all: (list_subagents/)
Cancel one: (cancel_subagent id="sub_xxxxxx"/)

## What a sub-agent can and cannot do
- It can use read, ls, grep. A worker also gets write, edit, mv, rm and sh.
- It cannot use web, bg_start, cd, confirm or spawn_subagent. It cannot start more sub-agents.
- It may send several tags in one reply. Exact repeats of the same call are ignored.
- It finishes with one reply that holds a summary block and no tool call.
- Worker sh runs in its own folder with a 120 second timeout and a 15000 character output cap. sudo, su and doas are blocked. A shell command is NOT truly sandboxed, so always review the diff.

## Writing the brief
The brief is the whole message the sub-agent gets. A vague brief is the usual reason a sub-agent fails. Include:
1. Goal, in one or two sentences.
2. Files in scope.
3. What done looks like, including the command that proves it for a worker.
4. What must not be touched.
5. The shape of the answer, with a length limit.

## Results
- When a sub-agent ends you get a one-line notice with the path of its result file. Read it with a normal read tag.
- A worker's changes are in a diff file next to its result file. Nothing is applied to your files automatically.

## Applying a worker's diff
1. Read the whole diff.
2. Run the tests.
3. From the repository root: patch -p1 < path/to/diff
4. Run the tests again.

## Rules
- Check a sub-agent's claims against the code before you act on them.
- Give each worker its own non-overlapping scope.
- Cancel a sub-agent that loops or is no longer needed.
- One failed or unclear sub-agent run: fix the brief and spawn again. Do not do the same work twice yourself.
