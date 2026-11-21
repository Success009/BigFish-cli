"""Unit tests for jobs.py — lifecycle, list-empty, stop-nonexistent, cleanup."""
import os
import sys
import unittest
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import jobs


class TestJobsEmptyState(unittest.TestCase):
    """List when no jobs running."""

    def setUp(self):
        # Stash and clear global registry so tests are isolated
        self._orig = jobs.ACTIVE_JOBS.copy()
        jobs.ACTIVE_JOBS.clear()

    def tearDown(self):
        # Stop any jobs created during test, then restore original state
        for jid in list(jobs.ACTIVE_JOBS.keys()):
            try:
                jobs.stop_background_job(jid)
            except Exception:
                pass
        jobs.ACTIVE_JOBS.clear()
        jobs.ACTIVE_JOBS.update(self._orig)

    def test_list_jobs_empty(self):
        out = jobs.list_background_jobs()
        self.assertIn("No active background jobs", out)

    def test_stop_nonexistent_job_returns_error(self):
        out = jobs.stop_background_job("job_doesnotexist")
        self.assertIn("ERROR", out)
        self.assertIn("job_doesnotexist", out)


class TestJobLifecycle(unittest.TestCase):
    """Start, list, stop a real background process."""

    def setUp(self):
        self._orig = jobs.ACTIVE_JOBS.copy()
        jobs.ACTIVE_JOBS.clear()

    def tearDown(self):
        for jid in list(jobs.ACTIVE_JOBS.keys()):
            try:
                jobs.stop_background_job(jid)
            except Exception:
                pass
        jobs.ACTIVE_JOBS.clear()
        jobs.ACTIVE_JOBS.update(self._orig)

    def test_background_job_full_lifecycle(self):
        result = jobs.start_background_job("sleep 30")
        self.assertIn("Success: Background job started", result)

        # Extract Job ID
        job_id = None
        for line in result.splitlines():
            if line.startswith("Job ID:"):
                job_id = line.split(":", 1)[1].strip()
                break
        self.assertIsNotNone(job_id, "Could not parse Job ID from output")

        # Appears in list
        list_res = jobs.list_background_jobs()
        self.assertIn(job_id, list_res)

        # Stop it
        stop_res = jobs.stop_background_job(job_id)
        self.assertIn("Successfully stopped background job", stop_res)

        # No longer listed
        list_after = jobs.list_background_jobs()
        self.assertIn("No active background jobs", list_after)

    def test_stop_already_stopped_job(self):
        result = jobs.start_background_job("sleep 30")
        job_id = None
        for line in result.splitlines():
            if line.startswith("Job ID:"):
                job_id = line.split(":", 1)[1].strip()
                break
        if job_id:
            jobs.stop_background_job(job_id)
            # Second stop must return an error, not raise
            out = jobs.stop_background_job(job_id)
            self.assertIn("ERROR", out)

    def test_instant_crash_job_reports_error(self):
        """A command that exits immediately must report an error."""
        out = jobs.start_background_job("false")
        self.assertIn("ERROR", out)

    def test_cleanup_all_jobs(self):
        jobs.start_background_job("sleep 60")
        jobs.start_background_job("sleep 60")
        self.assertGreater(len(jobs.ACTIVE_JOBS), 0)
        jobs.cleanup_all_jobs()
        self.assertEqual(len(jobs.ACTIVE_JOBS), 0)

    def test_list_shows_command(self):
        result = jobs.start_background_job("sleep 30")
        if "Success" in result:
            list_res = jobs.list_background_jobs()
            self.assertIn("sleep 30", list_res)


if __name__ == "__main__":
    unittest.main()
