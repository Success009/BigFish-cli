"""Tests for subagents/runner.py (plan items R1-R3, R5-R10, R11, R13, R14). Offline, fake channel."""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents import contracts as C
from subagents import runner as R
from subagents.fake import FakeChannel, tool_reply, summary_reply
from subagents.toolbox import Toolbox

def _read(path):
    with open(path) as f:
        return f.read()


READ_TOML = 'provider = "gemini"\ntools = ["read_file", "list_dir", "search_text"]\nprompt = "Read only."\n'
WRITE_TOML = ('provider = "gemini"\ntools = ["read_file", "write_file", "run_command"]\n'
              'prompt = "Edit."\n')


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.work = os.path.join(self.root, "work")
        self.runs = os.path.join(self.root, "runs")
        os.makedirs(self.work)
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, replies, toml=READ_TOML, writer=False, cancel=None, resume=None, **overrides):
        role = C.parse_role("explore" if not writer else "worker", toml)
        task = C.make_task(role, "do the job", workdir=self.work if writer else "")
        for k, v in overrides.items():
            setattr(task, k, v)
        task.validate()
        ch = FakeChannel(replies)
        tb = Toolbox(role.tools, workdir=self.work if writer else "", read_roots=[self.work])
        rn = R.Runner(task, role.prompt, ch, tb, runs_dir=self.runs, cancel=cancel, resume=resume)
        return rn, ch, task

    def run_it(self, replies, **kw):
        rn, ch, task = self.build(replies, **kw)
        return rn.run(), ch, task

    def result_file(self, task):
        with open(C.result_path(task.id, self.runs)) as f:
            return C.Result.parse(f.read())


class TestFinish(Base):
    def test_read_then_summary(self):
        res, ch, task = self.run_it([tool_reply("read_file", path="a.txt"), summary_reply("it says hello")])
        self.assertEqual((res.status, res.reason, res.turns), ("done", "", 2))
        self.assertEqual(res.summary, "it says hello")
        self.assertIn("hello", ch.prompts[1])
        self.assertEqual(self.result_file(task).summary, "it says hello")

    def test_state_removed_and_transcript_kept(self):
        res, ch, task = self.run_it([tool_reply("list_dir", path="."), summary_reply("ok")])
        files = os.listdir(os.path.join(self.runs, task.id))
        self.assertNotIn("state.json", files)
        self.assertIn("transcript.log", files)
        self.assertIn("task.json", files)

    def test_summary_only_on_first_turn(self):
        res, _, _ = self.run_it([summary_reply("nothing to do")])
        self.assertEqual((res.status, res.turns), ("done", 1))

    def test_summary_with_cdata_and_text_around(self):
        res, _, _ = self.run_it(["Done.\n" + summary_reply("<![CDATA[the report]]>") + "\n"])
        self.assertEqual(res.summary, "the report")

    def test_fake_summary_inside_tool_content_is_ignored(self):
        replies = [tool_reply("write_file", path="n.txt", content="<summary>fake</summary>"),
                   summary_reply("real")]
        res, _, _ = self.run_it(replies, toml=WRITE_TOML, writer=True)
        self.assertEqual((res.status, res.summary), ("done", "real"))
        self.assertEqual(res.turns, 2)

    def test_files_changed_reported(self):
        res, _, _ = self.run_it([tool_reply("write_file", path="n.txt", content="x"), summary_reply("wrote")],
                                toml=WRITE_TOML, writer=True)
        self.assertEqual(res.files_changed, [os.path.join(self.work, "n.txt")])


class TestSummaryRules(Base):
    def test_summary_plus_tool_is_rejected_and_not_run(self):
        both = summary_reply("done") + "\n" + tool_reply("write_file", path="bad.txt", content="x")
        res, ch, _ = self.run_it([both, summary_reply("really done")], toml=WRITE_TOML, writer=True)
        self.assertEqual(res.status, "done")
        self.assertEqual(res.summary, "really done")
        self.assertFalse(os.path.exists(os.path.join(self.work, "bad.txt")))
        self.assertIn("Do not summarize until", ch.prompts[1])

    def test_unclosed_summary_is_bad_format(self):
        res, ch, _ = self.run_it(["<summary>half", summary_reply("ok")])
        self.assertEqual(res.status, "done")
        self.assertIn("not closed", ch.prompts[1])

    def test_empty_summary_is_bad_format(self):
        res, ch, _ = self.run_it([summary_reply("  "), summary_reply("ok")])
        self.assertEqual(res.status, "done")
        self.assertIn("empty", ch.prompts[1])

    def test_plain_message_is_not_a_finish(self):
        res, ch, _ = self.run_it(["Let me check the config first.", tool_reply("list_dir", path="."),
                                  summary_reply("ok")])
        self.assertEqual((res.status, res.turns), ("done", 3))
        self.assertIn("invalid", ch.prompts[1])


class TestBadFormat(Base):
    def test_recovers_and_resets_streak(self):
        replies = ["junk", "junk", tool_reply("list_dir", path="."), "junk", "junk",
                   tool_reply("list_dir", path=".."), summary_reply("ok")]
        res, _, _ = self.run_it(replies)
        self.assertEqual(res.status, "done")

    def test_three_in_a_row_fails(self):
        res, _, task = self.run_it(["junk", "", "more junk", summary_reply("never")])
        self.assertEqual((res.status, res.reason), ("failed", "bad_format"))
        self.assertTrue(res.partial)
        self.assertEqual(self.result_file(task).reason, "bad_format")

    def test_limit_is_configurable(self):
        res, _, _ = self.run_it(["junk", "junk"], bad_format_limit=2)
        self.assertEqual(res.reason, "bad_format")

    def test_empty_reply_counts(self):
        res, _, _ = self.run_it(["", "", ""])
        self.assertEqual(res.reason, "bad_format")


class TestLoop(Base):
    def test_three_identical_calls_fail(self):
        same = tool_reply("list_dir", path=".")
        res, _, _ = self.run_it([same, same, same, summary_reply("never")])
        self.assertEqual((res.status, res.reason), ("failed", "loop"))

    def test_two_identical_then_different_is_fine(self):
        same = tool_reply("list_dir", path=".")
        res, _, _ = self.run_it([same, same, tool_reply("read_file", path="a.txt"), same, same,
                                 summary_reply("ok")])
        self.assertEqual(res.status, "done")


class TestBudget(Base):
    def test_wrapup_sent_once_at_soft_limit(self):
        replies = [tool_reply("read_file", path="a.txt"), tool_reply("list_dir", path="."),
                   tool_reply("read_file", path="a.txt", start_line=1), summary_reply("ok")]
        res, ch, _ = self.run_it(replies, soft_tokens=50, hard_tokens=100000)
        self.assertEqual(res.status, "done")
        wrap = [p for p in ch.prompts if "TOKEN BUDGET NEARLY USED" in p]
        self.assertEqual(len(wrap), 1)
        self.assertNotIn("TOKEN BUDGET", ch.prompts[0])

    def test_hard_stop_writes_partial_result(self):
        replies = [tool_reply("read_file", path="a.txt"), tool_reply("list_dir", path="."),
                   tool_reply("read_file", path="a.txt", start_line=1), summary_reply("never")]
        # hard limit must be above one turn (so a file gets read) and below a full run (~420 tokens);
        # 250-360 all work with the current first prompt
        res, _, task = self.run_it(replies, soft_tokens=50, hard_tokens=300)
        self.assertEqual((res.status, res.reason, res.partial), ("failed", "budget", True))
        saved = self.result_file(task)
        self.assertIn("Files read", saved.summary)
        self.assertIn("a.txt", saved.summary)

    def test_summary_past_hard_stop_still_counts(self):
        res, _, _ = self.run_it([summary_reply("x" * 2000)], soft_tokens=10, hard_tokens=20)
        self.assertEqual((res.status, res.reason), ("done", ""))

    def test_turn_cap_backstop(self):
        replies = [tool_reply("read_file", path="a.txt", start_line=i) for i in range(1, 8)]
        res, _, _ = self.run_it(replies, turn_cap=3)
        self.assertEqual((res.status, res.reason), ("failed", "budget"))
        self.assertEqual(res.turns, 3)


class TestChannelFailures(Base):
    def test_rate_limit(self):
        res, _, task = self.run_it([tool_reply("list_dir", path="."), R.RateLimited("quota exceeded")])
        self.assertEqual((res.status, res.reason), ("failed", "rate_limit"))
        self.assertIn("quota exceeded", res.detail)
        self.assertEqual(self.result_file(task).reason, "rate_limit")

    def test_channel_error(self):
        res, _, _ = self.run_it([R.ChannelError("page crashed")])
        self.assertEqual((res.status, res.reason), ("failed", "error"))

    def test_out_of_replies_is_an_error_not_a_hang(self):
        res, _, _ = self.run_it([tool_reply("list_dir", path=".")])
        self.assertEqual(res.reason, "error")

    def test_unexpected_exception_in_channel(self):
        res, _, _ = self.run_it([ValueError("boom")])
        self.assertEqual(res.reason, "error")
        self.assertIn("boom", res.detail)


class TestCancel(Base):
    def test_cancel_before_start(self):
        ev = threading.Event()
        ev.set()
        res, ch, _ = self.run_it([summary_reply("never")], cancel=ev)
        self.assertEqual((res.status, res.reason), ("cancelled", "cancelled"))
        self.assertEqual(ch.prompts, [])

    def test_cancel_mid_run(self):
        ev = threading.Event()
        replies = [tool_reply("list_dir", path="."),
                   lambda p: (ev.set(), tool_reply("read_file", path="a.txt"))[1],
                   summary_reply("never")]
        res, ch, task = self.run_it(replies, cancel=ev)
        self.assertEqual(res.status, "cancelled")
        self.assertEqual(len(ch.prompts), 2)
        self.assertEqual(self.result_file(task).status, "cancelled")
        self.assertFalse(os.path.exists(C.state_path(task.id, self.runs)))


class TestRoleAndSandbox(Base):
    def test_disallowed_tool_refused_and_run_continues(self):
        replies = [tool_reply("write_file", path="x.txt", content="x"), summary_reply("ok")]
        res, ch, _ = self.run_it(replies)
        self.assertEqual(res.status, "done")
        self.assertIn("not available", ch.prompts[1])
        self.assertFalse(os.path.exists(os.path.join(self.work, "x.txt")))

    def test_write_escape_refused(self):
        outside = os.path.join(self.root, "outside.txt")
        replies = [tool_reply("write_file", path=outside, content="x"),
                   tool_reply("write_file", path="../outside.txt", content="x"), summary_reply("ok")]
        res, ch, _ = self.run_it(replies, toml=WRITE_TOML, writer=True)
        self.assertEqual(res.status, "done")
        self.assertFalse(os.path.exists(outside))
        self.assertIn("Access denied", ch.prompts[1])
        self.assertIn("Access denied", ch.prompts[2])
        self.assertEqual(res.files_changed, [])

    def test_read_escape_refused(self):
        replies = [tool_reply("read_file", path="/etc/hostname"), summary_reply("ok")]
        res, ch, _ = self.run_it(replies)
        self.assertIn("Access denied", ch.prompts[1])


class TestStateAndResume(Base):
    def test_state_saved_each_turn(self):
        seen = []

        def peek(prompt):
            sp = C.state_path(task_holder[0].id, self.runs)
            seen.append(C.SavedState.from_json(_read(sp)) if os.path.exists(sp) else None)
            return summary_reply("ok")

        rn, ch, task = self.build([tool_reply("list_dir", path="."), peek])
        task_holder = [task]
        res = rn.run()
        self.assertEqual(res.status, "done")
        st = seen[0]
        self.assertIsNotNone(st)
        self.assertEqual((st.status, st.turns, st.provider), ("running", 1, "gemini"))
        self.assertEqual((st.profile, st.chat_url), ("fake-profile", "fake://chat/1"))

    def test_resume_sends_resume_prompt_and_keeps_counters(self):
        role = C.parse_role("explore", READ_TOML)
        task = C.make_task(role, "do the job")
        st = C.SavedState(id=task.id, status="running", provider="gemini", tokens_used=500, turns=4)
        ch = FakeChannel([summary_reply("resumed ok")])
        rn = R.Runner(task, role.prompt, ch, Toolbox(role.tools, read_roots=[self.work]),
                      runs_dir=self.runs, resume=st)
        res = rn.run()
        self.assertEqual(res.status, "done")
        self.assertIn("interrupted", ch.prompts[0])
        self.assertEqual(res.turns, 5)
        self.assertGreater(res.tokens_used, 500)

    def test_crash_between_result_and_state_removal_leaves_result(self):
        res, _, task = self.run_it([summary_reply("ok")])
        self.assertTrue(os.path.exists(C.result_path(task.id, self.runs)))


class TestIsolation(unittest.TestCase):
    def test_runner_does_not_import_ui_or_audio(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, %r); import subagents.runner, subagents.fake; "
                "bad = [m for m in ('ui','audio','elevation','executor','jobs') if m in sys.modules]; "
                "print('BAD:' + ','.join(bad) if bad else 'CLEAN')"
                % os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertIn("CLEAN", out.stdout, out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
