"""Reported CLI usage, not inferred prices or account quota percentages."""
import json
import math
from pathlib import Path


def normalize(value, agent, cost=None):
    if not isinstance(value, dict):
        return None
    counts = [value.get(key) for key in ('input_tokens', 'output_tokens')]
    if any(type(count) is not int or count < 0 for count in counts):
        return None
    cached = value.get('cached_input_tokens', value.get('cache_read_input_tokens', 0))
    created = value.get('cache_creation_input_tokens', 0) if agent == 'claude' else 0
    if any(type(count) is not int or count < 0 for count in (cached, created)):
        return None
    # Codex input includes cache hits; Claude reports cache hits/creation separately.
    incoming = counts[0] + (cached + created if agent == 'claude' else 0)
    if cached > incoming:
        return None
    result = {'input': incoming, 'output': counts[1], 'cached_input': cached,
              'total': incoming + counts[1]}
    if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
        result['cost_usd'] = cost
    return result


def run_usage(folder, agent):
    path = Path(folder) / 'events.jsonl'
    if path.is_symlink() or not path.is_file():
        return None
    records = []
    try:
        if agent == 'claude':
            with path.open() as stream:
                envelope = json.load(stream)
            return normalize(envelope.get('usage'), agent, envelope.get('total_cost_usd'))
        with path.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # Last streaming line may not be complete yet.
                if isinstance(event, dict) and event.get('type') == 'turn.completed':
                    record = normalize(event.get('usage'), agent)
                    if record is not None:
                        records.append(record)
    except (OSError, ValueError, UnicodeError, AttributeError):
        return None
    return {key: sum(record[key] for record in records) for key in ('input', 'output', 'cached_input', 'total')} if records else None


def task_usage(root, task_id):
    folder = Path(root) / 'runs' / task_id
    result = {'input': 0, 'output': 0, 'cached_input': 0, 'total': 0,
              'reported_runs': 0, 'missing_runs': 0, 'cost_usd': None, 'runs': []}
    if folder.is_symlink() or not folder.is_dir():
        return result
    for attempt in sorted(folder.iterdir()):
        if attempt.is_symlink() or not attempt.is_dir():
            continue
        try:
            jobpath = attempt / 'job.json'
            if jobpath.is_symlink():
                continue
            job = json.loads(jobpath.read_text()) if jobpath.exists() else {}
        except (ValueError, OSError):
            job = {}
        agent = job.get('agent', 'unknown')
        usage = run_usage(attempt, agent) if agent in ('claude', 'codex') else None
        completed = (attempt / 'exit.json').is_file()
        result['runs'].append({'name': attempt.name, 'agent': agent, 'completed': completed,
                               'usage': usage, 'prompt_chars': job.get('prompt_chars')})
        if usage:
            result['reported_runs'] += 1
            for key in ('input', 'output', 'cached_input', 'total'):
                result[key] += usage[key]
            if usage.get('cost_usd') is not None:
                result['cost_usd'] = (result['cost_usd'] or 0) + usage['cost_usd']
        elif completed:
            result['missing_runs'] += 1
    result['complete'] = result['missing_runs'] == 0
    return result
