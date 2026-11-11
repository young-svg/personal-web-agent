"""Single-worker, in-memory recruitment fixture using existing Starlette deps."""

from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path
import re
import os
import secrets
from uuid import uuid4

from pypdf import PdfReader
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from fixture_site.publishing import PublishState, publishing_routes

MAX_PDF_BYTES = 2 * 1024 * 1024
FIELDS = {
    "name": '<label for="name">姓名</label><input id="name" name="name" required maxlength="100" autocomplete="off">',
    "email": '<label for="email">邮箱</label><input id="email" name="email" type="email" required maxlength="200" autocomplete="off">',
    "contact": '<label for="contact">联系方式</label><input id="contact" name="contact" type="tel" required maxlength="100" autocomplete="off">',
    "introduction": '<label for="introduction">个人介绍</label><textarea id="introduction" name="introduction" required maxlength="2000"></textarea>',
    "resume": '<label for="resume">PDF 简历上传（最大 2 MiB）</label><input id="resume" name="resume" type="file" accept=".pdf,application/pdf"><p id="upload-status" role="status" aria-live="polite" data-state="idle">尚未上传</p>',
}
NORMAL_ORDER = ("name", "email", "contact", "introduction", "resume")
REORDERED = ("introduction", "resume", "contact", "name", "email")

CONTROL_FILE = Path(__file__).resolve().parents[2] / ".local/fixture-control-token"


def fixture_control_token():
    """Private local test control credential, never served to the browser/Agent."""
    CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(CONTROL_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return CONTROL_FILE.read_text().strip()
    with os.fdopen(fd, 'w') as file:
        token = secrets.token_urlsafe(32)
        file.write(token)
    return token


@dataclass
class TestState:
    upload_count: int = 0
    uploads: list[dict] = field(default_factory=list)
    submit_count: int = 0
    submissions: list[dict] = field(default_factory=list)
    final_status: str = "NOT_SUBMITTED"
    successful_submit_count: int = 0
    approval_status: str = "LOCKED"
    authorized_upload_id: str | None = None


def create_app(control_token: str | None = None) -> Starlette:
    control_token = control_token or secrets.token_urlsafe(32)

    def controlled(request):
        return secrets.compare_digest(request.headers.get('X-Fixture-Control', ''), control_token)

    async def index(request: Request):
        mode = request.query_params.get("order", "normal")
        upload_mode = request.query_params.get("upload", "normal")
        if mode not in {"normal", "reordered"} or upload_mode not in {"normal", "fail"}:
            return JSONResponse({"error": "Unknown fixture mode"}, status_code=400)
        order = REORDERED if mode == "reordered" else NORMAL_ORDER
        fields = "\n".join(f'<div class="field" data-field="{name}">{FIELDS[name]}</div>' for name in order)
        template = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
        return HTMLResponse(template.replace("{{FIELDS}}", fields).replace("{{ORDER}}", mode)
                            .replace("{{UPLOAD_ENDPOINT}}", "/api/upload?fail=1" if upload_mode == "fail" else "/api/upload"),
                            headers={"Cache-Control": "no-store"})

    async def upload(request: Request):
        filename, size, error = "", 0, None
        status_code = 200
        try:
            async with request.form(max_files=1, max_fields=0) as form:
                file = form.get("resume")
                if not isinstance(file, UploadFile):
                    raise ValueError("请选择 PDF 文件")
                filename = Path(file.filename or "").name
                content = await file.read(MAX_PDF_BYTES + 1)
                size = len(content)
                if request.query_params.get("fail") == "1":
                    status_code = 503
                    raise ValueError("模拟上传失败，请重试或切换正常模式")
                if size > MAX_PDF_BYTES:
                    status_code = 413
                    raise ValueError("PDF 超过 2 MiB")
                if not filename.lower().endswith(".pdf") or not content.startswith(b"%PDF-"):
                    raise ValueError("仅接受有效 PDF 文件")
                try:
                    pdf = PdfReader(BytesIO(content), strict=True)
                    if pdf.is_encrypted or not len(pdf.pages):
                        raise ValueError("Empty or encrypted PDF")
                except Exception as exc:
                    raise ValueError("PDF 无法解析或没有可读页面") from exc
        except Exception as exc:
            # Keep this fixture's errors clear without exposing parser internals.
            error = str(exc) if isinstance(exc, ValueError) else "上传请求无效"
            if status_code == 200:
                status_code = 400
        state = request.app.state.test_state
        state.upload_count += 1
        record = {"upload_id": str(uuid4()), "filename": filename, "size": size,
                  "success": error is None, "error": error}
        state.uploads.append(record)
        return JSONResponse(record, status_code=status_code)

    async def submit(request: Request):
        state = request.app.state.test_state
        # Count every request reaching the final endpoint, including rejection.
        state.submit_count += 1
        submission_id = str(uuid4())
        try:
            async with request.form(max_files=0, max_fields=5) as form:
                values = {name: str(form.get(name, "")).strip() for name in NORMAL_ORDER if name != "resume"}
                if not all(values.values()):
                    raise ValueError("请完整填写姓名、邮箱、联系方式和个人介绍")
                if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values["email"]):
                    raise ValueError("邮箱格式无效")
                if any(len(values[name]) > limit for name, limit in (("name", 100), ("email", 200), ("contact", 100), ("introduction", 2000))):
                    raise ValueError("字段内容过长")
                upload_id = str(form.get("upload_id", ""))
                if not any(u["upload_id"] == upload_id and u["success"] for u in state.uploads):
                    raise ValueError("必须先获得服务器确认的 PDF 上传成功记录")
            # No await between authorization check and consumption: only one
            # concurrent request can succeed in this single-worker fixture.
            if request.app.state.test_state is not state:
                raise ValueError("测试状态已重置，旧请求被拒绝")
            if state.approval_status != 'AUTHORIZED' or state.authorized_upload_id != upload_id:
                record = {"submission_id": submission_id, "status": "REJECTED", "error": "没有有效的单次人工授权"}
                state.submissions.append(record)
                return JSONResponse(record, status_code=403 if state.successful_submit_count == 0 else 409)
            state.approval_status = 'CONSUMED'
            state.authorized_upload_id = None
            state.successful_submit_count += 1
            record = {"submission_id": submission_id, "status": "COMPLETED", "upload_id": upload_id}
            code = 200
        except Exception as exc:
            record = {"submission_id": submission_id, "status": "REJECTED",
                      "error": str(exc) if isinstance(exc, ValueError) else "提交请求无效"}
            code = 422
        state.submissions.append(record)
        if record['status'] == 'COMPLETED':
            state.final_status = 'COMPLETED'
        return JSONResponse(record, status_code=code)

    async def state_response(request: Request):
        snapshot = asdict(request.app.state.test_state)
        snapshot['submit_attempt_count'] = snapshot['submit_count']
        snapshot['fixture_version'] = 'P1-C1'
        return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})

    async def authorize(request: Request):
        if not controlled(request):
            return JSONResponse({'error': 'Control credential required'}, status_code=403)
        data = await request.json()
        state = request.app.state.test_state
        upload_id = data.get('upload_id')
        if state.approval_status != 'LOCKED' or state.successful_submit_count:
            return JSONResponse({'error': 'Authorization is not reusable'}, status_code=409)
        if not any(u['upload_id'] == upload_id and u['success'] for u in state.uploads):
            return JSONResponse({'error': 'A successful upload is required'}, status_code=422)
        state.authorized_upload_id = upload_id
        state.approval_status = 'AUTHORIZED'
        return await state_response(request)

    async def cancel(request: Request):
        if not controlled(request):
            return JSONResponse({'error': 'Control credential required'}, status_code=403)
        state = request.app.state.test_state
        if state.successful_submit_count:
            return JSONResponse({'error': 'Already submitted; cancellation cannot erase success'}, status_code=409)
        state.approval_status = 'CANCELLED'
        state.authorized_upload_id = None
        return await state_response(request)

    async def reset(request: Request):
        if not controlled(request):
            return JSONResponse({'error': 'Control credential required'}, status_code=403)
        if request.app.state.test_state.approval_status == 'AUTHORIZED':
            return JSONResponse({'error': 'Cancel authorization before reset'}, status_code=409)
        request.app.state.test_state = TestState()
        return await state_response(request)

    app = Starlette(routes=[Route("/", index), Route("/api/upload", upload, methods=["POST"]),
                            Route("/api/submit", submit, methods=["POST"]), Route("/api/state", state_response),
                            Route("/api/reset", reset, methods=["POST"]), Route('/api/authorize', authorize, methods=['POST']),
                            Route('/api/cancel', cancel, methods=['POST'])] + publishing_routes(controlled))
    app.state.test_state = TestState()
    app.state.publish_state = PublishState()
    app.state.publish_images = {}
    return app


app = create_app(fixture_control_token())
