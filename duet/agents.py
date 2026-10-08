"""Restricted CLI workers and strict, structured worker results.

Command construction does not launch a model. Callers must wait for the process
and check its exit code before consuming a result with ``read_result``.
"""

import json
import math
import shutil
import subprocess
from pathlib import Path


RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "needs_fix", "needs_user"]},
        "summary": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "requests": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "changed_files", "risks", "requests"],
    "additionalProperties": False,
}


def _check_agent(agent: str) -> None:
    if agent not in ("codex", "claude"):
        raise ValueError("Unknown agent; expected 'codex' or 'claude'")


def build_command(
    agent: str,
    worktree: Path,
    run_dir: Path,
    role: str = "build",
    claude_budget: float = 0.5,
    images: tuple | list = (),
) -> list[str]:
    """Build argv with approvals disabled and a restricted tool/sandbox scope."""
    _check_agent(agent)
    if role not in ("build", "review"):
        raise ValueError("Unknown role; expected 'build' or 'review'")
    if agent == "claude" and (
        isinstance(claude_budget, bool)
        or not isinstance(claude_budget, (int, float))
        or not math.isfinite(claude_budget)
        or claude_budget <= 0
    ):
        raise ValueError("Claude budget must be a positive finite number")
    executable = shutil.which(agent)
    if executable is None:
        raise FileNotFoundError("CLI executable not found on PATH: " + agent)

    worktree = Path(worktree).resolve()
    run_dir = Path(run_dir).resolve()
    if agent == "codex":
        return [
            executable,
            "exec",
            "--cd", str(worktree),
            "--json",
            "--output-schema", str(run_dir / "result-schema.json"),
            "--output-last-message", str(run_dir / "final.json"),
            "--sandbox", "read-only" if role == "review" else "workspace-write",
            "--ignore-user-config",
            "-c", "approval_policy='never'",
            "-c", "sandbox_workspace_write.network_access=false",
            "-c", "web_search='disabled'",
            *[arg for image in images for arg in ("--image", str(Path(image).resolve()))],
            "-",
        ]

    tools = "Read,Glob,Grep" if role == "review" else "Read,Glob,Grep,Edit,Write"
    return [
        executable,
        "--safe-mode",
        "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(RESULT_SCHEMA, separators=(",", ":")),
        "--permission-mode", "dontAsk",
        "--tools", tools,
        "--allowedTools", tools,
        "--max-budget-usd", str(claude_budget),
        *[arg for directory in sorted({str(Path(image).resolve().parent) for image in images})
          for arg in ("--add-dir", directory)],
    ]


def start_worker(
    agent: str,
    worktree: Path,
    prompt: str,
    run_dir: Path,
    role: str = "build",
    claude_budget: float = 0.5,
    images: tuple | list = (),
) -> subprocess.Popen:
    """Save inputs and launch a worker; retain no parent-owned file handles."""
    worktree = Path(worktree).resolve()
    run_dir = Path(run_dir).resolve()
    command = build_command(agent, worktree, run_dir, role, claude_budget, images)
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = run_dir / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    (run_dir / "result-schema.json").write_text(
        json.dumps(RESULT_SCHEMA, indent=2) + "\n", encoding="utf-8"
    )

    with prompt_path.open("r", encoding="utf-8") as stdin, \
            (run_dir / "events.jsonl").open("w", encoding="utf-8") as stdout, \
            (run_dir / "stderr.log").open("w", encoding="utf-8") as stderr:
        return subprocess.Popen(
            command,
            cwd=worktree,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            shell=False,
        )


def validate_result(result: object) -> dict:
    """Validate the exact RESULT_SCHEMA contract, raising ValueError on failure."""
    if not isinstance(result, dict):
        raise ValueError("Result must be a JSON object")
    if set(result) != set(RESULT_SCHEMA["required"]):
        raise ValueError(
            "Result must contain exactly: " + ", ".join(RESULT_SCHEMA["required"])
        )
    if not isinstance(result["status"], str) or result["status"] not in (
        "ok", "needs_fix", "needs_user"
    ):
        raise ValueError("Result status must be 'ok', 'needs_fix', or 'needs_user'")
    if not isinstance(result["summary"], str):
        raise ValueError("Result summary must be a string")
    for field in ("changed_files", "risks", "requests"):
        if not isinstance(result[field], list) or not all(
            isinstance(item, str) for item in result[field]
        ):
            raise ValueError("Result " + field + " must be an array of strings")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Invalid JSON constant: " + value)


def _parse_json(text: str) -> object:
    return json.loads(
        text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )


def read_result(agent: str, run_dir: Path) -> dict:
    """Read a completed worker's result without guessing or repairing its JSON."""
    _check_agent(agent)
    path = Path(run_dir) / ("final.json" if agent == "codex" else "events.jsonl")
    try:
        result = _parse_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Cannot read valid worker JSON from " + str(path)) from exc

    if agent == "claude":
        if not isinstance(result, dict):
            raise ValueError("Claude output must be a JSON envelope")
        if result.get("is_error", False) is not False:
            raise ValueError("Claude envelope reports an error or invalid is_error")
        if "structured_output" in result:
            result = result["structured_output"]
        elif "result" in result:
            result = result["result"]
            if isinstance(result, str):
                try:
                    result = _parse_json(result)
                except ValueError as exc:
                    raise ValueError("Claude result is not valid JSON") from exc
        else:
            raise ValueError("Claude envelope has no structured_output or result")
    return validate_result(result)
