"""The shared channel works from every worktree and never wakes an agent twice."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from duet import coord


class CoordTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / 'repo'
        self.repo.mkdir()
        run = lambda *args: subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)
        run('init', '-q', '-b', 'main')
        run('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'Initial')
        self.worktree = Path(temp.name) / 'feature'
        run('worktree', 'add', '-q', '-b', 'feature', str(self.worktree))
        self.chdir(self.repo)

    def chdir(self, path):
        previous = os.getcwd()
        os.chdir(path)
        self.addCleanup(os.chdir, previous)

    def call(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = coord.main(list(argv))
        return code, out.getvalue()

    def test_status_and_messages_are_shared_across_worktrees(self):
        self.call('status', '--agent', 'claude', '--state', 'working', '--task', 'Speed up the build')
        self.call('send', '--agent', 'claude', '--to', 'codex', '--body', 'Build is green')
        self.call('send', '--agent', 'claude', '--to', 'other', '--body', 'Not for codex')
        self.chdir(self.worktree)
        code, out = self.call('read', '--agent', 'codex')
        self.assertEqual(code, 0)
        self.assertIn('Speed up the build', out)
        self.assertIn('Build is green', out)
        self.assertNotIn('Not for codex', out)
        status = json.loads((coord.channel() / 'agents' / 'claude.json').read_text())
        self.assertEqual(status['branch'], 'main')

    def test_claim_is_exclusive_and_only_the_owner_releases(self):
        self.call('claim', '--agent', 'codex', '--resource', 'gpu', '--task', 'Benchmark')
        with self.assertRaisesRegex(SystemExit, 'Resource is taken'):
            self.call('claim', '--agent', 'claude', '--resource', 'gpu')
        with self.assertRaisesRegex(SystemExit, 'another agent'):
            self.call('release', '--agent', 'claude', '--resource', 'gpu')
        self.call('release', '--agent', 'codex', '--resource', 'gpu')
        self.call('claim', '--agent', 'claude', '--resource', 'gpu')

    def test_wait_wakes_once_per_message_and_ignores_history(self):
        self.call('send', '--agent', 'claude', '--to', 'codex', '--body', 'old news')
        code, out = self.call('wait', '--agent', 'codex', '--timeout', '0.2', '--poll', '0.05')
        self.assertEqual((code, out.strip()), (2, 'no new messages'))
        self.call('send', '--agent', 'claude', '--to', 'all', '--body', 'fresh')
        code, out = self.call('wait', '--agent', 'codex', '--timeout', '1', '--poll', '0.05')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['body'], 'fresh')
        code, _ = self.call('wait', '--agent', 'codex', '--timeout', '0.2', '--poll', '0.05')
        self.assertEqual(code, 2)

    def test_identifiers_cannot_escape_the_channel(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.call('claim', '--agent', 'codex', '--resource', '../outside')
