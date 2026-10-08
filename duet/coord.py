"""Local channel for interactive agent sessions that share one repository.

Status, messages and resource claims live in <git common dir>/duet, so every
worktree of the repository sees the same channel. Local files only, no network.

    python3 -m duet.coord read    --agent claude
    python3 -m duet.coord status  --agent claude --state working --task "Speed up the build"
    python3 -m duet.coord send    --agent claude --to codex --body "Build is green, your turn"
    python3 -m duet.coord claim   --agent codex --resource gpu --task "Benchmark run"
    python3 -m duet.coord release --agent codex --resource gpu
    python3 -m duet.coord wait    --agent codex --timeout 1800
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def safe(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', value):
        raise argparse.ArgumentTypeError('use a short id: lowercase letters, digits, - or _')
    return value


def channel():
    common = subprocess.check_output(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], text=True).strip()
    return Path(common) / 'duet'


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.replace(temporary, path)


def incoming(root, agent):
    """Messages to this agent or to everyone, oldest first (names start with time_ns)."""
    result = []
    for file in sorted((root / 'messages').glob('*.json')):
        try:
            message = json.loads(file.read_text())
        except (OSError, json.JSONDecodeError):  # still being written
            continue
        if message.get('to') in (agent, 'all') and message.get('from') != agent:
            result.append((file.name, message))
    return result


def wait(root, agent, timeout, poll):
    """Block until a message this agent has not seen arrives; print it and return 0, or 2 on timeout."""
    seen_file = root / 'seen' / f'{agent}.txt'
    if not seen_file.exists():  # first use starts from now instead of waking on old history
        names = sorted(f.name for f in (root / 'messages').glob('*.json'))
        save_text(seen_file, names[-1] if names else '')
    seen = seen_file.read_text().strip()
    end = time.time() + timeout if timeout else None
    while True:
        new = [(name, m) for name, m in incoming(root, agent) if name > seen]
        if new:
            for name, message in new:
                print(json.dumps({'file': name, **message}, ensure_ascii=False), flush=True)
            save_text(seen_file, new[-1][0])
            return 0
        if end and time.time() > end:
            print('no new messages', flush=True)
            return 2
        time.sleep(poll)


def save_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def main(argv=None):
    p = argparse.ArgumentParser(prog='duet.coord', description='Status, messages and resource claims for agents sharing a repository')
    p.add_argument('command', choices=['read', 'status', 'send', 'claim', 'release', 'wait'])
    p.add_argument('--agent', required=True, type=safe)
    p.add_argument('--to', type=safe, help='recipient agent, or "all"')
    p.add_argument('--body')
    p.add_argument('--body-file', type=Path)
    p.add_argument('--state')
    p.add_argument('--task')
    p.add_argument('--resource', type=safe, default='shared')
    p.add_argument('--brief', action='store_true', help='read: skip CONTEXT.md and show only the last 8 messages')
    p.add_argument('--limit-messages', type=int, help='read: show only the last N incoming messages')
    p.add_argument('--timeout', type=float, default=1800, help='wait: seconds, 0 waits forever')
    p.add_argument('--poll', type=float, default=5, help='wait: seconds between checks')
    args = p.parse_args(argv)
    if args.limit_messages is not None and args.limit_messages < 0:
        p.error('--limit-messages must be non-negative')
    root = channel()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    if args.command == 'read':
        context = root / 'CONTEXT.md'
        if args.brief:
            print('Channel:', root)
        elif context.exists():
            print(context.read_text())
        for folder in ['agents', 'locks']:
            for file in sorted((root / folder).rglob('*.json')) if (root / folder).exists() else []:
                print(file.relative_to(root), file.read_text())
        messages = [(name, m) for name, m in incoming(root, args.agent)]
        limit = args.limit_messages if args.limit_messages is not None else (8 if args.brief else None)
        if limit is not None:
            messages = messages[-limit:] if limit else []
        for name, message in messages:
            print('message', name, json.dumps(message, ensure_ascii=False))
    elif args.command == 'status':
        if not args.state or not args.task:
            p.error('status needs --state and --task')
        branch = subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip()
        save(root / 'agents' / (args.agent + '.json'), {
            'agent': args.agent, 'updated_utc': stamp, 'branch': branch,
            'state': args.state, 'task': args.task,
        })
    elif args.command == 'send':
        if not args.to or bool(args.body) == bool(args.body_file):
            p.error('send needs --to and exactly one of --body or --body-file')
        body = args.body if args.body is not None else args.body_file.read_text()
        name = f'{time.time_ns()}-{args.agent}-{args.to}-{uuid.uuid4().hex[:8]}.json'
        save(root / 'messages' / name, {'from': args.agent, 'to': args.to, 'time_utc': stamp, 'body': body})
        print('Message written:', name)
    elif args.command == 'claim':
        lock = root / 'locks' / args.resource
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock.mkdir()
        except FileExistsError:
            owner = lock / 'owner.json'
            raise SystemExit('Resource is taken: ' + (owner.read_text() if owner.exists() else 'the owner is still writing the claim')) from None
        save(lock / 'owner.json', {'agent': args.agent, 'resource': args.resource, 'created_utc': stamp, 'task': args.task})
        print('Resource claimed:', args.resource)
    elif args.command == 'release':
        lock = root / 'locks' / args.resource
        owner = lock / 'owner.json'
        if not owner.exists() or json.loads(owner.read_text())['agent'] != args.agent:
            raise SystemExit('Cannot release a claim that is missing or belongs to another agent.')
        owner.unlink()
        lock.rmdir()
        print('Resource released:', args.resource)
    elif args.command == 'wait':
        return wait(root, args.agent, args.timeout, args.poll)
    return 0


if __name__ == '__main__':
    sys.exit(main())
