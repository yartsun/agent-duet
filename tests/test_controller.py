"""Exercise orchestration using disposable Git repositories and fake CLI programs."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from duet.controller import Controller, git, spec_checked

RESULT = {'status':'ok','summary':'Checked','changed_files':[],'risks':[],'requests':[]}
FAKE = '''#!/usr/bin/env python3
import json, pathlib, sys, time
prompt=sys.stdin.read()
time.sleep(0.1)
result={"status":"ok","summary":"Fake worker completed","changed_files":[],"risks":[],"requests":[]}
if pathlib.Path(sys.argv[0]).name=='codex':
    out=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1])
    out.write_text(json.dumps(result))
    print(json.dumps({"type":"turn.completed"}))
else:
    print(json.dumps({"is_error":False,"structured_output":result}))
'''


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        git(self.repo,'config','user.name','Test'); git(self.repo,'config','user.email','test@example.invalid')
        (self.repo/'README.md').write_text('Initial\n')
        git(self.repo,'add','README.md'); git(self.repo,'commit','-qm','Initial')
        git(self.repo,'branch','develop')
        self.controller = Controller(self.repo)
        self.addCleanup(self.controller.db.close)

    def enqueue(self, task_id='one', **fields):
        return self.controller.enqueue({'id':task_id,'title':'Test','prompt':'Read README', **fields})

    def ready(self, task_id='one', **fields):
        self.enqueue(task_id, **fields)
        self.controller.prepare(self.controller.task(task_id))
        return self.controller.task(task_id)

    def result(self, task, role='build', result=None):
        folder=self.root/('run-'+task['id']+'-'+role); folder.mkdir(exist_ok=True)
        (folder/'final.json').write_text(json.dumps(result or RESULT))
        (folder/'events.jsonl').write_text(json.dumps({'is_error':False,'structured_output':result or RESULT}))
        (folder/'exit.json').write_text('{"returncode":0}')
        if role=='review':
            (folder/'review-snapshot.json').write_text(json.dumps(self.controller.fingerprint(task['worktree'])))
        self.controller.update(task['id'],state='running',phase=role,attempts=1,run_dir=str(folder),pid=None,deadline=9999999999)
        return self.controller.task(task['id'])

    def test_duplicate_and_unknown_dependency_never_launch(self):
        self.enqueue()
        with self.assertRaisesRegex(ValueError,'already exists'): self.enqueue()
        with self.assertRaisesRegex(ValueError,'not found'): self.enqueue('other',depends_on=['missing'])
        self.assertEqual(len(self.controller.rows()),1)

    def test_validation_rejects_shell_shaped_checks_and_path_escape(self):
        for fields in ({'checks':['echo yes']},{'allowed_paths':['../x']},{'max_fixes':99},{'agent':'other'}):
            with self.assertRaises(ValueError): spec_checked({'title':'x','prompt':'x',**fields})

    def test_single_controller_lock_is_shared_across_instances(self):
        other=Controller(self.repo); self.addCleanup(other.db.close)
        with self.controller.exclusive():
            with self.assertRaisesRegex(ValueError,'already running'):
                with other.exclusive(): pass

    def test_real_fake_clis_complete_build_review_checks_without_modifying_checkout(self):
        binaries=self.root/'bin'; binaries.mkdir()
        for name in ('codex','claude'):
            p=binaries/name; p.write_text(FAKE); p.chmod(0o755)
        self.enqueue(read_only=True)
        with patch.dict(os.environ,{'PATH':str(binaries)+os.pathsep+os.environ['PATH']}):
            rows=self.controller.run(max_seconds=10,poll=0.05)
        self.assertEqual(rows[0]['state'],'done')
        self.assertEqual(rows[0]['attempts'],1)
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
        runs=list((self.controller.root/'runs/one').iterdir())
        self.assertEqual({r.name for r in runs},{'1-build','1-review'})
        prompt=(self.controller.root/'runs/one/1-review/prompt.txt').read_text()
        self.assertIn('Fake worker completed',prompt)

    def test_dependencies_and_parallel_cap(self):
        self.enqueue('first'); self.enqueue('second',depends_on=['first']); self.enqueue('third')
        def fake_launch(task): self.controller.update(task['id'],state='running')
        with patch.object(self.controller,'launch',side_effect=fake_launch):
            self.assertEqual(self.controller.tick(max_parallel=2),2)
        self.assertEqual(self.controller.task('second')['state'],'queued')
        self.controller.update('first',state='failed')
        with patch.object(self.controller,'finish_worker'):
            self.controller.tick()
        self.assertEqual(self.controller.task('second')['state'],'blocked')

    def test_out_of_scope_changes_stop_before_review(self):
        task=self.ready(allowed_paths=['approved.md'])
        (Path(task['worktree'])/'wrong.md').write_text('changed')
        self.controller.finish_worker(self.result(task))
        self.assertEqual(self.controller.task('one')['state'],'needs_user')
        self.assertTrue((Path(task['worktree'])/'wrong.md').exists())

    def test_user_request_is_recorded_and_not_executed(self):
        task=self.ready()
        result={**RESULT,'requests':['Download a model']}
        self.controller.finish_worker(self.result(task,result=result))
        self.assertEqual(self.controller.task('one')['state'],'needs_user')

    def test_reviewer_mutating_existing_contents_is_rejected(self):
        task=self.ready(allowed_paths=['README.md'])
        (Path(task['worktree'])/'README.md').write_text('Builder\n')
        running=self.result(task,role='review')
        (Path(task['worktree'])/'README.md').write_text('Reviewer changed it\n')
        self.controller.finish_worker(running)
        self.assertEqual(self.controller.task('one')['state'],'needs_user')

    def test_fix_retries_have_a_hard_limit(self):
        task=self.ready(max_fixes=1)
        self.controller.update('one',attempts=1)
        self.controller.needs_fix(self.controller.task('one'),'Issue')
        self.assertEqual(self.controller.task('one')['state'],'queued')
        self.controller.update('one',attempts=2)
        self.controller.needs_fix(self.controller.task('one'),'Still an issue')
        self.assertEqual(self.controller.task('one')['state'],'failed')

    def test_durable_exit_is_consumed_after_controller_restart_without_relaunch(self):
        task=self.ready(read_only=True)
        self.result(task)
        resumed=Controller(self.repo); self.addCleanup(resumed.db.close)
        resumed.finish_worker(resumed.task('one'))
        self.assertEqual(resumed.task('one')['phase'],'review')
        self.assertEqual(resumed.task('one')['state'],'queued')

    def test_interrupted_launch_is_not_requeued_or_duplicated(self):
        task=self.ready(); folder=self.root/'lost-launch'; folder.mkdir()
        self.controller.update('one',state='starting',run_dir=str(folder),deadline=0)
        with patch.object(self.controller,'launch') as launch:
            self.controller.tick()
        launch.assert_not_called()
        self.assertEqual(self.controller.task('one')['state'],'needs_user')

    def test_checks_commit_only_allowed_files_and_integrate_safely(self):
        task=self.ready(allowed_paths=['result.md'])
        folder=Path(task['worktree']); (folder/'result.md').write_text('Verified\n')
        self.controller.update('one',state='checking',attempts=1,result=json.dumps({**RESULT,'approved_snapshot':self.controller.fingerprint(task['worktree'])}))
        self.controller.checks(self.controller.task('one'))
        self.assertEqual(self.controller.task('one')['state'],'done')
        commit=self.controller.integrate('one')
        self.assertEqual(git(self.repo,'rev-parse','develop'),commit)
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
        self.assertEqual((self.repo/'README.md').read_text(),'Initial\n')

    def test_checks_failure_does_not_commit(self):
        task=self.ready(checks=[['python3','-c','raise SystemExit(1)']],max_fixes=0)
        self.controller.update('one',state='checking',attempts=1,result=json.dumps({**RESULT,'approved_snapshot':self.controller.fingerprint(task['worktree'])}))
        self.controller.checks(self.controller.task('one'))
        self.assertEqual(self.controller.task('one')['state'],'failed')
        self.assertEqual(git(task['worktree'],'rev-parse','HEAD'),task['base'])

    def test_cancel_request_works_while_controller_lock_is_held(self):
        self.enqueue()
        other=Controller(self.repo); self.addCleanup(other.db.close)
        with self.controller.exclusive():
            other.request_control('one','cancel')
            self.assertEqual(self.controller.task('one')['state'],'queued')
            self.controller.tick()
        self.assertEqual(self.controller.task('one')['state'],'cancelled')

    def test_retry_is_blocked_when_old_worker_exit_is_unconfirmed(self):
        task=self.ready(); folder=self.root/'unconfirmed'; folder.mkdir()
        self.controller.update('one',state='needs_user',run_dir=str(folder))
        self.controller.request_control('one','retry','Try again')
        self.assertEqual(self.controller.task('one')['state'],'needs_user')
        request=list((self.controller.root/'requests').glob('*.processed.json'))[0]
        self.assertEqual(json.loads(request.read_text())['outcome'],'rejected')

    def test_cancel_does_not_claim_shutdown_when_unconfirmed(self):
        task=self.ready(); folder=self.root/'unconfirmed'; folder.mkdir()
        self.controller.update('one',state='running',run_dir=str(folder))
        self.controller.request_control('one','cancel')
        self.assertEqual(self.controller.task('one')['state'],'running')

    def test_unaccepted_base_cannot_be_merged_into_develop(self):
        (self.repo/'other.md').write_text('Unrelated unaccepted change\n')
        git(self.repo,'add','other.md'); git(self.repo,'commit','-qm','Other branch work')
        task=self.ready(read_only=True)
        self.controller.update('one',state='checking',attempts=1,result=json.dumps({**RESULT,'approved_snapshot':self.controller.fingerprint(task['worktree'])}))
        self.controller.checks(self.controller.task('one'))
        before=git(self.repo,'rev-parse','develop')
        with self.assertRaisesRegex(ValueError,'is not in'):
            self.controller.integrate('one')
        self.assertEqual(git(self.repo,'rev-parse','develop'),before)

    def checking(self,task):
        self.controller.update(task['id'],state='checking',attempts=1,
            result=json.dumps({**RESULT,'approved_snapshot':self.controller.fingerprint(task['worktree'])}))
        return self.controller.task(task['id'])

    def test_hidden_index_content_cannot_enter_approved_commit(self):
        task=self.ready(allowed_paths=['**']); folder=Path(task['worktree'])
        (folder/'README.md').write_text('Unapproved staged text\n')
        git(folder,'add','README.md')
        (folder/'README.md').write_text('Initial\n')
        (folder/'result.md').write_text('Approved\n')
        self.controller.checks(self.checking(task))
        self.assertEqual(self.controller.task('one')['state'],'done')
        self.assertEqual(git(folder,'show','HEAD:README.md'),'Initial')

    def test_formatter_changes_require_new_review(self):
        task=self.ready(allowed_paths=['result.md'],checks=[['python3','-c',"from pathlib import Path;Path('result.md').write_text('Formatted\\n')"]])
        folder=Path(task['worktree']); (folder/'result.md').write_text('Before\n')
        self.controller.checks(self.checking(task))
        self.assertEqual(self.controller.task('one')['state'],'queued')
        self.assertEqual(self.controller.task('one')['phase'],'build')
        self.assertEqual(git(folder,'rev-parse','HEAD'),task['base'])

    def test_commit_intent_recovers_after_ref_update_before_database_completion(self):
        task=self.ready(allowed_paths=['result.md']); (Path(task['worktree'])/'result.md').write_text('Approved\n')
        with patch.object(self.controller,'complete_code',side_effect=RuntimeError('Simulated crash')):
            with self.assertRaisesRegex(RuntimeError,'Simulated crash'):
                self.controller.checks(self.checking(task))
        self.assertEqual(self.controller.task('one')['state'],'committing')
        resumed=Controller(self.repo); self.addCleanup(resumed.db.close)
        resumed.finalize_commit(resumed.task('one'))
        self.assertEqual(resumed.task('one')['state'],'done')
        self.assertEqual(git(task['worktree'],'show','HEAD:result.md'),'Approved')

    def test_dependency_unblocks_when_retried_parent_succeeds(self):
        self.enqueue('parent'); self.enqueue('child',depends_on=['parent'])
        self.controller.update('parent',state='needs_user')
        self.controller.tick()
        self.assertEqual(self.controller.task('child')['state'],'blocked')
        self.controller.update('parent',state='done')
        with patch.object(self.controller,'launch') as launch:
            self.controller.tick()
        self.assertEqual(launch.call_args.args[0]['id'],'child')

    def test_integration_checks_cannot_publish_a_mutated_worktree(self):
        task=self.ready(allowed_paths=['result.md']); (Path(task['worktree'])/'result.md').write_text('Approved\n')
        self.controller.checks(self.checking(task))
        row=self.controller.task('one'); spec=json.loads(row['spec'])
        spec['checks']=[['python3','-c',"from pathlib import Path;Path('result.md').write_text('Changed\\n')"]]
        self.controller.update('one',spec=json.dumps(spec))
        before=git(self.repo,'rev-parse','develop')
        with self.assertRaisesRegex(ValueError,'changed the integration'):
            self.controller.integrate('one')
        self.assertEqual(git(self.repo,'rev-parse','develop'),before)


if __name__=='__main__': unittest.main()
