"""Durable wrapper: a controller restart must not launch an agent twice."""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from duet.agents import start_worker  # noqa: E402
from duet.processes import atomic_json, drain_child, process_identity  # noqa: E402


def started(pid):
    return process_identity(pid)['started']


def main():
    job = json.loads(Path(sys.argv[1]).read_text())
    folder = Path(job['run_dir'])
    child = None
    cancelled = False
    child_record = {'pid': None, 'started': None, 'exited': True}

    def stop(signum, frame):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Code workers get no SSH agent and none of the variables listed in DUET_SCRUB_ENV.
    for name in ('SSH_AUTH_SOCK', *filter(None, os.environ.get('DUET_SCRUB_ENV', '').split(','))):
        os.environ.pop(name.strip(), None)
    try:
        atomic_json(folder / 'wrapper_pid.json', {'pid': os.getpid(), 'started': started(os.getpid())})
        # A crash between Popen and identity publication must conservatively block retry.
        atomic_json(folder / 'child_pid.json', {'pid': None, 'started': None, 'pending': True})
        if cancelled:
            result = {'returncode': -signal.SIGTERM}
        else:
            child = start_worker(job['agent'], Path(job['worktree']), job['prompt'], folder,
                                 role=job['role'], claude_budget=job['claude_budget'], images=job.get('images', []))
            child_record = {'pid': child.pid, 'started': None}
            try:
                child_record['started'] = started(child.pid)
            except (OSError, ValueError, subprocess.SubprocessError):
                # A fast CLI can finish before ps observes it; preserve its real exit code.
                if child.poll() is None:
                    raise
                child_record['exited'] = True
            atomic_json(folder / 'child_pid.json', child_record)
            while not cancelled:
                try:
                    code = child.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pass
            result = {'returncode': -signal.SIGTERM if cancelled else code}
    except Exception as error:
        result = {'returncode': 1, 'error': str(error)}
    finally:
        if child is not None and not drain_child(child):
            # Do not advertise completion while cleanup or group ownership is unresolved.
            raise RuntimeError('Cannot confirm worker process group has exited')
    child_record['exited'] = True
    atomic_json(folder / 'child_pid.json', child_record)
    atomic_json(folder / 'exit.json', result)


if __name__ == '__main__':
    main()
