"""Local dashboard for the queue: no shell access and no cloud services."""
import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from duet.attachments import MAX_BODY, folder_for, read_image, store_uploads
from duet.controller import LOCKED, TERMINAL, Controller, identifier
from duet.processes import atomic_json, process_identity
from duet.usage import task_usage

PACKAGE_PARENT = Path(__file__).resolve().parents[1]


def scrub(value):
    if isinstance(value, dict):
        return {k: ('[redacted]' if re.search(r'token|password|secret|api.?key', k, re.I) else scrub(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                       '[key redacted]', value, flags=re.S)
        value = re.sub(r'\b(?:(?:sk-|rpa_|ghp_|gho_|github_pat_)[A-Za-z0-9_-]{16,}|\d{6,12}:[A-Za-z0-9_-]{25,})\b',
                       '[token redacted]', value)
        return re.sub(r'(?i)(\b(?:api[_-]?key|password|secret|token)\s*[=:]\s*)[\"\']?[^\s\"\',}]+',
                      r'\1[redacted]', value)
    return value


def json_file(path, fallback=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return fallback


def decode(value, fallback=None):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


class Dashboard:
    def __init__(self, repo):
        controller = Controller(repo)
        self.repo, self.common, self.root = controller.repo, controller.common, controller.root
        controller.db.close()

    @contextlib.contextmanager
    def controller(self):
        controller = Controller(self.repo)
        try:
            yield controller
        finally:
            controller.db.close()

    def running(self):
        with (self.root / 'controller.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False

    def public_task(self, row, details=False):
        spec = decode(row['spec'], {})
        value = {k: row.get(k) for k in ('id', 'state', 'phase', 'attempts', 'created', 'updated',
                                         'error', 'commit_sha')}
        value.update({k: spec.get(k) for k in ('title', 'prompt', 'agent', 'reviewer')})
        value['read_only'] = spec.get('read_only', False)
        usage = task_usage(self.root, row['id'])
        budget = spec.get('token_budget')
        value['usage'] = {**usage, 'budget': budget,
                          'remaining': max(0, budget - usage['total']) if budget is not None else None,
                          'context_chars': spec.get('context_chars', 60000)}
        value['attachments'] = [{**item, 'url': '/api/tasks/' + row['id'] + '/images/' + item['id']}
                                for item in spec.get('attachments', [])]
        if details:
            value.update({k: spec.get(k, []) for k in ('allowed_paths', 'depends_on')})
            value['result'] = decode(row['result'], {})
        return value

    def locks(self):
        result = []
        folder = self.common / 'locks'
        for owner in sorted(folder.glob('*/owner.json')) if folder.is_dir() else []:
            value = json_file(owner)
            if isinstance(value, dict):
                result.append({k: value[k] for k in ('agent', 'resource', 'task', 'created_utc') if k in value})
        return result

    def state(self):
        with self.controller() as controller:
            tasks = [self.public_task(row) for row in reversed(controller.rows())]
        agents = [json_file(self.common / 'agents' / (name + '.json'), {'agent': name, 'state': 'unknown'})
                  for name in ('codex', 'claude')]
        counts = {'active': sum(t['state'] not in TERMINAL and t['state'] != 'queued' for t in tasks),
                  'queued': sum(t['state'] == 'queued' for t in tasks),
                  'done': sum(t['state'] == 'done' for t in tasks),
                  'attention': sum(t['state'] in {'needs_user', 'failed', 'blocked'} for t in tasks)}
        return scrub({'controller': {'running': self.running()}, 'tasks': tasks, 'agents': agents,
                      'locks': self.locks(), 'counts': counts})

    def safe_tail(self, path):
        path = Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            return ''
        try:
            with path.open('rb') as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 48000))
                return stream.read(48000).decode('utf-8', errors='replace')
        except OSError:
            return ''

    def detail(self, task_id):
        identifier(task_id)
        with self.controller() as controller:
            task = self.public_task(controller.task(task_id), details=True)
            events = [dict(row) for row in controller.db.execute(
                'SELECT time,state,detail FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 100', (task_id,))]
        logs = []
        folder = self.root / 'runs' / task_id
        if folder.is_dir() and not folder.is_symlink():
            for attempt in sorted(folder.iterdir())[-4:]:
                if attempt.is_symlink() or not attempt.is_dir():
                    continue
                for name in ('events.jsonl', 'stderr.log', 'wrapper.log'):
                    text = self.safe_tail(attempt / name)
                    if text.strip():
                        logs.append({'name': attempt.name + '/' + name, 'text': text})
        requests = []
        for path in sorted((self.root / 'requests').glob('*.json'), key=lambda p: p.stat().st_mtime)[-100:]:
            value = json_file(path, {})
            if value.get('task_id') == task_id:
                requests.append({k: value.get(k) for k in ('action', 'outcome', 'error')})
        return scrub({'task': task, 'events': list(reversed(events)), 'logs': logs, 'requests': requests[-10:]})

    def create(self, data):
        if not isinstance(data, dict) or set(data) - {'title', 'prompt', 'agent', 'read_only', 'allowed_paths', 'depends_on',
                                                      'attachments', 'token_budget', 'context_chars'}:
            raise ValueError('Invalid task fields')
        if not isinstance(data.get('prompt'), str) or not 1 <= len(data['prompt'].strip()) <= 20000:
            raise ValueError('Describe the task: 1 to 20000 characters')
        data = dict(data)
        data.setdefault('token_budget', 100000)
        data['prompt'] = data['prompt'].strip()
        if 'title' not in data or data['title'] == '':
            data['title'] = data['prompt'].splitlines()[0][:120]
        if not isinstance(data.get('title'), str) or not 1 <= len(data['title'].strip()) <= 160:
            raise ValueError('Title: 1 to 160 characters')
        # The user describes the goal and workers pick files inside their isolated
        # checkout; an explicit scope stays available as an advanced option.
        if data.get('allowed_paths') == [] and not data.get('read_only'):
            data.pop('allowed_paths')
        data['id'] = 'task-' + uuid.uuid4().hex[:10]
        uploads = data.pop('attachments', [])
        data['attachments'] = store_uploads(self.root, data['id'], uploads)
        try:
            with self.controller() as controller:
                return {'id': controller.enqueue(data)}
        except Exception:
            if data['attachments']:
                shutil.rmtree(folder_for(self.root, data['id']))
            raise

    def image(self, task_id, image_id):
        identifier(task_id)
        with self.controller() as controller:
            spec = decode(controller.task(task_id)['spec'], {})
        for item in spec.get('attachments', []):
            if item['id'] == image_id:
                return read_image(self.root, task_id, item)
        raise ValueError('Image not found in this task')

    def control(self, task_id, action, data):
        identifier(task_id)
        if not isinstance(data, dict) or set(data) - ({'feedback'} if action == 'retry' else set()):
            raise ValueError('Invalid request fields')
        if action == 'retry' and (not isinstance(data.get('feedback'), str) or not data['feedback'].strip()):
            raise ValueError('Say what needs to be fixed')
        with self.controller() as controller:
            task = controller.task(task_id)
            if action == 'cancel' and task['state'] in {'done', 'cancelled'}:
                raise ValueError('This task is already finished')
            if action == 'retry' and task['state'] not in TERMINAL - {'done'}:
                raise ValueError('Retry is not available for this task right now')
            folder = self.root / 'requests'
            folder.mkdir(exist_ok=True)
            name = uuid.uuid4().hex
            atomic_json(folder / (name + '.request.json'),
                        {'task_id': task_id, 'action': action, 'feedback': data.get('feedback')})
            try:
                with controller.exclusive():
                    controller.apply_requests()
            except ValueError as error:
                if str(error) != LOCKED:
                    raise
            outcome = json_file(folder / (name + '.processed.json'))
            if outcome:
                return {'accepted': outcome['outcome'] == 'applied',
                        'message': outcome.get('error') or 'Request applied'}
        return {'accepted': True, 'message': 'Request queued; the outcome will appear on the task card'}

    def start(self):
        with (self.root / 'launch.lock').open('a') as launch:
            fcntl.flock(launch, fcntl.LOCK_EX)
            if self.running():
                return {'running': True, 'message': 'The controller is already running'}
            env = {**os.environ, 'PYTHONPATH': os.pathsep.join(filter(None, [str(PACKAGE_PARENT), os.environ.get('PYTHONPATH')]))}
            with (self.root / 'controller.log').open('ab') as log:
                process = subprocess.Popen(
                    [sys.executable, '-u', '-m', 'duet', '--repo', str(self.repo),
                     'run', '--watch', '--max-seconds', '3600', '--max-launches', '10'],
                    cwd=self.repo, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            temporary = self.root / ('controller-' + str(os.getpid()) + '.tmp')
            temporary.write_text(str(process.pid) + '\n')
            os.replace(temporary, self.root / 'controller.pid')
            for _ in range(30):
                if self.running():
                    return {'running': True, 'message': 'Controller started for one hour, up to 10 CLI sessions'}
                if process.poll() is not None:
                    raise ValueError('The controller did not start; check controller.log')
                time.sleep(0.05)
            raise ValueError('Start is not confirmed yet; refresh the state')


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, dashboard):
        self.dashboard = dashboard
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Request contents and user prompts must not reach access logs.

    def headers_out(self, content_type):
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
                                                    "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; object-src 'none'")

    def response(self, status, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.headers_out('application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def static(self, name, content_type):
        raw = Path(__file__).with_name(name).read_bytes()
        self.send_response(200)
        self.headers_out(content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get('Host') in {f'127.0.0.1:{port}', f'localhost:{port}'}

    def do_GET(self):
        if not self.valid_host():
            return self.response(403, {'error': 'Only the local address is allowed'})
        path = urlsplit(self.path).path
        try:
            if path == '/':
                self.static('dashboard.html', 'text/html; charset=utf-8')
            elif path == '/api/state':
                self.response(200, self.server.dashboard.state())
            elif path == '/task_images.js':
                self.static('task_images.js', 'text/javascript; charset=utf-8')
            elif re.fullmatch(r'/api/tasks/[a-z0-9_-]+/images/[0-9a-f]{32}', path):
                parts = path.split('/')
                raw = self.server.dashboard.image(parts[3], parts[5])
                self.send_response(200)
                self.headers_out('image/png')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif re.fullmatch(r'/api/tasks/[a-z0-9_-]+', path):
                self.response(200, self.server.dashboard.detail(path.rsplit('/', 1)[-1]))
            else:
                self.response(404, {'error': 'Not found'})
        except (ValueError, OSError, sqlite3.Error) as error:
            self.response(400, {'error': scrub(str(error))})

    def do_POST(self):
        origin = self.headers.get('Origin')
        port = self.server.server_port
        if (not self.valid_host() or self.headers.get('X-Duet-Request') != '1' or
                origin and origin not in {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}):
            return self.response(403, {'error': 'Requests are accepted only from the local dashboard'})
        try:
            path = urlsplit(self.path).path
            length = int(self.headers.get('Content-Length', '0'))
            limit = MAX_BODY if path == '/api/tasks' else 65536
            if not 1 <= length <= limit or self.headers.get_content_type() != 'application/json':
                raise ValueError('The JSON request is too large or not JSON')
            data = json.loads(self.rfile.read(length))
            dashboard = self.server.dashboard
            if path == '/api/tasks':
                result = dashboard.create(data)
            elif path == '/api/controller/start':
                if data != {}:
                    raise ValueError('Invalid start request')
                result = dashboard.start()
            elif re.fullmatch(r'/api/tasks/[a-z0-9_-]+/(cancel|retry)', path):
                _, _, _, task_id, action = path.split('/')
                result = dashboard.control(task_id, action, data)
            else:
                return self.response(404, {'error': 'Unknown action'})
            self.response(200, result)
        except (ValueError, OSError, sqlite3.Error) as error:
            self.response(400, {'error': scrub(str(error))})


def main():
    parser = argparse.ArgumentParser(prog='duet.web', description='Local duet dashboard')
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--port', type=int, default=18789)
    args = parser.parse_args()
    dashboard = Dashboard(args.repo)
    server = DashboardServer(('127.0.0.1', args.port), dashboard)
    record = process_identity(os.getpid())
    record['url'] = f'http://127.0.0.1:{server.server_port}/'
    atomic_json(dashboard.root / 'web.json', record)
    print('Dashboard: ' + record['url'], flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
