# TS-DAF 协作者代码导览

## 先看结论

仓库根目录现在是生产 TS-DAF Harness，不再是早期 OCR 消融实验包。当前已有两个相互衔接的
生产工作流：

1. Task1–12 弱文章路由：从规范化 paper package 生成可审计的路由结果、handoff 和授权 image
   job。
2. Image processing v1：从授权 image job 和原始 image package 运行 MolYOLO、crop 和单 crop
   Gemini OCSR。

旧 OCR 和路线图片实验完整保存在 [`tests/legacy_harness/`](../../tests/legacy_harness/README.md)，
只作为历史测试项目使用。它不是生产依赖。

## 仓库目录与 legacy 边界

| 路径 | 责任 |
|---|---|
| `src/tsdaf/contracts/` | 已冻结的持久合同、schema 和 artifact identity |
| `src/tsdaf/lanes/document/` | package 读取、canonicalization、弱文章 Router 和 handoff 构造 |
| `src/tsdaf/lanes/image/` | image v1 私有模型、source 校验、坐标、crop 和 OCSR 解析 |
| `src/tsdaf/workflow/` | Local Runner、不可变执行证据和两个工作流 runtime |
| `src/tsdaf/adapters/` | Gemini 与外部 MolYOLO 的具体适配器 |
| `src/tsdaf/apps/` | CLI composition root；组装存储、runtime、adapter 和环境配置 |
| `prompts/` | 版本化 article Router 与 molecule OCSR prompt |
| `tests/unit/`、`tests/integration/` | 当前生产代码的离线测试 |
| `tests/live/` | 默认关闭、必须显式授权的付费测试门 |
| `tests/legacy_harness/` | 旧实验 Harness，拥有自己的配置、工具、数据和测试 |

生产代码不得导入 `tests/legacy_harness/`。根目录 Ruff/Mypy 门禁也不把 legacy 项目当成生产
Python；legacy 使用自己的 `pyproject.toml` 独立运行。

## 两个生产工作流

### Task1–12 弱文章路由

```text
normalized paper package
  -> package validation / canonicalization
  -> MainArticleRouter（文本优先）
  -> flagged-only visual triage
  -> VisualRoutingHandoff + CoarseTextHandoff
  -> release receipt
  -> ImageProcessingJob
```

关键入口：

- CLI：[`src/tsdaf/apps/commands/route.py`](../../src/tsdaf/apps/commands/route.py)
- application composition：[`src/tsdaf/apps/composition.py`](../../src/tsdaf/apps/composition.py)
- runtime：[`src/tsdaf/workflow/article_router_runtime.py`](../../src/tsdaf/workflow/article_router_runtime.py)
- Router：[`src/tsdaf/lanes/document/routing/main_router.py`](../../src/tsdaf/lanes/document/routing/main_router.py)
- handoff/job：[`src/tsdaf/lanes/document/routing/handoff_builder.py`](../../src/tsdaf/lanes/document/routing/handoff_builder.py)
  与 [`src/tsdaf/lanes/image/job_factory.py`](../../src/tsdaf/lanes/image/job_factory.py)

Main Router 不读取图片字节。只有需要视觉确认的资产进入隔离 visual triage；image job 必须由
匹配当前 handoff hash 的 release receipt 授权。

### Image processing v1

```text
ImageProcessingJob + initial image package
  -> source_loader 校验路径、hash、字节、媒体类型和尺寸
  -> MolYOLO / YOLOv10 检测
  -> 坐标统一、排序、M-ID 和确定性 crop
  -> 每个 crop 一次 Gemini OCSR
  -> MoleculeCropSet + GeminiOCSRResult
```

关键入口：

- CLI：[`src/tsdaf/apps/commands/image.py`](../../src/tsdaf/apps/commands/image.py)
- composition：[`src/tsdaf/apps/image_composition.py`](../../src/tsdaf/apps/image_composition.py)
- runtime：[`src/tsdaf/workflow/image_processing_runtime.py`](../../src/tsdaf/workflow/image_processing_runtime.py)
- image 私有模型：[`src/tsdaf/lanes/image/models.py`](../../src/tsdaf/lanes/image/models.py)
- MolYOLO adapter：[`src/tsdaf/adapters/moldet/molyolo_v10.py`](../../src/tsdaf/adapters/moldet/molyolo_v10.py)
- Gemini transport：[`src/tsdaf/adapters/gemini/interactions.py`](../../src/tsdaf/adapters/gemini/interactions.py)

输入必须是初始 image package，不是 PDF 或 MinerU package。系统不部署独立 OCR 引擎。当前输出
止于 crop 与 OCSR SMILES，尚未做 Semantic Binder、molecule binding 或最终 Image Package。

## 代码分层和关键入口

两条工作流遵循同一调用方向：

```text
CLI -> commands -> application composition -> workflow runtime -> lane / adapter
```

- `commands` 只解析参数并输出规范 JSON。
- `application composition` 是唯一组装点，可以读取进程环境并创建具体 adapter。
- `workflow runtime` 负责 stage、cache、attempt、event 和 immutable artifact。
- `lane` 负责可确定测试的领域行为，不读取 API key，也不实例化 provider client。
- `adapter` 封装外部 SDK/runtime，不能反向决定领域合同。

新增功能前先运行 `python scripts/check_import_boundaries.py src/tsdaf`，不要通过跨层 import 绕过
现有边界。

## 合同、私有模型和数据衔接

`src/tsdaf/contracts/` 是 Task1–12 已冻结的全局 wire contract。不要为了方便 image 内部阶段而
扩展它。`MoleculeCropSet`、`GeminiOCSRResult` 等仍是
[`src/tsdaf/lanes/image/models.py`](../../src/tsdaf/lanes/image/models.py) 中的 lane-private 模型。

文本到图片的合法衔接点是：

```text
VisualRoutingHandoff + release receipt -> ImageProcessingJob
```

图片 runtime 的稳定下游入口是 `ImageProcessingRun.crop_set` 与
`ImageProcessingRun.ocsr_results`。未来 binder 应作为其后的独立 stage，组合完整 Scheme、弱文本
工作流给出的资产/路线信息、检测坐标、M-ID 和 OCSR SMILES，而不是把 binding 逻辑塞进
MolYOLO adapter 或 `ocsr.py`。

## 外部 MolYOLO 与 Gemini 配置

MolYOLO 作为仓库外 bundle 提供，最小目录为：

```text
<bundle>/weights/MolYOLO.pt
<bundle>/.vendor/molyolo_runtime/ultralytics/__init__.py
```

权重和 vendored runtime 不进入 Git。adapter 会记录权重、runtime hash 和版本 provenance，并在
首次检测时延迟加载模型。

Gemini live 模式只接受进程环境中的 `OPENAI_API_KEY` 和安全的 `OPENAI_BASE_URL`。CLI 和代码
不会直接读取 `C:\.env`，也不会记录 key。OCSR 参数固定为：每请求一个 crop、并发 4、重试 2、
超时 120 秒、`gemini-3.7-flash-high`、`temperature=0`。

## Task 2 历史债务

原 Task1–12 工作中的 Task 2 建立了 `scripts/check_import_boundaries.py`。它是已接受、已冻结的
工程工具债务，不是通用解释器或领域架构范例：

- 不为假设性 Python 边缘语法继续扩展它；
- 不从生产代码导入它；
- 真实代码触发误报时，优先采用更清晰的现有架构表达；
- 只有不可规避的真实误报或已证明漏报，才重新讨论 checker；
- 如果维护 checker 的复杂度开始超过领域功能，立即停止扩展。

完整历史裁决见
[`WI-0001` 的 Task 2 债务章节](../work/WI-0001-weak-article-router-v1.md#02-task-2-工具债务冻结与后续复杂度预算2026-08-26)。

## 当前 API 测试状态

不要把“离线测试全绿”写成“远程 API 已验证”。当前事实是：

- 弱文章 Router 的第二远程端点能完成 Chat Completions 文本和 forced function calling。
- 首次论文运行曾得到 `ROUTER_NO_SUBMIT`，有界回合修正已经完成。
- 随后两轮收到旧式/臆造字段并得到 `ROUTER_SCHEMA_INVALID`，即使 exact schema 与
  `strict: true` 已进入请求。
- 后续两次无论文 exact-schema probe 都以 `PROVIDER_TRANSIENT` 结束，没有 normalized output。
- 因此 Task 12 五篇 live gold 仍暂停且未完成。
- Image v1 已完成 unit/recorded/fake 测试，但尚无被接受的真实 MolYOLO 权重/GPU 证据，也没有
  被接受的远程 Gemini OCSR canary。

所有 live 测试都必须另行授权，在仓库外持久 workspace 中运行。CI 永不进行付费调用。

## 测试命令

Active 离线全量：

```powershell
python -m pytest -q
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src/tsdaf
python scripts/check_import_boundaries.py src/tsdaf
python -m tsdaf docs check
```

弱文章工作流 focused：

```powershell
python -m pytest -q tests/unit/routing tests/unit/document tests/integration/routing tests/integration/test_cli_workflow.py tests/integration/test_five_paper_canonicalization.py tests/integration/test_paper_fixtures.py
```

Image v1 focused：

```powershell
python -m pytest -q tests/unit/image tests/unit/adapters/test_molyolo_v10.py tests/unit/adapters/test_gemini_interactions.py tests/unit/workflow/test_image_processing_runtime.py tests/integration/image tests/integration/test_cli.py
```

旧 Harness：

```powershell
$env:PYTHONPATH = (Resolve-Path 'tests/legacy_harness').Path
python -m pytest -q -c tests/legacy_harness/pyproject.toml tests/legacy_harness/tests
Remove-Item Env:PYTHONPATH
```

`tests/live/` 默认跳过。不要为了看到绿色结果而传入 live flag；只有单独授权后才按
[`LIVE_GOLD.md`](LIVE_GOLD.md) 执行。

## 下一步：绑定与 Image Package

下一阶段建议新增独立 Semantic Binder/runtime，而不是改造现有 Router、MolYOLO 或 OCSR：

1. 接收完整 Scheme 和弱文本工作流给出的路线/资产信息。
2. 接收 `MoleculeCropSet`、坐标、M-ID 与 `GeminiOCSRResult`。
3. 让 Gemini 直接读取 Scheme 上的 `7`、`12a`、`S4` 和箭头条件。
4. 生成可追溯的 molecule binding 候选与审核信息。
5. binding schema 稳定后，再设计最终 Image Package 合同。

在 binding 和最终 Image Package 设计获批前，不把临时字段加入冻结的全局合同。
