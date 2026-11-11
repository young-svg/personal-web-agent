"""R2 offline decisions/lifecycle only; never attach a browser or call a model."""
import asyncio
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from personal_web_agent import csdn
from personal_web_agent.records import TaskStore, TaskBlocked
from personal_web_agent.runner import _run_recorded


def observation(title='', body='', **overrides):
    return dict(url=csdn.EDITOR_URL, values={'title':title,'body':body},
                fields_present=True, field_counts={'title':1,'body':1}, ready_state='complete',
                restriction=False, saved_indicator=False, **overrides)


def guard():
    return csdn.DraftNetworkGuard(SimpleNamespace(images=()))


class PreflightTests(unittest.TestCase):
    def test_genuinely_blank_editor_passes_all_checks(self):
        result=csdn.preflight_evidence(observation(),guard())
        self.assertTrue(result['passed'])
        self.assertEqual(result['content_class'],'BLANK')
        self.assertTrue(all(c['status']=='PASS' for c in result['checks'].values()))

    def test_exact_default_pair_passes_but_keywords_or_arbitrary_title_do_not(self):
        # Synthetic full-template fingerprint tests classification without copying CSDN's article.
        sample='欢迎使用Markdown编辑器\n合成测试模板。'
        digest=hashlib.sha256(sample.encode()).hexdigest()
        with patch.object(csdn,'WELCOME_HASHES',{digest}):
            result=csdn.preflight_evidence(observation(csdn.DEFAULT_TITLE,sample),guard())
            self.assertTrue(result['passed'])
            self.assertEqual(result['content_class'],'DEFAULT_PLACEHOLDER')
            for title,body in [('私人标题',sample),(csdn.DEFAULT_TITLE,sample+'modified'),('',sample),(csdn.DEFAULT_TITLE,'')]:
                self.assertFalse(csdn.preflight_evidence(observation(title,body),guard())['passed'])
        self.assertFalse(csdn.preflight_evidence(observation(csdn.DEFAULT_TITLE,sample),guard())['passed'])

    def test_user_draft_and_saved_default_are_both_protected(self):
        sample='synthetic default';digest=hashlib.sha256(sample.encode()).hexdigest()
        with patch.object(csdn,'WELCOME_HASHES',{digest}):
            for title,body in [('个人稿件','不应覆盖的正文'),(csdn.DEFAULT_TITLE,sample)]:
                page=observation(title,body)
                page.update(url=csdn.EDITOR_URL+'?articleId=169845613',saved_indicator=True)
                result=csdn.preflight_evidence(page,guard())
                self.assertFalse(result['passed'])
                self.assertEqual(result['draft_id'],'169845613')
                self.assertIn('EXISTING_DRAFT_REQUIRES_HUMAN_REVIEW',result['rejection_reasons'])
                self.assertIn('SAVED_OR_UNKNOWN_STATE_REQUIRES_REVIEW',result['rejection_reasons'])

    def test_unknown_structure_and_guard_fault_fail_closed(self):
        for page in [{},observation() | {'field_counts':{'title':2,'body':1}},
                     observation() | {'url':'http://[malformed'},observation() | {'ready_state':'loading'}]:
            self.assertFalse(csdn.preflight_evidence(page,guard())['passed'])
        g=guard();g.errors=['not persisted'];g.dangerous_blocked=True
        result=csdn.preflight_evidence(observation(),g)
        self.assertEqual(result['checks']['guard_errors']['actual'],1)
        self.assertEqual(result['checks']['guard_blocked']['status'],'FAIL')
        self.assertFalse(csdn.preflight_evidence(observation(),None)['passed'])

    def test_evidence_has_no_private_title_body_query_token_or_guard_message(self):
        page=observation('PRIVATE_TITLE_CANARY','PRIVATE_BODY_CANARY')
        page['url']=csdn.EDITOR_URL+'?token=PRIVATE_TOKEN_CANARY'
        g=guard();g.errors=['PRIVATE_ERROR_CANARY']
        result=csdn.preflight_evidence(page,g)
        text=json.dumps(result)
        for marker in ['PRIVATE_TITLE_CANARY','PRIVATE_BODY_CANARY','PRIVATE_TOKEN_CANARY','PRIVATE_ERROR_CANARY']:
            self.assertNotIn(marker,text)
        self.assertIn('[REDACTED]',result['sanitized_url'])

    def test_preflight_waits_for_stable_fields_and_logs_decisions(self):
        saved=[]
        async def run():
            with patch.object(csdn,'read_preparation',new=AsyncMock(side_effect=[{},observation(),observation()])):
                return await csdn.inspect_initial(object(),guard(),lambda n,d:saved.append((n,d)),interval=0)
        _,result=asyncio.run(run())
        self.assertTrue(result['passed']);self.assertEqual(result['samples'],3)
        self.assertEqual(len(saved),3)
        self.assertFalse(saved[0][1]['passed'])

    def test_read_exception_timeout_and_cancellation_all_persist_failure(self):
        for error in [RuntimeError('PRIVATE_ERROR'),asyncio.CancelledError()]:
            saved=[]
            async def run():
                with patch.object(csdn,'read_preparation',new=AsyncMock(side_effect=error)):
                    await csdn.inspect_initial(object(),guard(),lambda n,d:saved.append(d),interval=0)
            with self.assertRaises(type(error)):asyncio.run(run())
            self.assertFalse(saved[-1]['passed']);self.assertNotIn('PRIVATE_ERROR',json.dumps(saved))
        saved=[]
        async def timeout():
            with patch.object(csdn,'read_preparation',new=AsyncMock(return_value={})):
                await csdn.inspect_initial(object(),guard(),lambda n,d:saved.append(d),timeout=0.01,interval=0.001)
        with self.assertRaises(TimeoutError):asyncio.run(timeout())
        self.assertEqual(saved[-1]['read_error'],'TimeoutError')


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_preservation_mode_rejects_even_an_otherwise_allowed_draft_request(self):
        callbacks={}
        fetch=SimpleNamespace(enable=AsyncMock(),continueRequest=AsyncMock(),failRequest=AsyncMock())
        network=SimpleNamespace(enable=AsyncMock(),setBypassServiceWorker=AsyncMock(),setBlockedURLs=AsyncMock())
        register=SimpleNamespace(
            Fetch=SimpleNamespace(requestPaused=lambda fn:callbacks.update(paused=fn)),
            Network=SimpleNamespace(responseReceived=lambda fn:None,loadingFinished=lambda fn:None))
        client=SimpleNamespace(send=SimpleNamespace(Fetch=fetch,Network=network),register=register)
        browser=SimpleNamespace(cdp_client=client,get_or_create_cdp_session=AsyncMock(return_value=SimpleNamespace(session_id='owned')))
        g=guard();await g.install(browser,'new-page')
        g.preserve_only=True
        request={'method':'POST','url':'https://bizapi.csdn.net/blog-console-api/v3/mdeditor/saveArticle',
                 'postData':json.dumps({'status':2,'pubStatus':'draft','source':'pc_mdeditor'})}
        self.assertTrue(csdn.allow_request(request)[0])
        callbacks['paused']({'request':request,'requestId':'req'},'owned')
        await asyncio.gather(*list(g.pending))
        fetch.failRequest.assert_awaited_once()
        fetch.continueRequest.assert_not_called()
        self.assertEqual(g.requests[-1]['category'],'preservation_block')

    def setup_session(self, *, freeze_error=None):
        events=[];g=guard();g.sid='new-editor-only'
        async def freeze(*args,**kwargs):
            events.append('freeze')
            self.assertTrue(g.preserve_only)
            if freeze_error:raise freeze_error
        async def finish():events.append('disable')
        async def stop():events.append('disconnect')
        g.client=SimpleNamespace(send=SimpleNamespace(Page=SimpleNamespace(setWebLifecycleState=AsyncMock(side_effect=freeze))))
        g.finish=AsyncMock(side_effect=finish)
        browser=SimpleNamespace(stop=AsyncMock(side_effect=stop),kill=AsyncMock())
        return g,browser,events

    async def test_failure_preserves_page_and_requests_freeze_before_releasing_guard(self):
        g,b,events=self.setup_session();saved=[]
        result=await csdn.cleanup_session(b,g,lambda n,d:saved.append(d),failed=True)
        self.assertEqual(events,['freeze','disable','disconnect'])
        self.assertTrue(result['page_preserved']);self.assertTrue(result['manual_review_required'])
        self.assertEqual(result['protection_after_disconnect'],'NOT_GUARANTEED')
        b.kill.assert_not_called()
        g.client.send.Page.setWebLifecycleState.assert_awaited_once_with({'state':'frozen'},session_id='new-editor-only')

    async def test_freeze_failure_is_not_reported_as_safe_and_resources_disconnect(self):
        g,b,events=self.setup_session(freeze_error=RuntimeError('no support'))
        result=await csdn.cleanup_session(b,g,lambda *a:None,failed=True)
        self.assertEqual(events,['freeze','disconnect']);g.finish.assert_not_called()
        self.assertFalse(result['freeze_acknowledged']);self.assertFalse(result['guard_disabled'])
        self.assertTrue(result['errors']);self.assertTrue(result['browser_disconnected'])
        b.kill.assert_not_called()

    async def test_normal_prepared_draft_keeps_original_disconnect_behavior(self):
        g,b,events=self.setup_session()
        result=await csdn.cleanup_session(b,g,lambda *a:None,failed=False)
        self.assertEqual(events,['disable','disconnect'])
        self.assertFalse(g.preserve_only);self.assertFalse(result['errors'])

    async def test_guard_finish_error_still_disconnects_and_saves_failure(self):
        g,b,_=self.setup_session();g.finish=AsyncMock(side_effect=RuntimeError('error'));saved=[]
        result=await csdn.cleanup_session(b,g,lambda n,d:saved.append(d),failed=True)
        self.assertTrue(result['errors']);self.assertTrue(result['browser_disconnected'])
        self.assertEqual(saved[-1],result)


class StateTests(unittest.TestCase):
    def test_preflight_failure_or_exception_stays_unknown_and_cannot_be_overwritten(self):
        inputs=SimpleNamespace(site='csdn',task_type='publish',url=csdn.EDITOR_URL,local_user_id='test',
                               title='合成测试标题',body='synthetic',image_hashes=('abc',))
        with tempfile.TemporaryDirectory() as folder:
            for raises in [False,True]:
                store=TaskStore(Path(folder)/str(raises)/'tasks.sqlite3')
                run=AsyncMock(side_effect=ValueError('CSDN_PREFLIGHT_REJECTED')) if raises else AsyncMock(return_value=1)
                with patch('personal_web_agent.records.TaskStore',return_value=store),patch('personal_web_agent.runner._run_agent',new=run),redirect_stdout(io.StringIO()):
                    if raises:
                        with self.assertRaises(ValueError):asyncio.run(_run_recorded(inputs,'test',human_handoff=True,draft_authorized=True))
                    else:self.assertEqual(asyncio.run(_run_recorded(inputs,'test',human_handoff=True,draft_authorized=True)),1)
                row=store.list()[0];self.assertEqual(row['state'],'UNKNOWN')
                for state,summary in [('FAILED','verified_no_effect_failure'),('COMPLETED','verified_completed')]:
                    with self.assertRaises(ValueError):store.transition(row['task_id'],state,summary,no_side_effects=True)
                with self.assertRaises(TaskBlocked):store.create(inputs,confirm_no_side_effects=True)
