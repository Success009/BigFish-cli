import inspect
import os
import sys
import time
import unittest
_HERE = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, '..')))
import jobs
def _job_id(result):
    for line in result.splitlines():
        if line.startswith('Job ID:'):
            return line.split(':', 1)[1].strip()
    return None
class TestJobsHardening(unittest.TestCase):
    def setUp(self):
        self._orig_active = jobs.ACTIVE_JOBS.copy()
        self._orig_finished = jobs.FINISHED_JOBS.copy()
        jobs.ACTIVE_JOBS.clear()
        jobs.FINISHED_JOBS.clear()
    def tearDown(self):
        for jid in list(jobs.ACTIVE_JOBS.keys()):
            try:
                jobs.stop_background_job(jid)
            except Exception:
                pass
        jobs.ACTIVE_JOBS.clear()
        jobs.FINISHED_JOBS.clear()
        jobs.ACTIVE_JOBS.update(self._orig_active)
        jobs.FINISHED_JOBS.update(self._orig_finished)
    def test_bad_cwd_gives_clear_error(self):
        out = jobs.start_background_job('echo hi', cwd='/nonexistent/dir/xyz')
        self.assertIn('ERROR', out)
        self.assertIn('Working directory does not exist', out)
        self.assertEqual(len(jobs.ACTIVE_JOBS), 0)
    def test_self_exited_job_listed_with_exit_code(self):
        out = jobs.start_background_job('sleep 3; exit 7')
        jid = _job_id(out)
        self.assertIsNotNone(jid, out)
        time.sleep(4.5)
        listing = jobs.list_background_jobs()
        self.assertIn('RECENTLY EXITED', listing)
        self.assertIn(jid, listing)
        self.assertIn('Exit code: 7', listing)
        self.assertNotIn(jid, jobs.ACTIVE_JOBS)
    def test_stop_already_exited_job_reports_truth(self):
        out = jobs.start_background_job('sleep 3; exit 5')
        jid = _job_id(out)
        self.assertIsNotNone(jid, out)
        time.sleep(4.5)
        res = jobs.stop_background_job(jid)
        self.assertNotIn('Successfully stopped', res)
        self.assertIn('exit code 5', res)
        res2 = jobs.stop_background_job(jid)
        self.assertIn('ERROR', res2)
        self.assertIn('exit code 5', res2)
    def test_user_stopped_job_not_in_finished(self):
        out = jobs.start_background_job('sleep 30')
        jid = _job_id(out)
        self.assertIsNotNone(jid, out)
        res = jobs.stop_background_job(jid)
        self.assertIn('Successfully stopped', res)
        self.assertNotIn(jid, jobs.FINISHED_JOBS)
