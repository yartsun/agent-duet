#!/usr/bin/env python3
"""Full duet cycle on a throwaway repository with fake agents: no Codex/Claude install, no API spend.

    python3 examples/demo.py            # build, review, check and commit five demo tasks
    python3 examples/demo.py --serve    # then open the dashboard at http://127.0.0.1:18789/
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from duet.controller import Controller  # noqa: E402

TASKS = [
    {'id': 'changelog', 'title': 'Add a CHANGELOG entry for the 0.2 release', 'prompt': 'Add a CHANGELOG entry for the 0.2 release.',
     'allowed_paths': ['CHANGELOG.md'], 'checks': [['python3', '-c', "assert 'release' in open('CHANGELOG.md').read()"]]},
    {'id': 'deploy-docs', 'title': 'Document the deploy steps', 'prompt': 'Write down the deploy steps in DEPLOY.md.',
     'agent': 'claude', 'allowed_paths': ['DEPLOY.md']},
    {'id': 'explain-cart', 'title': 'Explain how the cart total is computed', 'prompt': 'Explain how the cart total is computed.',
     'read_only': True, 'allowed_paths': []},
    {'id': 'lint-fix', 'title': 'Fix the lint warnings in cart.py', 'prompt': 'Fix the lint warnings in cart.py.',
     'allowed_paths': ['cart.py'], 'max_fixes': 0, 'checks': [['python3', '-c', 'raise SystemExit("lint: 3 warnings left")']]},
    {'id': 'release-notes', 'title': 'Draft the release notes', 'prompt': 'Draft the release notes from the changelog.',
     'depends_on': ['lint-fix'], 'allowed_paths': ['RELEASE.md']},
]


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dir', type=Path, help='where to create the demo repository (default: a temp folder)')
    parser.add_argument('--serve', action='store_true', help='start the dashboard when the queue is done')
    args = parser.parse_args()
    base = args.dir or Path(tempfile.mkdtemp(prefix='duet-demo-'))
    repo = base / 'shop'
    repo.mkdir(parents=True)
    sh('git', 'init', '-q', '-b', 'main', cwd=repo)
    (repo / 'cart.py').write_text('def total(items):\n    return sum(i.price * i.qty for i in items)\n')
    (repo / 'CHANGELOG.md').write_text('# Changelog\n\n')
    (repo / 'AGENTS.md').write_text('Keep changes small. Write in plain English.\n')
    sh('git', 'add', '.', cwd=repo)
    sh('git', '-c', 'user.name=Demo', '-c', 'user.email=demo@example.invalid', 'commit', '-qm', 'Initial shop', cwd=repo)
    sh('git', 'branch', 'develop', cwd=repo)
    os.environ['PATH'] = str(HERE / 'fake-agents') + os.pathsep + os.environ['PATH']
    os.environ.setdefault('GIT_AUTHOR_NAME', 'duet demo')
    os.environ.setdefault('GIT_AUTHOR_EMAIL', 'demo@example.invalid')
    os.environ.setdefault('GIT_COMMITTER_NAME', 'duet demo')
    os.environ.setdefault('GIT_COMMITTER_EMAIL', 'demo@example.invalid')

    controller = Controller(repo)
    for task in TASKS:
        controller.enqueue(task)
    rows = controller.run(max_seconds=120, poll=0.2)
    controller.integrate('changelog')
    controller.db.close()
    print('\n' + json.dumps([{k: r[k] for k in ('id', 'state', 'commit_sha')} for r in rows], indent=2))
    print('\nRepository:', repo)
    print('develop now has:', subprocess.check_output(['git', '-C', str(repo), 'log', '--oneline', '-3', 'develop'], text=True))
    if args.serve:
        subprocess.run([sys.executable, '-m', 'duet.web', '--repo', str(repo)], cwd=HERE.parent)


if __name__ == '__main__':
    main()
