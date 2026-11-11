# 本地招聘 fixture（当前 P1-C1 保护）

单进程内存测试站，复用现有 Starlette/Uvicorn/multipart/pypdf/HTTPX，不使用模型、数据库或真实个人资料。

在项目根目录启动：

```sh
.venv/bin/python -m uvicorn fixture_site.app:app --app-dir tests --host 127.0.0.1 --port 8000 --workers 1
```

正常地址 http://127.0.0.1:8000/ ，字段换序加 `?order=reordered`，上传失败加 `?upload=fail`，可以组合。
通过标签区分姓名、邮箱、联系方式、介绍和 PDF；选择文件调用独立 POST /api/upload，服务器检查有效 PDF、
未加密、有页面、最大 2 MiB。页面显示服务器确认的成功/失败，最终 POST /api/submit 不重新上传。

```sh
.venv/bin/python tests/fixture_site/sample_resume.py
PYTHONPATH=tests .venv/bin/python -m fixture_site.control state
PYTHONPATH=tests .venv/bin/python -m fixture_site.control reset
```

生成的 `.local/fixture-resume.pdf` 是带 FICTIONAL 标记的空白有效测试 PDF。
状态 GET /api/state 不需要控制凭据；reset/cancel/authorize 都需要私有 X-Fixture-Control 凭据。
模块启动时创建或读取 `.local/fixture-control-token`（0600），控制 CLI 和 Runner 使用它，但不打印或提供给网页/Agent。
只在两次运行之间重置；授权未消费时先取消再重置。重置会清空当前测试代次的记录和旧上传 ID。

## 提交保护与计数

- submit_count 是历史兼容字段，等于 submit_attempt_count；任何到达最终端点的请求均计入，包括拒绝。
- successful_submit_count 仅在已授权、资料及上传 ID 有效时增加。锁定和取消状态都不能成功。
- approval_status：LOCKED → AUTHORIZED → CONSUMED；取消可使尚未成功的状态转为 CANCELLED。
- CLI 授权绑定指定 upload_id，只允许一次成功；重复请求返回 409，不覆盖已经成功的业务终态或回执。
- submissions 记录所有成功/拒绝尝试；final_status 一旦成功保持 COMPLETED，不因重复请求被改写为失败。
- upload_count 和 uploads 仍独立记录上传次数、结果、文件名和大小；授权保护不影响上传。

授权消费在单 worker 的无 await 临界段内完成。状态在标签页之间共享、重启丢失；只证明本地测试站安全，
不能推论真实平台具备同样的拦截。文件内容和表单个人字段不持久化。
原页面仅显示提交和计数，不提供授权或重置按钮；批准必须在 Agent 已停止后的交互 CLI 中完成。

## 测试边界

```sh
# 离线协议、输入及回归测试；不调用模型
.venv/bin/python -m unittest discover -s tests -v
# 无模型的正常/换序上传、失败提示检查；不再自动点击最终 Submit
PYTHONPATH=tests .venv/bin/python -m fixture_site.browser_check
# 原生零步 run 收尾后同一浏览器/页面可用；不冒充真实 Agent 准备或真人操作
PYTHONPATH=tests .venv/bin/python -m fixture_site.lifecycle_check
```

当前浏览器检查输出 `.local/p1-c1-fixture-browser-result.json`，生命周期结果 `.local/p1-c1-lifecycle.json`；
测试结束自动关闭服务和测试浏览器。内部报告与旧记录仅留本地，不随公开源码分发；当前保护规则以上述说明为准。

完整真人步骤见根目录 [README](../../docs/USAGE.md#handoff)。真实接管使用已有 apply-prepare 加 --handoff；
不要用自动点击、管道批准或 HTTP 测试冒充真人。prepare_check.py 仍可用于 P1-B 三场景真实模型回归，会消耗额度，
本轮没有重复运行该付费套件。

## P3-A 图文发布页（同一服务）

仍用上面的 fixture_site.app:app 和单 worker 启动命令；已有旧进程需重启以加载新增路由。
招聘页 `/` 和 `/api/state` 保持原语义，图文状态独立。

- 正常图文页：http://127.0.0.1:8000/publish/
- 图片失败模式：http://127.0.0.1:8000/publish/?upload=fail
- 图文状态：http://127.0.0.1:8000/api/publish/state
- 发布后详情：`/publish/articles/<article_id>`，JSON：`/api/publish/articles/<article_id>`。

```sh
.venv/bin/python tests/fixture_site/sample_article.py
PYTHONPATH=tests .venv/bin/python -m fixture_site.control state --site publish
```

生成 `.local/publish-fixture/article.json` 和两张 `fictional-*.png`。文章与图片均为程序合成，无个人资料。
将文章标题、正文填入页面，选择两张测试图片：图片会立即发往**本地**上传端点。
页面显示服务器确认结果并预览实际服务器图片；本地文字预览不保存草稿、不发布。
点击“保存草稿”才写服务器草稿；允许保存不完整草稿，发布时要求标题、正文和至少一张成功图片。
不做自动保存。刷新页面能从服务器恢复当前显式保存的草稿，服务重启会丢失它。

状态与独立事件：

| 接口 | 事件及记录 |
| --- | --- |
| POST /api/publish/upload | upload_request_count / uploads；一次请求可传 1–5 张图片，PNG/JPEG 实际解码验证，每张 ≤2 MiB、尺寸 ≤4096×4096；批次全成功或全失败 |
| POST /api/publish/draft | draft_save_count 为尝试数，successful_draft_save_count 为成功数；draft_saves 记录结果，draft 保存标题/正文/图片顺序及新 draft_id |
| POST /api/publish/publish | publish_attempt_count 记录所有请求尝试；successful_publish_count 仅成功时增加；publications 记录成功/拒绝 |
| GET /api/publish/state | 查询上述事件、当前草稿、授权状态、已发布文章 ID/相对 URL 和内容 |

图片字节仅存于该进程内存，GET `/publish/images/<image_id>` 供预览和详情显示。
非法图片、超量、扩展名不符或模拟失败不会产生可引用的新 image_id；失败上传不能被用于草稿/发布。
所有图片、草稿和文章在图文 reset 或服务重启后消失；这不是 TaskStore，也没有复制 SQLite。

### 单次发布授权（测试控制协议，不是新增人工审批流程）

复用同一 `.local/fixture-control-token` 和 X-Fixture-Control 校验；页面/Agent 不获得凭据。
页面不含 authorize/reset 按钮；已有 Agent/人工接管用法见根 README。
在本地手工测试时，完成草稿后从状态中获取 draft_id，由测试人员显式执行：

```sh
PYTHONPATH=tests .venv/bin/python -m fixture_site.control authorize --site publish --draft-id '<draft_id>'
# 测试人员随后可在本地页面亲自点击 Publish，按回执链接查看文章
PYTHONPATH=tests .venv/bin/python -m fixture_site.control state --site publish
PYTHONPATH=tests .venv/bin/python -m fixture_site.control cancel --site publish
PYTHONPATH=tests .venv/bin/python -m fixture_site.control reset --site publish
```

LOCKED → AUTHORIZED → CONSUMED，授权绑定当前完整草稿；授权期间拒绝改草稿和 reset，先 cancel 才能 reset。
CANCELLED 不可再次授权，需 reset 新测试代次。每代最多一次成功发布，成功后不能通过重复授权或取消覆盖成功。
重复请求仍累计尝试，返回 409，文章内容/ID/URL 和 PUBLISHED 保持不变。
reset 只清图文状态，不影响招聘；原招聘 reset 也不清图文。

```sh
.venv/bin/python -m unittest discover -s tests -v
# 本地可见浏览器、无 Agent/模型；填合成内容、多图上传、预览、保存及恢复；不点击 Publish
PYTHONPATH=tests .venv/bin/python -m fixture_site.publish_browser_check
```

离线 HTTP 测试可以验证发布协议和文章详情，不冒充真人点击或 Agent E2E。
本地单 worker 的服务器保护不能推广到 WordPress 或其他真实网站；不要向此 fixture 放真实资料或对公网开放。

## P3-B 原生逐张上传兼容

browser-use 0.13.14 的 upload_file 一次一个路径。因此图片选择现在追加到已有成功图片列表，
仍支持一次多选；任何本次上传失败会清空页面可用图片 ID，显示明确失败，不把旧图片当成当前成功。
服务端成功图片元数据增加 sha256，供 Agent 准备结果独立核对字节和顺序，不以文件名作为唯一依据。
生成器新增 `.local/publish-fixture/body.txt` 供 publish-prepare --body-file 使用。
Agent 可点击的唯一按钮为 Save Draft，最终 Publish 只能由停止 Agent 后的真人完成；完整用法见根 README。
