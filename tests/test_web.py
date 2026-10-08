"""Exercise dashboard HTTP actions against disposable queues, never model CLIs."""
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from duet.web import Dashboard, DashboardServer, scrub
from test_attachments import png, upload
from unittest.mock import patch


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        for args in (['init', '-q'], ['config', 'user.name', 'Test'],
                     ['config', 'user.email', 'test@example.invalid']):
            subprocess.run(['git', '-C', str(self.repo), *args], check=True)
        (self.repo / 'README.md').write_text('Initial\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', 'README.md'], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-qm', 'Initial'], check=True)
        self.dashboard = Dashboard(self.repo)
        self.server = DashboardServer(('127.0.0.1', 0), self.dashboard)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, body=None, extra_headers=None):
        headers = {'X-Duet-Request': '1', 'Content-Type': 'application/json'}
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(self.url + path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def create(self):
        code, data = self.request('/api/tasks', {'title': 'Test', 'prompt': 'Read README',
                                'agent': 'codex', 'read_only': True, 'allowed_paths': []})
        self.assertEqual(code, 200)
        return data['id']

    def test_photo_roundtrip_worker_inputs_and_restart(self):
        code, result = self.request('/api/tasks', {'prompt': 'Check the images', 'attachments': [upload(), upload(name='Second.png')]})
        self.assertEqual(code, 200)
        task_id = result['id']
        details = self.request('/api/tasks/' + task_id)[1]['task']
        self.assertEqual(len(details['attachments']), 2)
        for item in details['attachments']:
            self.assertNotIn('data', item)
            with urllib.request.urlopen(self.url + item['url']) as response:
                self.assertEqual(response.headers.get_content_type(), 'image/png')
                self.assertEqual(response.read(), png())
        # Fresh controller and dashboard see exactly the same inputs.
        fresh = Dashboard(self.repo)
        self.assertEqual(fresh.detail(task_id)['task']['attachments'], details['attachments'])
        with fresh.controller() as controller:
            row = controller.task(task_id)
            folder = controller.prepare(row)
            row = controller.task(task_id)
            build = controller.prompt(row, 'build')
            review = controller.prompt(row, 'review')
            self.assertIn('Read', build)
            self.assertIn('Read', review)
            self.assertNotIn('data:image', build)
            with patch('duet.controller.subprocess.Popen') as popen:
                popen.return_value.pid = 123456
                controller.launch(row)
            job = json.loads((Path(controller.task(task_id)['run_dir']) / 'job.json').read_text())
            self.assertEqual(len(job['images']), 2)
            for image in job['images']:
                self.assertIn(image, build)
                self.assertIn(image, review)
                self.assertEqual(Path(image).read_bytes(), png())
            self.assertEqual(controller.changes(folder), [])
        self.assertFalse(self.dashboard.running())

    def test_invalid_attachments_create_no_queue_entry_or_orphan_files(self):
        bodies = [ {'prompt': 'x', 'attachments': [upload(b'not png')]},
                   {'prompt': 'x', 'attachments': [upload()] * 11},
                   {'prompt': 'x', 'agent': 'invalid', 'attachments': [upload()]},
                   {'prompt': 'x', 'attachments': [{'path': '/etc/passwd'}]} ]
        for body in bodies:
            self.assertEqual(self.request('/api/tasks', body)[0], 400)
        self.assertEqual(self.request('/api/state')[1]['tasks'], [])
        self.assertEqual(list((self.dashboard.root / 'attachments').glob('*')), [])

    def test_image_is_bound_to_task_and_cannot_read_arbitrary_path(self):
        _, result = self.request('/api/tasks', {'prompt': 'x', 'attachments': [upload()]})
        task_id = result['id']
        photo = self.dashboard.detail(task_id)['task']['attachments'][0]
        other = self.create()
        for path in ('/api/tasks/' + other + '/images/' + photo['id'],
                     '/api/tasks/' + task_id + '/images/' + '0' * 32):
            self.assertEqual(self.request(path)[0], 400)
        with self.dashboard.controller() as controller:
            original = controller.task(task_id)
            from duet.attachments import image_paths
            image = Path(image_paths(controller.root, task_id, json.loads(original['spec'])['attachments'])[0])
            image.write_bytes(b'changed')
            with self.assertRaises(ValueError): controller.prompt(original, 'build')
        self.assertEqual(self.request(photo['url'])[0], 400)

    def test_create_list_detail_and_cancel_preserve_same_queue(self):
        task_id = self.create()
        code, state = self.request('/api/state')
        self.assertEqual(state['counts']['queued'], 1)
        self.assertEqual(state['tasks'][0]['id'], task_id)
        self.assertFalse(state['controller']['running'])
        code, detail = self.request('/api/tasks/' + task_id)
        self.assertEqual(detail['task']['prompt'], 'Read README')
        self.assertEqual(detail['events'][0]['state'], 'queued')
        code, result = self.request('/api/tasks/' + task_id + '/cancel', {})
        self.assertTrue(result['accepted'])
        self.assertEqual(self.request('/api/state')[1]['tasks'][0]['state'], 'cancelled')

    def test_goal_only_creates_queued_task_with_repository_scope_and_auto_title(self):
        code, response = self.request('/api/tasks', {'prompt': '  Fix the header colors\nKeep the logo.  '})
        self.assertEqual(code, 200)
        with self.dashboard.controller() as controller:
            task = controller.task(response['id'])
            spec = json.loads(task['spec'])
        self.assertEqual(task['state'], 'queued')
        self.assertEqual(spec['title'], 'Fix the header colors')
        self.assertEqual(spec['allowed_paths'], ['**'])
        self.assertFalse(spec['read_only'])
        self.assertEqual(spec['agent'], 'codex')
        self.assertEqual(spec['reviewer'], 'claude')
        self.assertFalse(self.dashboard.running())

    def test_empty_paths_from_older_form_do_not_require_user_to_name_files(self):
        code, response = self.request('/api/tasks', {'title': 'Test', 'prompt': 'Test', 'allowed_paths': []})
        self.assertEqual(code, 200)
        with self.dashboard.controller() as controller:
            self.assertEqual(json.loads(controller.task(response['id'])['spec'])['allowed_paths'], ['**'])

    def test_advanced_read_only_and_explicit_paths_remain_effective(self):
        for read_only, paths in ((True, []), (False, ['README.md'])):
            code, response = self.request('/api/tasks', {'prompt': 'Check README',
                'read_only': read_only, 'allowed_paths': paths, 'agent': 'claude'})
            self.assertEqual(code, 200)
            with self.dashboard.controller() as controller:
                spec = json.loads(controller.task(response['id'])['spec'])
            self.assertEqual(spec['allowed_paths'], paths)
            self.assertEqual(spec['read_only'], read_only)
            self.assertEqual(spec['agent'], 'claude')

    def test_running_controller_receives_control_request_without_losing_it(self):
        task_id = self.create()
        with self.dashboard.controller() as controller:
            with controller.exclusive():
                self.assertTrue(self.request('/api/state')[1]['controller']['running'])
                code, response = self.request('/api/tasks/' + task_id + '/cancel', {})
                self.assertTrue(response['accepted'])
                self.assertEqual(controller.task(task_id)['state'], 'queued')
            controller.apply_requests()
            self.assertEqual(controller.task(task_id)['state'], 'cancelled')

    def test_cross_origin_or_rebound_host_cannot_create_tasks(self):
        body = {'title': 'Bad', 'prompt': 'Bad', 'read_only': True}
        for headers in ({'Origin': 'https://example.invalid'}, {'Host': 'evil.invalid'},
                        {'X-Duet-Request': ''}):
            code, response = self.request('/api/tasks', body, headers)
            self.assertEqual(code, 403)
        self.assertEqual(self.dashboard.state()['tasks'], [])

    def test_browser_cannot_inject_shell_checks_or_escape_paths(self):
        body = {'title': 'Test', 'prompt': 'Test', 'agent': 'codex', 'allowed_paths': ['README.md']}
        for delta in ({'checks': [['echo', 'bad']]}, {'allowed_paths': ['../outside']}):
            code, response = self.request('/api/tasks', {**body, **delta})
            self.assertEqual(code, 400)
        self.assertEqual(self.dashboard.state()['tasks'], [])

    def test_logs_are_scoped_and_secrets_are_redacted(self):
        task_id = self.create()
        folder = self.dashboard.root / 'runs' / task_id / '1-build'
        folder.mkdir(parents=True)
        (folder / 'stderr.log').write_text('token=abc-secret-value\nnormal log\n')
        outside = self.repo / 'private.txt'
        outside.write_text('private outside\n')
        (folder / 'wrapper.log').symlink_to(outside)
        logs = self.dashboard.detail(task_id)['logs']
        self.assertEqual(len(logs), 1)
        self.assertNotIn('abc-secret-value', logs[0]['text'])
        self.assertIn('normal log', logs[0]['text'])
        self.assertEqual(scrub({'api_key': 'secret'})['api_key'], '[redacted]')

    def test_retry_reports_actual_rejection_after_attempt_limit(self):
        task_id = self.create()
        with self.dashboard.controller() as controller:
            controller.update(task_id, state='failed', attempts=3)
        code, response = self.request(f'/api/tasks/{task_id}/retry', {'feedback': 'Again'})
        self.assertEqual(code, 200)
        self.assertFalse(response['accepted'])
        self.assertEqual(self.dashboard.state()['tasks'][0]['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
