# TS-DAF 本地 Web 工作台

Web 工作台是现有 CLI 的展示与操作层，不是另一套解析实现。它通过本地 API 调用公开的
`python -m tsdaf` 命令，因此包校验、路由、恢复和审计仍遵守 CLI 的原有合同与退出码。

## 能做什么

- 首页使用单一上传入口，自动识别原始 PDF 或标准 paper package ZIP；
- PDF 在独立 intake 层调用本地 MinerU，再生成标准 paper package；
- ZIP 直接导入并校验其中的 `manifest.json`；
- 选择文件后切换到独立任务页，不把上传、进度和结果纵向堆在首页；
- 选择本地标准 paper package 目录或仓库自带 fixture；
- 校验 manifest、资产清单、哈希和规范化内容；
- 将校验通过的标准 paper package 直接下载为 ZIP；
- 默认用接口模拟模式验证文章路由请求、响应与状态展示，不读取凭据、不调用模型；
- 显式切换 CLI 模式后，运行离线文章路由并明确展示 `LIVE_REQUIRED`；
- 在服务端已通过环境变量配置凭据时，显式选择 Live 路由；
- 查看和恢复 `.tsdaf-workspace` 中的 execution。
- 五篇已跑论文读取真实历史结果，不重复调用模型；
- 新论文可显式进入 `research/` 的导入、前处理、全链和统计四阶段流水线；
- 展示并下载录制 `machine-paper-result.json`，或 research `result.json` 与完整结果 ZIP。

MinerU intake 与 TS-DAF 路由保持为两个边界：工作台先调用官方 `mineru-kit`，读取它生成的
`middle_json.json` 与 `images/`，显式转换为 `tsdaf.paper-package.v1` 并用真实 loader 校验；
校验通过后才允许进入现有路由。这样提供一站式体验，同时不让生产 Router 直接读取 PDF
或原始 MinerU ZIP。

## 本地 MinerU

建议给 MinerU 使用独立虚拟环境，避免它的模型依赖影响 TS-DAF。MinerU 4.x 支持
Python 3.11–3.14：

```powershell
uv venv .mineru --python 3.13
uv pip install --python .mineru\Scripts\python.exe "mineru>=4,<5"
$env:TSDAF_MINERU_COMMAND=(Resolve-Path .mineru\Scripts\mineru-kit.exe)
$env:TSDAF_MINERU_TIER='standard'
```

WSL/Linux：

```bash
uv venv .mineru --python 3.13
uv pip install --python .mineru/bin/python 'mineru>=4,<5'
export TSDAF_MINERU_COMMAND="$PWD/.mineru/bin/mineru-kit"
export TSDAF_MINERU_TIER=standard
```

在 Windows 上，如果仓库根目录存在 WSL 创建的 `.mineru/bin/mineru-kit`，工作台会通过
`wsl.exe` 自动发现并运行它；因此重新打开 PowerShell 或重启 Web 服务后不需要重复设置
`TSDAF_MINERU_COMMAND`。页面健康状态会显示“WSL 本地 MinerU 已就绪”。

工作台执行的等价命令为：

```text
mineru-kit parse <paper.pdf> --output <result.zip> --format zip --tier standard
```

官方 MinerU ZIP 包含 `middle_json.json`、`structured_content.json`、`markdown.md` 与
`images/`。它不是 TS-DAF 标准包；工作台会转换出以下独立目录：

```text
manifest.json
source/paper.pdf
normalized/document.json
normalized/blocks.jsonl
images/*
```

`TSDAF_MINERU_TIER` 允许 `flash`、`basic`、`standard`、`advanced`，默认 `standard`。
首次解析可能需要准备模型，CPU 也能运行，但速度取决于页数和硬件。

网页显示的是阶段进度，不是 MinerU 内部逐页百分比：上传完成进入解析阶段时显示 20%，
MinerU 命令返回后进入标准化阶段显示 78%，标准包校验完成后显示 100%。因此长时间停留在
20% 通常表示 MinerU 仍在工作，不等于网络请求或页面已经卡死。

## 可选远程兼容接口

找不到本地 `mineru-kit` 时，仍可配置一个返回 TS-DAF 标准包 ZIP 的兼容服务：

```powershell
$env:TSDAF_UPSTREAM_PARSE_URL='https://parser.example.edu/v1/paper-packages'
$env:TSDAF_UPSTREAM_PARSE_TOKEN='可选的 bearer token'
```

兼容接口合同：

- 工作台向配置 URL 发送 `POST`；
- 请求体是原始 PDF 字节，`Content-Type: application/pdf`；
- `X-Filename` 是 URL 编码后的原始文件名；
- 设置 token 时发送 `Authorization: Bearer ...`；
- 成功响应必须是一个标准 paper package ZIP，且只包含一个 `manifest.json`；
- 上游可在请求内部执行异步流程，但 HTTP 响应必须在 15 分钟内返回；网页侧的上传和等待
  以本地后台任务运行，不会阻塞浏览器；
- URL 必须使用 HTTPS；只有 `localhost`、`127.0.0.1` 或 `::1` 可使用 HTTP。

真实 token 只进入启动服务的进程环境，不写入网页、仓库、事件或日志。本地 MinerU 可用时
优先使用本地 intake；两者都不可用时，标准解析包 ZIP、本地目录与内置样例仍可正常使用。

## 安装与启动

在仓库根目录使用 Python 3.11、3.12 或 3.13：

```powershell
uv venv .venv --python 3.13
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[web,research,dev]"
tsdaf-web
```

WSL/Linux：

```bash
uv venv .venv --python 3.13
source .venv/bin/activate
uv pip install -e ".[web,research,dev]"
tsdaf-web
```

浏览器打开 <http://127.0.0.1:8200/>。服务默认只监听回环地址，不向局域网或公网暴露；
端口 8200 也避免和旧审核原型使用的 8100 冲突。

## Live 边界

Web 默认使用 `TSDAF_WEB_ROUTE_MODE=contract-mock`，只验证标准包输入和接口回执，候选模型
预留为 `Gemini-4-argon` 与 `Gemini-3.8-flash`；该模式不读取凭据、不创建真实 execution，
也不产出或伪造路线结论。

只有显式设置 `TSDAF_WEB_ROUTE_MODE=cli` 后，网页才显示原有 Live 控件。此时默认仍完全离线。
只有用户在页面打开“显式 Live”，且启动服务的进程环境已经设置
`OPENAI_API_KEY` 与安全的 `OPENAI_BASE_URL` 时，CLI 才允许 provider 调用。网页没有填写、
保存或回显密钥的入口。

`research/` 中的 2026-10 语料流程是独立研究流程，不属于 `src/tsdaf` 生产路由。工作台只在
apps 层把一个标准解析包包装成该流程要求的 `INDEX.csv + article.json + main/` 单篇集合，
并在 `.tsdaf-web/research-runtime/` 隔离副本里运行，避免把运行数据写进仓库的
`tests/fixtures/` 或 `research/`。

化学入口先检查 `results/five-papers-20261010` 的真实历史结果；命中时直接展示，绝不重复
调用模型。未命中时默认只返回 `LIVE_REQUIRED` 预检结果。只有页面打开“显式 Live”，且启动
服务的进程环境已经配置 `OPENAI_BASE_URL`、`OPENAI_API_KEY`，才启动 research 的四阶段任务。
任务为异步执行并支持上游本身的断点续跑，页面轮询状态；完成后展示 `result.json` 的实验
记录、论文级记录和结构绑定摘要，并提供 JSON 与整个结果目录 ZIP。模型可用
`TSDAF_CHEMISTRY_MODEL` 指定，默认 `Gemini-3.8-flash`。

如果录制结果保存在仓库外，可设置 `TSDAF_CHEMISTRY_RESULTS_DIR`，目录布局为
`<根目录>/<paper_id>/machine-paper-result.json`。网页不提供填写、保存或回显密钥的入口。

## 前端源码

- API 与静态文件挂载：`src/tsdaf/apps/web/app.py`
- 化学结果录制数据适配：`src/tsdaf/apps/web/chemistry.py`
- research 化学任务适配：`src/tsdaf/apps/web/research_chemistry.py`
- 启动入口：`src/tsdaf/apps/web/server.py`
- 页面：`src/tsdaf/apps/web/static/index.html`
- 样式：`src/tsdaf/apps/web/static/styles.css`
- 交互：`src/tsdaf/apps/web/static/app.js`
