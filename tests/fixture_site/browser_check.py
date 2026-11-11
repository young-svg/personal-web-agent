"""Deterministic fixture UI check. No Agent, LLM, API key, or remote website."""

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
for name, value in {"ANONYMIZED_TELEMETRY": "false", "BROWSER_USE_CLOUD_SYNC": "false",
                    "BROWSER_USE_DISABLE_EXTENSIONS": "1",
                    "BROWSER_USE_CONFIG_DIR": str(ROOT / ".local/browser-use")}.items():
    os.environ[name] = value

from browser_use import Browser
import httpx
from fixture_site.app import NORMAL_ORDER, REORDERED
from fixture_site.sample_resume import sample_pdf_bytes


async def wait_for(page, expression):
    async with asyncio.timeout(15):
        while await page.evaluate(f"() => String({expression})") != "true":
            await asyncio.sleep(0.1)


async def set_pdf(browser, page, path):
    session_id = await page.session_id
    client = browser.cdp_client
    document = await client.send.DOM.getDocument(session_id=session_id)
    node = await client.send.DOM.querySelector(params={"nodeId": document["root"]["nodeId"], "selector": "#resume"}, session_id=session_id)
    await client.send.DOM.setFileInputFiles(params={"nodeId": node["nodeId"], "files": [str(path)]}, session_id=session_id)


async def check(base, evidence):
    with tempfile.TemporaryDirectory(prefix="browser-use-user-data-dir-", dir=ROOT / ".local") as profile:
        browser = Browser(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                          headless=False, use_cloud=False, user_data_dir=profile,
                          enable_default_extensions=False, keep_alive=False,
                          allowed_domains=["127.0.0.1"])
        pdf = Path(profile) / "fictional-resume.pdf"
        pdf.write_bytes(sample_pdf_bytes())
        try:
            await browser.start()
            async with httpx.AsyncClient(base_url=base, trust_env=False) as client:
                control = {'X-Fixture-Control': (ROOT / '.local/fixture-control-token').read_text().strip()}
                for mode, order in [("normal", NORMAL_ORDER), ("reordered", REORDERED)]:
                    (await client.post('/api/reset', headers=control)).raise_for_status()
                    page = await browser.new_page(base + ("/?order=reordered" if mode == "reordered" else "/"))
                    actual = json.loads(await page.evaluate("() => JSON.stringify(Array.from(document.querySelectorAll('[data-field]'), x => x.dataset.field))"))
                    assert actual == list(order)
                    for field, value in {"name": "虚构测试者", "email": "fixture@example.invalid", "contact": "000-0000-0000", "introduction": "只供本地测试，无真实个人信息。"}.items():
                        element = (await page.get_elements_by_css_selector('#' + field))[0]
                        await element.fill(value)
                    await set_pdf(browser, page, pdf)
                    await wait_for(page, "document.querySelector('#upload-status').dataset.state === 'success'")
                    before = (await client.get('/api/state')).json()
                    assert before['upload_count'] == 1 and before['submit_count'] == 0
                    assert before['final_status'] == 'NOT_SUBMITTED'
                    assert before['successful_submit_count'] == 0
                    evidence[mode] = {"order": actual, "after_upload": before,
                                      "scope": "No automatic Submit; human submission belongs to --handoff"}
                    (await client.post('/api/reset', headers=control)).raise_for_status()
                    reset = (await client.get('/api/state')).json()
                    assert reset['upload_count'] == 0 and reset['submit_count'] == 0 and not reset['uploads'] and not reset['submissions']
                    evidence[mode]['after_reset'] = reset

                page = await browser.new_page(base + '/?upload=fail')
                await set_pdf(browser, page, pdf)
                await wait_for(page, "document.querySelector('#upload-status').dataset.state === 'failure'")
                failed = (await client.get('/api/state')).json()
                assert failed['upload_count'] == 1 and failed['submit_count'] == 0
                assert failed['uploads'][0]['success'] is False
                assert await page.evaluate("() => document.querySelector('#upload-id').value") == ''
                evidence['upload_failure'] = {"state": failed, "page_status": await page.evaluate("() => document.querySelector('#upload-status').textContent")}
                (await client.post('/api/reset', headers=control)).raise_for_status()
                evidence['final_state'] = (await client.get('/api/state')).json()
        finally:
            await asyncio.wait_for(browser.kill(), timeout=20)
            evidence['browser_closed'] = True


async def main():
    private = ROOT / '.local'
    private.mkdir(exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    evidence = {"result": "FAIL", "model_calls": 0, "agent_used": False, "base_url": base}
    with (private / 'p1-c1-fixture-browser-server.log').open('w') as log:
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'fixture_site.app:app', '--app-dir', 'tests',
                                   '--host', '127.0.0.1', '--port', str(port), '--workers', '1'], cwd=ROOT, stdout=log, stderr=log)
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                async with asyncio.timeout(15):
                    while True:
                        if server.poll() is not None:
                            raise RuntimeError('Fixture server exited before startup')
                        try:
                            if (await client.get(base + '/api/state')).status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        await asyncio.sleep(0.1)
            async with asyncio.timeout(120):
                await check(base, evidence)
            evidence['result'] = 'PASS'
        except Exception as exc:
            evidence['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            evidence['server_stopped'] = True
            (private / 'p1-c1-fixture-browser-result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
            print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
