"""Endpoint tests: real ASGI handlers, independent upload/submission counters."""

from html.parser import HTMLParser
import unittest
from starlette.testclient import TestClient
from fixture_site.app import MAX_PDF_BYTES, NORMAL_ORDER, REORDERED, create_app
from fixture_site.sample_resume import sample_pdf_bytes

PROFILE = {"name": "虚构测试者", "email": "fixture@example.invalid", "contact": "000-0000-0000", "introduction": "仅用于本地测试的虚构介绍。"}
CONTROL = {'X-Fixture-Control': 'offline-test-control'}


class FieldParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if "data-field" in data:
            self.fields.append(data["data-field"])


class FixtureSiteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app('offline-test-control'))
        self.addCleanup(self.client.close)

    def state(self):
        return self.client.get('/api/state').json()

    def upload(self, **kwargs):
        return self.client.post('/api/upload' + kwargs.pop('query', ''),
                                files={'resume': ('fixture.pdf', kwargs.pop('content', sample_pdf_bytes()), 'application/pdf')}, **kwargs)

    def test_page_and_reordered_fields(self):
        for mode, order in [('', NORMAL_ORDER), ('?order=reordered', REORDERED)]:
            page = self.client.get('/' + mode)
            self.assertEqual(page.status_code, 200)
            self.assertEqual(FieldParser(page.text).fields, list(order))
            self.assertIn('type="submit">Submit', page.text)
            self.assertIn('id="upload-status" role="status"', page.text)
        self.assertEqual(self.state()['submit_count'], 0)

    def test_upload_success_without_submit(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        state = self.state()
        self.assertEqual(state['upload_count'], 1)
        self.assertEqual(state['submit_count'], 0)
        self.assertEqual(state['final_status'], 'NOT_SUBMITTED')

    def test_simulated_upload_failure_without_submit(self):
        page = self.client.get('/?order=reordered&upload=fail')
        self.assertIn("fetch('/api/upload?fail=1'", page.text)
        response = self.upload(query='?fail=1')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['success'])
        self.assertIn('模拟上传失败', response.json()['error'])
        self.assertEqual(self.state()['upload_count'], 1)
        self.assertEqual(self.state()['submit_count'], 0)

    def test_invalid_pdf_and_oversized_upload(self):
        for content, code in [(b'not a pdf', 400), (b'%PDF-invalid', 400), (b'%PDF-' + b'x' * MAX_PDF_BYTES, 413)]:
            self.assertEqual(self.upload(content=content).status_code, code)
        self.assertEqual(self.state()['upload_count'], 3)
        self.assertTrue(all(not u['success'] for u in self.state()['uploads']))
        self.assertEqual(self.state()['submit_count'], 0)

    def test_submit_increments_exactly_once_per_request(self):
        upload_id = self.upload().json()['upload_id']
        self.client.post('/api/authorize', headers=CONTROL, json={'upload_id': upload_id}).raise_for_status()
        for count in [1, 2]:
            response = self.client.post('/api/submit', data={**PROFILE, 'upload_id': upload_id})
            self.assertEqual(response.status_code, 200 if count == 1 else 409)
            self.assertEqual(response.json()['status'], 'COMPLETED' if count == 1 else 'REJECTED')
            self.assertTrue(response.json()['submission_id'])
            self.assertEqual(self.state()['submit_count'], count)
            self.assertEqual(self.state()['upload_count'], 1)
            self.assertEqual(self.state()['successful_submit_count'], 1)
        self.assertEqual(self.state()['final_status'], 'COMPLETED')

    def test_reject_missing_or_failed_upload(self):
        failed_id = self.upload(query='?fail=1').json()['upload_id']
        for upload_id in ['', failed_id]:
            response = self.client.post('/api/submit', data={**PROFILE, 'upload_id': upload_id})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.state()['submit_count'], 2)
        self.assertEqual(self.state()['successful_submit_count'], 0)

    def test_reset_invalidates_old_upload_ids_and_clears_records(self):
        upload_id = self.upload().json()['upload_id']
        self.client.post('/api/submit', data={**PROFILE, 'upload_id': upload_id})
        state = self.client.post('/api/reset', headers=CONTROL).json()
        self.assertEqual(state['upload_count'], 0)
        self.assertEqual(state['submit_count'], 0)
        self.assertEqual(state['successful_submit_count'], 0)
        self.assertEqual(state['uploads'], [])
        self.assertEqual(state['submissions'], [])
        self.assertEqual(state['approval_status'], 'LOCKED')
        self.assertEqual(self.client.post('/api/submit', data={**PROFILE, 'upload_id': upload_id}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
