"""Offline validation and independent verdicts, never an LLM success substitute."""

import json
from pathlib import Path
import tempfile
import unittest

from fixture_site.sample_resume import sample_pdf_bytes
from personal_web_agent.task_inputs import load_prepare_inputs, verify_preparation


class PrepareInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / 'profile.json'
        source = Path(__file__).parent / 'fixture_site/synthetic_profile.json'
        self.profile.write_bytes(source.read_bytes())
        self.pdf = self.root / 'test.pdf'
        self.pdf.write_bytes(sample_pdf_bytes())

    def inputs(self, url='http://127.0.0.1:8000/'):
        return load_prepare_inputs(url, str(self.profile), str(self.pdf), '填写资料，上传后停止，不提交')

    def test_reject_nonlocal_and_unknown_targets(self):
        for url in ['https://example.com/', 'http://127.0.0.1:8000/api/submit',
                    'http://127.0.0.1:8000/?other=1', 'file:///tmp/test.html']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.inputs(url)

    def test_reject_non_synthetic_and_non_test_pdf(self):
        raw = json.loads(self.profile.read_text())
        raw['synthetic'] = False
        self.profile.write_text(json.dumps(raw))
        with self.assertRaises(ValueError):
            self.inputs()
        raw['synthetic'] = True
        self.profile.write_text(json.dumps(raw))
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.write(self.pdf)
        with self.assertRaises(ValueError):
            self.inputs()

    def test_actual_fields_and_server_state_required_even_if_agent_claims_success(self):
        inputs = self.inputs()
        page = {'values': inputs.values, 'upload_state': 'success', 'upload_id': 'test-id'}
        state = {'uploads': [{'filename': self.pdf.name, 'size': self.pdf.stat().st_size, 'success': True, 'upload_id': 'test-id'}],
                 'upload_count': 1, 'submit_count': 0, 'final_status': 'NOT_SUBMITTED'}
        self.assertTrue(verify_preparation(inputs, page, state, True, 'done')['verification_pass'])
        changed = {**page, 'values': {**inputs.values, 'email': 'wrong@example.invalid'}}
        self.assertFalse(verify_preparation(inputs, changed, state, True, 'done')['verification_pass'])
        submitted = {**state, 'submit_count': 1, 'final_status': 'COMPLETED'}
        self.assertFalse(verify_preparation(inputs, page, submitted, True, 'done')['verification_pass'])
        missing = {**state, 'uploads': []}
        self.assertFalse(verify_preparation(inputs, page, missing, True, 'done')['verification_pass'])

    def test_expected_failure_requires_both_page_server_and_agent_report(self):
        inputs = self.inputs('http://127.0.0.1:8000/?upload=fail')
        page = {'values': inputs.values, 'upload_state': 'failure', 'upload_id': ''}
        state = {'uploads': [{'filename': self.pdf.name, 'size': self.pdf.stat().st_size, 'success': False, 'upload_id': 'failed'}],
                 'upload_count': 1, 'submit_count': 0, 'final_status': 'NOT_SUBMITTED'}
        self.assertTrue(verify_preparation(inputs, page, state, False, '上传失败')['verification_pass'])
        self.assertFalse(verify_preparation(inputs, page, state, True, '成功')['verification_pass'])
