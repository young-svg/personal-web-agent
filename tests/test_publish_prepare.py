"""Offline input/verification/shared-handoff/SQLite tests; never human evidence."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fixture_site.app import create_app
from fixture_site.sample_article import ARTICLE, sample_image_bytes
from personal_web_agent import publishing as pub
from personal_web_agent.approval import handoff
from personal_web_agent.records import TaskStore, TaskBlocked, identity
from personal_web_agent.runner import run_publish_prepare


class InputsMixin:
    def setup_inputs(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        body=self.root/'body.txt';body.write_text(ARTICLE['body'])
        paths=[]
        for n,c in enumerate(['steelblue','darkorange']):
            p=self.root/f'fictional-{n}.png';p.write_bytes(sample_image_bytes(c));paths.append(p)
        self.inputs=pub.load_inputs('http://127.0.0.1:8000/publish/',ARTICLE['title'],body,paths,'仅本地准备，不发布')


class PublishInputsTests(InputsMixin,unittest.TestCase):
    def setUp(self):self.setup_inputs()

    def test_reject_external_private_and_duplicate_images(self):
        for url in ['https://wordpress.org/','http://127.0.0.1:8000/api/publish/publish','http://127.0.0.1:8000/publish/?other=1']:
            with self.assertRaises(ValueError):pub.load_inputs(url,ARTICLE['title'],self.root/'body.txt',self.inputs.images,'test')
        (self.root/'body.txt').write_text('private article without fixture marker')
        with self.assertRaises(ValueError):pub.load_inputs(self.inputs.url,'title',self.root/'body.txt',self.inputs.images,'test')
        with self.assertRaises(ValueError):pub.load_inputs(self.inputs.url,'title',self.root/'body.txt',[self.inputs.images[0]]*2,'test')

    def test_stable_dedup_uses_bytes_order_and_content_not_paths_modes(self):
        aliases=replace(self.inputs,url='http://localhost:8000/publish/?upload=fail',instruction='changed')
        self.assertEqual(identity(self.inputs),identity(aliases))
        self.assertNotEqual(identity(self.inputs)[2],identity(replace(self.inputs,body=self.inputs.body+'\nextra'))[2])
        self.assertNotEqual(identity(self.inputs)[2],identity(replace(self.inputs,image_hashes=self.inputs.image_hashes[::-1]))[2])

    def test_action_guard_rejects_publish_wrong_text_or_extra_file(self):
        def output(kind,args):return SimpleNamespace(action=[SimpleNamespace(model_dump=lambda **kw:{kind:args})])
        dom=SimpleNamespace(selector_map={1:SimpleNamespace(attributes={'id':'save-draft','type':'button'}),
            2:SimpleNamespace(attributes={'id':'publish','type':'submit'}),3:SimpleNamespace(attributes={'id':'images'}),
            4:SimpleNamespace(attributes={'id':'article-title'})})
        state=SimpleNamespace(url=self.inputs.url,dom_state=dom)
        pub.validate_actions(self.inputs,state,output('click',{'index':1}),set())
        for kind,args in [('click',{'index':2}),('input',{'index':4,'text':'invented'}),('upload_file',{'index':3,'path':'/private/secret.png'}),('evaluate',{'code':'bad'}),('navigate',{'url':'http://elsewhere'})]:
            with self.subTest(kind=kind),self.assertRaises(ValueError):pub.validate_actions(self.inputs,state,output(kind,args),set())
        seen=set()
        pub.validate_actions(self.inputs,state,output('upload_file',{'index':3,'path':str(self.inputs.images[0])}),seen)
        with self.assertRaises(ValueError):pub.validate_actions(self.inputs,state,output('upload_file',{'index':3,'path':str(self.inputs.images[0])}),seen)

    def test_sqlite_success_evidence_and_duplicate_block(self):
        store=TaskStore(self.root/'tasks.sqlite3');task=store.create(self.inputs)
        for s in ['RUNNING','REVIEW_REQUIRED','OUTCOME_PENDING']:store.transition(task,s,s.lower())
        with self.assertRaises(ValueError):store.transition(task,'COMPLETED','verified_completed')
        aid=str(uuid4());url='http://127.0.0.1:8000/publish/articles/'+aid
        store.transition(task,'COMPLETED','verified_completed',article_id=aid,article_url=url)
        saved=TaskStore(store.path).get(task)
        self.assertEqual((saved['task_type'],saved['article_id'],saved['article_url']),('publish',aid,url))
        with self.assertRaises(TaskBlocked):store.create(self.inputs,confirm_no_side_effects=True)
        with patch('personal_web_agent.records.TaskStore',return_value=store),patch('personal_web_agent.runner._run_agent') as run,redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(run_publish_prepare(self.inputs)),2);run.assert_not_called()

    def test_unknown_pending_and_cancel_retry_policy(self):
        for final in ['UNKNOWN','OUTCOME_PENDING','CANCELLED','FAILED']:
            store=TaskStore(self.root/(final+'.sqlite3'));task=store.create(self.inputs);store.transition(task,'RUNNING','running')
            if final!='FAILED':store.transition(task,'REVIEW_REQUIRED','review_required')
            if final=='OUTCOME_PENDING':store.transition(task,final,final.lower())
            else:store.transition(task,final,{'UNKNOWN':'unverified_exit','CANCELLED':'verified_cancelled','FAILED':'verified_no_effect_failure'}[final],no_side_effects=final in {'CANCELLED','FAILED'})
            with self.assertRaises(TaskBlocked):store.create(self.inputs)
            if final in {'UNKNOWN','OUTCOME_PENDING'}:
                with self.assertRaises(TaskBlocked):store.create(self.inputs,confirm_no_side_effects=True)
            else:self.assertNotEqual(store.create(self.inputs,confirm_no_side_effects=True),task)

    def test_old_database_columns_are_extended_without_losing_rows(self):
        path=self.root/'legacy.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE tasks (task_id TEXT PRIMARY KEY,task_type TEXT,site TEXT,target_url TEXT,dedup_key TEXT,state TEXT,created_at TEXT,updated_at TEXT,summary TEXT,no_side_effects INTEGER)')
            db.execute("INSERT INTO tasks VALUES ('old','apply-prepare','local','local','hash','UNKNOWN','before','before','unverified_exit',0)")
        record=TaskStore(path).get('old')
        self.assertEqual(record['state'],'UNKNOWN');self.assertIsNone(record['article_id'])


class PublishHandoffTests(InputsMixin,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_inputs()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app('test-control')),base_url='http://127.0.0.1:8000')
        self.images=[]
        for path in self.inputs.images:
            response=await self.client.post('/api/publish/upload',files={'images':(path.name,path.read_bytes(),'image/png')})
            self.images+=response.json()['images']
        self.values={'title':self.inputs.title,'body':self.inputs.body}
        self.draft=(await self.client.post('/api/publish/draft',json={**self.values,'image_ids':[x['image_id'] for x in self.images]})).json()['draft_id']
        self.page={'target_id':'retained','url':self.inputs.url,'values':dict(self.values),'draft_id':self.draft,
            'image_ids':[x['image_id'] for x in self.images],'receipt_state':'idle','receipt_id':'','receipt_text':'','article_url':'',
            'upload_state':'success','draft_state':'success','preview_title':self.inputs.title,'preview_body':self.inputs.body,
            'preview_images':[{'url':x['url'],'loaded':True} for x in self.images]}
        self.store=TaskStore(self.root/'journal.sqlite3');self.task=self.store.create(self.inputs)
        for s in ['RUNNING','REVIEW_REQUIRED']:self.store.transition(self.task,s,s.lower())

    async def asyncTearDown(self):await self.client.aclose()

    def record(self,s):self.store.transition(self.task,s,s.lower())

    async def flow(self,choose):
        with patch('personal_web_agent.publishing.read_form',new=AsyncMock(side_effect=lambda _:dict(self.page))):
            result=await handoff(object(),'http://127.0.0.1:8000','test-control',dict(self.page),lambda *a:None,
                                 choose=choose,client=self.client,publishing=True,record_state=self.record)
        code={'CANCELLED':'verified_cancelled','COMPLETED':'verified_completed','UNKNOWN':'unverified_exit'}[result['stage']]
        evidence={'article_id':result['page']['receipt_id'],'article_url':'http://127.0.0.1:8000'+result['page']['article_url']} if result['stage']=='COMPLETED' else {}
        self.store.transition(self.task,result['stage'],code,no_side_effects=result['stage']=='CANCELLED',**evidence)
        return result

    async def test_verification_requires_actual_values_hashes_preview_and_draft(self):
        state=(await self.client.get('/api/publish/state')).json()
        history=SimpleNamespace(is_successful=lambda:True,final_result=lambda:'prepared')
        self.assertTrue(pub.verify(self.inputs,self.page,state,history)['verification_pass'])
        for page in [{**self.page,'preview_title':'wrong'},{**self.page,'image_ids':[]},{**self.page,'draft_id':'wrong'}]:
            self.assertFalse(pub.verify(self.inputs,page,state,history)['verification_pass'])
        state['uploads'][0]['images'][0]['sha256']='wrong'
        self.assertFalse(pub.verify(self.inputs,self.page,state,history)['verification_pass'])

    async def test_cancel_persists_zero_publication(self):
        async def choose(_):return 'cancel'
        result=await self.flow(choose)
        self.assertEqual(result['state']['successful_publish_count'],0)
        self.assertEqual(result['state']['publish_attempt_count'],0)
        self.assertEqual(self.store.get(self.task)['state'],'CANCELLED')

    async def test_shared_approval_records_pending_before_authorize_and_receipt(self):
        choices=iter(['approve','done'])
        async def choose(_):
            value=next(choices)
            if value=='done':
                self.assertEqual(TaskStore(self.store.path).get(self.task)['state'],'OUTCOME_PENDING')
                receipt=(await self.client.post('/api/publish/publish',json={'draft_id':self.draft})).json()
                self.page.update(receipt_state='success',receipt_id=receipt['article_id'],receipt_text='回执 '+receipt['article_id'],article_url=receipt['url'])
            return value
        result=await self.flow(choose)
        self.assertEqual(result['stage'],'COMPLETED')
        self.assertEqual(self.store.get(self.task)['article_id'],self.page['receipt_id'])
        self.assertEqual(result['confirmation_source'],'offline_test_input')

    async def test_no_receipt_is_unknown_and_cannot_retry(self):
        choices=iter(['approve','done'])
        async def choose(_):return next(choices)
        result=await self.flow(choose)
        self.assertEqual(result['stage'],'UNKNOWN')
        with self.assertRaises(TaskBlocked):self.store.create(self.inputs,confirm_no_side_effects=True)

    async def test_prepared_content_changed_blocks_authorization(self):
        self.page['values']['title']='tampered'
        async def choose(_):raise AssertionError('No approval should be requested')
        # Server draft still has the original title: comparison must reject it.
        result=await self.flow(choose)
        self.assertEqual(result['stage'],'UNKNOWN')
        self.assertFalse(result['authorized'])

    async def test_upload_failure_requires_page_server_and_model_report(self):
        app=create_app('test-control')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            path=self.inputs.images[0]
            (await client.post('/api/publish/upload?fail=1',files={'images':(path.name,path.read_bytes(),'image/png')}))
            state=(await client.get('/api/publish/state')).json()
        page={**self.page,'upload_state':'failure','image_ids':[],'draft_id':''}
        history=SimpleNamespace(is_successful=lambda:False,final_result=lambda:'上传失败')
        verdict=pub.verify(replace(self.inputs,expect_upload_failure=True),page,state,history)
        self.assertTrue(verdict['verification_pass']);self.assertEqual(verdict['preparation_status'],'UPLOAD_FAILED')

    async def test_done_before_click_prompts_again_without_agent(self):
        choices=iter(['approve','done','done']);prompts=[]
        async def choose(prompt):
            prompts.append(prompt)
            value=next(choices)
            if len(prompts)==3:
                receipt=(await self.client.post('/api/publish/publish',json={'draft_id':self.draft})).json()
                self.page.update(receipt_state='success',receipt_id=receipt['article_id'],receipt_text='回执 '+receipt['article_id'],article_url=receipt['url'])
            return value
        result=await self.flow(choose)
        self.assertEqual(result['stage'],'COMPLETED')
        self.assertIn('尚未收到',prompts[2])
        self.assertEqual(result['state']['publish_attempt_count'],1)
