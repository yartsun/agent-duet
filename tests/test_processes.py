"""Local process ownership, bounded shutdown and durable wrapper regressions."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from duet import processes, worker


class ProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run_dir = Path(temporary.name)
        # A valid historical wrapper record whose PID is absent on this host.
        processes.atomic_json(self.run_dir / 'wrapper_pid.json',
                              {'pid': 2 ** 30, 'started': 'historical wrapper'})

    def launch(self, ignore_term=False, record=True):
        script = (
            'import signal, time\n'
            + ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore_term else '')
            + 'print("ready", flush=True)\ntime.sleep(120)\n'
        )
        child = subprocess.Popen([sys.executable, '-u', '-c', script],
                                 start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

        def cleanup():
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=2)
            child.stdout.close()

        self.addCleanup(cleanup)
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        identity = processes.process_identity(child.pid)
        if record:
            processes.atomic_json(self.run_dir / 'child_pid.json',
                                  {'pid': child.pid, 'started': identity['started']})
        return child

    def test_sigterm_ignore_escalates_to_kill_and_confirms_exit(self):
        child = self.launch(ignore_term=True)
        self.assertTrue(processes.owned_alive(self.run_dir))
        begun = time.monotonic()
        with patch.object(processes.os, 'killpg', wraps=os.killpg) as kill:
            self.assertTrue(processes.terminate_owned(self.run_dir, grace=0.1))
        self.assertLess(time.monotonic() - begun, 3)
        self.assertEqual(child.wait(timeout=1), -signal.SIGKILL)
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(child.pid, signal.SIGTERM), (child.pid, signal.SIGKILL)])
        self.assertFalse(processes.owned_alive(self.run_dir))

    def test_sigterm_cooperative_process_does_not_need_kill(self):
        child = self.launch()
        with patch.object(processes.os, 'killpg', wraps=os.killpg) as kill:
            self.assertTrue(processes.terminate_owned(self.run_dir, grace=0.2))
        self.assertEqual(child.wait(timeout=1), -signal.SIGTERM)
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(child.pid, signal.SIGTERM)])

    def test_start_time_mismatch_never_signals_and_blocks_retry(self):
        child = self.launch(ignore_term=True)
        record = json.loads((self.run_dir / 'child_pid.json').read_text())
        record['started'] += ' mismatch'
        processes.atomic_json(self.run_dir / 'child_pid.json', record)
        with patch.object(processes.os, 'killpg') as kill:
            self.assertTrue(processes.owned_alive(self.run_dir))
            self.assertFalse(processes.terminate_owned(self.run_dir, grace=0.01))
        kill.assert_not_called()
        self.assertIsNone(child.poll())

    def test_group_identity_is_rechecked_before_signalling(self):
        child = self.launch(ignore_term=True)
        with patch.object(processes.os, 'getpgid', return_value=child.pid + 1), \
                patch.object(processes.os, 'killpg') as kill:
            self.assertFalse(processes.terminate_owned(self.run_dir, grace=0.01))
        kill.assert_not_called()
        self.assertIsNone(child.poll())

    def test_reused_pid_between_term_and_kill_is_not_signalled_again(self):
        child = self.launch(ignore_term=True)
        identity = processes.process_identity(child.pid)
        reused = {**identity, 'started': identity['started'] + ' reused'}
        observations = iter([identity])
        with patch.object(processes, 'process_identity',
                          side_effect=lambda pid: next(observations, reused)), \
                patch.object(processes.os, 'killpg') as kill, \
                patch.object(processes, '_KILL_WAIT', 0.03):
            self.assertFalse(processes.terminate_owned(self.run_dir, grace=0))
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(child.pid, signal.SIGTERM)])
        self.assertIsNone(child.poll())

    def test_missing_malformed_and_pending_metadata_block_retry(self):
        path = self.run_dir / 'child_pid.json'
        for contents in (None, '{', '[]', '{"pid":true,"started":"x"}',
                         '{"pid":null,"started":null,"pending":true}'):
            with self.subTest(contents=contents):
                if contents is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(contents)
                with patch.object(processes.os, 'killpg') as kill:
                    self.assertTrue(processes.owned_alive(self.run_dir))
                    self.assertFalse(processes.terminate_owned(self.run_dir, grace=0))
                kill.assert_not_called()

    def test_missing_wrapper_identity_blocks_retry_even_with_exited_child(self):
        (self.run_dir / 'wrapper_pid.json').unlink()
        processes.atomic_json(self.run_dir / 'child_pid.json',
                              {'pid': None, 'started': None, 'exited': True})
        self.assertTrue(processes.owned_alive(self.run_dir))
        self.assertFalse(processes.terminate_owned(self.run_dir, grace=0))

    def test_process_observation_failure_is_conservative(self):
        self.launch()
        with patch.object(processes, '_snapshot', return_value=None), \
                patch.object(processes.os, 'killpg') as kill:
            self.assertTrue(processes.owned_alive(self.run_dir))
            self.assertFalse(processes.terminate_owned(self.run_dir, grace=0))
        kill.assert_not_called()

    def test_zombie_counts_as_exited_but_live_group_members_do_not(self):
        pid, member = 321, 322
        processes.atomic_json(self.run_dir / 'child_pid.json', {'pid': pid, 'started': 'stamp'})
        zombie = {pid: {'pgid': pid, 'started': 'stamp', 'state': 'Z'}}
        with patch.object(processes, '_snapshot', return_value=zombie), \
                patch.object(processes.os, 'killpg') as kill:
            self.assertFalse(processes.owned_alive(self.run_dir))
            self.assertTrue(processes.terminate_owned(self.run_dir, grace=0))
        kill.assert_not_called()
        group = {**zombie, member: {'pgid': pid, 'started': 'other stamp', 'state': 'S'}}
        for snapshot in (group, {member: group[member]}):
            with self.subTest(snapshot=snapshot), \
                    patch.object(processes, '_snapshot', return_value=snapshot), \
                    patch.object(processes.os, 'killpg') as kill:
                self.assertTrue(processes.owned_alive(self.run_dir))
                self.assertFalse(processes.terminate_owned(self.run_dir, grace=0))
            kill.assert_not_called()

    def test_explicit_no_child_record_is_distinct_from_missing_metadata(self):
        processes.atomic_json(self.run_dir / 'child_pid.json',
                              {'pid': None, 'started': None, 'exited': True})
        self.assertFalse(processes.owned_alive(self.run_dir))
        self.assertTrue(processes.terminate_owned(self.run_dir, grace=0))

    def invoke_wrapper(self, launch, started=None, atomic=None):
        jobfile = self.run_dir / 'job.json'
        jobfile.write_text(json.dumps({'run_dir': str(self.run_dir), 'agent': 'codex',
                                      'worktree': str(self.run_dir), 'prompt': 'Local fake',
                                      'role': 'build', 'claude_budget': 0.5}))
        with patch.object(sys, 'argv', ['worker.py', str(jobfile)]), \
                patch.object(worker.signal, 'signal'), patch.dict(os.environ), \
                patch.object(worker, 'start_worker', side_effect=launch), \
                patch.object(worker, 'started', side_effect=started or worker.started), \
                patch.object(worker, 'atomic_json', side_effect=atomic or processes.atomic_json):
            worker.main()

    def test_wrapper_identity_failure_drains_sigterm_ignoring_child_before_exit(self):
        children = []
        def launch(*args, **kwargs):
            child = self.launch(ignore_term=True, record=False)
            children.append(child)
            return child
        def identity(pid):
            if pid == os.getpid():
                return 'wrapper start'
            raise OSError('injected ps failure after launch')
        self.invoke_wrapper(launch, started=identity)
        child = children[0]
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertNotIn(child.pid, processes._snapshot())
        result = json.loads((self.run_dir / 'exit.json').read_text())
        self.assertEqual(result['returncode'], 1)
        self.assertIn('injected ps failure', result['error'])

    def test_wrapper_pid_publication_failure_drains_child_before_exit(self):
        children = []
        def launch(*args, **kwargs):
            child = self.launch(ignore_term=True, record=False)
            children.append(child)
            return child
        def publish(path, value):
            if path.name == 'child_pid.json' and value.get('pid') and not value.get('exited'):
                raise OSError('injected metadata write failure')
            processes.atomic_json(path, value)
            if path.name == 'exit.json':
                self.assertIsNotNone(children[0].returncode)
                self.assertNotIn(children[0].pid, processes._snapshot())
        self.invoke_wrapper(launch, atomic=publish)
        self.assertEqual(children[0].returncode, -signal.SIGKILL)
        self.assertEqual(json.loads((self.run_dir / 'exit.json').read_text())['returncode'], 1)

    def test_real_wrapper_cancellation_drains_child_and_publishes_failure(self):
        jobfile = self.run_dir / 'job.json'
        jobfile.write_text(json.dumps({'run_dir': str(self.run_dir), 'agent': 'codex',
                                      'worktree': str(self.run_dir), 'prompt': 'Local fake',
                                      'role': 'build', 'claude_budget': 0.5}))
        script = '''
import subprocess, sys
from duet import worker
def launch(*args, **kwargs):
    child = subprocess.Popen([sys.executable, '-u', '-c',
        'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
        'print("ready",flush=True); time.sleep(120)'],
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    child.stdout.readline()
    child.stdout.close()
    return child
worker.start_worker = launch
worker.main()
'''
        wrapper = subprocess.Popen([sys.executable, '-B', '-c', script, str(jobfile)],
                                   start_new_session=True, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if wrapper.poll() is None:
                os.killpg(wrapper.pid, signal.SIGTERM)
                try:
                    wrapper.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(wrapper.pid, signal.SIGKILL)
            wrapper.wait(timeout=2)
            processes.terminate_owned(self.run_dir, grace=0.1)
            wrapper.stderr.close()
        self.addCleanup(cleanup)
        until = time.monotonic() + 3
        while time.monotonic() < until:
            records = processes._records(self.run_dir)
            if all(record and record.get('started') for record in records):
                break
            if wrapper.poll() is not None:
                self.fail(wrapper.stderr.read())
            time.sleep(0.01)
        else:
            self.fail('wrapper did not publish child identity')
        self.assertTrue(processes.terminate_owned(self.run_dir, grace=1.5))
        self.assertEqual(wrapper.wait(timeout=2), 0, wrapper.stderr.read())
        self.assertFalse(processes.owned_alive(self.run_dir))
        self.assertEqual(json.loads((self.run_dir / 'exit.json').read_text())['returncode'],
                         -signal.SIGTERM)

    def test_launch_failure_publishes_explicit_no_child_record(self):
        def launch(*args, **kwargs):
            raise FileNotFoundError('fake CLI absent')
        self.invoke_wrapper(launch)
        self.assertEqual(json.loads((self.run_dir / 'exit.json').read_text())['returncode'], 1)
        self.assertEqual(json.loads((self.run_dir / 'child_pid.json').read_text()),
                         {'pid': None, 'started': None, 'exited': True})

    def test_child_exiting_before_ps_preserves_successful_exit(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        child.wait(timeout=2)
        def identity(pid):
            if pid == os.getpid():
                return 'wrapper start'
            raise subprocess.CalledProcessError(1, ['ps'])
        with patch.object(processes.os, 'killpg') as kill:
            self.invoke_wrapper(lambda *args, **kwargs: child, started=identity)
        kill.assert_not_called()
        self.assertEqual(json.loads((self.run_dir / 'exit.json').read_text())['returncode'], 0)
        record = json.loads((self.run_dir / 'child_pid.json').read_text())
        self.assertEqual(record, {'pid': child.pid, 'started': None, 'exited': True})

    def test_wrapper_does_not_publish_exit_if_cleanup_cannot_be_confirmed(self):
        child = self.launch()
        with patch.object(worker, 'drain_child', return_value=False), \
                self.assertRaisesRegex(RuntimeError, 'Cannot confirm'):
            self.invoke_wrapper(lambda *args, **kwargs: child,
                                started=lambda pid: (_ for _ in ()).throw(OSError('metadata error'))
                                if pid == child.pid else 'wrapper start')
        self.assertFalse((self.run_dir / 'exit.json').exists())
        self.assertIsNone(child.poll())

    def test_atomic_publication_keeps_previous_json_on_rename_failure(self):
        path = self.run_dir / 'child_pid.json'
        previous = {'pid': None, 'started': None, 'exited': True}
        processes.atomic_json(path, previous)
        with patch.object(processes.os, 'replace', side_effect=OSError('rename failed')), \
                self.assertRaisesRegex(OSError, 'rename failed'):
            processes.atomic_json(path, {'pid': 123, 'started': 'new'})
        self.assertEqual(json.loads(path.read_text()), previous)
        self.assertEqual(list(self.run_dir.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
