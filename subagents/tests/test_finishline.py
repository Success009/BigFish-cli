import unittest

from subagents import contracts as C
from subagents import finishline as FL

RUNS = "/tmp/bf_runs_test"


def notice(status, reason="", detail=""):
    return C.format_notice("sub_abcdef", "explore", status, reason, detail, RUNS)


class TestFinishLine(unittest.TestCase):
    def test_done_notice(self):
        line = FL.finish_line(notice(C.DONE))
        self.assertTrue(line.startswith("sub-agent sub_abcdef (explore) done"))
        self.assertIn(" - result: ", line)
        self.assertIn(RUNS, line)
        self.assertNotIn("[", line)

    def test_failed_notice_keeps_reason_and_detail(self):
        line = FL.finish_line(notice(C.FAILED, C.BUDGET, "token hard stop"))
        self.assertIn("failed [%s]" % C.BUDGET, line)
        self.assertIn(": token hard stop", line)
        self.assertIn(" - result: ", line)

    def test_cancelled_notice(self):
        line = FL.finish_line(notice(C.CANCELLED, C.R_CANCELLED))
        self.assertIn("cancelled", line)

    def test_long_detail_is_truncated(self):
        line = FL.finish_line(notice(C.FAILED, C.ERROR, "x" * 150), max_detail=40)
        detail = line.split(": ", 1)[1].split(" - result: ")[0]
        self.assertEqual(len(detail), 40)
        self.assertTrue(detail.endswith("..."))

    def test_unrecognised_text_is_returned_capped(self):
        self.assertEqual(FL.finish_line("something else"), "something else")
        self.assertEqual(len(FL.finish_line("y" * 500)), FL.MAX_LINE)

    def test_empty_and_none(self):
        self.assertEqual(FL.finish_line(""), "")
        self.assertEqual(FL.finish_line(None), "")


class TestPrinter(unittest.TestCase):
    def test_printer_passes_the_finish_line(self):
        seen = []
        FL.make_printer(seen.append)(notice(C.DONE))
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("sub-agent sub_abcdef"))

    def test_printer_swallows_print_errors(self):
        def boom(_):
            raise RuntimeError("terminal gone")
        FL.make_printer(boom)(notice(C.DONE))  # must not raise


if __name__ == "__main__":
    unittest.main()
