# TS-DAF 主线提升与协作设计

## 目标

在不重写 Git 历史的前提下，让生产级 TS-DAF Harness 成为仓库主线。旧 OCR 和路线图片实验
Harness 继续作为隔离的测试资产保存；Task1–12 弱文章路由工作流和 image processing v1 按原
提交顺序进入 `main`；协作者获得一份简洁、准确的中文代码导览和未完成验证清单。

## 当前仓库事实

当前提交历史是线性的：

```text
90b96d3  main 上原有的多模态实验 Harness
653f051  Task1–12 弱文章 Router 与生产 TS-DAF 基座
348cf50  image processing v1：MolYOLO -> crop -> Gemini OCSR
```

`653f051` 已经完成目录迁移。旧实验项目现完整保存在 `tests/legacy_harness/`，并能作为独立
项目运行；生产代码、prompt、合同、治理文档和当前测试位于仓库根目录。因此不需要再次搬运大量
文件。

`main` 是当前 image 分支的祖先，主线提升可以使用严格快进。无需 merge commit、squash、
rebase、force-push 或其他历史重写。

## 目标目录边界

- `src/tsdaf/`、`prompts/`、根目录 `pyproject.toml` 和根目录治理文档属于生产 TS-DAF。
- `tests/unit/`、`tests/integration/` 和 `tests/live/` 验证生产工作流。
- `tests/legacy_harness/` 完整保存旧 OCR 和路线图片实验，包括自己的 `pyproject.toml`、
  工具、数据、prompt 和测试。
- 生产模块不得导入 `tests/legacy_harness/`。
- 真实 `.env`、API 凭据、MolYOLO 权重和 vendored MolYOLO runtime 必须留在 Git 之外；
  只能跟踪不含密钥的 `.env.example`。

## 进入 main 的两个生产工作流

### Task1–12 弱文章路由工作流

标准输入仍是规范化 paper package，调用链为：

```text
package 校验 -> canonicalization -> 弱文章 Router
-> 可选 visual triage -> immutable handoff -> release-gated image job
```

主线提升不修改已冻结合同、immutable artifact store、Local Runner、provider adapter、prompt、
CLI 和五篇 fixture。

### Image processing v1

标准输入是已授权的 image job 和初始 image package，调用链为：

```text
source 校验 -> MolYOLO/YOLOv10 -> 检测框统一化
-> 确定性 molecule crop -> 单 crop Gemini OCSR
```

它不读取 PDF 或 MinerU package，不部署独立 OCR 引擎，也暂不实现 molecule binding、
Semantic Binder 和最终 Image Package。

OCSR 固定运行参数保持不变：

- 每个请求只发送一个 crop；
- 默认并发数为 4；
- 默认重试次数为 2；
- 单次请求超时为 120 秒；
- 模型固定为 `gemini-3.7-flash-high`；
- `temperature=0`。

## Task 2 历史工程债务

`scripts/check_import_boundaries.py` 是已接受并冻结的 Task 2 工程债务。它只是仓库导入边界
守卫，不是领域架构示例，也不是通用 Python 解释器。

- 不为假设性的 Python 边缘语法继续扩展 checker。
- 生产代码不得导入 checker，也不得把它变成 runtime 依赖。
- 真实生产代码优先采用清晰、符合既有依赖方向的写法。
- 只有出现无法规避的真实误报或已经证明的漏报时，才重新讨论 checker。
- active Ruff 门禁排除 `tests/legacy_harness/**`；旧 Harness 使用自己的配置单独检查。

协作说明必须明确这项债务，避免合作者继续把项目复杂度投入 checker，或误把它当作可复用框架。

## API 测试现状

离线行为以及 recorded/fake provider 测试已经实现。付费 live 验证尚未完成，主线提升后也不得
将其描述为已完成。

### 弱文章 Router

- 第二个远程端点已经接受 Chat Completions 文本调用和 forced function calling。
- 第一次论文运行以 `ROUTER_NO_SUBMIT` 结束，随后已对回合上限做有界修正。
- 后续运行进入 submit，但远程兼容层即使收到了精确 schema 和 `strict: true`，仍返回旧字段
  或臆造字段，因此以 `ROUTER_SCHEMA_INVALID` 结束。
- 此后的两次无论文 exact-schema probe 均在产生 normalized output 前以
  `PROVIDER_TRANSIENT` 结束。
- 因此远端对完整复杂 schema 的兼容性既未被证明，也未被否定；Task 12 五篇 live gold 仍处于
  暂停和未完成状态。

### Image processing v1

- unit 和 recorded/fake integration 行为已经实现。
- 仓库证据中尚无被接受的真实 MolYOLO 权重/GPU 运行。
- 仓库证据中尚无被接受的远程 Gemini OCSR canary。
- molecule binding 和最终 Image Package 的测试属于下一阶段。

CI 和默认命令必须保持离线。任何付费 live 测试都需要单独授权、仓库外持久 workspace，以及
通过进程环境注入的凭据。代码和 CLI 不得直接读取 `C:\.env`。

## 必须执行的测试矩阵

主线提升必须分别证明两个生产工作流以及旧 Harness 的状态。

1. **Active 仓库**：运行完整离线测试、Ruff、Ruff format、Mypy、import-boundary、文档治理和
   diff 检查。
2. **弱文章 Router**：运行 unit 和 integration 测试，包括五个规范化 paper fixture、
   recorded/fake provider、handoff/release gate、CLI、cache、resume 和 immutable evidence。
   不得宣称 Task 12 live gold 通过。
3. **Image processing v1**：运行 source loader、坐标、crop、MolYOLO adapter、单 crop OCSR、
   runtime cache、CLI 和 recorded/fake 端到端 integration 测试。真实权重/GPU 和远程 OCSR
   继续作为显式 future-live 门禁。
4. **旧 Harness**：使用 `tests/legacy_harness` 的 `PYTHONPATH` 和独立
   `pyproject.toml` 运行其测试，证明迁移后仍能独立工作。
5. **快进后复验**：在真正的 `main` worktree 中重新运行 active 离线测试和全部治理检查，
   通过后才允许 push `main`。

## 协作者代码说明

新增 `docs/guides/CODEBASE_GUIDE_ZH.md`，用中文说明：

- 生产 TS-DAF 与旧 Harness 的目录边界；
- `CLI -> command -> application composition -> workflow runtime -> lane/adapter` 调用关系；
- 弱文章工作流如何通过 handoff/release/job 向图片工作流提供合法输入；
- 已冻结的全局合同与 image lane 私有模型的区别；
- 两个工作流的关键入口文件；
- 不含凭据的 MolYOLO bundle 与 Gemini 配置方法；
- Task 2 checker 债务和停止条件；
- 当前准确的 API/live 验证状态；
- 离线、focused、legacy 和未来 live 测试命令；
- Semantic Binder、molecule binding 和最终 Image Package 的后续接入点。

更新 `README.md`，让生产 TS-DAF 成为仓库的明确身份，并链接中文导览。仅在旧 Slice 1 描述与
已实现图片能力直接冲突时，更新 `docs/architecture/SYSTEM_OVERVIEW.md`、
`docs/PROJECT_CHARTER.md` 和 `docs/ROADMAP.md`。更新 `CHANGELOG.md`，记录两个生产
工作流和旧 Harness 迁移。不得手工编辑生成文件 `docs/STATUS.md`。

## 主线提升步骤

1. 在 `codex/image-processing-v1` 上补齐并验证协作文档。
2. 将协作文档作为独立、可审阅的提交。
3. 确认 feature worktree 和现有 main worktree 都干净，并确认 `main` 没有移动。
4. 运行规定的提升前测试矩阵。
5. 在现有 main worktree 中执行 `git merge --ff-only codex/image-processing-v1`。
6. 在 main worktree 中运行提升后测试。
7. 正常 push `main` 到 `origin`，绝不 force-push。
8. 验证 `origin/main` 与本地 main 完全相同，并确认提交中没有真实 `.env`、权重或
   vendored runtime。

如果 `main` 已移动、任一 worktree 不干净、无法快进，或任何规定测试失败，则停止 push 并
报告准确阻塞原因。

## 验收标准

- `origin/main` 最终指向协作文档提交，并保留 `653f051` 与 `348cf50` 为祖先。
- 仓库根目录明确呈现生产 TS-DAF，而不是旧实验 Harness。
- 旧实验 Harness 在 `tests/legacy_harness/` 中保持完整且可独立运行。
- 两个生产工作流分别具有明确、通过的离线测试证据，future-live 门禁被清楚分开。
- Task 2 债务和未完成的 API/live 状态被显著说明，不得误报为已经关闭。
- 合作者无需回读聊天记录，即可找到代码入口、正确运行测试并定位后续 binding 接入点。
- 不提交或 push 真实 `.env`、API 凭据、MolYOLO 权重或 vendored runtime。
