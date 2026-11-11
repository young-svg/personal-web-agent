"""Offline persistence tests; HTTP submissions below are simulated, never human E2E."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from personal_web_agent.records import TaskStore, TaskBlocked, identity
from personal_web_agent.runner import run_apply_prepare
import test_handoff


def inputs():
    return SimpleNamespace(url='http://127.0.0.1:8000/', values={'email': 'synthetic@example.invalid'})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'tasks.sqlite3'
        self.store = TaskStore(self.path)

    def create(self):
        return self.store.create(inputs())

    def advance(self, task_id):
        for state in ['RUNNING', 'REVIEW_REQUIRED', 'OUTCOME_PENDING']:
            self.store.transition(task_id, state, state.lower())

    def test_create_query_private_and_redacted(self):
        task = self.store.get(self.create())
        self.assertEqual(task['state'], 'CREATED')
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('synthetic@example.invalid', self.path.read_bytes().decode(errors='ignore'))
        self.assertEqual(task['site'], 'http://127.0.0.1:8000')
        self.assertTrue(task['created_at'])

    def test_illegal_transitions_and_arbitrary_summary(self):
        task = self.create()
        for state, summary in [('COMPLETED', 'verified_completed'), ('RUNNING', 'secret arbitrary error')]:
            with self.assertRaises(ValueError):
                self.store.transition(task, state, summary)
        self.assertEqual(self.store.get(task)['state'], 'CREATED')

    def test_completed_blocks_even_explicit_retry(self):
        task = self.create()
        self.advance(task)
        self.store.transition(task, 'COMPLETED', 'verified_completed')
        with self.assertRaises(TaskBlocked):
            self.store.create(inputs(), confirm_no_side_effects=True)

    def test_all_unfinished_and_unknown_states_block(self):
        for state in ['CREATED', 'RUNNING', 'REVIEW_REQUIRED', 'OUTCOME_PENDING', 'UNKNOWN']:
            with self.subTest(state=state):
                store = TaskStore(Path(self.tmp.name) / (state + '.sqlite3'))
                task = store.create(inputs())
                if state != 'CREATED':
                    for next_state in ['RUNNING', 'REVIEW_REQUIRED', 'OUTCOME_PENDING']:
                        if state == 'UNKNOWN':
                            store.transition(task, 'UNKNOWN', 'unverified_exit')
                            break
                        store.transition(task, next_state, next_state.lower())
                        if next_state == state:
                            break
                with self.assertRaises(TaskBlocked):
                    store.create(inputs(), confirm_no_side_effects=True)
                if state == 'UNKNOWN':
                    self.assertEqual(store.get(task)['state'], 'UNKNOWN')
                    with self.assertRaises(ValueError):
                        store.transition(task, 'COMPLETED', 'verified_completed')

    def test_failed_cancelled_require_evidence_and_explicit_retry(self):
        for state in ['FAILED', 'CANCELLED']:
            with self.subTest(state=state):
                store = TaskStore(Path(self.tmp.name) / (state + '.sqlite3'))
                task = store.create(inputs())
                store.transition(task, 'RUNNING', 'running')
                if state == 'CANCELLED':
                    store.transition(task, 'REVIEW_REQUIRED', 'review_required')
                code = 'verified_cancelled' if state == 'CANCELLED' else 'verified_no_effect_failure'
                with self.assertRaises(ValueError):
                    store.transition(task, state, code)
                store.transition(task, state, code, no_side_effects=True)
                with self.assertRaises(TaskBlocked):
                    store.create(inputs())
                new = store.create(inputs(), confirm_no_side_effects=True)
                self.assertNotEqual(task, new)
                self.assertEqual(store.get(task)['state'], state)
                with self.assertRaises(TaskBlocked):
                    store.create(inputs(), confirm_no_side_effects=True)

    def test_atomic_duplicate_creation(self):
        def create(_):
            try:
                return TaskStore(self.path).create(inputs())
            except TaskBlocked:
                return None
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(create, range(4)))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_query_modes_pdf_and_instruction_do_not_bypass_identity(self):
        other = inputs()
        other.url += '?order=reordered&upload=fail'
        self.assertEqual(identity(inputs())[2], identity(other)[2])
        self.assertEqual(identity(other)[1], other.url)

    def test_process_exit_after_pending_and_restart_blocks(self):
        code = '''import os,sys
from types import SimpleNamespace
from personal_web_agent.records import TaskStore
s=TaskStore(sys.argv[1]); t=s.create(SimpleNamespace(url='http://127.0.0.1:8000/',values={'email':'synthetic@example.invalid'}))
for state in ['RUNNING','REVIEW_REQUIRED','OUTCOME_PENDING']: s.transition(t,state,state.lower())
os._exit(73)
'''
        proc = subprocess.run([sys.executable, '-c', code, str(self.path)], capture_output=True)
        self.assertEqual(proc.returncode, 73, proc.stderr)
        task = TaskStore(self.path).list()[0]
        self.assertEqual(task['state'], 'OUTCOME_PENDING')
        with self.assertRaises(TaskBlocked):
            TaskStore(self.path).create(inputs(), confirm_no_side_effects=True)
        query = subprocess.run([sys.executable, '-c',
            'import json,sys; from personal_web_agent.records import TaskStore; print(json.dumps(TaskStore(sys.argv[1]).list()))', str(self.path)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(query.stdout)[0]['state'], 'OUTCOME_PENDING')


class PersistentHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_handoff.HandoffProtocolTests.asyncSetUp(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name) / 'tasks.sqlite3')
        self.task = self.store.create(inputs())
        self.store.transition(self.task, 'RUNNING', 'running')
        self.store.transition(self.task, 'REVIEW_REQUIRED', 'review_required')

    async def asyncTearDown(self):
        await test_handoff.HandoffProtocolTests.asyncTearDown(self)

    def record(self, state):
        code = {'OUTCOME_PENDING': 'outcome_pending', 'COMPLETED': 'verified_completed',
                'CANCELLED': 'verified_cancelled', 'UNKNOWN': 'unverified_exit'}[state]
        self.store.transition(self.task, state, code, no_side_effects=state == 'CANCELLED')

    async def flow(self, choose, record=None):
        result = await test_handoff.HandoffProtocolTests.run_flow(self, choose, record_state=record or self.record)
        self.record(result['stage'])
        return result

    async def test_commit_visible_before_authorization_then_success(self):
        choices = iter(['approve', 'done'])
        async def choose(_):
            decision = next(choices)
            if decision == 'done':
                self.assertEqual(TaskStore(self.store.path).get(self.task)['state'], 'OUTCOME_PENDING')
                record = (await self.client.post('/api/submit', data={**self.page_data['values'], 'upload_id': self.upload_id})).json()
                self.page_data.update(receipt_state='success', receipt_id=record['submission_id'], receipt_text=record['submission_id'])
            return decision
        original_post = self.client.post
        async def post(url, **kwargs):
            if url == '/api/authorize':
                self.assertEqual(TaskStore(self.store.path).get(self.task)['state'], 'OUTCOME_PENDING')
            return await original_post(url, **kwargs)
        with patch.object(self.client, 'post', side_effect=post):
            result = await self.flow(choose)
        self.assertEqual(result['stage'], 'COMPLETED')
        self.assertEqual(self.store.get(self.task)['state'], 'COMPLETED')
        self.assertEqual(result['state']['successful_submit_count'], 1)

    async def test_database_failure_prevents_authorization(self):
        async def choose(_): return 'approve'
        def broken(_): raise OSError('simulated disk failure')
        result = await self.flow(choose, record=broken)
        self.assertFalse(result['authorized'])
        self.assertEqual(result['state']['successful_submit_count'], 0)
        self.assertEqual(self.store.get(self.task)['state'], 'UNKNOWN')

    async def test_cancel_persists_with_zero_success(self):
        async def choose(_): return 'cancel'
        result = await self.flow(choose)
        self.assertEqual(self.store.get(self.task)['state'], 'CANCELLED')
        self.assertEqual(result['state']['submit_attempt_count'], 0)

    async def test_missing_receipt_remains_unknown(self):
        choices = iter(['approve', 'done'])
        async def choose(_): return next(choices)
        result = await self.flow(choose)
        self.assertEqual(result['stage'], 'UNKNOWN')
        self.assertEqual(self.store.get(self.task)['state'], 'UNKNOWN')
        with self.assertRaises(TaskBlocked):
            self.store.create(inputs(), confirm_no_side_effects=True)


class RunnerJournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_interruption_marks_unknown_and_duplicate_never_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / 'tasks.sqlite3')
            with patch('personal_web_agent.records.TaskStore', return_value=store), \
                 patch('personal_web_agent.runner.sys.stdin.isatty', return_value=True), \
                 patch('personal_web_agent.task_inputs.preparation_task', return_value='synthetic task'), \
                 patch('personal_web_agent.runner._run_agent', side_effect=asyncio.CancelledError) as run:
                with self.assertRaises(asyncio.CancelledError):
                    await run_apply_prepare(inputs(), human_handoff=True)
                self.assertEqual(store.list()[0]['state'], 'UNKNOWN')
                self.assertEqual(await run_apply_prepare(inputs(), human_handoff=True), 2)
                self.assertEqual(run.call_count, 1)

    async def test_runner_journals_each_handoff_outcome(self):
        for outcome in ['COMPLETED', 'CANCELLED', 'UNKNOWN', 'FAILED']:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                store = TaskStore(Path(directory) / 'tasks.sqlite3')
                async def fake_run(**kwargs):
                    record = kwargs['record_state']
                    self.assertEqual(store.list()[0]['state'], 'RUNNING')
                    if outcome != 'FAILED':
                        record('REVIEW_REQUIRED')
                        self.assertEqual(store.list()[0]['state'], 'REVIEW_REQUIRED')
                        if outcome != 'CANCELLED':
                            record('OUTCOME_PENDING')
                    record(outcome)
                    return 0 if outcome in {'COMPLETED', 'CANCELLED'} else 1
                with patch('personal_web_agent.records.TaskStore', return_value=store), \
                     patch('personal_web_agent.runner.sys.stdin.isatty', return_value=True), \
                     patch('personal_web_agent.task_inputs.preparation_task', return_value='synthetic'), \
                     patch('personal_web_agent.runner._run_agent', side_effect=fake_run):
                    await run_apply_prepare(inputs(), human_handoff=True)
                self.assertEqual(store.list()[0]['state'], outcome)

    async def test_missing_key_is_known_failure_without_loading_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / 'tasks.sqlite3')
            with patch('personal_web_agent.records.TaskStore', return_value=store), \
                 patch('personal_web_agent.runner.sys.stdin.isatty', return_value=True), \
                 patch('personal_web_agent.task_inputs.preparation_task', return_value='synthetic'), \
                 patch('personal_web_agent.runner.dotenv_values', return_value={}):
                self.assertEqual(await run_apply_prepare(inputs(), human_handoff=True), 2)
            self.assertEqual(store.list()[0]['state'], 'FAILED')
            self.assertEqual(store.list()[0]['no_side_effects'], 1)
