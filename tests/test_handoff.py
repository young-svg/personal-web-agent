"""Offline protocol tests. Simulated terminal/HTTP events are NOT human evidence."""

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from starlette.testclient import TestClient
from fixture_site.app import create_app
from fixture_site.sample_resume import sample_pdf_bytes
from test_fixture_site import PROFILE, CONTROL
from personal_web_agent.approval import handoff


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app('offline-test-control'))
        self.addCleanup(self.client.close)
        self.upload_id = self.client.post('/api/upload', files={'resume': ('test.pdf', sample_pdf_bytes(), 'application/pdf')}).json()['upload_id']

    def submit(self):
        return self.client.post('/api/submit', data={**PROFILE, 'upload_id': self.upload_id})

    def state(self):
        return self.client.get('/api/state').json()

    def test_locked_submit_cannot_succeed_and_upload_is_unaffected(self):
        self.assertEqual(self.submit().status_code, 403)
        state = self.state()
        self.assertEqual((state['submit_attempt_count'], state['successful_submit_count'], state['upload_count']), (1, 0, 1))

    def test_control_endpoints_reject_missing_credentials(self):
        for route in ['/api/authorize', '/api/cancel', '/api/reset']:
            self.assertEqual(self.client.post(route, json={'upload_id': self.upload_id}).status_code, 403)
        self.assertEqual(self.state()['approval_status'], 'LOCKED')

    def test_cancel_revokes_authorization_and_cannot_be_reapproved(self):
        self.client.post('/api/authorize', headers=CONTROL, json={'upload_id': self.upload_id}).raise_for_status()
        self.client.post('/api/cancel', headers=CONTROL).raise_for_status()
        self.assertEqual(self.submit().status_code, 403)
        self.assertEqual(self.state()['successful_submit_count'], 0)
        self.assertEqual(self.client.post('/api/authorize', headers=CONTROL, json={'upload_id': self.upload_id}).status_code, 409)

    def test_once_only_and_later_rejection_does_not_erase_success(self):
        self.client.post('/api/authorize', headers=CONTROL, json={'upload_id': self.upload_id}).raise_for_status()
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.submit().status_code, 409)
        state = self.state()
        self.assertEqual((state['submit_attempt_count'], state['successful_submit_count']), (2, 1))
        self.assertEqual(state['final_status'], 'COMPLETED')
        self.assertEqual(self.client.post('/api/cancel', headers=CONTROL).status_code, 409)
        self.assertEqual(self.client.post('/api/authorize', headers=CONTROL, json={'upload_id': self.upload_id}).status_code, 409)


class HandoffProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app('offline-test-control')), base_url='http://127.0.0.1:8000')
        self.upload_id = (await self.client.post('/api/upload', files={'resume': ('test.pdf', sample_pdf_bytes(), 'application/pdf')})).json()['upload_id']
        self.page_data = {'target_id': 'same-test-target', 'url': 'http://127.0.0.1:8000/', 'values': dict(PROFILE),
                          'upload_id': self.upload_id, 'upload_state': 'success', 'receipt_state': 'idle', 'receipt_id': '', 'receipt_text': ''}
        self.saved = []

    async def asyncTearDown(self):
        await self.client.aclose()

    async def run_flow(self, choose, **kwargs):
        with patch('personal_web_agent.approval.read_form', new=AsyncMock(side_effect=lambda _: dict(self.page_data))):
            return await handoff(object(), 'http://127.0.0.1:8000', 'offline-test-control', dict(self.page_data),
                                 lambda name, value: self.saved.append(copy.deepcopy(value)), choose=choose, client=self.client, **kwargs)

    async def test_cancel_without_submit(self):
        async def choose(_): return 'cancel'
        result = await self.run_flow(choose)
        self.assertEqual(result['stage'], 'CANCELLED')
        self.assertEqual(result['state']['successful_submit_count'], 0)
        self.assertEqual(result['state']['approval_status'], 'CANCELLED')

    async def test_approved_http_submission_and_receipt_protocol(self):
        decisions = iter(['approve', 'done'])
        async def choose(_):
            decision = next(decisions)
            if decision == 'done':
                response = await self.client.post('/api/submit', data={**PROFILE, 'upload_id': self.upload_id})
                response.raise_for_status()
                record = response.json()
                self.page_data.update(receipt_state='success', receipt_id=record['submission_id'], receipt_text='回执 ' + record['submission_id'])
            return decision
        result = await self.run_flow(choose)
        self.assertEqual(result['stage'], 'COMPLETED')
        self.assertEqual(result['state']['successful_submit_count'], 1)
        self.assertEqual(result['page']['target_id'], 'same-test-target')

    async def test_eof_after_approval_revokes_unused_grant(self):
        decisions = iter(['approve', ''])
        async def choose(_): return next(decisions)
        result = await self.run_flow(choose)
        self.assertEqual(result['stage'], 'CANCELLED')
        self.assertEqual(result['state']['approval_status'], 'CANCELLED')

    async def test_parallel_submissions_can_only_succeed_once(self):
        await self.client.post('/api/authorize', headers=CONTROL, json={'upload_id': self.upload_id})
        responses = await asyncio.gather(*(self.client.post('/api/submit', data={**PROFILE, 'upload_id': self.upload_id}) for _ in range(4)))
        self.assertEqual(sum(r.status_code == 200 for r in responses), 1)
        state = (await self.client.get('/api/state')).json()
        self.assertEqual((state['submit_attempt_count'], state['successful_submit_count']), (4, 1))

    async def test_cancel_after_success_cannot_be_reported_as_cancelled(self):
        decisions = iter(['approve', 'cancel'])
        async def choose(_):
            decision = next(decisions)
            if decision == 'cancel':
                record = (await self.client.post('/api/submit', data={**PROFILE, 'upload_id': self.upload_id})).json()
                self.page_data.update(receipt_state='success', receipt_id=record['submission_id'], receipt_text=record['submission_id'])
            return decision
        result = await self.run_flow(choose)
        self.assertEqual(result['stage'], 'COMPLETED')
        self.assertEqual(result['state']['successful_submit_count'], 1)

    async def test_timeout_revokes_without_any_submission(self):
        async def choose(_):
            await asyncio.sleep(1)
        with patch('personal_web_agent.approval.read_form', new=AsyncMock(side_effect=lambda _: dict(self.page_data))):
            result = await handoff(object(), 'http://127.0.0.1:8000', 'offline-test-control', dict(self.page_data),
                                   lambda *args: None, choose=choose, client=self.client, timeout=0.01)
        self.assertEqual(result['stage'], 'UNKNOWN')
        self.assertEqual(result['state']['approval_status'], 'CANCELLED')
        self.assertEqual(result['state']['successful_submit_count'], 0)
