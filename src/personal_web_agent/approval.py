"""Local fixture handoff only. No Agent reference, model call, or browser action."""

import asyncio
import json
import sys
from urllib.parse import urlsplit

import httpx


async def console_choice(prompt):
    """Keep the event loop/CDP alive while waiting for terminal input (macOS)."""
    print(prompt, flush=True)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    fd = sys.stdin.fileno()

    def ready():
        if not future.done():
            future.set_result(sys.stdin.readline().strip().lower())

    loop.add_reader(fd, ready)
    try:
        return await future
    finally:
        loop.remove_reader(fd)


async def review_draft(page, inputs, prepared_page, save, *, choose=console_choice, timeout=300):
    """Same retained-page human review, draft only. No publication authorization/API."""
    from personal_web_agent.csdn import read_preparation
    target = (await page.get_target_info())['targetId']
    result = {'stage':'REVIEW_REQUIRED', 'human_checked':False, 'publication_authorized':False,
              'confirmation_source':'terminal' if choose is console_choice else 'offline_test_input'}
    print('Agent 已停止。请在同一 Edge 检查测试标题、正文、图片和草稿状态。不要点击发布。',flush=True)
    try:
        async with asyncio.timeout(timeout):
            decision = await choose('检查完成请输入 done；不继续请输入 cancel。两者都不会发布或删除草稿。')
        current = await read_preparation(page)
        result['same_target'] = (await page.get_target_info())['targetId'] == target
        result['content_unchanged'] = current['values'] == prepared_page['values']
        result['human_checked'] = decision == 'done' and result['same_target'] and result['content_unchanged']
        result['cancelled_review'] = decision != 'done'
    except (Exception, asyncio.CancelledError) as exc:
        result['error'] = type(exc).__name__
        if isinstance(exc, asyncio.CancelledError):raise
    finally:
        save('handoff.json',result)
    return result


async def read_form(page):
    info = await page.get_target_info()
    data = json.loads(await page.evaluate("""() => JSON.stringify({
        url: location.href, title: document.title,
        values: Object.fromEntries(['name','email','contact','introduction'].map(name => [name, document.querySelector('[name="'+name+'"]').value])),
        upload_id: document.querySelector('#upload-id').value,
        upload_state: document.querySelector('#upload-status').dataset.state,
        upload_text: document.querySelector('#upload-status').textContent,
        receipt_state: document.querySelector('#receipt').dataset.state,
        receipt_id: document.querySelector('#receipt').dataset.submissionId || '',
        receipt_text: document.querySelector('#receipt').textContent
    })"""))
    data['target_id'] = info['targetId']
    return data


def receipt_verdict(state, page, target_id, upload_id):
    records = [r for r in state['submissions'] if r['status'] == 'COMPLETED']
    return (page.get('target_id') == target_id and state['successful_submit_count'] == 1
            and len(records) == 1 and records[0]['upload_id'] == upload_id
            and page.get('receipt_state') == 'success' and page.get('receipt_id') == records[0]['submission_id']
            and records[0]['submission_id'] in page.get('receipt_text', ''))


async def handoff(page, origin, token, prepared_page, save, *, choose=console_choice, client=None, timeout=600, record_state=None, publishing=False):
    """Only authorize/revoke local fixture and read the retained page. Never click."""
    parsed = urlsplit(origin)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}
            or parsed.username or parsed.password or not parsed.port or parsed.path not in {'', '/'}
            or parsed.query or parsed.fragment):
        raise ValueError('Fixture authorization endpoints cannot be used for a real platform')
    if client is None:
        async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=10) as local:
            return await handoff(page, origin, token, prepared_page, save, choose=choose, client=local, timeout=timeout, record_state=record_state, publishing=publishing)
    if publishing:
        from personal_web_agent import publishing as business
        read_page, verdict = business.read_form, business.receipt_verdict
    else:
        read_page, verdict = read_form, receipt_verdict
    prefix = '/api/publish' if publishing else '/api'
    count_key = 'successful_publish_count' if publishing else 'successful_submit_count'
    attempt_key = 'publish_attempt_count' if publishing else 'submit_count'
    identity_key = 'draft_id' if publishing else 'upload_id'
    button = 'Publish' if publishing else 'Submit'
    headers = {'X-Fixture-Control': token}
    result = {'stage': 'REVIEW_REQUIRED', 'confirmation_source': 'terminal' if choose is console_choice else 'offline_test_input',
              'human_test': 'requires real operator', 'authorized': False}
    target_id, upload_id = prepared_page['target_id'], prepared_page[identity_key]

    async def capture():
        response = await client.get(prefix + '/state')
        response.raise_for_status()
        result['state'] = response.json()
        result['page'] = await read_page(page)
        if result['page']['target_id'] != target_id:
            raise RuntimeError('Original browser target was lost; handoff aborted')
        save('handoff.json', result)

    async def revoke():
        response = await client.post(prefix + '/cancel', headers=headers)
        if response.status_code not in (200, 409):
            response.raise_for_status()

    try:
        await capture()
        matches = business.same_preparation(result['page'], prepared_page, result['state']) if publishing else (
            result['page']['values'] == prepared_page['values'] and result['page']['upload_id'] == upload_id)
        if (not matches
                or result['state'][count_key] or result['state']['approval_status'] != 'LOCKED'):
            raise RuntimeError('Retained page/state does not match verified preparation')
        print('Agent.run 已返回，后续不会恢复 Agent。原表单保留在同一可见 Chrome。', flush=True)
        print(f"当前 URL: {result['page']['url']}；上传已确认；提交尝试 {result['state'][attempt_key]}；成功提交 0。", flush=True)
        async with asyncio.timeout(timeout):
            decision = await choose('请在原浏览器检查。输入 approve 授权一次手动提交；输入 cancel 或其他内容取消。')
            if decision != 'approve':
                await revoke()
                await capture()
                result['stage'] = 'CANCELLED' if result['state'][count_key] == 0 else 'UNKNOWN'
                return result
            if record_state:
                record_state('OUTCOME_PENDING')  # Commit MUST finish before authorization.
            result['stage'] = 'OUTCOME_PENDING'
            save('handoff.json', result)
            response = await client.post(prefix + '/authorize', headers=headers, json={identity_key: upload_id})
            response.raise_for_status()
            result['authorized'] = True
            save('handoff.json', result)
            decision = await choose(f'已授权一次。请你亲自在原浏览器点击 {button}，看到回执后输入 done；放弃请输入 cancel。程序不会点击。')
            for confirmation in range(3):
                if decision != 'done':
                    await revoke()
                # Read only; allow an in-flight human click's receipt to render.
                for _ in range(20):
                    await capture()
                    if verdict(result['state'], result['page'], target_id, upload_id):
                        result['stage'] = 'COMPLETED'
                        return result
                    if decision != 'done' and result['state'][count_key] == 0:
                        result['stage'] = 'CANCELLED'
                        return result
                    await asyncio.sleep(0.1)
                if (confirmation < 2 and decision == 'done' and result['state'][attempt_key] == 0
                        and result['state']['approval_status'] == 'AUTHORIZED'):
                    decision = await choose(f'服务器尚未收到 {button} 请求。approve 只授权，不会代点。请亲自在原 Chrome 点击 {button}，看到成功回执再输入 done；取消输入 cancel。')
                    continue
                result['stage'] = 'UNKNOWN'
                return result
    except (Exception, asyncio.CancelledError) as exc:
        result['stage'] = 'UNKNOWN'
        result['error'] = f'{type(exc).__name__}: {exc}'
        if isinstance(exc, asyncio.CancelledError):
            raise
        return result
    finally:
        # Revoke any unused grant on EOF/timeout/error too; never hide a success.
        try:
            await revoke()
            await capture()
            if result['stage'] == 'COMPLETED' and not verdict(result['state'], result['page'], target_id, upload_id):
                result['stage'] = 'UNKNOWN'
            if result['stage'] == 'CANCELLED' and result['state'][count_key] != 0:
                result['stage'] = 'UNKNOWN'
        except Exception as exc:
            result['cleanup_error'] = f'{type(exc).__name__}: {exc}'
            result['stage'] = 'UNKNOWN'
        save('handoff.json', result)
