"""Deterministic local publishing UI check; no Agent/model or automatic Publish."""
import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
os.environ.update(PYTHON_DOTENV_DISABLED='1', ANONYMIZED_TELEMETRY='false', BROWSER_USE_CLOUD_SYNC='false',
                  BROWSER_USE_DISABLE_EXTENSIONS='1', BROWSER_USE_CONFIG_DIR=str(ROOT/'.local/browser-use'))
from browser_use import Browser
import httpx
from fixture_site.browser_check import wait_for
from fixture_site.sample_article import ARTICLE, sample_image_bytes


async def set_images(browser, page, paths):
    sid = await page.session_id
    document = await browser.cdp_client.send.DOM.getDocument(session_id=sid)
    node = await browser.cdp_client.send.DOM.querySelector({'nodeId': document['root']['nodeId'], 'selector': '#images'},session_id=sid)
    await browser.cdp_client.send.DOM.setFileInputFiles({'nodeId': node['nodeId'], 'files': [str(p) for p in paths]},session_id=sid)


async def check(base, result):
    with tempfile.TemporaryDirectory(prefix='browser-use-user-data-dir-',dir=ROOT/'.local') as profile:
        paths=[]
        for n,color in enumerate(['steelblue','darkorange']):
            path=Path(profile)/f'fictional-{n}.png';path.write_bytes(sample_image_bytes(color));paths.append(path)
        browser=Browser(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                        headless=False,use_cloud=False,user_data_dir=profile,enable_default_extensions=False,
                        allowed_domains=['127.0.0.1'],keep_alive=False)
        try:
            await browser.start()
            async with httpx.AsyncClient(base_url=base,trust_env=False) as client:
                headers={'X-Fixture-Control':(ROOT/'.local/fixture-control-token').read_text().strip()}
                page=await browser.new_page(base+'/publish/')
                title=(await page.get_elements_by_css_selector('#article-title'))[0]
                body=(await page.get_elements_by_css_selector('#article-body'))[0]
                await title.fill(ARTICLE['title']);await body.fill(ARTICLE['body'])
                await wait_for(page,'document.querySelector("#preview-title").textContent === '+json.dumps(ARTICLE['title']))
                before=(await client.get('/api/publish/state')).json()
                assert before['draft_save_count']==before['publish_attempt_count']==0
                await set_images(browser,page,paths)
                await wait_for(page,"document.querySelector('#image-upload-status').dataset.state === 'success'")
                await wait_for(page,"[...document.querySelectorAll('#preview-images img')].length === 2 && [...document.querySelectorAll('#preview-images img')].every(i=>i.naturalWidth>0)")
                assert json.loads(await page.evaluate("() => JSON.stringify(document.querySelector('#preview-body').textContent)"))==ARTICLE['body']
                button=(await page.get_elements_by_css_selector('#save-draft'))[0]
                await button.click()
                await wait_for(page,"document.querySelector('#draft-status').dataset.state === 'success'")
                saved=(await client.get('/api/publish/state')).json()
                assert saved['draft']['title']==ARTICLE['title'] and saved['draft']['body']==ARTICLE['body']
                assert len(saved['draft']['image_ids'])==2 and saved['upload_request_count']==1
                assert saved['draft_save_count']==1 and saved['successful_draft_save_count']==1
                assert saved['publish_attempt_count']==saved['successful_publish_count']==0
                result['normal']={'state':saved,'preview_images_loaded':2,'no_autosave_before_click':True}
                (ROOT/'.local/p3-a-page.png').write_bytes(base64.b64decode(await page.screenshot()))
                await page.reload()
                await wait_for(page,"document.querySelector('#draft-id').value === "+json.dumps(saved['draft']['draft_id']))
                restored=json.loads(await page.evaluate("() => JSON.stringify({title:document.querySelector('#article-title').value,body:document.querySelector('#article-body').value})"))
                assert restored=={'title':ARTICLE['title'],'body':ARTICLE['body']}
                assert (await client.get('/api/publish/state')).json()['draft_save_count']==1
                result['restored_after_reload']=True
                (await client.post('/api/publish/reset',headers=headers)).raise_for_status()
                page=await browser.new_page(base+'/publish/?upload=fail')
                await set_images(browser,page,paths)
                await wait_for(page,"document.querySelector('#image-upload-status').dataset.state === 'failure'")
                assert await page.evaluate("() => document.querySelector('#image-ids').value")=='[]'
                failed=(await client.get('/api/publish/state')).json()
                assert failed['upload_request_count']==1 and not failed['uploads'][0]['success']
                assert failed['publish_attempt_count']==failed['successful_publish_count']==0
                result['upload_failure']={'state':failed,'page_status':await page.evaluate("() => document.querySelector('#image-upload-status').textContent")}
                recruitment=(await client.get('/api/state')).json()
                assert recruitment['submit_attempt_count']==recruitment['successful_submit_count']==recruitment['upload_count']==0
                result['recruitment_untouched']=True
        finally:
            await asyncio.wait_for(browser.kill(),timeout=20)
            result['browser_closed']=True


async def main():
    (ROOT/'.local').mkdir(exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    result={'result':'FAIL','model_calls':0,'agent_used':False,'publish_clicked':False,'base_url':base}
    with (ROOT/'.local/p3-a-browser-server.log').open('w') as log:
        server=subprocess.Popen([sys.executable,'-m','uvicorn','fixture_site.app:app','--app-dir','tests','--host','127.0.0.1','--port',str(port),'--workers','1'],cwd=ROOT,stdout=log,stderr=log)
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                async with asyncio.timeout(15):
                    while True:
                        if server.poll() is not None:raise RuntimeError('Fixture server did not start')
                        try:
                            if (await client.get(base+'/api/publish/state')).status_code==200:break
                        except httpx.ConnectError:pass
                        await asyncio.sleep(0.1)
            async with asyncio.timeout(120):await check(base,result)
            result['result']='PASS'
        except Exception as exc:
            result['error']=f'{type(exc).__name__}: {exc}'
            raise
        finally:
            server.terminate()
            try:server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill();server.wait(timeout=5)
            result['server_stopped']=True
            (ROOT/'.local/p3-a-browser-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
            print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':asyncio.run(main())
