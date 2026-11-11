"""Three real P1-B Agent runs against an isolated local fixture; costs API credits."""

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
from fixture_site.sample_resume import sample_pdf_bytes

ROOT = Path(__file__).resolve().parents[2]


def main():
    suite = ROOT / '.local/p1-b-suite' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    suite.mkdir(parents=True)
    pdf = suite / 'fictional-resume.pdf'
    pdf.write_bytes(sample_pdf_bytes())
    profile = ROOT / 'tests/fixture_site/synthetic_profile.json'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    report = {'result': 'FAIL', 'cases': [], 'base_url': base, 'unexpected_submission': False}
    with (suite / 'server.log').open('w') as server_log:
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'fixture_site.app:app', '--app-dir', 'tests',
                                   '--host', '127.0.0.1', '--port', str(port), '--workers', '1'],
                                  cwd=ROOT, stdout=server_log, stderr=server_log)
        try:
            with httpx.Client(trust_env=False, timeout=10) as client:
                deadline = time.monotonic() + 15
                while True:
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Fixture failed to start')
                    try:
                        if client.get(base + '/api/state').status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.1)
                for name, query, expected_exit in [('normal', '', 0), ('reordered', '?order=reordered', 0), ('upload_failure', '?upload=fail', 1)]:
                    # Reset only between runs, after the previous result is captured.
                    control = {'X-Fixture-Control': (ROOT / '.local/fixture-control-token').read_text().strip()}
                    client.post(base + '/api/reset', headers=control).raise_for_status()
                    before = set((ROOT / '.local/apply-prepare').glob('*'))
                    print(f'Starting {name}', flush=True)
                    with (suite / f'{name}.log').open('w') as log:
                        completed = subprocess.run([sys.executable, '-m', 'personal_web_agent.cli', 'apply-prepare',
                            '--url', base + '/' + query, '--profile', str(profile), '--resume', str(pdf),
                            '--instruction', '请填写提供的虚构资料并上传测试 PDF，核对页面状态后停止，绝不点击 Submit；上传失败时如实报告且不要重试。'],
                            cwd=ROOT, stdout=log, stderr=log, timeout=540)
                    created = set((ROOT / '.local/apply-prepare').glob('*')) - before
                    if len(created) != 1:
                        raise RuntimeError('Expected exactly one new run evidence directory')
                    run_dir = created.pop()
                    summary = json.loads((run_dir / 'summary.json').read_text())
                    server_state = client.get(base + '/api/state').json()
                    case = {'name': name, 'exit_code': completed.returncode, 'expected_exit_code': expected_exit,
                            'evidence': str(run_dir.relative_to(ROOT)), 'summary': summary, 'server_state_after_run': server_state}
                    case['passed'] = summary['result'] == 'PASS' and completed.returncode == expected_exit and server_state['submit_count'] == 0
                    report['cases'].append(case)
                    (suite / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
                    print(f"Finished {name}: passed={case['passed']}, submits={server_state['submit_count']}, calls={summary['api_requests']}", flush=True)
                    if server_state['submit_count'] != 0:
                        report['unexpected_submission'] = True
                        raise RuntimeError('Unexpected submission preserved; refusing further resets/runs')
                report['result'] = 'PASS' if all(c['passed'] for c in report['cases']) else 'FAIL'
        except Exception as exc:
            report['error'] = f'{type(exc).__name__}: {exc}'
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            report['server_stopped'] = True
            (suite / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Suite result: {report['result']}; evidence: {suite.relative_to(ROOT)}", flush=True)
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
