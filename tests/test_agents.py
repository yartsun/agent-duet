"""Exercise CLI isolation and result handling without launching either CLI."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from duet.agents import (
    RESULT_SCHEMA,
    build_command,
    read_result,
    start_worker,
    validate_result,
)


def valid_result(status="ok"):
    return {
        "status": status,
        "summary": "Checked",
        "changed_files": ["duet/agents.py"],
        "risks": [],
        "requests": [],
    }


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.worktree = self.root / "work tree"
        self.run_dir = self.root / "run dir"
        which_patch = patch(
            "duet.agents.shutil.which", side_effect=lambda agent: "/bin/" + agent
        )
        self.which = which_patch.start()
        self.addCleanup(which_patch.stop)

    def test_codex_build_argv_restricts_approvals_network_and_search(self):
        argv = build_command("codex", self.worktree, self.run_dir)
        self.which.assert_called_once_with("codex")
        self.assertEqual(argv[:2], ["/bin/codex", "exec"])
        self.assertEqual(flag_value(argv, "--cd"), str(self.worktree))
        self.assertIn("--json", argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertEqual(flag_value(argv, "--sandbox"), "workspace-write")
        self.assertEqual(
            flag_value(argv, "--output-schema"), str(self.run_dir / "result-schema.json")
        )
        self.assertEqual(
            flag_value(argv, "--output-last-message"), str(self.run_dir / "final.json")
        )
        overrides = [argv[i + 1] for i, value in enumerate(argv) if value == "-c"]
        self.assertEqual(overrides, [
            "approval_policy='never'",
            "sandbox_workspace_write.network_access=false",
            "web_search='disabled'",
        ])
        self.assertEqual(argv[-1], "-")

    def test_codex_review_uses_read_only_sandbox(self):
        argv = build_command("codex", self.worktree, self.run_dir, role="review")
        self.assertEqual(flag_value(argv, "--sandbox"), "read-only")

    def test_claude_build_argv_limits_tools_and_budget(self):
        argv = build_command("claude", self.worktree, self.run_dir, claude_budget=0.25)
        self.which.assert_called_once_with("claude")
        self.assertEqual(argv[0], "/bin/claude")
        self.assertIn("--safe-mode", argv)
        self.assertIn("-p", argv)
        self.assertEqual(flag_value(argv, "--output-format"), "json")
        self.assertEqual(json.loads(flag_value(argv, "--json-schema")), RESULT_SCHEMA)
        self.assertEqual(flag_value(argv, "--permission-mode"), "dontAsk")
        self.assertEqual(flag_value(argv, "--max-budget-usd"), "0.25")
        for flag in ("--tools", "--allowedTools"):
            self.assertEqual(flag_value(argv, flag), "Read,Glob,Grep,Edit,Write")

    def test_claude_review_has_only_read_tools(self):
        argv = build_command("claude", self.worktree, self.run_dir, role="review")
        for flag in ("--tools", "--allowedTools"):
            self.assertEqual(flag_value(argv, flag), "Read,Glob,Grep")
        self.assertEqual(flag_value(argv, "--max-budget-usd"), "0.5")

    def test_commands_never_include_bypass_or_external_tools(self):
        forbidden = (
            "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto",
            "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
            "--bypass-permissions", "Bash", "Web", "MCP",
        )
        for agent in ("codex", "claude"):
            for role in ("build", "review"):
                argv = build_command(agent, self.worktree, self.run_dir, role=role)
                for forbidden_value in forbidden:
                    with self.subTest(agent=agent, role=role, forbidden=forbidden_value):
                        self.assertFalse(any(forbidden_value in value for value in argv))

    def test_missing_executable_fails(self):
        self.which.side_effect = None
        self.which.return_value = None
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent), self.assertRaises(FileNotFoundError):
                build_command(agent, self.worktree, self.run_dir)

    def test_unknown_agent_or_role_fails_before_cli_resolution(self):
        with self.assertRaises(ValueError):
            build_command("other", self.worktree, self.run_dir)
        with self.assertRaises(ValueError):
            build_command("claude", self.worktree, self.run_dir, role="admin")
        self.which.assert_not_called()

    def test_invalid_claude_budget_fails(self):
        for budget in (0, -1, float("nan"), float("inf"), -float("inf"), True, "0.5", None):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                build_command("claude", self.worktree, self.run_dir, claude_budget=budget)
        self.which.assert_not_called()

    def test_relative_artifact_paths_are_absolute(self):
        argv = build_command("codex", Path("work tree"), Path("run dir"))
        for flag in ("--cd", "--output-schema", "--output-last-message"):
            self.assertTrue(Path(flag_value(argv, flag)).is_absolute())
        self.assertFalse(self.run_dir.exists())


class ValidationTests(unittest.TestCase):
    def test_schema_has_exact_required_fields_and_no_additional_properties(self):
        self.assertEqual(RESULT_SCHEMA["type"], "object")
        self.assertIs(RESULT_SCHEMA["additionalProperties"], False)
        self.assertEqual(set(RESULT_SCHEMA["required"]), set(valid_result()))
        self.assertEqual(set(RESULT_SCHEMA["properties"]), set(valid_result()))
        self.assertEqual(RESULT_SCHEMA["properties"]["status"], {
            "type": "string", "enum": ["ok", "needs_fix", "needs_user"],
        })
        self.assertEqual(RESULT_SCHEMA["properties"]["summary"], {"type": "string"})
        for field in ("changed_files", "risks", "requests"):
            self.assertEqual(RESULT_SCHEMA["properties"][field], {
                "type": "array", "items": {"type": "string"},
            })

    def test_all_contract_statuses_are_preserved(self):
        for status in ("ok", "needs_fix", "needs_user"):
            result = valid_result(status)
            result["risks"] = ["Review pending"]
            result["requests"] = ["User input required"]
            with self.subTest(status=status):
                self.assertEqual(validate_result(result), result)

    def test_empty_strings_and_arrays_are_valid_schema_values(self):
        result = valid_result()
        result.update(summary="", changed_files=[], risks=[""], requests=[])
        self.assertEqual(validate_result(result), result)

    def test_non_objects_are_rejected(self):
        for result in (None, True, 1, "ok", [], [valid_result()]):
            with self.subTest(result=result), self.assertRaises(ValueError):
                validate_result(result)

    def test_each_missing_or_extra_field_is_rejected(self):
        for field in valid_result():
            result = valid_result()
            del result[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                validate_result(result)
        result = valid_result()
        result["extra"] = "ignored?"
        with self.assertRaises(ValueError):
            validate_result(result)

    def test_wrong_scalar_types_and_statuses_are_rejected(self):
        for status in ("success", "OK", "", None, False, 0, [], {}):
            with self.subTest(status=status), self.assertRaises(ValueError):
                validate_result(dict(valid_result(), status=status))
        for summary in (None, True, 1, [], {}):
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                validate_result(dict(valid_result(), summary=summary))

    def test_each_array_requires_a_list_of_strings(self):
        for field in ("changed_files", "risks", "requests"):
            for value in (None, "file.py", {}, (), [None], [False], [1], [[]], ["ok", {}]):
                result = valid_result()
                result[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    validate_result(result)


class ReadResultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run_dir = Path(self.temporary.name)

    def write_output(self, agent, output):
        filename = "final.json" if agent == "codex" else "events.jsonl"
        (self.run_dir / filename).write_text(output, encoding="utf-8")

    def test_codex_reads_final_json_instead_of_events(self):
        result = valid_result("needs_fix")
        self.write_output("codex", json.dumps(result))
        self.write_output("claude", "not JSON")
        self.assertEqual(read_result("codex", self.run_dir), result)

    def test_claude_reads_pretty_printed_structured_envelope(self):
        result = valid_result("needs_user")
        self.write_output("claude", json.dumps({
            "type": "result", "is_error": False,
            "structured_output": result, "result": "non-JSON commentary",
        }, indent=2, ensure_ascii=False))
        self.write_output("codex", "not JSON")
        self.assertEqual(read_result("claude", self.run_dir), result)

    def test_claude_result_fallback_accepts_json_string_or_object(self):
        result = valid_result()
        for fallback in (result, json.dumps(result)):
            with self.subTest(fallback=fallback):
                self.write_output("claude", json.dumps({"is_error": False, "result": fallback}))
                self.assertEqual(read_result("claude", self.run_dir), result)

    def test_claude_error_cannot_be_hidden_by_valid_result(self):
        for field in ("structured_output", "result"):
            self.write_output("claude", json.dumps({"is_error": True, field: valid_result()}))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "error"):
                read_result("claude", self.run_dir)

    def test_claude_malformed_is_error_is_rejected(self):
        for error in (None, "false", "true", 0, 1, [], {}):
            self.write_output("claude", json.dumps({
                "is_error": error, "structured_output": valid_result(),
            }))
            with self.subTest(error=error), self.assertRaises(ValueError):
                read_result("claude", self.run_dir)

    def test_claude_invalid_envelopes_and_fallbacks_are_rejected(self):
        for envelope in (
            None, [], valid_result(), {}, {"is_error": False},
            {"structured_output": None}, {"structured_output": "not an object"},
            {"result": ""}, {"result": "not JSON"}, {"result": "```json\n{}\n```"},
            {"result": "{}"}, {"result": None}, {"result": False},
            {"structured_output": {}, "result": json.dumps(valid_result())},
        ):
            self.write_output("claude", json.dumps(envelope))
            with self.subTest(envelope=envelope), self.assertRaises(ValueError):
                read_result("claude", self.run_dir)

    def test_both_agents_reject_schema_invalid_results(self):
        for agent in ("codex", "claude"):
            for result in ({}, dict(valid_result(), status="failed"), dict(valid_result(), risks=[0])):
                output = result if agent == "codex" else {"structured_output": result}
                self.write_output(agent, json.dumps(output))
                with self.subTest(agent=agent, result=result), self.assertRaises(ValueError):
                    read_result(agent, self.run_dir)

    def test_codex_error_envelope_is_rejected(self):
        self.write_output("codex", json.dumps({"type": "error", "message": "worker failed"}))
        with self.assertRaises(ValueError):
            read_result("codex", self.run_dir)

    def test_both_agents_reject_empty_malformed_or_multiple_json_documents(self):
        for agent in ("codex", "claude"):
            for output in ("", " \n", "not JSON", "{", "{}\n{}", "```json\n{}\n```"):
                self.write_output(agent, output)
                with self.subTest(agent=agent, output=output), self.assertRaises(ValueError):
                    read_result(agent, self.run_dir)

    def test_duplicate_keys_and_non_json_constants_are_rejected(self):
        result = json.dumps(valid_result())
        for agent, output in (
            ("codex", result.replace('"status": "ok"', '"status": "needs_fix", "status": "ok"')),
            ("claude", '{"is_error": true, "is_error": false, "structured_output": ' + result + '}'),
            ("claude", '{"structured_output": ' + result + ', "cost": NaN}'),
            ("claude", json.dumps({"result": result.replace('"status": "ok"', '"status": "bad", "status": "ok"')})),
        ):
            self.write_output(agent, output)
            with self.subTest(agent=agent, output=output), self.assertRaises(ValueError):
                read_result(agent, self.run_dir)

    def test_missing_result_and_invalid_utf8_are_rejected(self):
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent), self.assertRaises(ValueError):
                read_result(agent, self.run_dir)
        for filename, agent in (("final.json", "codex"), ("events.jsonl", "claude")):
            (self.run_dir / filename).write_bytes(b"\xff")
            with self.subTest(agent=agent), self.assertRaises(ValueError):
                read_result(agent, self.run_dir)

    def test_unknown_agent_is_rejected(self):
        with self.assertRaises(ValueError):
            read_result("other", self.run_dir)


class StartWorkerTests(unittest.TestCase):
    def test_start_uses_stdin_logs_worktree_and_closes_parent_handles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree = root / "work tree"
            worktree.mkdir()
            prompt = "Full prompt\n$(never-execute); `never-execute`\n"
            for agent, role in (("codex", "build"), ("claude", "review")):
                run_dir = root / agent / "run"
                worker = Mock()
                opened = []

                def launch(argv, **kwargs):
                    self.assertEqual(kwargs["cwd"], worktree)
                    self.assertIs(kwargs["start_new_session"], True)
                    self.assertIs(kwargs["shell"], False)
                    self.assertEqual(kwargs["stdin"].read(), prompt)
                    for name, filename in (
                        ("stdin", "prompt.txt"), ("stdout", "events.jsonl"), ("stderr", "stderr.log")
                    ):
                        handle = kwargs[name]
                        self.assertFalse(handle.closed)
                        self.assertEqual(Path(handle.name), run_dir / filename)
                        opened.append(handle)
                    self.assertEqual(argv[0], "/bin/" + agent)
                    if agent == "claude":
                        self.assertEqual(flag_value(argv, "--tools"), "Read,Glob,Grep")
                        self.assertEqual(flag_value(argv, "--max-budget-usd"), "0.75")
                    kwargs["stdout"].write('{"event":"test"}\n')
                    kwargs["stderr"].write("test diagnostic\n")
                    return worker

                with self.subTest(agent=agent), \
                        patch("duet.agents.shutil.which", return_value="/bin/" + agent), \
                        patch("duet.agents.subprocess.Popen", side_effect=launch) as popen:
                    result = start_worker(agent, worktree, prompt, run_dir, role=role, claude_budget=0.75)
                    self.assertIs(result, worker)
                    popen.assert_called_once()
                self.assertTrue(all(handle.closed for handle in opened))
                self.assertEqual((run_dir / "prompt.txt").read_text(encoding="utf-8"), prompt)
                self.assertEqual(json.loads((run_dir / "result-schema.json").read_text()), RESULT_SCHEMA)
                self.assertEqual((run_dir / "events.jsonl").read_text(), '{"event":"test"}\n')
                self.assertEqual((run_dir / "stderr.log").read_text(), "test diagnostic\n")

    def test_launch_failure_closes_parent_handles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            opened = []

            def fail_launch(argv, **kwargs):
                opened.extend(kwargs[name] for name in ("stdin", "stdout", "stderr"))
                raise OSError("launch failed")

            with patch("duet.agents.shutil.which", return_value="/bin/codex"), \
                    patch("duet.agents.subprocess.Popen", side_effect=fail_launch), \
                    self.assertRaisesRegex(OSError, "launch failed"):
                start_worker("codex", root, "prompt", run_dir)
            self.assertEqual(len(opened), 3)
            self.assertTrue(all(handle.closed for handle in opened))
            self.assertTrue((run_dir / "prompt.txt").exists())
            self.assertTrue((run_dir / "result-schema.json").exists())

    def test_missing_cli_never_launches_or_creates_run_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            with patch("duet.agents.shutil.which", return_value=None), \
                    patch("duet.agents.subprocess.Popen") as popen, \
                    self.assertRaises(FileNotFoundError):
                start_worker("claude", root, "prompt", run_dir)
            popen.assert_not_called()
            self.assertFalse(run_dir.exists())


if __name__ == "__main__":
    unittest.main()
