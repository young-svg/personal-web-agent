"""Offline CSDN contracts/guards with synthetic fixture assets; no live model/browser."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fixture_site.sample_article import ARTICLE, sample_image_bytes
from personal_web_agent import csdn
from personal_web_agent.approval import handoff
from personal_web_agent.records import identity, TaskStore, TaskBlocked
from personal_web_agent.runner import run_publish_prepare, _run_agent, _run_recorded

SNAPSHOT = Path(__file__).parent/'snapshots/csdn_editor.json'


class CsdnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.body = self.root/'article.md'; self.body.write_text(ARTICLE['body'], encoding='utf-8')
        self.images = []
        for n, color in enumerate(['steelblue', 'darkorange']):
            path = self.root/f'image-{n}.png'; path.write_bytes(sample_image_bytes(color)); self.images.append(path)
        self.inputs = self.load()

    def load(self, **kw):
        args = dict(url=csdn.EDITOR_URL, title=ARTICLE['title'], body_file=self.body, images=self.images,
                    instruction='仅离线合成资料检查', local_user_id='synthetic-account', snapshot=SNAPSHOT)
        return csdn.load_inputs(**{**args, **kw})

    def test_actual_snapshot_shape_and_redacted_diagnostics(self):
        result = csdn.analyze(self.inputs)
        self.assertTrue(result['structure_ready'])
        self.assertTrue(result['title_activation_required'])
        self.assertFalse(result['image_upload_identified'])
        self.assertTrue(result['image_dialog_unverified'])
        self.assertEqual(result['draft_status'], 'UNKNOWN')
        self.assertFalse(result['execution_allowed'])
        text = json.dumps(result, ensure_ascii=False)
        for private in [self.inputs.title, self.inputs.body, self.inputs.local_user_id, str(self.images[0]), self.inputs.instruction]:
            self.assertNotIn(private, text)

    def test_missing_ambiguous_and_hidden_body_fail_closed(self):
        for controls in [[], self.inputs.snapshot['controls']*2,
                         [{**c, 'visible': False} for c in self.inputs.snapshot['controls']]]:
            inputs = replace(self.inputs, snapshot={**self.inputs.snapshot, 'controls': controls})
            self.assertFalse(csdn.analyze(inputs)['structure_ready'])
        with self.assertRaises(ValueError):
            csdn.analyze(replace(self.inputs, snapshot={**self.inputs.snapshot, 'location': 'https://evil.invalid/md/'}))

    def test_article_upload_is_distinct_from_feedback_and_markdown_import(self):
        page = {**self.inputs.snapshot, 'controls': self.inputs.snapshot['controls'] + [
            {'tag':'INPUT','type':'file','id':'file-image__upload','accept':'image/png,image/jpeg','multiple':True}]}
        self.assertTrue(csdn.analyze(replace(self.inputs, snapshot=page))['image_upload_identified'])
        self.assertFalse(csdn.analyze(self.inputs)['image_upload_identified'])

    def test_draft_hint_is_not_saved_evidence(self):
        for text in ['', '填写标题才可自动保存', '模型称已保存', '文章已保存坏格式']:
            self.assertEqual(csdn.draft_status(text), 'UNKNOWN')
        self.assertEqual(csdn.draft_status('文章已保存12:34:56'), 'SAVED_INDICATOR')
        self.assertEqual(csdn.draft_status('自动保存中'), 'SAVING')
        self.assertEqual(csdn.draft_status('草稿保存失败'), 'FAILED')

    def test_edge_options_preserve_session_without_credentials_or_profile_copy(self):
        options = csdn.edge_browser_options()
        self.assertTrue(options['keep_alive']); self.assertFalse(options['is_local'])
        self.assertFalse(options['use_cloud']); self.assertIsNone(options['storage_state'])
        self.assertNotIn('user_data_dir', options); self.assertNotIn('executable_path', options)
        for endpoint in ['http://0.0.0.0:9223','https://127.0.0.1:9223','http://127.0.0.1',
                         'http://user:secret@127.0.0.1:9223','http://127.0.0.1:9223/?token=secret',
                         'http://127.0.0.1:9223/json/version','http://evil.invalid:9223']:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):csdn.edge_browser_options(endpoint)

    def test_input_target_account_and_image_validation(self):
        self.assertEqual(self.load(url='https://editor.csdn.net//md').url, csdn.EDITOR_URL)
        for changes in [{'url':'https://editor.csdn.net/md/?articleId=123'}, {'url':'https://editor.csdn.net.evil.invalid/md/'},
                        {'local_user_id':'someone@example.com'}, {'local_user_id':''}, {'title':'short'+' '*100},
                        {'images':self.images*2}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):self.load(**changes)
        wrong=self.root/'fake.png'; wrong.write_text('not an image')
        with self.assertRaises(OSError):self.load(images=[wrong])

    def test_block_before_key_database_browser_agent_and_approval_even_with_handoff(self):
        with patch('personal_web_agent.runner.dotenv_values') as key, \
             patch('personal_web_agent.records.TaskStore') as store, \
             patch('personal_web_agent.runner._run_agent') as agent, \
             patch('personal_web_agent.approval.handoff') as approval, redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(run_publish_prepare(self.inputs, human_handoff=True,
                                                           confirm_no_side_effects=True)), 2)
            self.assertEqual(asyncio.run(run_publish_prepare(self.inputs, check_only=True)), 0)
            for mocked in [key, store, agent, approval]:mocked.assert_not_called()
        with self.assertRaisesRegex(ValueError, csdn.EXECUTION_BLOCK):
            asyncio.run(_run_agent(preparation=self.inputs))
        with patch('personal_web_agent.records.TaskStore') as store:
            with self.assertRaisesRegex(ValueError, csdn.EXECUTION_BLOCK):
                asyncio.run(_run_recorded(self.inputs, 'test', human_handoff=True))
            store.assert_not_called()

    def test_real_origin_cannot_receive_fixture_approval_requests(self):
        client = AsyncMock(); choose = AsyncMock()
        with self.assertRaisesRegex(ValueError, 'Fixture authorization'):
            asyncio.run(handoff(object(), 'https://editor.csdn.net', 'not-a-real-token', {}, lambda *a:None,
                                client=client, choose=choose, publishing=True))
        client.get.assert_not_called(); client.post.assert_not_called(); choose.assert_not_called()

    def test_task_contract_keeps_agent_decisions_and_explicit_content(self):
        task = csdn.preparation_task(self.inputs)
        self.assertIn('自主识别', task); self.assertIn('原生 upload_file', task)
        self.assertIn(self.inputs.title, task); self.assertIn('最终发布始终由用户本人完成', task)
        self.assertNotIn('querySelector', task)

    def test_dedup_scopes_account_content_order_not_endpoint_or_instruction(self):
        original = identity(self.inputs)
        self.assertEqual(identity(replace(self.inputs, instruction='other', cdp_url='http://127.0.0.1:9999')), original)
        for changes in [{'body':self.inputs.body+'extra'}, {'local_user_id':'another-account'},
                        {'image_hashes':self.inputs.image_hashes[::-1]}]:
            self.assertNotEqual(identity(replace(self.inputs, **changes))[2], original[2])
        self.assertNotIn(self.inputs.local_user_id, json.dumps(original))

    def test_url_candidate_validation_does_not_mark_a_task_completed(self):
        url='https://blog.csdn.net/synthetic_author/article/details/123456789'
        self.assertEqual(csdn.article_identity(url), ('synthetic_author','123456789',url))
        for invalid in [url+'?token=x',url+'#fragment',url.replace('blog.csdn.net','evil.invalid'),
                        'https://blog.csdn.net/user/article/details/0','https://editor.csdn.net/md/?articleId=123']:
            with self.assertRaises(ValueError):csdn.article_identity(invalid)
        store=TaskStore(self.root/'tasks.sqlite3'); tid=store.create(self.inputs)
        csdn.article_identity(url)
        self.assertEqual(store.get(tid)['state'],'CREATED')
        for state in ['RUNNING','REVIEW_REQUIRED','OUTCOME_PENDING']:store.transition(tid,state,state.lower())
        with self.assertRaises(ValueError):
            store.transition(tid,'COMPLETED','verified_completed',article_id='wrong',article_url=url)
        self.assertEqual(store.get(tid)['state'],'OUTCOME_PENDING')
        store.transition(tid,'COMPLETED','verified_completed',article_id='123456789',article_url=url)
        self.assertEqual(TaskStore(store.path).get(tid)['article_url'],url)
        with self.assertRaises(TaskBlocked):TaskStore(store.path).create(self.inputs,confirm_no_side_effects=True)

    def test_pending_and_unknown_survive_restart_and_block_retry(self):
        for final in ['OUTCOME_PENDING','UNKNOWN']:
            store=TaskStore(self.root/(final+'.sqlite3')); tid=store.create(self.inputs)
            for state in ['RUNNING','REVIEW_REQUIRED','OUTCOME_PENDING']:store.transition(tid,state,state.lower())
            if final=='UNKNOWN':store.transition(tid,'UNKNOWN','unverified_exit')
            reopened=TaskStore(store.path)
            self.assertEqual(reopened.get(tid)['state'],final)
            with self.assertRaises(TaskBlocked):reopened.create(self.inputs,confirm_no_side_effects=True)
            self.assertIsNone(reopened.get(tid)['article_url'])

    def test_current_creator_entry_and_redirect_parameter(self):
        inputs=self.load(url=csdn.CREATION_ENTRY)
        self.assertEqual(inputs.url,csdn.EDITOR_URL)
        self.assertEqual(inputs.entry_url,csdn.CREATION_ENTRY)
        self.assertTrue(csdn.is_editor_location(csdn.EDITOR_URL+'?not_checkout=1'))
        self.assertTrue(csdn.is_editor_location(csdn.EDITOR_URL+'?not_checkout=1&articleId=123'))
        for query in ['not_checkout=2','token=secret','articleId=123&articleId=456']:
            self.assertFalse(csdn.is_editor_location(csdn.EDITOR_URL+'?'+query))

    def test_new_draft_binds_only_this_runs_successful_save_response(self):
        g=csdn.DraftNetworkGuard(self.inputs)
        evidence={'passed':True,'draft_id_present':False,'saved_state':'NOT_OBSERVED'}
        for invalid in [{**evidence,'draft_id_present':True},{**evidence,'saved_state':'SAVED_INDICATOR'}]:
            with self.assertRaises(ValueError):g.arm_new_draft(invalid)
        g.arm_new_draft(evidence)
        self.assertTrue(g.valid_draft_target({'id':''}))
        self.assertFalse(g.valid_draft_target({'id':'169845613'}))
        g.bind_draft_response('unowned',{'code':200,'data':{'id':111}})
        self.assertIsNone(g.bound_draft_id)
        g.draft_request_ids.add('owned')
        g.bind_draft_response('owned',{'code':200,'data':{'id':222}})
        self.assertEqual(g.bound_draft_id,'222')
        self.assertTrue(g.valid_draft_target({'id':222}))
        self.assertFalse(g.valid_draft_target({'id':333}))
        with self.assertRaises(ValueError):g.bind_draft_response('owned',{'code':200,'data':{'id':333}})

    def test_explicit_draft_flag_is_required_and_passed_to_existing_runner(self):
        with patch('personal_web_agent.runner._run_recorded',new=AsyncMock(return_value=0)) as run,redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(run_publish_prepare(self.inputs)),2)
            run.assert_not_called()
            self.assertEqual(asyncio.run(run_publish_prepare(self.inputs,allow_csdn_draft=True)),0)
            self.assertTrue(run.call_args.kwargs['draft_authorized'])
            self.assertIn('原生 upload_file',run.call_args.args[1])

    def test_network_guard_distinguishes_draft_from_publish_on_same_endpoint(self):
        base={'method':'POST','url':'https://bizapi.csdn.net/blog-console-api/v3/mdeditor/saveArticle'}
        for body in [{'status':0,'pubStatus':'publish','source':'pc_mdeditor'},
                     {'status':2,'pubStatus':'publish','source':'pc_mdeditor'},
                     {'status':2,'source':'pc_mdeditor'},{}]:
            self.assertFalse(csdn.allow_request({**base,'postData':json.dumps(body)})[0])
        draft={'status':2,'pubStatus':'draft','source':'pc_mdeditor'}
        self.assertEqual(csdn.allow_request({**base,'postData':json.dumps(draft)}),(True,'draft'))
        self.assertFalse(csdn.allow_request({**base,'postData':'not-json'})[0])
        self.assertFalse(csdn.allow_request({'method':'POST','url':'https://evil.invalid/publish'})[0])

    def test_network_upload_requires_native_upload_stage_and_expected_signature(self):
        req={'method':'POST','url':'https://bizapi.csdn.net/resource-api/v1/image/direct/upload/signature',
             'postData':json.dumps({'appName':'direct_blog','imageSuffix':'png'})}
        self.assertFalse(csdn.allow_request(req)[0])
        self.assertTrue(csdn.allow_request(req,upload_started=True)[0])
        self.assertFalse(csdn.allow_request({**req,'postData':json.dumps({'appName':'video','imageSuffix':'png'})},upload_started=True)[0])
        upload={'method':'POST','url':'https://synthetic-bucket.oss-cn-test.aliyuncs.com/',
                'headers':{'Content-Type':'multipart/form-data; boundary=test'}}
        self.assertTrue(csdn.allow_request(upload,upload_started=True)[0])
        self.assertFalse(csdn.allow_request({**upload,'url':'https://evil.invalid/'},upload_started=True)[0])

    def test_csdn_action_guard_rejects_publish_keys_and_wrong_upload(self):
        def node(attrs,text=''):
            return SimpleNamespace(attributes=attrs,get_all_children_text=lambda:text)
        state=SimpleNamespace(url=csdn.EDITOR_URL,dom_state=SimpleNamespace(selector_map={
            1:node({'class':'btn btn-publish'},'发布文章'),2:node({'class':'article-bar__title--input'}),
            3:node({'class':'editor__inner','contenteditable':'true'}),
            4:node({'id':'cfw-file-input'}),5:node({'id':'file-image__upload'})}))
        def output(kind,args):return SimpleNamespace(action=[SimpleNamespace(model_dump=lambda **kw:{kind:args})])
        for kind,args in [('click',{'index':1}),('input',{'index':2,'text':'invented'}),
                          ('send_keys',{'keys':'CTRL+ENTER'}),('evaluate',{'code':'x'}),
                          ('upload_file',{'index':4,'path':str(self.images[0])}),
                          ('navigate',{'url':csdn.EDITOR_URL})]:
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                csdn.validate_actions(self.inputs,state,output(kind,args),set())
        csdn.validate_actions(self.inputs,state,output('input',{'index':3,'text':self.inputs.body}),set())
        seen=set();csdn.validate_actions(self.inputs,state,output('upload_file',{'index':5,'path':str(self.inputs.images[0])}),seen)
        with self.assertRaises(ValueError):csdn.validate_actions(self.inputs,state,output('upload_file',{'index':5,'path':str(self.inputs.images[0])}),seen)

    def test_draft_verification_requires_text_upload_preview_and_server_save(self):
        urls=['https://i-blog.csdnimg.cn/direct/synthetic-1.png','https://i-blog.csdnimg.cn/direct/synthetic-2.png']
        page={'values':{'title':self.inputs.title,'body':self.inputs.body+'\n'+''.join('![]('+u+')\n' for u in urls)},
              'images':[{'url':u,'loaded':True} for u in urls],'saved_indicator':True,'restriction':False}
        guard=SimpleNamespace(uploaded_urls=urls,saved_drafts=[{'matches_expected':True,'code':200}],errors=[],dangerous_blocked=False)
        self.assertTrue(csdn.verify_preparation(self.inputs,page,guard)['verification_pass'])
        for changed in [{**page,'saved_indicator':False},{**page,'images':[]},
                        {**page,'values':{**page['values'],'title':'wrong'}}]:
            self.assertFalse(csdn.verify_preparation(self.inputs,changed,guard)['verification_pass'])
        guard.saved_drafts=[]
        self.assertFalse(csdn.verify_preparation(self.inputs,page,guard)['verification_pass'])

    def test_draft_handoff_never_authorizes_publication_or_marks_completed(self):
        from personal_web_agent.approval import review_draft
        page=SimpleNamespace(get_target_info=AsyncMock(return_value={'targetId':'same'}))
        prepared={'values':{'title':'synthetic','body':'test'}}
        for decision in ['done','cancel']:
            with patch('personal_web_agent.csdn.read_preparation',new=AsyncMock(return_value=prepared)),redirect_stdout(io.StringIO()):
                result=asyncio.run(review_draft(page,self.inputs,prepared,lambda *a:None,choose=AsyncMock(return_value=decision)))
            self.assertEqual(result['stage'],'REVIEW_REQUIRED')
            self.assertFalse(result['publication_authorized'])
            self.assertEqual(result['human_checked'],decision=='done')


if __name__ == '__main__':
    unittest.main()
