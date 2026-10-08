"""CLI accounting and launch boundaries, not live paid calls."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from duet.usage import normalize, run_usage, task_usage
from duet.controller import Controller, spec_checked


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_files(self, agent, events, completed=True, name='1-build'):
        folder = self.root / 'runs' / 'task-test' / name
        folder.mkdir(parents=True)
        (folder / 'job.json').write_text(json.dumps({'agent': agent, 'prompt_chars': 1234}))
        (folder / 'events.jsonl').write_text(events)
        if completed: (folder / 'exit.json').write_text('{"returncode":0}')
        return folder

    def test_codex_cache_is_subset_not_double_counted_and_streaming_tail_ignored(self):
        event = {'type':'turn.completed', 'usage': {'input_tokens':1000,'output_tokens':100,'cached_input_tokens':700}}
        folder = self.run_files('codex', json.dumps(event) + '\n' + json.dumps(event) + '\n{"partial":')
        self.assertEqual(run_usage(folder,'codex'), {'input':2000,'output':200,'cached_input':1400,'total':2200})

    def test_claude_cache_counts_are_separate_and_price_is_reported(self):
        event = {'usage': {'input_tokens':100,'output_tokens':50,'cache_read_input_tokens':500,'cache_creation_input_tokens':200},'total_cost_usd':0.03}
        folder = self.run_files('claude',json.dumps(event))
        self.assertEqual(run_usage(folder,'claude'), {'input':800,'output':50,'cached_input':500,'total':850,'cost_usd':0.03})

    def test_missing_metrics_are_unknown_not_zero_and_failed_runs_still_count(self):
        self.run_files('claude','{"is_error":true}')
        folder = self.run_files('codex',json.dumps({'type':'turn.completed','usage':{'input_tokens':200,'output_tokens':10}}),name='2-build')
        (folder / 'exit.json').write_text('{"returncode":1}')
        usage = task_usage(self.root,'task-test')
        self.assertEqual(usage['total'],210)
        self.assertEqual(usage['missing_runs'],1)
        self.assertFalse(usage['complete'])
        self.assertIsNone(usage['cost_usd'])

    def test_negative_bool_invalid_and_nonfinite_metrics_rejected(self):
        for value in ({'input_tokens':True,'output_tokens':1},{'input_tokens':-1,'output_tokens':1},
                      {'input_tokens':1,'output_tokens':1,'cached_input_tokens':5}):
            self.assertIsNone(normalize(value,'codex'))
        self.assertNotIn('cost_usd',normalize({'input_tokens':1,'output_tokens':1},'claude',float('nan')))

    def test_limits_validate_without_modifying_legacy_tasks(self):
        spec = spec_checked({'title':'x','prompt':'x'})
        self.assertIsNone(spec['token_budget'])
        for fields in ({'token_budget': True},{'token_budget':999},{'context_chars':1000},{'context_chars':float('inf')}):
            with self.assertRaises(ValueError): spec_checked({'title':'x','prompt':'x',**fields})

    def controller(self):
        repo = self.root / 'repo'; repo.mkdir()
        subprocess.run(['git','init','-q',str(repo)],check=True)
        (repo/'AGENTS.md').write_text('Rules\n' + 'supporting context\n' * 10000)
        subprocess.run(['git','-C',str(repo),'add','.'],check=True)
        subprocess.run(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','Initial'],check=True)
        controller = Controller(repo, root=self.root / 'coordination')
        self.addCleanup(controller.db.close)
        controller.enqueue({'id':'task-test','title':'Test','prompt':'User goal must remain complete', 'token_budget':1000,'context_chars':25000})
        return controller

    def test_context_shortens_supporting_text_but_keeps_goal_and_limits(self):
        controller=self.controller()
        prompt=controller.prompt(controller.task('task-test'),'build')
        self.assertLessEqual(len(prompt),25000)
        self.assertIn('User goal must remain complete',prompt)
        self.assertIn('context truncated',prompt)
        self.assertIn('Do not touch cloud resources',prompt)

    def test_exhausted_or_unknown_budget_never_launches_another_cli(self):
        controller=self.controller()
        folder=controller.root/'runs'/'task-test'/'1-build';folder.mkdir(parents=True)
        (folder/'job.json').write_text('{"agent":"codex"}')
        (folder/'exit.json').write_text('{"returncode":0}')
        with patch('duet.controller.subprocess.Popen') as popen:
            with self.assertRaisesRegex(ValueError,'did not report'): controller.launch(controller.task('task-test'))
            (folder/'events.jsonl').write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':1000,'output_tokens':10}}))
            with self.assertRaisesRegex(ValueError,'budget is spent'): controller.launch(controller.task('task-test'))
            popen.assert_not_called()

    def test_usage_numeric_fields_are_not_redacted_as_credentials(self):
        from duet.web import scrub
        usage=normalize({'input_tokens':200,'output_tokens':10},'codex')
        value=scrub({'usage':usage,'token':'secret-value'})
        self.assertEqual(value['usage']['total'],210)
        self.assertEqual(value['token'],'[redacted]')
