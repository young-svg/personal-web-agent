# 使用指南

[返回项目首页](../README.md)。所有命令均从项目根目录执行；本指南保留详细 CLI、人工步骤、状态与站点限制。

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

Python 范围为 `>=3.12,<3.13`。运行依赖显式声明 browser-use、python-dotenv、HTTPX、Pillow、pypdf；`fixture` 依赖组声明本地服务/测试直接使用的 Starlette、Uvicorn、python-multipart。全部沿用已验证版本；`uv.lock` 是安装依据，[DEPENDENCY_VERSIONS.txt](../DEPENDENCY_VERSIONS.txt) 是原开发环境快照。上游仍可能将这些测试组件作为传递依赖安装。

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

<a id="smoke"></a>

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

| 场景 | 最大步数 | 最大模型请求 | 连续失败上限 | 每步最多动作 | 整体超时 |
| --- | --- | --- | --- | --- | --- |
| 文档 Smoke | 6 | 8 | 2 | 1 | 360 秒 |
| 本地求职准备 | 10 | 12 | 2 | 3 | 480 秒 |
| 本地图文准备 | 12 | 12 | 2 | 1 | 480 秒 |
| CSDN 实验分支 | 14 | 14 | 1 | 1 | 480 秒 |

统一限制每次最多 4096 输出 Token、SDK 自动重试为 0。`api_calls.json` 记录实际 Token usage；没有据此估算费用。

**数据边界：** 即使浏览器本地运行，指令、正文、表单字段及必要页面观察仍可能发送到 DeepSeek；上传文件会交给目标站点。不要在示例中使用真实简历、账号凭据或私人文章。`.env.example` 和 Runner 禁用 browser-use 遥测、Cloud sync 与扩展下载。

## 启动本地测试站

在项目根目录的一个终端运行，绑定回环、单 worker，不对公网开放：

```sh
.venv/bin/python -m uvicorn fixture_site.app:app --app-dir tests --host 127.0.0.1 --port 8000 --workers 1
```

- 求职页：<http://127.0.0.1:8000/>；字段换序 `?order=reordered`，上传失败 `?upload=fail`。
- 图文页：<http://127.0.0.1:8000/publish/>；上传失败 `?upload=fail`。
- 状态：`/api/state`、`/api/publish/state`。上传、草稿与最终提交/发布分别计数。

站点重启/reset 会丢失网页状态，SQLite 任务记录保留。上传成功必须由页面和服务端状态确认，不能只看输入框文件名或模型自述。更多协议见 [fixture 说明](../tests/fixture_site/README.md)。

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

仅接受 `synthetic: true` 的 JSON；`name`、`email`、`contact`、`introduction` 为非空单行字符串，邮箱必须为 `@example.invalid`。PDF 最大 2 MiB，须能解析且未加密、有页面，并带生成器写入的 FICTIONAL 元数据标记，不接收真实简历。准备阶段不开放提交动作，fixture 服务器另设单次授权保护；不能将这种保护推广到真实招聘网站。

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

本地输入契约：标题非空且最多 200 字；正文为 UTF-8、最多 40,000 字节/10,000 字符，须保留生成器的合成前缀；1～5 张不同的合成 PNG，每张最多 2 MiB、每边最多 4096 像素，拒绝重复图片字节。该契约服务于 fixture 验证，不是实际平台的上传规格。

<a id="handoff"></a>

## 人工确认与同会话接管

使用 `--handoff` 保留同一浏览器和页面，等 CLI 明确显示 **Agent.run 已返回**后：

1. 在原浏览器检查字段、正文、附件及预览。
2. 终端输入 `approve`，等待一次授权成功；这不会代你点击。
3. 本人亲自在原浏览器点击 Submit / Publish。
4. 看到原页成功回执（发布含文章 ID）后，在终端输入 `done`；程序只读核验，最后关闭本轮本地测试浏览器。

输入 `cancel` 不会自动提交，未消费授权会撤销；审批阶段不恢复 Agent，也没有额外模型调用。不要用管道批准或自动点击冒充真人操作。fixture 授权绑定上传/草稿，同一授权最多一次成功；重复点击仍增加尝试数。真实站点没有这层测试服务器保护。

<a id="records"></a>

## 任务状态与防重复

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

<a id="platforms"></a>

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

CSDN 实验分支的 `--handoff` 只供检查草稿：`done`/`cancel` 都不会授权发布或删除草稿，准备成功保留 `REVIEW_REQUIRED`，不会据此记录 `COMPLETED`。结束只断开 CDP 并保留 Edge；异常时保留页面供核对，不能保证断开后自动保存完全隔离。

最近一次真实尝试虽通过初始空白页预检并调用模型，但模型返回不存在的索引 0，输入前被动作校验拒绝；相关记录为 UNKNOWN。后续标题激活修复只有离线证据，尚未真实重验，不要绕过去重直接重跑。

## 常用参数与退出结果

| 参数/命令 | 适用范围与含义 |
| --- | --- |
| `--site` | 求职默认 fixture，可选 greenhouse；发布默认 fixture，可选 csdn |
| `--instruction` | 必须提供自然语言任务；资料缺失时不允许模型编造 |
| `--handoff` | 准备通过后保留页面交给真人，要求交互终端 |
| `--snapshot`、`--check-only` | 指定结构输入并执行 Greenhouse/CSDN 离线检查；不代表允许真实运行 |
| `--local-user-id`、`--cdp-url` | CSDN 的稳定本地账号别名与已有 Edge 回环端点；不填凭据 |
| `--confirm-no-side-effects` | 仅在核对已有 FAILED/CANCELLED 记录后，明确确认可安全重跑 |
| `tasks [--id ...]` | 查询已有任务，无模型调用 |

正常准备/核验通过返回 0；执行或准备失败通常返回 1；输入/配置问题或安全阻断返回 2。上传失败场景即使正确报告了预期错误，准备命令仍退出 1，不能把它当成业务准备成功。

## 日志、数据与公开范围

每次执行在 `.local/smoke/`、`.local/apply-prepare/` 或 `.local/publish-prepare/` 下保存本轮目录：

| 文件 | 用途 |
| --- | --- |
| `history.json` | browser-use 原生 Agent History |
| `api_calls.json` | 模型请求数、HTTP 状态、服务端 Token usage |
| `steps.json` | 动作后的页面观察；fixture 模式包含服务器状态 |
| `summary.json` | 准备/接管结果、错误、实际用量及浏览器清理情况 |
| `preparation_summary.json`、`handoff.json` | 使用人工接管时的准备快照与核验结果 |

运行器脱敏密钥、控制令牌和项目路径；部分站点还过滤查询参数和账号 URL。这不等于完整匿名化：History 和任务提示可能含传入正文、字段或页面账号信息。不要公开上传原始记录、截图、Cookie 或浏览器存储。

`.env`、`.local/`、`.venv/`、`private/` 及日志受 Git 忽略；内部阶段报告、真实页面采集和历史诊断脚本不随源码分发。忽略规则不会保护放错目录或强制暂存的文件，发布前仍需检查实际清单。

## 实现入口

| 模块 | 职责 |
| --- | --- |
| [cli.py](../src/personal_web_agent/cli.py) | argparse 命令及场景分支 |
| [task_inputs.py](../src/personal_web_agent/task_inputs.py) / [publishing.py](../src/personal_web_agent/publishing.py) | 本地业务输入、任务描述与结果核验 |
| [runner.py](../src/personal_web_agent/runner.py) | 共享模型/浏览器生命周期、工具限制、预算与历史记录 |
| [approval.py](../src/personal_web_agent/approval.py) | Agent 停止后的同页人工接管与回执核验 |
| [records.py](../src/personal_web_agent/records.py) | SQLite 状态流转、事务与任务去重 |
| [greenhouse.py](../src/personal_web_agent/greenhouse.py) / [csdn.py](../src/personal_web_agent/csdn.py) | 有限站点适配与执行边界；不是通用平台支持 |

## 许可证

本仓库没有提供 LICENSE；第三方依赖仍适用各自许可证。
