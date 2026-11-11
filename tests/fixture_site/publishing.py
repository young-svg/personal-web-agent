"""Publishing routes on the existing fixture app. Single worker, in-memory only."""
from dataclasses import asdict, dataclass, field
from html import escape
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import hashlib
import warnings

from PIL import Image
from starlette.datastructures import UploadFile
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGES = 5


@dataclass
class PublishState:
    upload_request_count: int = 0
    uploads: list = field(default_factory=list)
    draft_save_count: int = 0
    successful_draft_save_count: int = 0
    draft_saves: list = field(default_factory=list)
    draft: dict | None = None
    publish_attempt_count: int = 0
    successful_publish_count: int = 0
    publications: list = field(default_factory=list)
    articles: list = field(default_factory=list)
    approval_status: str = 'LOCKED'
    authorized_draft_id: str | None = None
    final_status: str = 'NOT_PUBLISHED'


def error(message, code=422):
    return JSONResponse({'error': message}, status_code=code)


def content_values(data, images, *, complete):
    if not isinstance(data, dict) or set(data) - {'title', 'body', 'image_ids'}:
        raise ValueError('只接受 title、body、image_ids')
    title, body, ids = data.get('title', ''), data.get('body', ''), data.get('image_ids', [])
    if not isinstance(title, str) or not isinstance(body, str) or len(title) > 200 or len(body) > 10000:
        raise ValueError('标题最大 200 字符，正文最大 10000 字符')
    if (not isinstance(ids, list) or len(ids) > MAX_IMAGES or any(not isinstance(x, str) for x in ids)
            or len(set(ids)) != len(ids) or any(x not in images for x in ids)):
        raise ValueError('图片 ID 必须唯一、已成功上传，且最多五张')
    if complete and (not title.strip() or not body.strip() or not ids):
        raise ValueError('发布必须有标题、正文和至少一张已上传图片')
    return {'title': title, 'body': body, 'image_ids': list(ids)}


def publishing_routes(controlled):
    async def index(request):
        mode = request.query_params.get('upload', 'normal')
        if mode not in {'normal', 'fail'}:
            return error('Unknown fixture mode', 400)
        text = Path(__file__).with_name('publish.html').read_text(encoding='utf-8')
        return HTMLResponse(text.replace('{{UPLOAD_ENDPOINT}}', '/api/publish/upload?fail=1' if mode == 'fail' else '/api/publish/upload'),
                            headers={'Cache-Control': 'no-store'})

    async def state_response(request):
        return JSONResponse({**asdict(request.app.state.publish_state), 'fixture_version': 'P3-A'},
                            headers={'Cache-Control': 'no-store'})

    async def upload(request):
        state = request.app.state.publish_state
        state.upload_request_count += 1
        record = {'upload_id': str(uuid4()), 'success': False, 'images': [], 'error': None}
        pending = {}
        code = 200
        try:
            async with request.form(max_files=MAX_IMAGES, max_fields=0) as form:
                files = form.getlist('images')
                if not files or len(files) > MAX_IMAGES or len(form.multi_items()) != len(files):
                    raise ValueError('请选择一至五张 PNG/JPEG 测试图片')
                for file in files:
                    if not isinstance(file, UploadFile):
                        raise ValueError('图片必须是文件')
                    data = await file.read(MAX_IMAGE_BYTES + 1)
                    if len(data) > MAX_IMAGE_BYTES:
                        code = 413
                        raise ValueError('单张图片超过 2 MiB')
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter('error', Image.DecompressionBombWarning)
                            with Image.open(BytesIO(data)) as image:
                                fmt = image.format
                                width, height = image.size
                                if fmt not in {'PNG', 'JPEG'} or max(width, height) > 4096:
                                    raise ValueError('仅接受最大 4096×4096 的 PNG/JPEG')
                                image.verify()
                            with Image.open(BytesIO(data)) as decoded:
                                decoded.load()
                    except Exception as exc:
                        raise ValueError('图片无效或格式/尺寸不支持') from exc
                    expected = {'.png'} if fmt == 'PNG' else {'.jpg', '.jpeg'}
                    if Path(file.filename or '').suffix.lower() not in expected:
                        raise ValueError('扩展名与图片实际格式不符')
                    image_id = str(uuid4())
                    mime = 'image/png' if fmt == 'PNG' else 'image/jpeg'
                    meta = {'image_id': image_id, 'filename': Path(file.filename).name, 'size': len(data),
                            'content_type': mime, 'width': width, 'height': height, 'sha256': hashlib.sha256(data).hexdigest(),
                            'url': '/publish/images/' + image_id}
                    record['images'].append(meta)
                    pending[image_id] = {'data': data, 'meta': meta}
            if request.query_params.get('fail') == '1':
                code = 503
                raise ValueError('模拟图片上传失败')
            if request.app.state.publish_state is not state:
                code = 409
                raise ValueError('测试状态已重置')
            request.app.state.publish_images.update(pending)
            record['success'] = True
        except Exception as exc:
            record['images'] = []  # Batch is atomic: never expose partial success IDs.
            record['error'] = str(exc) if isinstance(exc, ValueError) else '图片上传请求无效'
            if code == 200:
                code = 400
        state.uploads.append(record)
        return JSONResponse(record, status_code=code)

    async def save_draft(request):
        state = request.app.state.publish_state
        state.draft_save_count += 1
        record = {'success': False, 'draft_id': None, 'error': None}
        code = 200
        try:
            data = await request.json()
            values = content_values(data, request.app.state.publish_images, complete=False)
            if state is not request.app.state.publish_state or state.approval_status != 'LOCKED':
                code = 409
                raise ValueError('须在未授权代次保存；授权后请先取消，取消/发布后重置再测')
            draft = {'draft_id': str(uuid4()), **values}
            state.draft = draft
            state.successful_draft_save_count += 1
            record.update(success=True, draft_id=draft['draft_id'])
        except Exception as exc:
            record['error'] = str(exc) if isinstance(exc, ValueError) else '草稿请求无效'
            if code == 200:
                code = 422
        state.draft_saves.append(record)
        return JSONResponse(record, status_code=code)

    async def authorize(request):
        if not controlled(request):
            return error('Control credential required', 403)
        state = request.app.state.publish_state
        try:
            data = await request.json()
            if (not isinstance(data, dict) or not state.draft or data.get('draft_id') != state.draft['draft_id']):
                return error('必须指定当前已保存的 draft_id')
            content_values({k: state.draft[k] for k in ('title', 'body', 'image_ids')}, request.app.state.publish_images, complete=True)
            if state is not request.app.state.publish_state or state.approval_status != 'LOCKED':
                return error('Authorization is not reusable', 409)
            state.authorized_draft_id = state.draft['draft_id']
            state.approval_status = 'AUTHORIZED'
            return await state_response(request)
        except (ValueError, KeyError):
            return error('草稿不完整或授权请求无效')

    async def publish(request):
        state = request.app.state.publish_state
        state.publish_attempt_count += 1
        record = {'attempt_id': str(uuid4()), 'status': 'REJECTED', 'error': None}
        code = 200
        try:
            data = await request.json()
            if (not isinstance(data, dict) or set(data) != {'draft_id'} or not state.draft
                    or data.get('draft_id') != state.draft['draft_id']):
                raise ValueError('只能发布当前已保存的草稿')
            values = content_values({k: state.draft[k] for k in ('title', 'body', 'image_ids')}, request.app.state.publish_images, complete=True)
            # Atomic in the existing single-worker fixture: no await from here
            # through validation of the authorization and its consumption.
            if state is not request.app.state.publish_state:
                code = 409
                raise ValueError('测试状态已重置')
            if state.approval_status != 'AUTHORIZED' or state.authorized_draft_id != data['draft_id']:
                code = 409 if state.successful_publish_count else 403
                raise ValueError('没有有效的单次发布授权')
            article_id = str(uuid4())
            article = {'article_id': article_id, 'url': '/publish/articles/' + article_id, **values}
            state.articles.append(article)
            state.approval_status = 'CONSUMED'
            state.authorized_draft_id = None
            state.successful_publish_count += 1
            state.final_status = 'PUBLISHED'
            record.update(status='COMPLETED', article_id=article_id, url=article['url'], draft_id=data['draft_id'])
        except Exception as exc:
            record['error'] = str(exc) if isinstance(exc, ValueError) else '发布请求无效'
            if code == 200:
                code = 422
        state.publications.append(record)
        return JSONResponse(record, status_code=code)

    async def cancel(request):
        if not controlled(request):
            return error('Control credential required', 403)
        state = request.app.state.publish_state
        if state.successful_publish_count:
            return error('Already published; cancellation cannot erase success', 409)
        state.approval_status = 'CANCELLED'
        state.authorized_draft_id = None
        return await state_response(request)

    async def reset(request):
        if not controlled(request):
            return error('Control credential required', 403)
        if request.app.state.publish_state.approval_status == 'AUTHORIZED':
            return error('Cancel authorization before reset', 409)
        request.app.state.publish_state = PublishState()
        request.app.state.publish_images = {}
        return await state_response(request)

    async def image_response(request):
        image = request.app.state.publish_images.get(request.path_params['image_id'])
        if image is None:
            return error('Image not found', 404)
        return Response(image['data'], media_type=image['meta']['content_type'], headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})

    async def article_response(request):
        article = next((a for a in request.app.state.publish_state.articles if a['article_id'] == request.path_params['article_id']), None)
        if article is None:
            return error('Article not found', 404)
        if request.url.path.startswith('/api/'):
            return JSONResponse(article)
        images = ''.join(f'<img src="/publish/images/{escape(i)}" alt="合成文章图片" style="max-width:100%">' for i in article['image_ids'])
        return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                            f'<title>{escape(article["title"])}</title><main style="max-width:760px;margin:32px auto;font:16px/1.7 system-ui">'
                            f'<h1>{escape(article["title"])}</h1><p id="article-id">{article["article_id"]}</p>'
                            f'<div id="article-body" style="white-space:pre-wrap">{escape(article["body"])}</div>{images}</main></html>',
                            headers={'Cache-Control': 'no-store'})

    return [Route('/publish/', index), Route('/api/publish/state', state_response),
            Route('/api/publish/upload', upload, methods=['POST']), Route('/api/publish/draft', save_draft, methods=['POST']),
            Route('/api/publish/authorize', authorize, methods=['POST']), Route('/api/publish/publish', publish, methods=['POST']),
            Route('/api/publish/cancel', cancel, methods=['POST']), Route('/api/publish/reset', reset, methods=['POST']),
            Route('/publish/images/{image_id}', image_response), Route('/publish/articles/{article_id}', article_response),
            Route('/api/publish/articles/{article_id}', article_response)]
