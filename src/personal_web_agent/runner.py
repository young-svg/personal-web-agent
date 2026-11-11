"""Shared thin native browser-use Runner for P0 smoke and P1-B preparation."""

import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import sys
from urllib.parse import urlsplit

from dotenv import dotenv_values
from personal_web_agent import fixture

ROOT = Path(__file__).resolve().parents[2]
MODEL = "deepseek-flash"
MAX_STEPS = 6
MAX_REQUESTS = 8
TASK = (
    "打开 https://docs.python.org/3/，观察页面，找到 Python Tutorial 的链接并点击进入。"
    "必须根据实际观察到的页面链接选择点击，不要猜测或直接导航到 Tutorial URL。"
    "进入后读取并报告页面标题和当前 URL，然后结束。只浏览文档，不登录、输入、提交或上传。"
)


def _json_default(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


async def run_smoke() -> int:
    return await _run_agent()


async def run_apply_prepare(inputs, *, human_handoff=False, confirm_no_side_effects=False, check_only=False) -> int:
    if getattr(inputs, 'site', 'fixture') == 'greenhouse':
        from personal_web_agent.greenhouse import analyze, ensure_execution_allowed
        _, diagnostics = analyze(inputs)
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        if check_only:
            return 0 if diagnostics['data_ready'] else 2
        try:
            ensure_execution_allowed(inputs)
        except ValueError as exc:
            print(f'BLOCKED: {exc}; no browser, model or fixture authorization started')
            return 2
    if check_only:
        raise ValueError('--check-only is for Greenhouse offline snapshots')
    if human_handoff and not sys.stdin.isatty():
        print("BLOCKED: --handoff requires a real interactive terminal; do not pipe approval input.")
        return 2
    from personal_web_agent.task_inputs import preparation_task
    if not human_handoff:
        return await _run_agent(task=preparation_task(inputs), target_url=inputs.url, preparation=inputs)
    return await _run_recorded(inputs, preparation_task(inputs), human_handoff=True,
                               confirm_no_side_effects=confirm_no_side_effects)


async def run_publish_prepare(inputs, *, human_handoff=False, confirm_no_side_effects=False, check_only=False, allow_csdn_draft=False):
    if getattr(inputs, 'site', 'fixture') == 'csdn':
        from personal_web_agent.csdn import analyze, EXECUTION_BLOCK
        diagnostics = analyze(inputs)
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        if check_only:
            return 0 if diagnostics['structure_ready'] else 2
        if not allow_csdn_draft:
            print(f'BLOCKED: {EXECUTION_BLOCK}; no browser, model, approval or task record started')
            return 2
    if check_only:
        raise ValueError('--check-only requires --site csdn')
    if human_handoff and not sys.stdin.isatty():
        print('BLOCKED: --handoff requires a real interactive terminal; never pipe approval.')
        return 2
    if getattr(inputs, 'site', 'fixture') == 'csdn':
        from personal_web_agent.csdn import preparation_task
    else:
        if allow_csdn_draft:
            raise ValueError('CSDN draft authorization cannot be used for another site')
        from personal_web_agent.publishing import preparation_task
    return await _run_recorded(inputs, preparation_task(inputs), human_handoff=human_handoff,
                               confirm_no_side_effects=confirm_no_side_effects, draft_authorized=allow_csdn_draft)


async def _run_recorded(inputs, task, *, human_handoff, confirm_no_side_effects=False, draft_authorized=False):
    if getattr(inputs, 'site', 'fixture') == 'csdn':
        from personal_web_agent.csdn import ensure_execution_allowed
        ensure_execution_allowed(inputs, draft_authorized=draft_authorized)
    from personal_web_agent.records import TaskStore, TaskBlocked
    store = TaskStore()
    try:
        task_id = store.create(inputs, confirm_no_side_effects=confirm_no_side_effects)
    except TaskBlocked as exc:
        print(f'BLOCKED: {exc}')
        return 2
    print(f'Task ID: {task_id}', flush=True)
    store.transition(task_id, 'RUNNING', 'running')

    def record(state, *, evidence=None):
        code = {'REVIEW_REQUIRED': 'review_required', 'OUTCOME_PENDING': 'outcome_pending',
                'COMPLETED': 'verified_completed', 'CANCELLED': 'verified_cancelled',
                'FAILED': 'verified_no_effect_failure', 'UNKNOWN': 'unverified_exit'}[state]
        store.transition(task_id, state, code, no_side_effects=state in {'CANCELLED', 'FAILED'}, **(evidence or {}))

    try:
        return await _run_agent(task=task, target_url=inputs.url, preparation=inputs,
                                human_handoff=human_handoff, record_state=record, task_id=task_id,
                                draft_authorized=draft_authorized)
    finally:
        # SIGKILL/power loss leaves the last committed state, which also blocks retries.
        state = store.get(task_id)['state']
        if state in {'CREATED', 'RUNNING', 'REVIEW_REQUIRED', 'OUTCOME_PENDING'} and not (state=='REVIEW_REQUIRED' and (not human_handoff or getattr(inputs,'site','fixture')=='csdn')):
            record('UNKNOWN')


async def _run_agent(*, task=TASK, target_url="https://docs.python.org/3/", preparation=None, human_handoff=False, record_state=None, task_id=None, draft_authorized=False) -> int:
    is_csdn = getattr(preparation, 'site', 'fixture') == 'csdn'
    if is_csdn:
        from personal_web_agent.csdn import ensure_execution_allowed
        ensure_execution_allowed(preparation, draft_authorized=draft_authorized)
    elif preparation is not None and getattr(preparation, 'site', 'fixture') != 'fixture':
        from personal_web_agent.greenhouse import ensure_execution_allowed
        ensure_execution_allowed(preparation)
    is_publish = getattr(preparation, 'task_type', '') == 'publish'
    if is_csdn:
        from personal_web_agent import csdn as business
    elif is_publish:
        from personal_web_agent import publishing as business
    else:
        business = fixture
    # Read this project's .env explicitly; never include its contents in logs/history.
    key = (dotenv_values(ROOT / ".env", interpolate=False).get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        print("BLOCKED: fill DEEPSEEK_API_KEY in the project .env; no API calls made.")
        if record_state:
            record_state('FAILED')
        return 2
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not is_csdn and not chrome.is_file():
        print("BLOCKED: expected local Chrome is missing.")
        if record_state:
            record_state('FAILED')
        return 2
    for name, value in {
        "ANONYMIZED_TELEMETRY": "false", "BROWSER_USE_CLOUD_SYNC": "false",
        "BROWSER_USE_DISABLE_EXTENSIONS": "1",
        "BROWSER_USE_CONFIG_DIR": str(ROOT / ".local/browser-use"),
    }.items():
        os.environ[name] = value

    from browser_use import Agent, Browser, ChatDeepSeek, Tools
    import httpx
    control_token = ''
    if human_handoff and not is_csdn:
        control_path = ROOT / '.local/fixture-control-token'
        if not control_path.is_file():
            print('BLOCKED: start the protected local fixture first; control credential missing.')
            if record_state:
                record_state('FAILED')
            return 2
        control_token = control_path.read_text().strip()

    def redact(text: str) -> str:
        text = text.replace(key, "[REDACTED]")
        if control_token:
            text = text.replace(control_token, '[REDACTED-CONTROL]')
        text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
        if is_csdn:
            text = re.sub(r'(https?://[^\s"<>?]+)\?[^\s"<>]*', r'\1?[REDACTED-QUERY]', text)
            text = re.sub(r'(https://blog\.csdn\.net/)[A-Za-z0-9_-]+', r'\1[ACCOUNT]', text)
        return text.replace(str(ROOT), "<PROJECT>")

    class RedactedFormatter(logging.Formatter):
        def format(self, record):
            return redact(super().format(record))

    files = [str(p) for p in preparation.images] if is_publish else [str(preparation.resume)] if preparation else []
    run_kind = "publish-prepare" if is_publish else "apply-prepare" if preparation else "smoke"
    max_steps, max_requests = (10, 12) if preparation else (MAX_STEPS, MAX_REQUESTS)
    if is_publish:
        max_steps, max_requests = 12, 12
    if is_csdn:
        max_steps, max_requests = 14, 14
    max_actions = 1 if is_publish else 3 if preparation else 1
    timeout = 480 if preparation else 360
    run_dir = ROOT / ".local" / run_kind / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir.mkdir(parents=True, mode=0o700)
    formatter = RedactedFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    file_handler = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    # Apply redaction to existing browser-use and console handlers too.
    for logger in [root_logger] + [v for v in logging.Logger.manager.loggerDict.values() if isinstance(v, logging.Logger)]:
        for handler in logger.handlers:
            handler.setFormatter(formatter)

    def save(name, data):
        if is_csdn:
            if name == 'preflight.json':summary['preflight'] = data
            def minimal(value):
                if isinstance(value, dict):
                    return {k:minimal(v) for k,v in value.items() if k not in {'screenshot','screenshot_path','cookies','storage_state'}}
                if isinstance(value, list):return [minimal(v) for v in value]
                return value
            data = minimal(data)
        (run_dir / name).write_text(redact(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)), encoding="utf-8")

    events = []
    live_page = {}
    api = []
    autonomy_finished = False
    retained_page = None
    guard = None
    summary = {
        "task_id": task_id,
        "model": MODEL, "browser_use": version("browser-use"),
        "python_dotenv": version("python-dotenv"), "openai": version("openai"),
        "use_vision": False, "use_cloud": False, "headless": False,
        "max_steps": max_steps, "max_failures": 1 if is_csdn else 2, "max_requests": max_requests,
        "max_actions_per_step": max_actions, "max_output_tokens_per_request": 4096,
        "sdk_retries": 0, "overall_timeout_seconds": timeout,
        "thinking": "disabled via request hook (0.13.14 model-name compatibility)",
        "task": task, "result": "FAIL", "browser_closed": False,
        "errors": [], "cost": "not collected; native ChatDeepSeek returns usage=None",
    }

    async def request_hook(request):
        if autonomy_finished:
            raise RuntimeError('Model calls are forbidden after Agent.run has returned')
        if len(api) >= max_requests:
            raise RuntimeError("Agent API request budget exhausted")
        body = json.loads(request.content)
        # ChatDeepSeek 0.13.14 only sends thinking=False for names containing
        # 'deepseek-v4'. Use the official HTTPX request hook to supply the same
        # documented parameter for the current name; keep the native adapter.
        body["thinking"] = {"type": "disabled"}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request.stream = httpx.ByteStream(payload)
        request.headers["Content-Length"] = str(len(payload))
        api.append({"request": len(api) + 1, "model": body.get("model"),
                    "endpoint": str(request.url), "thinking": body["thinking"], "status": None})
        save("api_calls.json", api)

    async def response_hook(response):
        await response.aread()
        body = response.json()
        api[-1].update({"status": response.status_code, "response_model": body.get("model"),
                        "usage": body.get("usage"), "error": body.get("error"),
                        "tool_calls": sum(len(c.get("message", {}).get("tool_calls") or []) for c in body.get("choices", []))})
        save("api_calls.json", api)

    async def on_step_end(agent):
        nonlocal retained_page
        live_page.update(url=await browser.get_current_page_url(), title=await browser.get_current_page_title())
        if preparation:
            page = await browser.get_current_page()
            retained_page = page
            if is_csdn:
                observed = await business.read_preparation(page)
                state = None
                if observed['restriction'] or guard.errors or guard.dangerous_blocked:
                    summary['errors'].append('CSDN restriction, network error or non-draft save detected')
                    agent.stop()
            else:
                observed, state = await business.observe(page, fixture_origin)
            live_page.update(observed)
            if not is_csdn:
                summary["fixture_state"] = state
            if not is_csdn and state["publish_attempt_count" if is_publish else "submit_count"] != 0:
                summary["errors"].append("UNEXPECTED_SUBMISSION: fixture counter is nonzero")
                agent.stop()
        events.append({"step": agent.state.n_steps, "page_after_actions": dict(live_page)})
        if preparation and not is_csdn:
            events[-1]["fixture_state"] = state
        save("steps.json", events)

    uploaded_paths = set()
    async def validate_model_files(state, output, step):
        if is_publish:
            if is_csdn:
                save(f'index_evidence_{step}.json', business.index_evidence(state,output))
                business.validate_actions(preparation,state,output,uploaded_paths,draft_id=guard.bound_draft_id)
            else:
                business.validate_actions(preparation, state, output, uploaded_paths)
            if is_csdn:
                guard.upload_started = bool(uploaded_paths)
            return
        for action in output.action:
            upload = action.model_dump(exclude_none=True).get("upload_file")
            if upload and upload["path"] != str(preparation.resume):
                raise ValueError("Agent upload path is outside this run's sole allowed PDF")

    agent = None
    browser = None
    try:
        fixture_origin = f"{urlsplit(target_url).scheme}://{urlsplit(target_url).netloc}"
        if preparation and not is_csdn:
            initial = await business.read_state(fixture_origin)
            summary["initial_fixture_state"] = initial
            business.validate_initial(initial, human_handoff)
        async with httpx.AsyncClient(timeout=60, event_hooks={"request": [request_hook], "response": [response_hook]}) as client:
            llm = ChatDeepSeek(model=MODEL, api_key=key, base_url="https://api.deepseek.com",
                               max_tokens=4096, timeout=60,
                               client_params={"http_client": client, "max_retries": 0})
            with (nullcontext(None) if is_csdn else tempfile.TemporaryDirectory(prefix="browser-use-user-data-dir-", dir=ROOT / ".local")) as profile:
                browser = Browser(**business.edge_browser_options(preparation.cdp_url)) if is_csdn else Browser(executable_path=str(chrome), headless=False, use_cloud=False,
                                  user_data_dir=profile, downloads_path=str(run_dir / "downloads"),
                                  allowed_domains=[urlsplit(target_url).hostname], enable_default_extensions=False,
                                  accept_downloads=False,
                                  keep_alive=human_handoff)
                if is_csdn:
                    await browser.start()
                    retained_page = await browser.new_page('about:blank')
                    guard = business.DraftNetworkGuard(preparation)
                    guard.preserve_only = True
                    await guard.install(browser, (await retained_page.get_target_info())['targetId'])
                    await retained_page.goto(preparation.entry_url)
                    initial, evidence = await business.inspect_initial(retained_page, guard, save)
                    if not evidence['passed']:
                        raise ValueError('CSDN_PREFLIGHT_REJECTED: '+','.join(evidence['rejection_reasons']))
                    guard.arm_new_draft(evidence)
                    summary['title_readiness'] = await business.prepare_title_control(
                        retained_page,browser,save,draft_authorized=draft_authorized)
                    summary['creation_entry'] = preparation.entry_url
                    summary['draft_write_authorized'] = True
                    summary['draft_only'] = True
                else:
                    assert Path(browser.browser_profile.user_data_dir).resolve() == Path(profile).resolve()
                tools = Tools()
                allowed = {"navigate", "click", "scroll", "find_text", "search_page", "find_elements", "go_back", "wait", "done", "switch"}
                if preparation:
                    allowed = {"navigate", "input", "upload_file", "scroll", "find_text", "search_page", "find_elements", "wait", "done"}
                if is_publish:
                    allowed.add('click')  # Target-constrained to Save Draft by pre-action callback.
                if is_csdn:
                    allowed.discard('navigate')
                for action in list(tools.registry.registry.actions):
                    if action not in allowed:
                        tools.exclude_action(action)
                summary["allowed_tools"] = sorted(tools.registry.registry.actions)
                try:
                    csdn_options = {}
                    if is_csdn:
                        from browser_use.dom.views import DEFAULT_INCLUDE_ATTRIBUTES
                        csdn_options['include_attributes'] = list(dict.fromkeys([*DEFAULT_INCLUDE_ATTRIBUTES,'class','data-title']))
                    agent = Agent(task=task, llm=llm, browser=browser, tools=tools, **csdn_options,
                                  initial_actions=[] if is_csdn else [{"navigate": {"url": target_url, "new_tab": False}}],
                                  register_new_step_callback=validate_model_files if preparation else None,
                                  use_vision=False, max_failures=1 if is_csdn else 2, max_actions_per_step=max_actions,
                                  use_judge=False, calculate_cost=False, use_thinking=False,
                                  enable_planning=False, message_compaction=False,
                                  final_response_after_failure=False, llm_timeout=70, step_timeout=90,
                                  file_system_path=str(run_dir / "agent-files"), available_file_paths=files,
                                  directly_open_url=not is_csdn, generate_gif=False, enable_signal_handler=False)
                    async with asyncio.timeout(timeout):
                        history = await agent.run(max_steps=max_steps, on_step_end=on_step_end)
                    autonomy_finished = True
                    summary.update(agent_success=history.is_successful(), final_result=history.final_result(),
                                   actions=history.model_actions(), observed_urls=history.urls())
                    summary["errors"].extend(e for e in history.errors() if e)
                    clicked = any("click" in a for a in history.model_actions())
                    url = urlsplit(live_page.get("url", ""))
                    passed = (history.is_successful() is True and clicked
                              and url.hostname == "docs.python.org" and "/tutorial/" in url.path
                              and "tutorial" in live_page.get("title", "").lower()
                              and any(c.get("status") == 200 for c in api))
                    if preparation:
                        if is_csdn:
                            if guard.pending:
                                await asyncio.wait_for(asyncio.gather(*list(guard.pending)),timeout=10)
                            live_page.update(await business.read_preparation(retained_page))
                            summary.update(business.verify_preparation(preparation, live_page, guard))
                            summary['csdn_network'] = guard.evidence()
                        else:
                            state = await business.read_state(fixture_origin)
                            summary["fixture_state"] = state
                            summary.update(business.verify(preparation, live_page, state, history))
                        summary["available_file_paths"] = files
                        summary["agent_steps"] = len(events)
                        passed = summary["verification_pass"] and not summary["errors"] and history.is_done()
                    summary["result"] = "PASS" if passed else "FAIL"
                    if is_publish and not human_handoff and passed and summary.get('preparation_status')=='READY':
                        record_state('REVIEW_REQUIRED')
                    if human_handoff and passed and summary.get('preparation_status') == 'READY':
                        from personal_web_agent.approval import handoff as handoff_session
                        agent.stop()
                        for action in list(tools.registry.registry.actions):
                            tools.exclude_action(action)
                        await client.aclose()
                        summary['agent_stopped_before_handoff'] = True
                        summary['browser_session_id'] = browser.id
                        summary['api_requests_before_handoff'] = len(api)
                        save('history.json', history.model_dump())
                        save('preparation_summary.json', summary)
                        if record_state:
                            record_state('REVIEW_REQUIRED')
                        if is_csdn:
                            from personal_web_agent.approval import review_draft
                            result = await review_draft(retained_page, preparation, dict(live_page), save)
                        else:
                            result = await handoff_session(retained_page, fixture_origin, control_token, dict(live_page), save,
                                                           record_state=record_state, publishing=is_publish)
                        if record_state and not is_csdn:
                            if is_publish and result['stage']=='COMPLETED':
                                record_state('COMPLETED', evidence={'article_id': result['page']['receipt_id'],
                                             'article_url': f"http://127.0.0.1:{urlsplit(target_url).port}" + result['page']['article_url']})
                            else:
                                record_state(result['stage'])
                        summary['handoff'] = result
                        summary['api_requests_after_handoff'] = len(api)
                        expected = result.get('human_checked') if is_csdn else result['stage'] in {'COMPLETED', 'CANCELLED'}
                        summary['result'] = 'PASS' if expected and len(api) == summary['api_requests_before_handoff'] else 'FAIL'
                finally:
                    try:
                        if agent is not None:
                            save("history.json", agent.history.model_dump())
                    finally:
                        if not is_csdn:
                            await asyncio.wait_for(browser.kill(), timeout=20)
                            summary["browser_closed"] = True
    except asyncio.CancelledError:
        summary['result'] = 'FAIL'
        summary['errors'].append('Interrupted; human handoff was not verified')
        if human_handoff:
            summary['handoff'] = {'stage': 'UNKNOWN', 'error': 'Interrupted; inspect fixture records before retrying'}
        raise
    except Exception as exc:
        summary["errors"].append(f"{type(exc).__name__}: {exc}")
        summary["result"] = "FAIL"
    finally:
        autonomy_finished = True
        if is_csdn and browser is not None:
            if agent is not None:
                agent.stop()
                for action in list(tools.registry.registry.actions):tools.exclude_action(action)
            if 'preflight' not in summary:
                evidence = business.preflight_evidence({}, guard)
                evidence['stage_error'] = 'INITIAL_OBSERVATION_NOT_COMPLETED'
                save('preflight.json',evidence)
            cleanup = await business.cleanup_session(browser,guard,save,failed=summary['result']!='PASS')
            summary['cleanup'] = cleanup
            summary['browser_disconnected'] = cleanup['browser_disconnected']
            summary['browser_kept_open'] = cleanup['page_preserved']
            summary['errors'].extend(cleanup['errors'])
            if guard is not None:summary['csdn_network'] = guard.evidence()
        if record_state and not summary.get('handoff') and not is_csdn:
            try:
                state = await business.read_state(fixture_origin)
                if business.known_no_effects(state):
                    record_state('FAILED')
                else:
                    record_state('UNKNOWN')
            except Exception:
                # Wrapper marks UNKNOWN; never infer failure from missing evidence.
                pass
        summary.update(final_page=live_page, api_requests=len(api),
                       api_successes=sum(c.get("status") == 200 for c in api))
        summary["tokens"] = {name: sum((call.get("usage") or {}).get(name, 0) for call in api)
                            for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
        if (not summary["browser_closed"] and not (is_csdn and summary.get('browser_disconnected'))) or summary['errors']:
            summary["result"] = "FAIL"
        save("summary.json", summary)
        save("api_calls.json", api)
        root_logger.removeHandler(file_handler)
        file_handler.close()
    print((run_dir / "summary.json").read_text(encoding="utf-8"))
    print(f"Evidence: {run_dir.relative_to(ROOT)}")
    return 0 if summary["result"] == "PASS" and (not preparation or summary.get("preparation_status") == "READY") else 1
