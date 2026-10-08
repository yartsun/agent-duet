"""Bounded shutdown of explicitly recorded, identity-checked worker groups."""
import json
import math
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

_PID_FILES = ('child_pid.json', 'wrapper_pid.json')
_POLL = 0.05
_KILL_WAIT = 1.0
_PS_TIMEOUT = 0.5


def atomic_json(path: Path, value: dict) -> None:
    """Publish a complete record, leaving the previous record intact on failure."""
    path = Path(path)
    temporary = path.with_name(path.name + '-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process_identity(pid: int) -> dict:
    """Read the start time, process group and state in one ps observation."""
    output = subprocess.check_output(
        ['ps', '-p', str(pid), '-o', 'pgid=,stat=,lstart='],
        text=True, stderr=subprocess.DEVNULL, timeout=_PS_TIMEOUT,
    ).strip()
    pgid, state, started = output.split(None, 2)
    return {'pid': pid, 'pgid': int(pgid), 'state': state, 'started': started.strip()}


def _snapshot():
    # Group members still matter when their leader has exited or become a zombie.
    try:
        output = subprocess.check_output(
            ['ps', '-A', '-o', 'pid=,pgid=,stat=,lstart='],
            text=True, stderr=subprocess.DEVNULL, timeout=_PS_TIMEOUT,
        )
        result = {}
        for line in output.splitlines():
            pid, pgid, state, started = line.split(None, 3)
            result[int(pid)] = {'pgid': int(pgid), 'state': state, 'started': started.strip()}
        return result or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _records(run_dir):
    result = []
    for name in _PID_FILES:
        try:
            value = json.loads((Path(run_dir) / name).read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                value = None
            # A wrapper may explicitly confirm that no child was launched.
            if name == 'wrapper_pid.json' and value and value.get('pid') is None:
                value = None
        except (OSError, UnicodeError, ValueError):
            value = None
        result.append(value)
    return result


def _state(record, snapshot):
    if record is None:
        return 'unknown'
    pid, started = record.get('pid'), record.get('started')
    if pid is None and started is None and record.get('exited') is True:
        return 'dead'  # Explicitly no child; an absent file is never equivalent.
    if type(pid) is not int or pid <= 1:
        return 'unknown'
    if not (isinstance(started, str) and started.strip()):
        if started is not None or record.get('exited') is not True:
            return 'unknown'
    if snapshot is None:
        return 'unknown'
    actual = snapshot.get(pid)
    group_live = any(p['pgid'] == pid and not p['state'].startswith('Z')
                     for p in snapshot.values())
    if actual is None:
        return 'unknown' if group_live else 'dead'
    if actual['started'] != started or actual['pgid'] != pid:
        return 'unknown'
    if actual['state'].startswith('Z'):
        return 'unknown' if group_live else 'dead'
    return 'live'


def owned_alive(run_dir: Path) -> bool:
    """True for live owned groups OR uncertainty that must block a retry."""
    records = _records(run_dir)
    snapshot = _snapshot()
    return any(_state(record, snapshot) != 'dead' for record in records)


def _signal_record(record, signum):
    pid = record['pid']
    try:
        actual = process_identity(pid)
        if (actual['started'] != record['started'] or actual['pgid'] != pid
                or actual['state'].startswith('Z') or os.getpgid(pid) != pid):
            return False
        os.killpg(pid, signum)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def terminate_owned(run_dir: Path, grace: float = 2.0) -> bool:
    """TERM, bounded grace, KILL; return true only after all groups are gone.

    Missing/malformed records, reused PIDs and unverifiable surviving groups
    return false. Never signal a group on the strength of a PID alone.
    """
    if not math.isfinite(grace) or grace < 0:
        raise ValueError('grace must be finite and non-negative')
    term_until = time.monotonic() + grace
    deadline = term_until + _KILL_WAIT
    sent_term, sent_kill = set(), set()
    while True:
        records, snapshot = _records(run_dir), _snapshot()
        states = [_state(record, snapshot) for record in records]
        if all(state == 'dead' for state in states):
            return True
        # Nothing can safely be signalled, including a leaderless live group.
        if 'live' not in states:
            return False
        current = time.monotonic()
        for record, state in zip(records, states, strict=True):
            if state != 'live':
                continue
            identity = (record['pid'], record['started'])
            if identity not in sent_term and _signal_record(record, signal.SIGTERM):
                sent_term.add(identity)
            if current >= term_until and identity in sent_term and identity not in sent_kill:
                if _signal_record(record, signal.SIGKILL):
                    sent_kill.add(identity)
        if current >= deadline:
            return False
        time.sleep(min(_POLL, deadline - current))


def drain_child(child: subprocess.Popen, grace: float = 1.0) -> bool:
    """Drain a directly retained Popen handle, even if PID publication failed."""
    def send(signum):
        if child.returncode is not None:
            return
        try:
            if os.getpgid(child.pid) == child.pid:
                os.killpg(child.pid, signum)
            else:
                child.send_signal(signum)
        except ProcessLookupError:
            pass

    try:
        send(signal.SIGTERM)
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            send(signal.SIGKILL)
            child.wait(timeout=_KILL_WAIT)
    except (OSError, subprocess.SubprocessError):
        return False
    snapshot = _snapshot()
    return snapshot is not None and not any(
        p['pgid'] == child.pid and not p['state'].startswith('Z')
        for p in snapshot.values()
    )
