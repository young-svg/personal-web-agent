"""Offline ASGI protocol checks. HTTP authorization/submission are not human E2E."""
import asyncio
import json
from pathlib import Path
import unittest

import httpx
from starlette.testclient import TestClient
from fixture_site.app import create_app
from fixture_site.publishing import MAX_IMAGE_BYTES, MAX_IMAGES
from fixture_site.sample_article import ARTICLE, sample_image_bytes
from fixture_site.sample_resume import sample_pdf_bytes
from test_fixture_site import CONTROL, PROFILE


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app('offline-test-control'))
        self.addCleanup(self.client.close)

    def state(self):
        return self.client.get('/api/publish/state').json()

    def upload(self, query='', files=None):
        return self.client.post('/api/publish/upload'+query, files=files or [
            ('images', ('blue.png', sample_image_bytes(), 'image/png')),
            ('images', ('orange.jpg', sample_image_bytes('darkorange', 'JPEG'), 'image/jpeg'))])

    def draft(self, **changes):
        images = self.upload().json()['images']
        data = {'title': ARTICLE['title'], 'body': ARTICLE['body'], 'image_ids': [i['image_id'] for i in images]}
        data.update(changes)
        return self.client.post('/api/publish/draft', json=data)

    def authorize(self, draft_id):
        return self.client.post('/api/publish/authorize', headers=CONTROL, json={'draft_id': draft_id})

    def publish(self, draft_id):
        return self.client.post('/api/publish/publish', json={'draft_id': draft_id})

    def test_pages_modes_controls_and_no_implicit_writes(self):
        for url in ['/publish/', '/publish/?upload=fail']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            for marker in ['id="article-title"', 'id="article-body"', 'multiple accept=', 'id="preview-body"', '>Publish</button>']:
                self.assertIn(marker, response.text)
        self.assertIn("fetch('/api/publish/upload?fail=1'", self.client.get('/publish/?upload=fail').text)
        self.assertEqual(self.client.get('/publish/?upload=bad').status_code, 400)
        state = self.state()
        for key in ['upload_request_count','draft_save_count','publish_attempt_count','successful_publish_count']:
            self.assertEqual(state[key], 0)

    def test_multi_image_upload_real_bytes_and_separate_events(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        images = response.json()['images']
        self.assertEqual(len(images), 2)
        for image in images:
            download = self.client.get(image['url'])
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.headers['content-type'], image['content_type'])
            self.assertEqual(len(download.content), image['size'])
            self.assertEqual((image['width'],image['height']), (96,64))
        state = self.state()
        self.assertEqual((state['upload_request_count'],state['draft_save_count'],state['publish_attempt_count']), (1,0,0))
        self.assertEqual(state['successful_publish_count'], 0)

    def test_simulated_upload_failure_no_image_ids_or_partial_store(self):
        response = self.upload('?fail=1')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['success'])
        self.assertEqual(response.json()['images'], [])
        self.assertEqual(self.client.app.state.publish_images, {})
        self.assertEqual(self.state()['upload_request_count'], 1)
        self.assertEqual(self.state()['publish_attempt_count'], 0)

    def test_invalid_oversize_extension_and_partial_batches(self):
        cases = [([('images',('x.png', b'bad', 'image/png'))],400),
                 ([('images',('x.png', b'x'*(MAX_IMAGE_BYTES+1), 'image/png'))],413),
                 ([('images',('x.jpg',sample_image_bytes(),'image/jpeg'))],400),
                 ([('images',('good.png',sample_image_bytes(),'image/png')),('images',('bad.png',b'bad','image/png'))],400),
                 ([('images',(f'{n}.png',sample_image_bytes(),'image/png')) for n in range(MAX_IMAGES+1)],400)]
        for files,code in cases:
            with self.subTest(code=code):
                response=self.upload(files=files)
                self.assertEqual(response.status_code,code)
                self.assertEqual(response.json()['images'],[])
        self.assertEqual(self.client.app.state.publish_images,{})
        self.assertEqual(self.state()['upload_request_count'],len(cases))

    def test_draft_saves_title_body_and_image_order_without_publish(self):
        draft_id=self.draft().json()['draft_id']
        state=self.state()
        self.assertEqual(state['draft']['title'],ARTICLE['title'])
        self.assertEqual(state['draft']['body'],ARTICLE['body'])
        self.assertEqual(len(state['draft']['image_ids']),2)
        self.assertEqual(state['draft']['draft_id'],draft_id)
        self.assertEqual((state['draft_save_count'],state['successful_draft_save_count'],state['publish_attempt_count']), (1,1,0))
        changed=self.client.post('/api/publish/draft',json={'title':'修改后的合成标题','body':'新正文','image_ids':state['draft']['image_ids']}).json()
        self.assertNotEqual(changed['draft_id'],draft_id)
        self.assertEqual(self.authorize(draft_id).status_code,422)

    def test_missing_content_can_save_but_cannot_authorize_or_publish(self):
        for changes in [{'title':''},{'body':'  '},{'image_ids':[]}]:
            with self.subTest(changes=changes):
                draft=self.draft(**changes)
                self.assertEqual(draft.status_code,200)
                draft_id=draft.json()['draft_id']
                self.assertEqual(self.authorize(draft_id).status_code,422)
                self.assertEqual(self.publish(draft_id).status_code,422)
                self.assertEqual(self.state()['successful_publish_count'],0)
        self.assertEqual(self.state()['publish_attempt_count'],3)

    def test_unknown_duplicate_and_failed_image_ids_rejected(self):
        image_id=self.upload().json()['images'][0]['image_id']
        for ids in [['missing'],[image_id,image_id], [42]]:
            response=self.client.post('/api/publish/draft',json={'title':'测试','body':'测试','image_ids':ids})
            self.assertEqual(response.status_code,422)
        self.assertEqual(self.state()['successful_draft_save_count'],0)

    def test_unauthorized_publish_fails_then_authorized_once(self):
        draft_id=self.draft().json()['draft_id']
        self.assertEqual(self.publish(draft_id).status_code,403)
        self.assertEqual(self.state()['successful_publish_count'],0)
        self.authorize(draft_id).raise_for_status()
        response=self.publish(draft_id)
        self.assertEqual(response.status_code,200)
        state=self.state()
        self.assertEqual((state['publish_attempt_count'],state['successful_publish_count']), (2,1))
        self.assertEqual(state['approval_status'],'CONSUMED')
        self.assertEqual(state['final_status'],'PUBLISHED')
        self.assertEqual(len(state['articles']),1)

    def test_duplicate_does_not_erase_original_receipt(self):
        draft_id=self.draft().json()['draft_id'];self.authorize(draft_id).raise_for_status()
        original=self.publish(draft_id).json()
        self.assertEqual(self.publish(draft_id).status_code,409)
        self.assertEqual(self.authorize(draft_id).status_code,409)
        self.assertEqual(self.client.post('/api/publish/cancel',headers=CONTROL).status_code,409)
        state=self.state()
        self.assertEqual((state['publish_attempt_count'],state['successful_publish_count']), (2,1))
        self.assertEqual(state['articles'][0]['article_id'],original['article_id'])
        self.assertEqual(state['final_status'],'PUBLISHED')

    def test_authorization_binds_immutable_draft_and_cannot_reset_while_live(self):
        draft_id=self.draft().json()['draft_id'];self.authorize(draft_id).raise_for_status()
        self.assertEqual(self.client.post('/api/publish/draft',json={'title':'tamper','body':'tamper'}).status_code,409)
        self.assertEqual(self.client.post('/api/publish/reset',headers=CONTROL).status_code,409)
        self.assertEqual(self.publish('wrong-draft').status_code,422)
        self.assertEqual(self.state()['approval_status'],'AUTHORIZED')
        response=self.publish(draft_id)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.state()['articles'][0]['title'],ARTICLE['title'])

    def test_cancel_revokes_then_reset_invalidates_assets_and_draft(self):
        draft_id=self.draft().json()['draft_id'];state=self.state()
        image_url=state['uploads'][0]['images'][0]['url']
        self.authorize(draft_id).raise_for_status()
        self.client.post('/api/publish/cancel',headers=CONTROL).raise_for_status()
        self.assertEqual(self.publish(draft_id).status_code,403)
        self.assertEqual(self.authorize(draft_id).status_code,409)
        reset=self.client.post('/api/publish/reset',headers=CONTROL).json()
        self.assertEqual(reset['publish_attempt_count'],0)
        self.assertEqual(reset['uploads'],[])
        self.assertIsNone(reset['draft'])
        self.assertEqual(self.client.get(image_url).status_code,404)
        self.assertEqual(self.publish(draft_id).status_code,422)

    def test_control_credentials_not_served_and_required(self):
        draft_id=self.draft().json()['draft_id']
        for action in ['authorize','cancel','reset']:
            self.assertEqual(self.client.post('/api/publish/'+action,json={'draft_id':draft_id}).status_code,403)
        for path in ['/publish/','/api/publish/state']:
            self.assertNotIn('offline-test-control',self.client.get(path).text)
        self.assertEqual(self.state()['approval_status'],'LOCKED')

    def test_article_detail_query_escaping_and_reset(self):
        draft_id=self.draft(title='<script>alert(1)</script>',body='<img onerror=evil>\n第二段').json()['draft_id']
        self.authorize(draft_id).raise_for_status()
        receipt=self.publish(draft_id).json()
        article=self.client.get('/api/publish/articles/'+receipt['article_id']).json()
        self.assertEqual(article['url'],receipt['url'])
        html=self.client.get(receipt['url'])
        self.assertEqual(html.status_code,200)
        self.assertIn('&lt;script&gt;',html.text)
        self.assertNotIn('<script>',html.text)
        self.assertIn('&lt;img onerror=evil&gt;',html.text)
        self.assertEqual(html.text.count('<img src='),2)
        self.client.post('/api/publish/reset',headers=CONTROL).raise_for_status()
        self.assertEqual(self.client.get(receipt['url']).status_code,404)

    def test_malformed_requests_counted_and_no_success(self):
        for path in ['draft','publish']:
            self.assertEqual(self.client.post('/api/publish/'+path,content='bad json').status_code,422)
        self.assertEqual(self.state()['draft_save_count'],1)
        self.assertEqual(self.state()['publish_attempt_count'],1)
        self.assertEqual(self.state()['successful_publish_count'],0)

    def test_recruitment_and_publishing_state_are_independent(self):
        upload_id=self.client.post('/api/upload',files={'resume':('test.pdf',sample_pdf_bytes(),'application/pdf')}).json()['upload_id']
        before=self.client.get('/api/state').json()
        draft_id=self.draft().json()['draft_id'];self.authorize(draft_id).raise_for_status();self.publish(draft_id).raise_for_status()
        self.client.post('/api/publish/reset',headers=CONTROL).raise_for_status()
        self.assertEqual(self.client.get('/api/state').json(),before)
        self.client.post('/api/authorize',headers=CONTROL,json={'upload_id':upload_id}).raise_for_status()
        self.client.post('/api/submit',data={**PROFILE,'upload_id':upload_id}).raise_for_status()
        publish_before=self.state()
        self.client.post('/api/reset',headers=CONTROL).raise_for_status()
        self.assertEqual(self.state(),publish_before)


class ConcurrentPublishingTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_grant_four_requests_only_one_success(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app('offline-test-control')),base_url='http://testserver') as client:
            image=(await client.post('/api/publish/upload',files={'images':('x.png',sample_image_bytes(),'image/png')})).json()['images'][0]
            draft=(await client.post('/api/publish/draft',json={'title':ARTICLE['title'],'body':ARTICLE['body'],'image_ids':[image['image_id']]})).json()['draft_id']
            (await client.post('/api/publish/authorize',headers=CONTROL,json={'draft_id':draft})).raise_for_status()
            responses=await asyncio.gather(*(client.post('/api/publish/publish',json={'draft_id':draft}) for _ in range(4)))
            self.assertEqual(sum(r.status_code==200 for r in responses),1)
            state=(await client.get('/api/publish/state')).json()
            self.assertEqual((state['publish_attempt_count'],state['successful_publish_count']),(4,1))
            self.assertEqual(len(state['articles']),1)
            Path('.local/p3-a-concurrent-evidence.json').write_text(json.dumps(state,ensure_ascii=False,indent=2))
