"""Zero-step native Agent.run/keep_alive check, NOT a real human handoff."""

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
os.environ.update(ANONYMIZED_TELEMETRY='false', BROWSER_USE_CLOUD_SYNC='false', BROWSER_USE_DISABLE_EXTENSIONS='1',
                  BROWSER_USE_CONFIG_DIR=str(ROOT / '.local/browser-use'))

from browser_use import Agent, Browser, ChatDeepSeek
import httpx
from personal_web_agent.approval import read_form


async def check(base, result):
    with tempfile.TemporaryDirectory(prefix='browser-use-user-data-dir-', dir=ROOT / '.local') as profile:
        browser = Browser(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                          user_data_dir=profile, keep_alive=True, headless=False, use_cloud=False,
                          enable_default_extensions=False, allowed_domains=['127.0.0.1'])
        try:
            await browser.start()
            page = await browser.new_page(base)
            before = await read_form(page)
            before_cdp_session = await page.session_id
            before_browser_id = browser.id
            async def deny_request(request):
                result['model_calls'] += 1
                raise RuntimeError('Network model requests are forbidden in the lifecycle check')

            async with httpx.AsyncClient(event_hooks={'request': [deny_request]}) as model_client:
                llm = ChatDeepSeek(model='deepseek-flash', api_key='offline-unused',
                                   client_params={'http_client': model_client, 'max_retries': 0})
                agent = Agent(task='Zero-step lifecycle check only.', llm=llm, browser=browser,
                              use_vision=False, use_judge=False, enable_planning=False, calculate_cost=False,
                              directly_open_url=False, final_response_after_failure=False,
                              file_system_path=str(ROOT / '.local/lifecycle-agent-files'), enable_signal_handler=False)
                # A zero-step run returns an incomplete task, but exercises the
                # real run() finally/event-bus/close path without any LLM response.
                result['agent_run_called'] = True
                result['agent_run_max_steps'] = 0
                history = await agent.run(max_steps=0)
                result['agent_task_success'] = history.is_successful()
                assert result['model_calls'] == 0
            after = await read_form(page)
            element = (await page.get_elements_by_css_selector('#name'))[0]
            await element.fill('虚构生命周期检查')
            edited = await read_form(page)
            result.update(target_id_before=before['target_id'], target_id_after=after['target_id'],
                          same_target=before['target_id'] == after['target_id'],
                          same_cdp_session=before_cdp_session == await page.session_id,
                          same_browser_session=before_browser_id == browser.id,
                          editable_after_agent_close=edited['values']['name'] == '虚构生命周期检查')
            assert result['same_target'] and result['same_cdp_session'] and result['same_browser_session']
            assert result['editable_after_agent_close']
        finally:
            await asyncio.wait_for(browser.kill(), timeout=20)
            result['browser_closed'] = True


async def main():
    private = ROOT / '.local'
    private.mkdir(exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    result = {'result': 'FAIL', 'model_calls': 0, 'agent_run_called': False, 'human_test_completed': False}
    with (private / 'lifecycle-server.log').open('w') as log:
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'fixture_site.app:app', '--app-dir', 'tests',
                                   '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, stdout=log, stderr=log)
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                async with asyncio.timeout(15):
                    while True:
                        try:
                            if (await client.get(base + '/api/state')).status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        await asyncio.sleep(0.1)
            await check(base, result)
            async with httpx.AsyncClient(trust_env=False) as client:
                result['fixture_state'] = (await client.get(base + '/api/state')).json()
            assert result['fixture_state']['successful_submit_count'] == result['fixture_state']['submit_attempt_count'] == 0
            result['result'] = 'PASS'
        except Exception as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            result['server_stopped'] = True
            (private / 'p1-c1-lifecycle.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
