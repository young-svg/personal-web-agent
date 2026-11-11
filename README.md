# Personal Web Agent

用自然语言驱动本地可见浏览器，准备求职申请或图文内容，在最终提交前停止 Agent，由用户检查并亲自点击，程序再核验结果。项目是个人工程实践原型，按 [开发规划 v1.1](DEVELOPMENT_PLAN.md) 分阶段验收。

## 当前能力与边界

| 能力 | 已验证范围 | 未完成部分 |
| --- | --- | --- |
| DeepSeek + browser-use | 真实 Chrome 打开 Python 文档、自主进入 Tutorial 并读取标题与 URL | 不承诺任意页面成功率 |
| 求职申请 | 本地 fixture：自主填表、字段换序、PDF 上传/失败、真人取消/提交、同会话回执核验 | 真实招聘平台填写、上传和投递 |
| 图文发布 | 本地 fixture：标题/正文、多图上传、草稿、真人取消/发布、成功 ID/URL 核验 | 真实内容平台完整发布链路 |
| SQLite | 状态持久化、重复拦截、结果不确定时阻止自动重试；离线测试覆盖 | 不恢复浏览器或远端业务事务 |
| Greenhouse | 真实页面只读检查、离线字段匹配与稳定岗位去重 | 真实写入保持 BLOCKED，无真实申请成功证据 |
| CSDN / Edge | 独立 Profile 登录态复用、只读结构检查；离线适配与回归 | 真实编辑、图片上传、草稿核验、发布均未完成验收 |

CSDN 最近一次真实尝试通过空白页预检并成功调用模型 1 次，但返回不存在的索引 0，输入前被拒绝。标题激活的最小修复仅通过离线测试，未在真实页面重验。相关任务保留 UNKNOWN，不能据此宣称 CSDN 编辑成功。相关原始执行证据只保留本地，不随源码发布。

本地 E2E 证据来自历史真实模型运行和用户本人操作；内部验收报告与原始日志不随公开源码分发。当前 121 项自动化测试通过不等同于本轮重新完成真人或真实平台 E2E。

## 技术架构

Python 3.12 + uv；开源 **browser-use Python Library 0.13.14**；DeepSeek `deepseek-flash`，`use_vision=False`；本地 Chrome，CSDN 实验分支附加 Edge 回环 CDP。

```text
CLI → 输入校验 → 共享 Runner → browser-use Agent + DeepSeek → 可见浏览器
                        ↓                   ↓
                     SQLite ← 人工接管 ← 准备结果独立核验
                                  ↓
                      用户亲自点击 → 只读结果核验
```

同一个 Agent 执行循环，招聘/发布共用 Runner、approval 和标准库 sqlite3 Task Store。fixture 是 Starlette/Uvicorn 单进程内存服务；不使用 Browser Use Cloud、ORM、LangGraph 或 Multi-Agent。上游依赖包含 Cloud SDK/MCP 等包，不表示本项目调用这些服务。

## 环境安装

已验证环境为 macOS arm64、Python 3.12 和可见 Google Chrome。运行器当前使用 macOS Chrome 安装路径 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`，需要桌面登录会话。Windows/Linux 浏览器执行尚未适配或验收；安装与离线测试命令不依赖作者的个人目录。不要将下列命令视为跨平台浏览器支持承诺。

在项目根目录执行。已有 uv 可跳过第一条；本例将工具和缓存放在 Git 忽略目录：

```sh
python3 -m pip install --no-cache-dir --target .local/tools uv==0.13.6
export PATH="$PWD/.local/tools/bin:$PATH"
export UV_CACHE_DIR="$PWD/.local/uv-cache"
export UV_PYTHON_INSTALL_DIR="$PWD/.local/python"
uv python install --no-bin 3.12
uv sync --locked --group fixture
.venv/bin/personal-web-agent --help
```

Python 范围为 `>=3.12,<3.13`。运行依赖显式声明 browser-use、python-dotenv、HTTPX、Pillow、pypdf；`fixture` 依赖组声明本地服务/测试直接使用的 Starlette、Uvicorn、python-multipart。全部沿用已验证版本；`uv.lock` 是安装依据，[DEPENDENCY_VERSIONS.txt](DEPENDENCY_VERSIONS.txt) 是原开发环境快照。上游仍可能将这些测试组件作为传递依赖安装。

## 最小测试：无需 Key、无需真实浏览器

完成上述安装后：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/check_imports.py
uv pip check --python .venv/bin/python
.venv/bin/personal-web-agent apply-prepare --help
.venv/bin/personal-web-agent publish-prepare --help
```

自动化测试使用临时数据库、合成内容、本地 ASGI/模拟浏览器对象，覆盖输入、上传/发布协议、人工接管分支、状态持久化和防重复，不调用 DeepSeek 或真实网站。基础导入脚本阻断 socket，仅本地构造模型对象。

## DeepSeek API 配置与真实 Smoke

仅在需要真实 Agent 时配置。不要覆盖已有 `.env`，也不要将密钥发送到聊天或上传 Git：

```sh
# 仅当 .env 不存在时复制
cp -n .env.example .env
chmod 600 .env
# 使用本地编辑器填写 DEEPSEEK_API_KEY 后再运行
.venv/bin/personal-web-agent smoke
```

项目从根目录 `.env` 读取 `DEEPSEEK_API_KEY`，显式使用 `ChatDeepSeek(model="deepseek-flash", base_url="https://api.deepseek.com")`；缺少 Key 则停止，不以 Mock 冒充。安装版默认模型不是项目默认值；现有 HTTPX Hook 补充关闭 thinking 的参数，不修改 browser-use 源码。

此命令会消耗 API 额度：最多 6 步/8 次请求、连续失败上限 2、整体超时 360 秒，SDK 不自动重试。只导航公开 Python 文档，Tutorial 点击由模型自主选择。其他场景也有步数、请求数、失败和超时上限，实际用量写入私有记录。

**数据边界：** 即使浏览器本地运行，指令、正文、表单字段及必要页面观察仍可能发送到 DeepSeek；上传文件会交给目标站点。不要在示例中使用真实简历、账号凭据或私人文章。`.env.example` 和 Runner 禁用 browser-use 遥测、Cloud sync 与扩展下载。

## 启动本地测试站

在项目根目录的一个终端运行，绑定回环、单 worker，不对公网开放：

```sh
.venv/bin/python -m uvicorn fixture_site.app:app --app-dir tests --host 127.0.0.1 --port 8000 --workers 1
```

- 求职页：<http://127.0.0.1:8000/>；字段换序 `?order=reordered`，上传失败 `?upload=fail`。
- 图文页：<http://127.0.0.1:8000/publish/>；上传失败 `?upload=fail`。
- 状态：`/api/state`、`/api/publish/state`。上传、草稿与最终提交/发布分别计数。

站点重启/reset 会丢失网页状态，SQLite 任务记录保留。上传成功必须由页面和服务端状态确认，不能只看输入框文件名或模型自述。更多协议见 [fixture 说明](tests/fixture_site/README.md)。

## 求职申请 CLI：仅本地合成资料

另一个**真实交互终端**运行（会消耗 DeepSeek API 额度）：

```sh
.venv/bin/python tests/fixture_site/sample_resume.py
.venv/bin/personal-web-agent apply-prepare --handoff \
  --url http://127.0.0.1:8000/ \
  --profile tests/fixture_site/synthetic_profile.json \
  --resume .local/fixture-resume.pdf \
  --instruction '自主识别字段，填写合成资料并上传指定 PDF，核验后停止，等我亲自提交。'
```

仅接受合成 JSON 和带 FICTIONAL 标记的测试 PDF，不接收真实简历。准备阶段不开放提交动作，fixture 服务器另设单次授权保护；不能将这种保护推广到真实招聘网站。

## 图文发布 CLI：仅本地合成内容

先确保图文 fixture 为空，再在真实交互终端运行（会消耗 DeepSeek API 额度）：

```sh
.venv/bin/python tests/fixture_site/sample_article.py
.venv/bin/personal-web-agent publish-prepare --handoff \
  --url http://127.0.0.1:8000/publish/ \
  --title '本地发布测试：合成文章' \
  --body-file .local/publish-fixture/body.txt \
  --images .local/publish-fixture/fictional-1.png .local/publish-fixture/fictional-2.png \
  --instruction '填写合成内容，上传指定图片，核对预览并保存草稿，最终 Publish 前停止。'
```

生成器随仓库提供，生成路径是项目相对路径，不依赖作者私有文件。正文和图片均有合成测试标记；图片使用原生 `upload_file` 逐张上传。图文准备开放必要的 Save Draft，最终 Publish 不向 Agent 开放。

## 人工确认、状态与防重复

使用 `--handoff` 保留同一浏览器和页面，等 CLI 明确显示 **Agent.run 已返回**后：

1. 在原浏览器检查字段、正文、附件及预览。
2. 终端输入 `approve`，等待一次授权成功；这不会代你点击。
3. 本人亲自在原浏览器点击 Submit / Publish。
4. 看到原页成功回执（发布含文章 ID）后，在终端输入 `done`；程序只读核验，最后关闭本轮本地测试浏览器。

输入 `cancel` 不会自动提交，未消费授权会撤销；审批阶段不恢复 Agent，也没有额外模型调用。不要用管道批准或自动点击冒充真人操作。fixture 授权绑定上传/草稿，同一授权最多一次成功；重复点击仍增加尝试数。真实站点没有这层测试服务器保护。

默认数据库 `.local/tasks/tasks.sqlite3`（目录 0700/文件 0600），不保存 API Key、Cookie 或完整个人资料。查询不调用模型：

```sh
.venv/bin/personal-web-agent tasks
.venv/bin/personal-web-agent tasks --id '<Task ID>'
```

状态主路径：`CREATED → RUNNING → REVIEW_REQUIRED → OUTCOME_PENDING → COMPLETED`。人工进入可能提交的阶段前先持久化 OUTCOME_PENDING；核实未最终提交的取消/失败才记 CANCELLED/FAILED，其余不确定结果保留 UNKNOWN。草稿不等于正式发布。

COMPLETED、UNKNOWN、OUTCOME_PENDING 和其他未结束状态阻止重跑。FAILED/CANCELLED 必须有无最终提交的证据，操作者核对后才可在原命令添加 `--confirm-no-side-effects`；保留旧记录，新建任务。不要删除数据库、改账号别名或内容来绕过拦截。上传和草稿仍是独立副作用。

申请接管任务及发布任务纳入持久化；不带 `--handoff` 的本地申请纯准备不创建提交任务。发布不带接管时准备后关闭浏览器并保留 REVIEW_REQUIRED，不能重连旧会话。强制中断可能保留 RUNNING 等状态，查询不会自动恢复或重试。

fixture 必须在两次运行之间、人工保存证据并确认不再需要旧状态后才重置；不删除 SQLite：

```sh
PYTHONPATH=tests .venv/bin/python -m fixture_site.control state
PYTHONPATH=tests .venv/bin/python -m fixture_site.control reset
PYTHONPATH=tests .venv/bin/python -m fixture_site.control state --site publish
PYTHONPATH=tests .venv/bin/python -m fixture_site.control reset --site publish
```

## 真实平台：仅离线适配示例

以下命令不连接真实网站、浏览器或模型；需先运行前文合成资料生成器：

```sh
.venv/bin/personal-web-agent apply-prepare --site greenhouse --check-only \
  --url https://job-boards.greenhouse.io/sertis/jobs/7847750002 \
  --profile tests/fixture_site/greenhouse_synthetic_profile.json \
  --resume .local/fixture-resume.pdf \
  --snapshot tests/snapshots/greenhouse_synthetic.json \
  --instruction '仅离线核对合成资料'

.venv/bin/personal-web-agent publish-prepare --site csdn --check-only \
  --url https://editor.csdn.net/md/ \
  --title '合成文章：离线适配测试' \
  --body-file .local/publish-fixture/body.txt \
  --images .local/publish-fixture/fictional-1.png \
  --instruction '仅离线检查结构' \
  --local-user-id synthetic-csdn-account \
  --snapshot tests/snapshots/csdn_editor.json \
  --cdp-url http://127.0.0.1:9223
```

Greenhouse 示例使用虚构岗位名称、公司和精简自造选项；URL 中保留岗位标识仅用于测试现有 allowlist/去重契约，不证明该岗位当前存在或页面仍匹配。原始实采快照未公开。

退出 0 仅代表离线检查通过，`execution_allowed` 仍为 false。Greenhouse 真实写入强制阻断。CSDN 默认阻断，实验开关 `--allow-csdn-draft` 仅供获得明确草稿/自动保存、图片上传及模型传输授权后的专项测试，**不授权最终发布**；本 README 不提供真实写入快速开始。Edge 独立 Profile、回环 CDP、登录和验证码由用户本人管理，不复制日常浏览器凭据。

CSDN 输入本身可能自动保存，不能把“没有点击保存”当作无写入。真实编辑兼容性、上传流程、草稿结果和真人发布回执尚待验收；不要将 fixture 的 authorize/cancel 用于真实平台。

## 目录、隐私与许可

- `src/personal_web_agent/`：共享 CLI、Runner、人工接管、状态与少量站点适配。
- `tests/fixture_site/`、`tests/snapshots/`：本地服务、原创合成资料与结构测试输入；来源及边界见 [测试输入说明](tests/snapshots/README.md)。
- `scripts/`：公开环境检查工具；历史真实站点诊断脚本仅留本地并由 Git 忽略。
- 内部阶段报告及原始采集不随仓库分发；保留必要开发规划与公开测试说明。

`.env`、`.local/`、`.venv/`、`private/`、数据库/Profile/日志所在私有目录已忽略。原生 History 的脱敏不等于完全匿名，可能含页面账号信息；不要公开上传原始 History、截图、Cookie 或浏览器存储。Git 忽略规则不会自动保护放错目录的文件，发布前应重新检查实际候选和暂存内容。

**许可证待所有者确认，当前未授予 MIT 许可。** MIT 是候选，需要所有者确认自有代码权属、版权持有人及年份后再补 LICENSE 和包元数据。原始第三方采集已排除，公开结构输入用于合成契约测试，不是实站成功证据。第三方依赖保留各自许可，不能被项目候选 MIT 覆盖。
