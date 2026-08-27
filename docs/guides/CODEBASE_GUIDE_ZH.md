# TS-DAF 协作者代码库指南

## 入口与工作流

TS-DAF 生产目录包含相互衔接、但严格隔离的文章路由与图像处理两个工作流。先读根目录 [README](../../README.md) 与 [AGENTS](../../AGENTS.md)，再按任务进入[文章路由指南](ARTICLE_ROUTING_WORKFLOW_ZH.md)或[图像处理指南](IMAGE_PROCESSING_WORKFLOW_ZH.md)。

文章路由的输入是 normalized paper package，Main Router 文本优先且没有 image bytes；它只 materialize 两个 `PROPOSED` handoff。release receipt 与 `ImageProcessingJob` 是 route CLI 之外的严格 hash-bound 授权边界。图像处理仅接收已授权 job 与原始 image package，输出为 lane-private `MoleculeCropSet`/`GeminiOCSRResult`，并不负责 binding 或最终 Image Package。

## 目录责任

| 路径 | 责任 |
|---|---|
| `src/tsdaf/contracts/` | 冻结的持久合同、provider-visible schema 与 artifact identity |
| `src/tsdaf/lanes/document/` | package、canonicalization、文本优先路由与 handoff 构建 |
| `src/tsdaf/lanes/image/` | image 私有模型、源验证、坐标、crop 与 OCSR 结果 |
| `src/tsdaf/workflow/` | runtime、stage/cache/attempt/event 与不可变执行证据 |
| `src/tsdaf/adapters/` | Gemini 与外部 MolYOLO runtime 的适配 |
| `src/tsdaf/apps/` | CLI composition root、环境配置与具体 adapter 组装 |
| `prompts/` | 版本化 article Router 与 molecule OCSR prompt |
| `tests/unit/`、`tests/integration/` | 生产代码的离线验证 |
| `tests/live/` | 默认关闭、明确授权才执行的 live 验证 |
| `tests/legacy_harness/` | 独立配置的历史实验 Harness，非生产依赖 |

调用方向固定为：`CLI -> commands -> application composition -> workflow runtime -> lane / adapter`。lane 不读取 API key 或实例化 provider client；adapter 不反向决定领域合同。新增功能不能用跨层 import 绕过这一边界。

文章路由的主要入口是 [`route.py`](../../src/tsdaf/apps/commands/route.py)、[`composition.py`](../../src/tsdaf/apps/composition.py)、[`article_router_runtime.py`](../../src/tsdaf/workflow/article_router_runtime.py)、[`main_router.py`](../../src/tsdaf/lanes/document/routing/main_router.py) 和 [`handoff_builder.py`](../../src/tsdaf/lanes/document/routing/handoff_builder.py)。图像工作流的主要入口是 [`image.py`](../../src/tsdaf/apps/commands/image.py)、[`image_composition.py`](../../src/tsdaf/apps/image_composition.py)、[`image_processing_runtime.py`](../../src/tsdaf/workflow/image_processing_runtime.py)、[`models.py`](../../src/tsdaf/lanes/image/models.py)、[`molyolo_v10.py`](../../src/tsdaf/adapters/moldet/molyolo_v10.py) 和 [`interactions.py`](../../src/tsdaf/adapters/gemini/interactions.py)。

## 合同、证据与恢复

合同只服务真实消费者；一条规则只有一个事实源，Host validation 与 provider-visible schema 必须保持一致，禁止静默改写无效模型结果。原始 PDF/资产是不可变证据，normalized artifact 是有独立 hash 的派生物。内容寻址对象、seal、events 和 manifest 是权威事实；SQLite 是可删除、可重建的投影。

`VisualRoutingHandoff`、`CoarseTextHandoff`、release receipt 和 image job 以 content hash 严格绑定。没有有效 receipt，不能创建 image job。image 内部 crop/SMILES/detector 数据继续放在 lane-private model，不应膨胀冻结的全局 document contract。

## 配置和历史边界

默认离线，CI 不产生 live 调用。live 必须单独获授权，凭据只从进程环境进入，CLI 不直接读取 `.env`。文章路由允许绝对 HTTPS `/v1`，以及仅 `127.0.0.1`、`localhost`、`::1` 的 HTTP `/v1`；图像 OCSR 一律要求 HTTPS `/v1`。详情见 [CLI](CLI.md)。

外部 MolYOLO weights 和 vendored runtime 保留在 Git 外；不要把它们、secret 或外部 live workspace 放入仓库。`scripts/check_import_boundaries.py` 是冻结的 Task 2 工具债务，不是生产依赖、架构模型或 Python interpreter 项目。若两轮 review/fix 暴露同一根因，应停止打补丁并请求 scope/design 决策。

当前已完成五篇文章的本地显式 live 验证；它不验证 image OCSR。精确证据和操作政策见 [LIVE_GOLD](LIVE_GOLD.md)。
