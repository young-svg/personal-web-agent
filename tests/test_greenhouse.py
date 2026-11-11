"""Offline snapshot contract tests, no real site or model calls."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fixture_site.sample_resume import sample_pdf_bytes
from personal_web_agent.greenhouse import (load_inputs, analyze, preparation_task, job_identity, EXECUTION_BLOCK)
from personal_web_agent.records import TaskStore, TaskBlocked, identity
from personal_web_agent.runner import run_apply_prepare, _run_agent
from personal_web_agent import fixture

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'tests/snapshots/greenhouse_synthetic.json'
PROFILE = ROOT / 'tests/fixture_site/greenhouse_synthetic_profile.json'
URL = 'https://job-boards.greenhouse.io/sertis/jobs/7847750002'


class GreenhouseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.pdf = self.directory/'test.pdf'
        self.pdf.write_bytes(sample_pdf_bytes())
        self.inputs = load_inputs(URL, PROFILE, self.pdf, '仅做本地合成资料检查', SNAPSHOT)

    def changed(self, field, **updates):
        snapshot = copy.deepcopy(self.inputs.snapshot)
        for c in snapshot['controls']:
            if c.get('id') == field:
                c.update(updates)
        return replace(self.inputs, snapshot=snapshot)

    def test_job_company_stable_id_and_dropdown_binding(self):
        self.assertTrue(json.loads(SNAPSHOT.read_text())["synthetic"])
        bindings, report = analyze(self.inputs)
        self.assertEqual((report['company'],report['job_title'],report['job_id']), ('Fictional Test Company','Synthetic Test Role','7847750002'))
        self.assertTrue(report['data_ready'])
        self.assertFalse(report['execution_allowed'])
        by_field = {x['field']:x for x in bindings}
        self.assertEqual(by_field['country']['option'], {'label':'Synthetic Country CN','value':'cn'})
        self.assertEqual(by_field['visa_sponsorship']['option'], {'label':'Yes','value':'1'})
        self.assertEqual(by_field['resume']['kind'], 'upload_file')
        self.assertEqual(len([x for x in bindings if x['kind']=='upload_file']), 1)
        self.assertNotIn('cover_letter', by_field)

    def test_required_and_optional_missing_no_invention(self):
        values={k:v for k,v in self.inputs.values.items() if k not in {'visa_sponsorship','intern_start','intern_duration'}}
        changed=replace(self.inputs,values=values)
        bindings, report=analyze(changed)
        self.assertEqual(report['missing_required'], ['intern_duration','intern_start','visa_sponsorship'])
        self.assertFalse(report['data_ready'])
        self.assertNotIn('visa_sponsorship', [b['field'] for b in bindings])
        self.assertNotIn('linkedin', report['missing_required'])
        self.assertNotIn('Yes', preparation_task(changed,bindings))

    def test_order_and_generated_question_ids_do_not_matter(self):
        snapshot=copy.deepcopy(self.inputs.snapshot)
        snapshot['controls'].reverse()
        for c in snapshot['controls']:
            if c['id'].startswith('question_'):
                c['id']='changed_'+c['id']
        bindings, report=analyze(replace(self.inputs,snapshot=snapshot))
        self.assertTrue(report['data_ready'])
        self.assertTrue(next(b for b in bindings if b['field']=='intern_start')['element_id'].startswith('changed_'))

    def test_unknown_required_and_ambiguous_fields_block(self):
        snapshot=copy.deepcopy(self.inputs.snapshot)
        snapshot['controls'].append({'tag':'INPUT','label':'Education history*','id':'new','visible':True})
        self.assertFalse(analyze(replace(self.inputs,snapshot=snapshot))[1]['data_ready'])
        snapshot['controls'].append(copy.deepcopy(snapshot['controls'][1]))
        self.assertIn('first_name:ambiguous', analyze(replace(self.inputs,snapshot=snapshot))[1]['invalid_fields'])

    def test_invalid_and_unavailable_dropdown_options_block(self):
        for values in [{**self.inputs.values,'country':'Atlantis'}, {**self.inputs.values,'visa_sponsorship':'maybe'}]:
            self.assertFalse(analyze(replace(self.inputs,values=values))[1]['data_ready'])
        self.assertFalse(analyze(self.changed('country',react_options=[]))[1]['data_ready'])

    def test_native_select_and_explicit_optional_values(self):
        changed=self.changed('country',tag='SELECT',role=None,react_options=[],native_options=[{'label':'Synthetic Country CN','value':'cn'}])
        changed=replace(changed,values={**changed.values,'linkedin':'https://example.invalid/fictional'})
        bindings, report=analyze(changed)
        self.assertTrue(report['data_ready'])
        self.assertIn('linkedin',[b['field'] for b in bindings])

    def test_missing_resume_accept_and_submit_button_block(self):
        changed=self.changed('resume',accept='.doc')
        self.assertIn('resume:pdf_not_accepted',analyze(changed)[1]['invalid_fields'])
        snapshot=copy.deepcopy(self.inputs.snapshot)
        snapshot['controls']=[c for c in snapshot['controls'] if c['type']!='submit']
        self.assertFalse(analyze(replace(self.inputs,snapshot=snapshot))[1]['data_ready'])

    def test_missing_baseline_and_mismatched_job_fail_closed(self):
        snapshot=copy.deepcopy(self.inputs.snapshot)
        snapshot['controls']=[c for c in snapshot['controls'] if c['id']!='phone']
        self.assertIn('phone',analyze(replace(self.inputs,snapshot=snapshot))[1]['absent_fields'])
        snapshot['url']=URL.replace('7847750002','99999999')
        with self.assertRaises(ValueError):
            analyze(replace(self.inputs,snapshot=snapshot))
        snapshot['url']=URL
        snapshot['title']='Job Application for Wrong Role at Sertis'
        with self.assertRaises(ValueError):
            analyze(replace(self.inputs,snapshot=snapshot))

    def test_explicit_profile_contract(self):
        raw=json.loads(PROFILE.read_text())
        for change in [{'synthetic':False}, {'local_user_id':''}, {'values':{**raw['values'],'education':'made up'}},
                       {'values':{**raw['values'],'visa_sponsorship':False}}, {'values':{**raw['values'],'email':'real@example.com'}}]:
            path=self.directory/'bad.json';path.write_text(json.dumps({**raw,**change}))
            with self.assertRaises(ValueError):
                load_inputs(URL,path,self.pdf,'test',SNAPSHOT)

    def test_only_approved_target_and_alias_normalization(self):
        self.assertEqual(job_identity(URL), job_identity('https://boards.greenhouse.io/sertis/jobs/7847750002/?gh_src=abc'))
        for url in [URL.replace('sertis','other'),URL.replace('https:','http:'),URL.replace('.io/','.io.evil/'),URL.replace('.io/','.io:443/')]:
            with self.assertRaises(ValueError): job_identity(url)

    def test_sqlite_uses_platform_job_and_local_user_not_email(self):
        alias=replace(self.inputs,url='https://boards.greenhouse.io/sertis/jobs/7847750002/?gh_src=x',
                      values={**self.inputs.values,'email':'other@example.invalid'})
        self.assertEqual(identity(self.inputs),identity(alias))
        other=replace(self.inputs,local_user_id='different-local-user')
        self.assertNotEqual(identity(self.inputs)[2],identity(other)[2])
        store=TaskStore(self.directory/'tasks.sqlite3')
        task=store.create(self.inputs)
        store.transition(task,'RUNNING','running')
        store.transition(task,'UNKNOWN','unverified_exit')
        with self.assertRaises(TaskBlocked): store.create(alias,confirm_no_side_effects=True)
        raw=(self.directory/'tasks.sqlite3').read_bytes()
        self.assertNotIn(b'fixture-user-01',raw)
        self.assertNotIn(b'fixture@example.invalid',raw)

    def test_redacted_cli_check_and_real_execution_gate(self):
        stream=io.StringIO()
        with patch('personal_web_agent.runner._run_agent') as run, \
             patch('personal_web_agent.runner.dotenv_values',side_effect=AssertionError('must not load secrets')), \
             patch('personal_web_agent.fixture.read_state',side_effect=AssertionError('no fixture requests')), redirect_stdout(stream):
            self.assertEqual(asyncio.run(run_apply_prepare(self.inputs,check_only=True)),0)
            self.assertEqual(asyncio.run(run_apply_prepare(self.inputs,human_handoff=True)),2)
            run.assert_not_called()
        for value in [str(self.pdf),'fixture@example.invalid','fixture-user-01','00000000000']:
            self.assertNotIn(value,stream.getvalue())
        self.assertIn(EXECUTION_BLOCK,stream.getvalue())

    def test_private_runner_gate_cannot_be_bypassed(self):
        with patch('personal_web_agent.runner.dotenv_values',side_effect=AssertionError('no .env')):
            with self.assertRaisesRegex(ValueError,EXECUTION_BLOCK):
                asyncio.run(_run_agent(preparation=self.inputs,target_url=URL))

    def test_actual_cli_check_subprocess_and_missing_snapshot(self):
        command=[sys.executable,'-m','personal_web_agent.cli','apply-prepare','--site','greenhouse',
                 '--url',URL,'--profile',str(PROFILE),'--resume',str(self.pdf),'--instruction','synthetic check']
        result=subprocess.run(command+['--snapshot',str(SNAPSHOT),'--check-only'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(json.loads(result.stdout)['execution_allowed'])
        result=subprocess.run(command+['--snapshot',str(SNAPSHOT)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn(EXECUTION_BLOCK,result.stdout)
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,2)

    def test_fixture_contract_refactor(self):
        state={'fixture_version':'P1-C1','approval_status':'LOCKED','submit_count':0,
               'submit_attempt_count':0,'successful_submit_count':0,'upload_count':0,'final_status':'NOT_SUBMITTED'}
        fixture.validate_initial(state,True)
        self.assertTrue(fixture.known_no_effects(state))
        for change in [{'approval_status':'AUTHORIZED'},{'submit_count':1},{'upload_count':1}]:
            with self.assertRaises(ValueError):fixture.validate_initial({**state,**change},True)
        self.assertFalse(fixture.known_no_effects({**state,'successful_submit_count':1}))
