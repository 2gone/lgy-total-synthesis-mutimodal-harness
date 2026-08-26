---
id: WI-0001
title: Weak Article Router v1
status: active
focus: true
owner: codex
depends_on: []
acceptance:
  - five-paper gold passes
  - all artifacts sealed
  - no paid calls in CI
---

目标仓库：`total-synthesis-multimodal-harness`

# TS-DAF 第一阶段实施计划

## 0. 执行、压缩恢复与协作协议

本文档是 Slice 1 的唯一实施计划和长期进度入口。聊天记录、临时 todo 和子智能体口头总结都不能替代本文档。

### 每次上下文压缩或会话恢复后的强制顺序

1. 从头重新读取本文档。
2. 读取本计划专属的 `.superpowers/sdd/WI-0001-weak-article-router-v1/progress.md`。
3. 执行 `git status --short`、`git branch --show-current` 和 `git log -5 --oneline`，以磁盘与 Git 事实恢复进度。
4. 不重新分派 ledger 中已经标记完成的任务。
5. 从第一个没有完成记录的 Task 或未结束的 fix round 恢复。
6. 每次任务实现、复核、修复和验证后，立即更新 ledger；涉及范围或架构变化时同步更新本文档。

### 多智能体协作约束

- 主智能体只负责架构裁决、任务切分、跨任务集成、ledger 和最终验证。
- 每个实现子智能体只接收一个 Task 的独立 brief，不读取整段聊天历史，也不得重新解释项目目标。
- brief 必须写明：允许修改的文件、禁止修改的文件、依赖接口、精确常量、TDD 命令、验收标准、报告文件路径。
- 实现与复核由不同子智能体承担；复核者只读，不修改工作树。
- 不并行派发会修改相同文件或共享接口的实现任务。真正独立的只读调查可以并行。
- 子智能体必须把详细过程写入报告文件，返回消息保持简短，避免主上下文被报告撑满。
- 任何架构歧义、计划冲突或越界需求必须返回 `NEEDS_CONTEXT` 或 `BLOCKED`，禁止自行扩展范围。
- 未得到用户明确授权前，任何智能体都不得创建 Git commit、push 或 PR。任务复核暂以限定路径 diff、测试输出和文件 SHA-256 为证据；获得授权后再切换为 commit-range review。

### 全局硬约束（所有 Task 均适用）

- 工作分支：`codex/tsdaf-router-v1`；不得直接修改 `main`。
- Python：`>=3.11,<3.14`；单一 distribution：`tsdaf`。
- 生产代码不得导入 `tests/legacy_harness`。
- `contracts` 不得导入 Gemini、Prefect、SQLite、CLI 或环境配置实现。
- 主 Router 必须 text-first，不能接收图片；只有 `NEEDS_VISUAL_VERIFICATION` 进入独立 Visual Triage。
- Slice 1 只接收规范化 TS-DAF package，不直接解析 PDF 或原始 MinerU ZIP。
- Router 不裁剪图片、不做 panel 定位、不提取 SMILES、不创建真实图像处理任务。
- 所有正式文本必须由 host 使用稳定 block ID 从 canonical 原文物化，不能使用模型改写文本。
- 所有图片必须且只能得到一个 `VisualDecision`；下游发送资格由 host 派生。
- artifact 不可变、内容寻址、原子写入；SQLite 仅为可重建派生索引。
- 默认 CLI 不允许付费调用；只有显式 `--live` 才能访问 Gemini；CI 永不调用付费 API。
- 每个生产行为必须遵守 RED → 验证预期失败 → GREEN → 验证通过 → REFACTOR。
- 不生成图形化流程图；需要结构表达时只使用 Markdown 列表、表格或纯文本代码块。

### 当前 checkpoint

- [x] 规划讨论完成。
- [x] 隔离 worktree 已建立。
- [x] 完整实施计划已持久化到本文档。
- [x] 建立 plan 专属 SDD ledger。
- [x] 基线离线测试：`python -m pytest`，24 passed，0 warnings。
- [x] 完成计划冲突预检并写入下述裁决。
- [x] Task 0 完成：旧 harness 已完整迁入 `tests/legacy_harness/`，独立复核通过；24 个 legacy tests 与 5 个 active tests 均通过。
- [x] Task 1 完成：项目记忆、七个 ADR、确定性 STATUS 与 DOC001–DOC009 校验器已实现；修复轮次后 active tests 36/36。
- [x] Task 2 完成：48 行首轮 RED 曾按旧规范得到 36/12；H6Q 经独立语义裁决改为 `(7,)` sound conservative control，修正后 handler RED 为 11/5、合并 RED 为 35/13。最终 round5 48、active 281、legacy 24，控制器完整复跑全绿；独立 actual-code/I1–I6 审查以 27 个编译有效新探针加 H6Q/反例得到 `PASS`（0 Critical / 0 Important / 0 Minor）。两个核定生成 residue 已安全清理且最终不存在；Task 3 依赖门禁已释放。
- [x] Task 3 施工准备：B1–B7 已裁决，42 exports / 26 root models / 9 taxonomy enums 的详细 construction brief 经三项修订后独立复审 `PASS`（0 Critical / 0 Important / 0 Minor）；Task 2 门禁已满足，轻量领域比例检查完成，Task 3 已释放进入收窄的 RED-first 实现。
- [x] Task 3 集成占位迁移：Task 2 的 scaffold 原先把全部边界包暂时断言为空导出；`tsdaf.contracts` 进入正式合同阶段后，控制器仅将该测试迁移为“contracts 显式导出、其余未施工边界包仍为空”。精确 42 项清单继续只由 Task 3 schema 测试维护，未修改生产设计或 Task 2 checker。
- [x] Task 3 完成：六组合同测试先得到完整 RED，随后以直接 Pydantic 领域代码实现 source/package/routing/handoff/receipt/execution/schema；focused `180 passed`、active `461 passed`，Ruff、mypy、boundary、STATUS、docs 与 diff gate 全绿。控制器复核 42 exports / 26 roots / 9 enums、41,519-byte golden 及全部实现/测试 hash，并清理三个可再生测试目录；不同 reviewer 对领域完整性、schema 稳定、receipt/handoff 边界和越权字段给出双 `PASS`、0 Critical / 0 Important / 0 Minor。Task 4 依赖门禁已释放。
- [x] Task 4 施工准备：轻量只读 inventory 已闭合五份原始 PDF、615 个有序 block 和 37 个被引用资产；确定只机械复制最终 fixture、按最终字节计算 hash，并将 CRLF normalized text 固定为 LF。六项复杂度预算和 fixture-only brief 已写入磁盘；不新增生产代码、loader、schema 或通用 fixture framework。
- [x] Task 4 完成：五篇自包含 package 共 57 个文件、17,149,756 bytes，615 个 block 与 37 个资产引用完整闭合；focused `4 passed`、active `465 passed`，静态与文档门禁全绿。不同 reviewer 独立重算全部源/目标 hash 并给出双 `PASS`、0 Critical / 0 Important / 0 Minor；没有新增生产抽象或技术债务。Task 5 依赖门禁已释放。
- [x] Task 5 完成：可验证 package loader、稳定 canonicalization、可逆 clean view、caption/visual group、decorative suppression 和确定性 block search 已交付；focused `22 passed`、active `487 passed`，独立 reviewer `PASS`，0 Critical / 0 Important / 0 Minor。Task 3 contracts/schema 与 Task 4 fixture 保持冻结。
- [x] Task 6 完成：一个具体本地 runtime 已交付 content-addressed immutable object、原子写/seal、content-key cache、本地锁、execution/attempt/event、resume、terminal manifest 和可删除重建的五表 SQLite 派生索引。实现者 focused `7 passed`、原始 active `494 passed`；控制器只复核两条关键修复路径，不重复全仓门禁。不同 reviewer 的 terminal-immutability closure re-review 最终 `PASS`，0 Critical / 0 Important；异常原文不落盘且终态 execution 不再发生 write-before-reject。没有新增 contract、port、FTS、provider、CLI 或通用框架；Task 7/8 纵向链路已释放。
- [x] Task 7 完成：最小 Gemini Interactions 2.x 边界与 recorded offline replay 已交付，固定 `gemini-3.7-flash`、low thinking、非流式、`store=False` 和显式 API-key 注入；SDK 对象与异常原文不越过 adapter。实现者 focused `6 passed`、原始 active `500 passed`；唯一 response-envelope Important 经一次集中 TDD 修复后，窄范围 closure review `PASS`、0 Critical / 0 Important。没有新增 contract/schema、provider port/registry、prompt、Router、CLI 或 live 调用；Task 8 离线纵向闭环已释放。
- [x] Task 8 完成：五篇 fixture 已实际离线运行至 sealed `ArticleRoutingResult`，控制器演示得到 615 blocks、37 个唯一 visual decision、9 个原始完整资产、0 escalation，且相同 Hyperfirin 输入缓存复用时 provider 调用为 0。实现者 focused `6 passed`、原始 active `506 passed`，全部规定静态门禁通过；不同 reviewer 只运行一次五篇集成测试并给出 `PASS`、0 Critical / 0 Important / 0 Minor。没有新增 contract/schema/port/framework、图片读取、CLI、live 调用或 checker 工作；Task 9/10 已释放。
- [x] Task 9 完成：独立 Visual Triage 只处理 `NEEDS_VISUAL_VERIFICATION`；五篇已完成结果保持零 package I/O/零 provider，flagged 路径只发送原始完整资产、按 `4+1` 分批、medium resolution 和最小原文上下文，`UNCERTAIN` 形成 `REVIEW_REQUIRED`。实现者 focused `4 passed`、原始 active `510 passed`；不同 reviewer 仅运行一次 flagged/source-integrity 测试并给出 `PASS`、0 Critical / 0 Important / 0 Minor。只新增一个 resolver 和 Main Router 可选 callback，无合同/schema/port/framework/workflow/adapter/CLI/live 变化；Task 10 已释放。
- [x] Task 10 完成：现有 typed package/routing result 可确定性物化两个 immutable `PROPOSED` handoff，五篇得到精确 `3/2/1/2/1` 共九个原始完整资产，支持多路线 asset-level 顺序、唯一 final-decision 派生和逐字 canonical 文本；image job 仅在当前 handoff hash、release receipt 与 receipt artifact 三者严格绑定时构造，handoff 任一内容变化使旧 receipt 失效。实现者 focused `5 passed`、唯一 active `515 passed`，不同 reviewer `PASS`、0 Critical / 0 Important / 0 Minor。只新增 builder、job factory 和必要导出，无合同/schema/runner/export/policy/worker/framework 变化；Task 11 已释放。
- [x] Task 11 完成：五个固定 CLI 命令已暴露默认离线安全的 package validation、route run/resume、execution-scoped inspect 与 checkout-only docs check；Router、interaction、handoff、attempt、cache 与 terminal seal 证据均通过现有 local runtime 落盘并可审计。实现者 focused `26 passed`、Task 8 regression `3 passed`、唯一 active `519 passed`；首次独立复核发现的三个 Important 经一次集中修复后，窄范围复核 `PASS`、0 Critical / 0 Important / 0 Minor。没有新增合同/schema、通用 CLI/观察性/发布框架、worker 或 live 行为；Task 12 已释放。
- [x] Task 12 离线准备完成：独立于模型结果的五篇人工 gold 已冻结，SHA-256 `6af5e09151bd2b904ceefc28337655d5e14e28188824bc6c033f3801b22705af`；它绑定五个 canonical package、37 个 visual 和精确 `3/2/1/2/1` 九个原始资产。explicit-live 测试默认不被 active suite 发现，直接收集时也会跳过；只有显式 flag 加仓库外持久目录才能执行，并在全部 prediction seal 后才读取 gold。实现者 active `520 passed`，不同 reviewer `PASS`、0 Critical / 0 Important / 0 Minor；没有生产、合同/schema、prompt、fixture、SDK、provider 或 live 变化。
- [ ] Task 12 direct-live 已获授权、尚未完成：用户明确指定 `C:\.env` 第 6–19 行的第二套远程 API，而不是前四行的本地 CLIProxyAPI。固定 Chat Completions profile 已完成 TDD 和独立复核。第一次论文运行封存为 `ROUTER_NO_SUBMIT`，其五回合/Router `1.1.0` 窄修正已通过 active `523 passed` 与独立 reviewer `PASS`。第二次首篇运行暴露 missing-strict；submit-only `strict: true` 与 provider profile `3.1.0` 的 TDD 已通过 active `523 passed` 和独立 reviewer `PASS`。第三次首篇执行实际携带 exact contract schema 与 submit strict，仍连续两次生成旧式/臆造字段并封存为 `ROUTER_SCHEMA_INVALID`。用户现已明确授权一个有界的 terminal `response_format/json_schema` fallback：先用无论文/无 gold 的 exact-contract probe 验证远端能力；通过后只修改 Main Router 及其现有聚合测试，完成 TDD 和一次独立复核，再允许一轮 live。retrieval tools、adapter、prompt、gold、contracts、fixtures 与 `5/6` 回合预算继续冻结。

- [ ] Task 12 live 暂停：原 probe 及用户额外授权的一次简单重试均明确使用第二 API，并都返回安全错误 `PROVIDER_TRANSIENT`，没有 normalized output。用户判定当前为 API 问题并要求暂不继续；没有释放 TDD、实现者、reviewer 或生产/测试改动。

### Task 12 direct-live profile（2026-08-27）

本节覆盖剩余 live gold 的 provider transport，但不改动冻结的人工 gold、领域 contracts/schema、五篇 fixture、prompt 指令语义、no-paid-CI 或 no-commit 约束。

- live 进程只从 apps composition root 接收 `OPENAI_BASE_URL` 与 `OPENAI_API_KEY`；代码不读取或硬编码 `C:\.env`，控制器运行时只把第二套远程块注入当前子进程。前四行本地代理变量、管理 key、exe 与 8317 端口不再使用。
- 固定请求别名为 `gemini-3.7-flash-high`，固定 high reasoning、nonstreaming 与 `store=False`。provenance 记录请求 alias 和具体 OpenAI-compatible Chat Completions profile；provider 返回的实际 model 字段保留在 immutable response evidence 中，不冒充固定的官方低档 profile。
- 远程端点已经证明支持 Chat Completions 文本和 function calling，不支持 Interactions。因此只允许一个私有、具体、确定性的请求/响应转换器复用现有 Router tool loop；不得新增 provider registry、通用翻译框架、port 或 contract/schema。
- 在一个实现者完成 RED/GREEN、focused + 唯一 active suite，并由一个不同 reviewer 清零 Critical/Important 后，才从仓库外持久 workspace 运行五篇 live gold。失败或 mismatch 只分析 sealed artifacts/resume 证据，不自动修改 prompt 或 gold。

### 第一次论文 live 证据与窄修正（2026-08-27）

- 仓库外 workspace 中的 execution `exec_92d4bcdd961249a084d977fe7aa78998` 已终态封存为 `FAILED_TERMINAL`，failure code 为 `ROUTER_NO_SUBMIT`。四个 response artifact 都通过现有边界验证，依次调用 `read_blocks`、`search_blocks`、`read_blocks`、`read_blocks`；总计四个 function call、零 visual triage、零 handoff。
- 该运行在首篇论文即停止；gold 尚未读取，其余四篇未调用，receipt 不存在。provider transport、固定请求 alias、high reasoning、stateless history 和 immutable evidence 均正常，失败来源是四回合上限没有给完成第四次检索后的提交留下回合。
- 最小修正只将 Main Router 的 provider turn ceiling 从 4 调为 5，并将 Router 语义版本从 `1.0.0` 调为 `1.1.0`，以自动改变 cache identity；六次 retrieval ceiling、prompt 文件、context/schema/tool 合同、gold、fixtures、adapter 和 workflow 均不改变。
- 新测试必须证明前四回合检索、第五回合提交成功，同时保留既有 tool-budget/no-call/correction 行为。一个实现者完成一次 RED/GREEN 和唯一 active suite，一个不同 reviewer 做一次有界复核。若下一次 live 仍以同一 no-submit 根因失败，不再自动增加回合或调 prompt，而是暂停裁决。

### 第二次论文 live 证据与 strict-submit 窄修正（2026-08-27）

- execution `exec_0d8b48d045cd4b779c598da1198b50e9`、attempt `attempt_16aa063b0a0e4cb3a35b905c17c169d0` 已在同一外部 workspace 封存为 `FAILED_TERMINAL` / `ROUTER_SCHEMA_INVALID`。Router `1.1.0` 和新的 content key 已生效；两个有效回合都调用 `submit_article_routing`，未进入 triage/handoff，也未读取 gold 或运行其余四篇。
- 两个提交分别产生 65/64 个 schema errors：包括错误 enum、旧式 route/visual/text 字段名、缺失 required 字段和越权 rationale；第二次安全 correction 后仍重复同一结构。sealed request 重建证明 `ArticleRoutingResult` 的完整 `$defs`/properties/required/additionalProperties schema 正确，但三个 Chat function 都没有 strict 标记。
- 第二套远程 API 的两个小型 high-reasoning forced-function smoke 分别以 344/297 total tokens 成功：`strict: true` 被接受并给出精确 enum/array arguments，含一个未列入 required 的 optional nullable/default 字段时也能合法省略。因此不需要修改 contract/schema 或 prompt。
- 最小修正只给 `submit_article_routing` declaration 增加 `strict: true`，search/read 继续保留现有可选参数和 non-strict 行为；provider profile version 从 `3.0.0` 升到 `3.1.0`，使 provenance/cache identity 改变。adapter 已原样透传声明，不修改；Router `1.1.0`、五回合/六检索上限、prompt、gold、fixtures、contracts 和 workflow 全部冻结。
- 一个实现者只在现有 Main Router 聚合测试中先证明 missing-strict/profile RED，再最小 GREEN 并运行唯一 active suite；一个不同 reviewer 只复核 submit strict、retrieval 不变、profile/cache 绑定和冻结范围。若下一次 live 再出现同类 schema-invention root，停止自动修复并向用户裁决。

### 第三次论文 live 证据与停止条件（2026-08-27）

- execution `exec_0389df97bc0d43aa947f601bc940f016`、attempt `attempt_255b8261a7c04d56b37222faf3de5de4` 已在同一外部 workspace 封存为 `FAILED_TERMINAL` / `ROUTER_SCHEMA_INVALID`。首篇依次调用 `search_blocks`、`read_blocks`、`submit_article_routing`、`submit_article_routing`；零 triage、零 handoff，其他四篇未运行，gold 未读取，receipt 不存在。
- 确定性重建实际 Chat wire 证明 search/read 无 strict，submit 为 `strict: true`，submit parameters 与 `ArticleRoutingResult.model_json_schema(mode="validation")` 完全一致，同时 high reasoning、`store=False`、nonstreaming 均生效。
- 两次 submit 仍各产生 38 个 schema errors，重复旧式 `target_name`/`visual_ids`/`decision` 字段，缺少当前 route/visual 必填字段，并产生缺失或非法 route-group 引用。这与上一轮 schema-invention 属于同一根因，说明该远程兼容层接受 strict 标记但未对当前复杂函数合同形成可靠约束。
- 本轮约 144 秒即停止，sealed observability 记录四个 turns、四个 function calls、119,868 total tokens 与 141,835 ms provider latency。events/manifest 均已 seal；未继续消耗其余论文。
- 同根因停止条件现已触发：不再自动修改 prompt、schema、adapter、correction loop 或继续 live 调用。下一步需要用户明确裁决是否扩大范围，例如更换能可靠执行复杂结构化输出的 transport/model，或授权一个新的有界终态结构化输出方案。

### Terminal response-format fallback 授权（2026-08-27）

- 用户已选择并授权同一第二 API/同一模型下的最小终态结构化输出方案，而不是立即更换 provider/model。设计记录为 `task-12-live-response-format-design.md`，SHA-256 `eabefb430b7b942fdc6c0b4edf78778b8e393ae5e63dc6e43583841b8ce31233`。
- 施工入口为 `task-12-live-response-format-brief.md`，SHA-256 `9efa84af8c272c997539ef6c86e3378b628b28bd1b8a846407d49e1b74905b4d`。第一道门禁是一个使用完整 `ArticleRoutingResult` schema 与纯合成 `NOT_IN_SCOPE` 对象的 secret-safe response-format probe；不发送论文、fixture 或 gold。
- probe 通过后，保留第一轮 strict submit；只有首个无效 submit 才把既有一次 correction 变为无 tools、exact `response_format.schema` 的 structured `model_output`。Router 升为 `1.2.0` 以改变 cache identity，provider profile 保持 `3.1.0`；不得增加回合、检索、semantic attempt 或新框架。
- probe 失败则不写生产代码；实现复核后的下一次 live 若仍复现同一 schema-invention 根因，则再次停止，不自动扩大 prompt/schema/adapter 或更换模型。
- 已授权的单次 probe 于 2026-08-27 以进程退出码 `2` 和安全输出 `PROBE=FAIL ADAPTER_CODE=PROVIDER_TRANSIENT` 结束，未返回 normalized output，因此既未证明兼容也未证明不兼容。一次性门禁按设计关闭：未重试、未释放 TDD、未派实现者/复核者，Router、测试、adapter、gold 和全部冻结范围保持原 hash。详细证据见 `task-12-live-response-format-probe.md`。
- 用户随后只授权一次简单重试。控制器以硬校验绑定 `C:\.env` 第 6/7 行的第二远程 API，纯合成合同对象本地预检通过；同一远端 invocation 约 299 秒后仍以 `PROBE=FAIL API_BLOCK=SECOND_REMOTE ADAPTER_CODE=PROVIDER_TRANSIENT` 结束。没有论文/gold、normalized output、token receipt 或代码改动；重试证据见 `task-12-live-response-format-probe-retry.md`。用户已将其归因为 API 问题并暂停本 live 路线。

## 0.1 实施前预检裁决

以下裁决是后续 Task brief 的强制输入，优先于后文章节中可能存在的简写。

### Task 依赖

| Task | 必须完成的前置 Task |
|---|---|
| 0 | 无 |
| 1 | 0 |
| 2 | 0、1 |
| 3 | 2 |
| 4 | 3 |
| 5 | 3、4 |
| 6 | 3、5 |
| 7 | 2、3 |
| 8 | 3、5、6、7 |
| 9 | 3、7、8 |
| 10 | 3、5、8、9 |
| 11 | 1、2、6、7、8、9、10 |
| 12 | 4、5、6、7、8、9、10、11 |

### Legacy 精确迁移映射（供任务零使用）

只迁移实施开始前已经 tracked 的旧项目内容；不得移动本文档或 `.superpowers/`。

| 源 | 目标 |
|---|---|
| `corpus/` | `tests/legacy_harness/corpus/` |
| `experiments/` | `tests/legacy_harness/experiments/` |
| `harness/` | `tests/legacy_harness/harness/` |
| `results/` | `tests/legacy_harness/results/` |
| `tools/` | `tests/legacy_harness/tools/` |
| 原有 `tests/test_*.py` | `tests/legacy_harness/tests/test_*.py` |
| `docs/COLLABORATION.md`、`DATA_PROVENANCE.md`、`RUNBOOK.md`、`SECURITY.md`、`docs/superpowers/` | `tests/legacy_harness/docs/` 下保持原相对结构 |
| 原有 `.github/`、`.env.example`、`.gitattributes`、`.gitignore`、`MANIFEST.sha256.json`、`NOTICE.md`、`README.md`、`pyproject.toml` | `tests/legacy_harness/` 下保持原根相对结构 |

迁移顺序必须是：先创建目标容器；再逐个移动原测试；再移动无歧义顶层目录；再逐个移动旧 docs；最后移动根配置并创建新的生产根替代文件。禁止执行 `git mv tests ...` 或 `git mv docs ...`。旧 manifest 内容保持不变；只允许对 relocated legacy test 做必要的 Git-index 路径前缀兼容。

Task 0 的 import-boundary detector 必须用临时合成源码证明能抓住非法 import；在 `src/tsdaf` 尚不存在时不得把空扫描宣称为生产边界已验证。真实非空 production import graph 的验收归 Task 2。

### 后续接口裁决

- Task 1 保留并补充现有 `WI-0001`，不得重建或覆盖其正文。Task 1 直接以 `python scripts/validate_docs.py` 验收；`tsdaf docs check` 的 CLI wiring 归 Task 2。
- `pydantic>=2.10,<3` 是 core dependency；`google-genai` 与 `pydantic-settings` 属于 `router` extra。Task 7 实施时以官方 2026-08 Interactions 新 schema 为准，将 Python SDK 下限修正为 `google-genai>=2.3,<3`；1.x legacy schema 已在 2026-06-08 sunset，不得继续以旧 constraint 构建生产 adapter。
- `VisualGroup` 只表示 caption/版面关联，不是图片处置计数单位。每个 `CanonicalVisualAsset.visual_id` 必须且只能有一个最终 decision，并携带所属 `visual_group_id`。
- `RouteGroup.ordered_visual_ids` 引用 asset-level `visual_id`。一个选中的资产物化一个 `RouteImageItem`；多成员 group 按 canonical reading order 展平，因此五篇 gold 仍精确计为 9 个 asset item。
- Main Router 对每个 asset 输出一个 initial decision；它不得直接输出 `UNCERTAIN`。`NEEDS_VISUAL_VERIFICATION` 才触发 Visual Triage。最终记录同时保存 initial decision、可选 triage result 和唯一 final decision；triage 的 `UNCERTAIN` 产生 `REVIEW_REQUIRED`。
- Task 3 拥有 taxonomy/schema 的 Python 单一事实源和导出 API；Task 8 拥有 prompt 文件、由 contract 生成的 `taxonomy.md`、tool schema 及漂移测试；Task 11 只暴露检查命令，不复制定义。
- Task 7 已冻结唯一的 low-thinking Gemini profile，因此 Task 8 的一次 semantic correction 仍使用同一 low profile，并在四个总 turn 预算内通过 stateless full-history `function_result` 请求重交；本文后文“medium-thinking second attempt”的旧措辞不再授权重开 Task 7、增加模型 profile 或伪称 medium thinking。
- Task 5 已定义确定性的 `SearchIndex` port 和内存实现。为优先形成离线纵向闭环，Task 8 先直接使用该实现；SQLite FTS5 搜索不再属于 Task 6，只有真实规模/性能消费者出现后才另行评估。Task 6 的 SQLite 仅索引 immutable execution/artifact facts，并能从 execution manifests 和 sealed event records 重建。
- `events.jsonl` 在执行期间是只追加日志，不是已 seal artifact；终态时生成内容 hash 并纳入 terminal manifest，之后不可修改。
- Gemini Interactions 固定 `store=False`，因此 Task 7/8 使用客户端维护的 stateless full-history continuation：后续请求在 `input` 中包含初始 `user_input`、模型返回的全部 `steps` 和 host 的 `function_result`，不得使用依赖服务端存储的 `previous_interaction_id`。
- `tsdaf route resume` 未带 `--live` 时只允许验证、缓存命中和本地 stage；若下一个 stage 需要 provider，返回 `LIVE_REQUIRED`，不创建新 attempt，也不改变原 execution terminal/nonterminal 状态。
- `tsdaf docs check` 在 v1 中是 checkout 内的仓库维护命令：CLI 从当前目录向上定位同时包含 `pyproject.toml` 与 `scripts/validate_docs.py` 的根目录，以 `shell=False` 子进程调用唯一 validator 并原样传播退出码；wheel 在 checkout 外调用该命令时稳定返回 2。
- 真实环境变量读取只允许发生在 `apps` composition root。未来 `adapters/config` 只能解析显式传入的 mapping，不得直接读取 `os.environ` 或实例化环境型 settings。
- Task 2 建立的 Ruff 门禁覆盖整个 active 仓库并排除 `tests/legacy_harness/**`。为让门禁从创建之日即全绿，Task 2 可对 Task 1 的 active Python 做机械格式化、import 排序和等价长行拆分；不得改变文档校验语义、错误码、确定性 STATUS 字节或研究文件。禁止通过缩小 Ruff 扫描范围、增加 `noqa` 或扩大 exclude 来隐藏既有问题，整理后必须重跑 Task 1 focused tests、active suite、STATUS byte-stability、`DOCS_OK` 和研究文件 hash 校验。

## 0.2 Task 2 工具债务冻结与后续复杂度预算（2026-08-26）

以下执行方向优先于本文此前关于审查深度、fresh probe 数量和基础设施扩展的默认做法，但不改变 TS-DAF 的科学目标、核心数据边界、TDD、独立复核或 no-commit 约束。

### Task 2 checker 的最终定位

`scripts/check_import_boundaries.py` 是已接受、已冻结的工程工具债务，不是领域架构范例：

- Slice 1 不再扩展它的 Python 语言语义覆盖；Task 3–12 不得因人为语言边缘案例修改它。
- 真实生产代码若触发误报，优先改写为更清晰且符合既有架构的表达；不得为了取悦 checker 牺牲可读性。
- 只有真实生产代码出现无法规避的误报或明确漏报，才暂停并向用户报告是否重新打开该工具。
- checker 不得被 `src/tsdaf` 导入，不得成为领域层依赖、设计中心或后续 Task 的复杂度来源。
- Task 2 已完成，不回滚、不重写、不追加修复轮次。

### 后续 Task 的复杂度来源与停止条件

复杂度预算优先投入领域模型、provenance、不可变 package/route/handoff/receipt、真实五篇 fixture、恢复和审计。不得默认投入 Python 边缘语义、小型解释器、无当前消费者的通用框架、没有真实失败样本的大型探针矩阵，或为了内部工具而扭曲领域设计。

项目尺度按“逻辑清晰的小型 Harness”控制：本轮因搭统一底座而细致，但不升级为平台工程或重大基础设施项目。后续 Task 仍完整执行计划中的业务行为，却采用更轻的施工与复核；合同和 schema 的质量以真实消费者、统一边界和可读实现衡量，不以数量或复杂度衡量。

每个 Task 施工前，ledger 必须用短条目记录：领域结果、允许修改的生产文件、关键领域不变量、明确非目标、预计测试类别和明确下游消费者。出现以下任一情况立即暂停并向用户报告原目标、复杂度偏移、最小可行方案和是否扩展范围：

1. 支撑工具的代码量或认知复杂度超过领域实现。
2. Task 从领域交付演变为通用 framework、registry、解释器或基础设施项目。
3. 连续两轮 review 出现同一类根因；此时需要架构裁决，不自动继续打补丁。
4. reviewer 提出冻结需求之外的新语言语义或未来扩展要求。
5. 为通过 checker 而使生产代码更难理解。
6. 内部工具开始需要自己的状态机、复杂 fixed point 或大量专用矩阵。
7. 新抽象没有至少两个当前、明确的消费者。

多智能体仍采用一个实现者加一个不同复核者；两者读取精确 brief、写入不同文件、详细证据落盘。复核只覆盖冻结领域需求和真实回归，每个风险类别保留少量代表性 fresh cases。一次实现复审加一次必要修复复审是常态；第二次仍出现同根因就停止，不自动进入第三至第五轮。

后续汇报按领域价值组织：完成的真实能力、直接消费者、新增核心不变量、新抽象或技术债务、测试/独立复核结果，以及下一步是否仍在主线。探针数量不再作为主要质量指标。

### 从 Task 6 起纵向交付优先（2026-08-26）

当前最高优先级是在不改变数据完整性、provenance、no-paid-CI 和 no-commit 硬约束的前提下，尽快形成可运行的离线纵向链路：

1. 五篇 fixture 进入 canonical package。
2. 具体本地 artifact/runner 保存并恢复执行。
3. recorded 或 fake provider 驱动 Main Router。
4. host 生成 `VisualRoutingHandoff` 与 `CoarseTextHandoff`。

在该链路形成前，不扩展工具语义、通用框架、未来 backend 或没有直接消费者的基础设施。Task 6 只交付 filesystem content-addressed store、hash/atomic write/seal、execution manifest、append-only events、content-key cache、本地锁、attempt/resume，以及可从不可变记录重建的 SQLite execution/artifact 索引；不实现 SQLite FTS 搜索、provider、prompt、Router、handoff、CLI、Prefect 或新 ports。

从 Task 6 起每个 Task 采用一次性 brief、一个实现者 TDD、实现者 focused/full gates、控制器关键核对、一个不同 reviewer 的单次有界复核。Critical/Important 只允许一次集中修复；同根因复现即停止裁决。Minor 记录但不阻塞主线。Task 8 完成时必须展示一次真实可运行的离线纵向结果，而不能只报告测试数量。

## 0.3 Task 3 轻量比例检查与收窄执行

本检查只确认已接受 construction brief 的领域比例，不重新开启多轮规划。结论是：42 exports、26 root models、9 enums 是已审查上限；当前清单均有 Slice 1 明确消费者，但不得继续新增。

| Contract 组 | 当前真实业务不变量 | 直接消费者 | 现在是否必须 |
|---|---|---|---|
| `ArtifactRef` / `BoundingBox` / `SourceLocator` | 内容身份、原始定位、无路径逃逸语义、不可变来源 | Task 4 fixture、Task 5 package/canonicalization、Task 6 artifact store、Task 10 handoff | 必须 |
| manifest / canonical blocks / visual assets / groups / suppression / clean view | 完整 inventory、稳定 ID、可逆 text partition、原图不被 crop 替换、hash 闭合 | Task 4、5、8、10、12 | 必须 |
| article/route/visual/text-role taxonomy 与 `ArticleRoutingResult` | 每资产唯一 decision、triage trace、route/reference closure、派生 disposition | Task 8 Main Router、Task 9 Triage、Task 10 Handoff、Task 12 gold | 必须 |
| visual/coarse handoff 与 route item | 原始 `ArtifactRef`、逐字 canonical text、顺序和 provenance | Task 10、Task 12 | 必须 |
| review/release receipt 与 image-job shape | handoff hash 绑定、immutable audit metadata、无 receipt 不得形成合法 job 数据 | Task 10 release gate、Task 11 inspect、后续 image lane | 必须；真正授权仍归 Task 10，不扩展为 capability framework |
| `TypedFailure` / `StageResult` | provider-neutral failure、terminal result matrix、cache/attempt 可审计 shape | Task 6 Runner、Task 7 adapter、Task 8/9 stages、Task 11 inspect | 必须；不实现 Runner/state machine |
| schema catalog / canonical JSON | contract/taxonomy 单一事实源、稳定 provenance hash、同资源 `$ref` 闭合 | Task 8 schema/tool provenance、Task 11 checks、Task 12 receipts | 必须；仅显式 registry，不做 plugin/reflection framework |

字段审计结论：现有字段均来自上述真实不变量；18 个 required-nullable 字段和 3 个 defaults 是稳定 wire contract，不是未来设想。`ImageProcessingJob` 当前只冻结数据 shape；worker、授权服务和运行行为以后实现。没有需要提前实现的 future-only runtime 行为。

允许的共享抽象只有两个：私有 `ContractModel` 被所有 persistent models 直接消费；canonical JSON/hash primitive 被 package hash、routing/handoff binding 和 schema bundle 直接消费。两者均有至少两个当前消费者。禁止新增通用 repository、plugin、service locator、自动反射 registry、schema framework 或运行时层。

Task 3 测试集中于领域不变量：package/hash/reference/order 闭合、asset-level decision/triage/disposition、handoff exact-copy、receipt-handoff binding、无 release receipt 拒绝 job、stage-result/failure matrix、schema 稳定和越权字段拒绝。Pydantic 已保证的普通行为只做少量代表性 strict/extra/frozen/tuple 测试；不得扩张成框架自测或大规模通用探针矩阵。

Task 3 明确非目标保持不变：Runner、Gemini、SQLite、Prefect、prompt、CLI 新功能、package loader、真实授权服务，以及 Task 4 以后运行行为。完成一次比例检查后立即回到六文件 RED-first TDD；不再围绕 brief 进行形式化重审。


## 1. 第一阶段交付目标

第一阶段不是实现完整的全合成抽取系统，而是交付一个可以重复运行、可审计、可扩展的生产基础工作流：

> Weak Full-Text Reader / Article Route Router v1

它负责：

1. 接收已经规范化的 TS-DAF 文献包。
2. 机械清理正文阅读视图，但不修改或丢失原始内容。
3. 恢复图片、表格与 Figure/Scheme caption 的关联。
4. 使用 Gemini 3.7 Flash 阅读全文并判断文章类型。
5. 识别论文中一个或多个实际执行的合成路线组。
6. 对所有图片进行且仅进行一次分类。
7. 将实际执行的正向合成路线图片移交给图像通道。
8. 将路线核心文本及相关优化、机理、失败尝试上下文移交给文本通道。
9. 保存完整模型交互、工具调用、版本、成本、耗时和来源证据。
10. 支持缓存、失败恢复、重跑、审计和人工复核。

第一阶段的最终产物是两个 `PROPOSED` handoff：

- `VisualRoutingHandoff`
- `CoarseTextHandoff`

第一阶段不会直接生成 `ImageProcessingJob`。只有后续独立的 `ReleaseReceipt` 才能授权进入 YOLO、MolDet、OCSR 图像流水线。

---

## 2. 对原任务说明的边界修订

附加任务说明是重要参考，但以下内容以我们后来讨论确定的决策为准。

| 原任务说明中的建议 | 第一阶段最终决定 |
|---|---|
| 直接读取 PDF 或原始 MinerU ZIP | Slice 1 只接收规范化 TS-DAF package；origin PDF 仅作为来源证据 |
| Router 内做图片 crop/panel/localization | 不做裁剪和 panel 定位，交付完整 MinerU 图片资产 |
| 所有图片都交给多模态模型检查 | 主 Router 完全 text-first；只有不确定图片进入独立 Visual Triage |
| Pass C 形成完整 Text Package | Slice 1 只形成粗粒度 `CoarseTextHandoff` |
| 第一阶段包含 Review UI | Review UI 放到 Slice 2；Slice 1 只定义 receipt contract |
| 第一阶段处理 SI | SI 通道放到 Slice 4 |
| Router 动态加载 Skill | 生产行为由固定版本 prompt、schema、validator 和状态机约束 |
| `UNCERTAIN` 作为文章类别 | `UNCERTAIN` 是 disposition，不是 `ArticleType` |
| Router 决定是否发送到图像管线 | Router 只输出语义分类；是否发送由 host 根据规则派生 |

这样可以防止执行过程中重新引入已经否定的设计。

---

# 3. 总体代码架构

采用：

- 一个 Git 仓库
- 一个 Python distribution：`tsdaf`
- 多个内部 lane
- 一个独立图像 worker 入口
- 纯 Python 业务阶段
- 通过 port/adapter 隔离 Gemini、文件系统、SQLite 和未来 Prefect

建议目录如下：

```text
total-synthesis-multimodal-harness/
├── AGENTS.md
├── CHANGELOG.md
├── README.md
├── LICENSE
├── pyproject.toml
├── .env.example
├── .gitignore
│
├── prompts/
│   └── article_router/
│       └── v1/
│           ├── manifest.toml
│           ├── system.md
│           ├── task.md
│           └── taxonomy.md
│
├── src/
│   └── tsdaf/
│       ├── __init__.py
│       ├── py.typed
│       │
│       ├── contracts/
│       │   ├── common.py
│       │   ├── package.py
│       │   ├── canonical.py
│       │   ├── routing.py
│       │   ├── handoff.py
│       │   ├── review.py
│       │   ├── stage.py
│       │   └── errors.py
│       │
│       ├── workflow/
│       │   ├── ports.py
│       │   ├── runner.py
│       │   ├── execution.py
│       │   ├── states.py
│       │   ├── cache.py
│       │   └── sealing.py
│       │
│       ├── lanes/
│       │   ├── document/
│       │   │   ├── package_loader.py
│       │   │   ├── canonicalizer.py
│       │   │   ├── cleaner.py
│       │   │   ├── caption_linker.py
│       │   │   ├── visual_grouper.py
│       │   │   ├── context_compiler.py
│       │   │   ├── search_index.py
│       │   │   └── routing/
│       │   │       ├── service.py
│       │   │       ├── harness.py
│       │   │       ├── tools.py
│       │   │       ├── validator.py
│       │   │       ├── visual_triage.py
│       │   │       └── handoff_builder.py
│       │   │
│       │   ├── text/
│       │   │   ├── source_reader.py
│       │   │   └── coarse_handoff.py
│       │   │
│       │   ├── image/
│       │   │   ├── contracts.py
│       │   │   └── job_factory.py
│       │   │
│       │   └── fusion/
│       │       └── __init__.py
│       │
│       ├── adapters/
│       │   ├── gemini/
│       │   │   ├── client.py
│       │   │   ├── interactions.py
│       │   │   ├── serializer.py
│       │   │   └── usage.py
│       │   ├── storage/
│       │   │   ├── filesystem.py
│       │   │   ├── sqlite.py
│       │   │   └── locking.py
│       │   └── config/
│       │       └── settings.py
│       │
│       └── apps/
│           ├── cli.py
│           ├── composition.py
│           └── commands/
│               ├── package.py
│               ├── route.py
│               ├── run.py
│               └── docs.py
│
├── tests/
│   ├── unit/
│   │   ├── contracts/
│   │   ├── workflow/
│   │   ├── document/
│   │   └── routing/
│   ├── integration/
│   ├── live/
│   ├── gold/
│   │   └── article_routing_v1.json
│   ├── fixtures/
│   │   └── paper_packages/
│   └── legacy_harness/
│
├── docs/
│   ├── PROJECT_CHARTER.md
│   ├── ROADMAP.md
│   ├── STATUS.md
│   ├── architecture/
│   ├── decisions/
│   ├── guides/
│   ├── research/
│   └── work/
│
└── scripts/
    ├── generate_status.py
    ├── validate_docs.py
    └── rebuild_index.py
```

## 3.1 依赖方向

依赖必须单向：

```text
apps
  -> workflow / use cases
      -> lanes
          -> contracts

adapters
  -> workflow ports / contracts
```

硬性约束：

- `contracts` 不允许导入 Gemini、Prefect、SQLite 或 CLI 类型。
- `lanes` 不允许直接读取环境变量。
- `lanes` 不允许实例化具体 Gemini client。
- `workflow` 不包含全合成领域判断规则。
- `apps` 只负责参数解析、配置加载和依赖组装。
- `adapters` 实现 port，不向内层泄漏 SDK 对象。
- Gemini response 必须在 adapter 边界转换为 TS-DAF 自有类型。
- 业务模块之间不通过磁盘路径字符串隐式通信，只通过 contract 和 `ArtifactRef`。
- 所有“是否进入下游”的派生逻辑由 host 实现，不能依赖模型输出冗余布尔值。
- 禁止生产代码导入 `tests/legacy_harness` 中的任何模块。

这些规则应通过 import-boundary 测试或静态检查固定下来。

---

# 4. 核心数据模型

## 4.1 通用标识和来源

```python
class ArtifactRef(BaseModel):
    sha256: str
    media_type: str
    byte_size: int
    relative_path: str

class BoundingBox(BaseModel):
    x0: int
    y0: int
    x1: int
    y1: int
    coordinate_space: Literal["normalized_1000"]

class SourceLocator(BaseModel):
    document_id: str
    page_index: int
    bbox: BoundingBox | None
    native_block_index: int | None
    source_asset_ref: ArtifactRef | None
```

原则：

- `SourceLocator` 永远指向原始来源。
- 清理、合并 caption、文本选择不能覆盖来源定位。
- 任何导出的文本和图片都必须能够反向定位到输入 package。
- `ArtifactRef.sha256` 是内容身份，不以文件名作为身份。

## 4.2 文献包

```python
class PaperManifest(BaseModel):
    schema_version: Literal["tsdaf.paper-package.v1"]
    paper_id: str
    display_name: str
    source_pdf: ArtifactRef
    normalized_document: ArtifactRef
    normalized_blocks: ArtifactRef
    assets: list[ArtifactRef]

class CanonicalTextBlock(BaseModel):
    block_id: str
    page_index: int
    reading_order: int
    block_type: TextBlockType
    raw_text: str
    section_path: list[str]
    source: SourceLocator

class CanonicalVisualAsset(BaseModel):
    visual_id: str
    asset: ArtifactRef
    page_index: int
    bbox: BoundingBox | None
    width: int | None
    height: int | None
    native_caption: str | None
    source: SourceLocator

class VisualGroup(BaseModel):
    visual_group_id: str
    member_visual_ids: list[str]
    label_candidates: list[str]
    caption_block_ids: list[str]
    caption_text: str | None
    association_status: Literal[
        "CONFIRMED",
        "AMBIGUOUS",
        "NO_CAPTION",
    ]
```

稳定 ID 规则：

- 文本块：`MAIN-P0002-B0013`
- 图片资产：`MAIN-P0002-A0003`
- 视觉组：`MAIN-P0002-VG0002`
- 路线组：`RG001`
- 路线图片项：`RIMG001`

ID 根据规范化输入中的页码、阅读顺序和资产顺序生成，不依赖某次模型运行。

不承诺不同 MinerU 版本产生完全相同的 block ID；这种变化通过 package hash 和 canonicalizer version 显式记录。

## 4.3 清理视图

清理不生成被永久修改的正文副本，而是生成一个可逆视图：

```python
class SuppressionRecord(BaseModel):
    block_id: str
    reason: Literal[
        "PAGE_HEADER",
        "PAGE_FOOTER",
        "PAGE_NUMBER",
        "RUNNING_TEXT",
        "REFERENCES_SECTION",
        "DECORATIVE_ASSET",
    ]
    rule_version: str

class CleanDocumentView(BaseModel):
    included_block_ids: list[str]
    suppressed: list[SuppressionRecord]
```

正文仍从 `CanonicalTextBlock.raw_text` 读取。这样：

- 不会因清理丢失证据。
- 可以审计某段文字为什么没有进入模型上下文。
- 修改清理规则时不需要重新解析 PDF。
- 参考文献可不进入主上下文，但仍保存在 package 中。

## 4.4 文章和路线分类

```python
class ArticleType(StrEnum):
    TOTAL_SYNTHESIS = "TOTAL_SYNTHESIS"
    FORMAL_SYNTHESIS = "FORMAL_SYNTHESIS"
    SEMISYNTHESIS = "SEMISYNTHESIS"
    SYNTHETIC_APPROACH = "SYNTHETIC_APPROACH"
    NOT_IN_SCOPE = "NOT_IN_SCOPE"

class RoutingDisposition(StrEnum):
    ROUTED = "ROUTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"
```

路线修饰词独立保存：

```python
class RouteAttribute(StrEnum):
    ASYMMETRIC = "ASYMMETRIC"
    RACEMIC = "RACEMIC"
    BIOMIMETIC = "BIOMIMETIC"
    CHEMOENZYMATIC = "CHEMOENZYMATIC"
    DIVERGENT = "DIVERGENT"
    COLLECTIVE = "COLLECTIVE"
```

一篇论文只有一个主 `ArticleType`，但可以包含多个 `RouteGroup`：

```python
class RouteGroup(BaseModel):
    route_group_id: str
    target_names: list[str]
    synthesis_type: ArticleType
    attributes: list[RouteAttribute]
    ordered_visual_ids: list[str]
    core_text_block_ids: list[str]
```

## 4.5 图片分类

每个 `VisualGroup` 必须出现且只能出现一次：

```python
class VisualDecision(StrEnum):
    FORWARD_ROUTE = "FORWARD_ROUTE"
    RETROSYNTHESIS = "RETROSYNTHESIS"
    MECHANISM = "MECHANISM"
    OPTIMIZATION_SCREENING = "OPTIMIZATION_SCREENING"
    FAILED_ONLY_ROUTE = "FAILED_ONLY_ROUTE"
    BIOSYNTHESIS = "BIOSYNTHESIS"
    ANALYTICAL = "ANALYTICAL"
    OTHER = "OTHER"
    NEEDS_VISUAL_VERIFICATION = "NEEDS_VISUAL_VERIFICATION"
    UNCERTAIN = "UNCERTAIN"
```

`FORWARD_ROUTE` 的边界是：

- 实际执行过；
- 沿正向合成方向推进；
- 通向天然产物、形式合成端点或文中明确的目标端点。

以下内容不进入图像处理：

- retrosynthesis；
- 纯机理图；
- 条件优化表；
- 只有失败路线；
- 生物合成路径；
- 分析谱图；
- 论文封面；
- ACS/SI 小图标；
- 仅展示分子结构但不构成路线的图。

但与路线直接相关的优化、机理和失败尝试文本可以进入 `CoarseTextHandoff`，并保留独立标签。

## 4.6 文本角色

```python
class CoarseTextRole(StrEnum):
    ROUTE_CORE = "ROUTE_CORE"
    DIRECT_OPTIMIZATION_CONTEXT = "DIRECT_OPTIMIZATION_CONTEXT"
    DIRECT_MECHANISM_CONTEXT = "DIRECT_MECHANISM_CONTEXT"
    DIRECT_FAILED_ATTEMPT_CONTEXT = "DIRECT_FAILED_ATTEMPT_CONTEXT"
```

模型只选择 block ID 和角色。host 根据 block ID 从 canonical package 复制精确原文，不能接受模型重新转述的文本作为正式证据。

---

# 5. Handoff、审核与图像任务解耦

## 5.1 `VisualRoutingHandoff`

这是论文级交付包：

```python
class VisualRoutingHandoff(BaseModel):
    schema_version: Literal["tsdaf.visual-routing-handoff.v1"]
    paper_id: str
    execution_id: str
    article_type: ArticleType
    route_groups: list[RouteGroup]
    items: list[RouteImageItem]
    provenance: RouterProvenance
    status: Literal["PROPOSED"]
```

## 5.2 `RouteImageItem`

```python
class RouteImageItem(BaseModel):
    route_image_item_id: str
    visual_group_id: str
    source_asset: ArtifactRef
    page_index: int
    bbox: BoundingBox | None
    dimensions: tuple[int, int] | None
    figure_or_scheme_label: str | None
    caption: str | None
    related_block_ids: list[str]
    related_original_paragraphs: list[str]
    route_group_id: str
    sequence_index: int
    router_execution_id: str
```

约束：

- `source_asset.sha256` 必须等于输入 MinerU 资产 hash。
- 不允许生成 crop hash。
- 不允许加入 SMILES、反应边界、箭头、panel 坐标。
- 相关段落限制为 1–2 段精确原文。
- route order 必须在 route group 内连续且无重复。

## 5.3 审核 receipt

```python
class ReviewReceipt(BaseModel):
    review_id: str
    handoff_sha256: str
    reviewer_id: str
    decision: Literal["APPROVE", "REJECT", "REQUEST_CHANGES"]
    reviewed_at: datetime
    notes: str | None

class ReleaseReceipt(BaseModel):
    release_id: str
    handoff_sha256: str
    authority: Literal["HUMAN_APPROVED", "POLICY_APPROVED"]
    policy_version: str | None
    issued_at: datetime
```

审批不能修改 handoff。若审核后内容有变化：

1. 生成新的 handoff。
2. 得到新的 hash。
3. 原 receipt 仍指向旧 handoff。
4. 新 handoff 必须重新审核。

## 5.4 `ImageProcessingJob`

只有合法 `ReleaseReceipt` 才能创建：

```python
class ImageProcessingJob(BaseModel):
    job_id: str
    route_image_item: RouteImageItem
    release_receipt: ArtifactRef
    requested_pipeline: Literal["YOLO_MOLDET_OCSR_V1"]
```

Slice 1 只实现 contract 与拒绝无 receipt 的守卫，不启动真实 worker。

---

# 6. 工作流执行架构

所有业务阶段均为普通 Python 对象：

```python
class Stage(Protocol, Generic[InputT, OutputT]):
    name: str
    version: str

    def run(
        self,
        request: InputT,
        context: StageContext,
    ) -> OutputT:
        ...
```

`StageContext` 注入：

```python
class StageContext(BaseModel):
    execution_id: str
    attempt_id: str
    artifact_store: ArtifactStore
    run_repository: RunRepository
    clock: Clock
```

主要 port：

```python
class ArtifactStore(Protocol):
    def put_bytes(...) -> ArtifactRef: ...
    def put_json(...) -> ArtifactRef: ...
    def read_bytes(...) -> bytes: ...
    def verify(...) -> None: ...

class RunRepository(Protocol):
    def create_execution(...) -> None: ...
    def append_event(...) -> None: ...
    def record_attempt(...) -> None: ...
    def find_cached_result(...) -> StageResult | None: ...

class RouterProvider(Protocol):
    def create_interaction(...) -> ProviderTurn: ...
    def continue_interaction(...) -> ProviderTurn: ...

class PromptCatalog(Protocol):
    def load(...) -> PromptBundle: ...
```

本地 Runner 负责：

- 计算输入 content key；
- 查找缓存；
- 获取同 content key 的执行锁；
- 创建 attempt；
- 执行 stage；
- 写 artifact；
- 验证 artifact hash；
- seal stage result；
- 更新 SQLite 派生索引；
- 根据错误类型决定重试或终止。

Prefect 后续只能调用这些 stage，不能重写业务逻辑。

---

# 7. 端到端处理步骤

## 7.1 Package validation

执行：

```text
tsdaf package validate <package-dir>
```

验证：

- `manifest.json` schema；
- origin PDF 存在；
- `document.json`、`blocks.jsonl` 存在；
- manifest 中所有资产存在；
- hash、文件大小、media type 一致；
- block page index 合法；
- block/source locator 完整；
- 资产引用没有越界；
- 不允许路径逃逸 package 根目录；
- 不允许绝对路径写入正式 manifest。

错误直接终止，不调用模型。

## 7.2 Canonicalization

按以下固定顺序处理：

1. 读取 normalized document。
2. 读取 `blocks.jsonl`。
3. 正规化页码和 reading order。
4. 生成稳定 block/asset ID。
5. 识别 section path。
6. 建立所有 visual/table inventory。
7. 进行机械 suppression。
8. 恢复 caption 关联。
9. 构建 `VisualGroup`。
10. 建立全文检索索引。
11. 输出 `CanonicalPaperPackage`。

## 7.3 Caption stitching

处理优先级：

1. 优先采用 MinerU 已提供的 caption。
2. 检测相邻 block 中的 `Figure/Fig./Scheme/Table` 标签。
3. 限制在同页或明确跨页延续范围内关联。
4. 根据阅读顺序、距离和标签匹配评分。
5. 同一标签下的多个 visual 可组成一个 `VisualGroup`。
6. 有多个同分候选时不得强行选择，标记 `AMBIGUOUS`。
7. caption 原始 block 保留，不被合并删除。
8. 图片 caption 为空，但正文明确引用时，保留引用 block 作为候选证据。

五篇试点中 9 张正确路线图有 7 张原生 caption 不完整，因此这是 Slice 1 的关键模块，不是简单预处理。

## 7.4 Router context compilation

主 Router 初始上下文包含：

- 清理后的完整正文；
- page 和 section 边界；
- 所有 visual group ID；
- Figure/Scheme label；
- stitched caption；
- page、bbox、asset type；
- 完整分类 taxonomy；
- 输出 schema 和工具说明。

不包含：

- 图片二进制；
- 图片 base64；
- 预先判断的“候选路线图”；
- 语义过滤后的正文；
- 后续 OCSR 输出。

编译前做 token preflight：

- 目标上限约 900k input tokens；
- 给工具结果、输出和 provider 开销留出余量；
- 超过上限返回 `CONTEXT_TOO_LARGE`；
- 禁止静默截断正文。

---

# 8. Main Article Router Harness

## 8.1 模型配置

生产 profile 固定：

- SDK：官方 `google-genai`
- API：Interactions API
- 模型：`gemini-3.7-flash`
- `store=False`
- thinking level：`low`
- 最多 4 个模型 turn
- 最多 6 次检索工具调用
- transport retry 最多 3 次

模型配置会写入执行清单。生产模式不允许任意模型覆盖；若 `GEMINI_MODEL` 与固定模型不同，启动时直接报配置错误。

模型与 API 使用方式以官方文档为准：[Gemini 3.7 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash)、[Interactions API](https://ai.google.dev/gemini-api/docs/interactions-overview)、[Function calling](https://ai.google.dev/gemini-api/docs/function-calling)。

## 8.2 工具

### `search_blocks`

```python
search_blocks(
    query: str,
    page_range: tuple[int, int] | None = None,
    section_filter: list[str] | None = None,
    limit: int = 20,
)
```

规则：

- `limit <= 20`
- 返回稳定 block ID、页码、section、原文摘要
- SQLite FTS5 可用时使用 FTS5
- FTS5 不可用时使用确定性的 token/substring fallback
- 相同输入必须得到相同排序
- 工具不能修改状态

### `read_blocks`

```python
read_blocks(
    block_ids: list[str],
    neighbor_count: int = 1,
)
```

规则：

- 单次最多 40 个 block ID
- `neighbor_count <= 2`
- 返回精确原文和相邻 block
- 不允许读取 package 以外的路径

### `submit_article_routing`

这是唯一允许终止主 Router 的工具。

schema 从 Pydantic contract 生成，不能在 prompt 中复制一份容易漂移的手写 JSON schema。

如果达到 turn/tool budget 仍未调用 submit，返回 `ROUTER_NO_SUBMIT` 或 `TOOL_BUDGET_EXHAUSTED`。

## 8.3 Host validation

模型 submit 后必须依次经过：

1. JSON/schema 验证。
2. article type 合法性验证。
3. 所有 block ID 存在性验证。
4. 所有 visual group ID 存在性验证。
5. 每个 visual 恰好有一个 disposition。
6. route group 引用闭合。
7. route order 无重复。
8. `FORWARD_ROUTE` visual 必须属于某个 route group。
9. 每个有效 route group 至少有一个 `ROUTE_CORE` block。
10. `NOT_IN_SCOPE` 不得产生正向路线 handoff。
11. coarse text role 与引用 block 一一对应。
12. 不能引用 suppressed decorative asset 作为 forward route，除非进入视觉复核。

Host 不修改模型的有效语义判断。无效输出进入一次新的 medium-thinking attempt，而不是悄悄修补。

---

# 9. Visual Triage

只有 `NEEDS_VISUAL_VERIFICATION` 的 visual 才进入多模态检查。

每次请求最多包含 4 张图片，并且只提供：

- 图片；
- stitched caption；
- 邻近原始文本；
- visual ID；
- 当前候选类别；
- route group 候选。

明确不提供：

- 全文；
- 其他未标记图片；
- 反应解析任务；
- SMILES 提取任务；
- crop/panel 任务。

配置：

- media resolution：medium
- 只做高层图片类别判断
- 无法确定时输出 `UNCERTAIN`
- 仍不确定则整个执行进入 `REVIEW_REQUIRED`

五篇 pilot 的目标 escalation rate 是 0。前 100 篇目标不超过 5%；超过 10% 必须停止放量，优先修复 canonicalization、caption stitching 或 prompt。

---

# 10. Prompt 资产管理

`prompts/article_router/v1/manifest.toml` 记录：

```toml
prompt_id = "article-router"
version = "1.0.0"
schema_version = "tsdaf.article-routing.v1"
model_profile = "gemini-3.7-flash-low"
```

执行时记录：

- 每个 prompt 文件 SHA-256；
- 合并后的 prompt SHA-256；
- schema SHA-256；
- taxonomy version；
- tool schema SHA-256；
- model ID；
- SDK version。

Prompt 规则：

- 不动态注入任意 Skill。
- 不包含五篇 gold paper 的答案。
- 不在 prompt 和代码中维护两份 taxonomy。
- taxonomy 的正式来源是 Python contract；`taxonomy.md` 由 contract 生成或由验证器检查一致性。
- Prompt 修改必须提升版本并重新跑 live gold。

---

# 11. 存储和执行目录

默认工作区：

```text
.tsdaf-workspace/
├── objects/
│   └── sha256/
│       └── ab/
│           └── abcdef...
├── executions/
│   └── exec_<uuid>/
│       ├── manifest.json
│       ├── events.jsonl
│       └── attempts/
├── stage-results/
│   └── <stage-name>/
│       └── <content-key>/
│           └── result.json
├── exports/
│   └── <paper-id>/
│       └── <execution-id>/
│           ├── visual_routing_handoff.json
│           ├── coarse_text_handoff.json
│           └── route_images/
├── locks/
└── index.sqlite
```

## 11.1 内容寻址

stage cache key：

```text
sha256(
    stage_name
    + stage_version
    + canonical_input_hashes
    + normalized_settings_json
    + prompt_hash
    + schema_hash
    + model_profile
)
```

以下任一变化都必须产生新的 key：

- 输入 package；
- canonicalizer；
- prompt；
- schema；
- taxonomy；
-模型 profile；
- stage 实现版本；
-影响语义的配置。

## 11.2 原子写入

所有 artifact：

1. 写入目标目录下的临时文件。
2. flush。
3. fsync。
4. 计算 hash。
5. 使用 `os.replace` 原子替换。
6. 再次验证 hash。
7. 写 seal。
8. 更新 SQLite。

sealed object 不得原位覆盖。

## 11.3 SQLite 定位

SQLite 只是派生索引，不是科学事实的唯一来源。

建议表：

- `papers`
- `packages`
- `executions`
- `execution_events`
- `stage_attempts`
- `artifacts`
- `stage_outputs`
- `routing_summaries`
- `review_receipts`
- `release_receipts`

启用：

- foreign keys；
- WAL；
- 明确 transaction；
- schema migration version。

删除 `index.sqlite` 后，应能通过 immutable manifests 完整重建。

---

# 12. 状态机与错误处理

## 12.1 执行状态

```text
PENDING
RUNNING
SUCCEEDED
REVIEW_REQUIRED
FAILED_RETRYABLE
FAILED_TERMINAL
CACHED
```

允许的主要转移：

- `PENDING -> RUNNING`
- `RUNNING -> SUCCEEDED`
- `RUNNING -> REVIEW_REQUIRED`
- `RUNNING -> FAILED_RETRYABLE`
- `RUNNING -> FAILED_TERMINAL`
- `PENDING -> CACHED`
- `FAILED_RETRYABLE -> RUNNING`，但创建新 attempt

不允许把同一个 attempt 从失败状态改回运行状态。

## 12.2 错误分类

输入错误：

- `PACKAGE_INVALID`
- `ASSET_MISSING`
- `HASH_MISMATCH`
- `CONTEXT_TOO_LARGE`

Provider 错误：

- `PROVIDER_RATE_LIMIT`
- `PROVIDER_TRANSIENT`
- `PROVIDER_REJECTED`

Router 错误：

- `ROUTER_NO_SUBMIT`
- `ROUTER_SCHEMA_INVALID`
- `ROUTER_REFERENCE_INVALID`
- `TOOL_BUDGET_EXHAUSTED`

Triage 错误：

- `VISUAL_VERIFICATION_REQUIRED`
- `VISUAL_VERIFICATION_UNRESOLVED`

物化错误：

- `HANDOFF_INVALID`
- `ARTIFACT_WRITE_FAILED`

## 12.3 重试策略

只自动重试：

- 429；
- 5xx；
- 网络超时；
- provider 短暂连接错误。

规则：

- 尊重 `Retry-After`；
- 最多 3 次 transport retry；
- exponential backoff 加 bounded jitter；
- 每次请求分配 request ID；
- 每次失败均保留记录；
- schema/reference 错误不按 transport retry 处理；
- schema/reference 失败只允许一个新的 medium-thinking semantic attempt；
- 已 sealed 的结果直接复用，不重复产生付费调用。

---

# 13. 安全、隐私和付费调用

CLI 规则：

```text
tsdaf route run <package>
```

默认只做 offline validation/preflight，不产生付费调用。

只有显式执行：

```text
tsdaf route run <package> --live
```

才允许调用 Gemini。

其他安全规则：

- API key 只从环境变量读取。
- 禁止进入配置文件、manifest、日志、exception message。
- 日志自动 redact key、authorization header 和自定义 endpoint。
- `.env` 被 Git 忽略。
- 提供 `.env.example`，只包含变量名。
- CI 永远不运行 `--live`。
- provider request 记录内容 hash 和必要审计数据，但不会把密钥写入 artifact。
- package loader 拒绝 `../`、绝对路径和 symlink path escape。

---

# 14. CLI 设计

第一阶段固定以下用户接口：

```text
tsdaf package validate <package-dir>
tsdaf route run <package-dir> [--live]
tsdaf route resume <execution-id> [--live]
tsdaf run inspect <execution-id>
tsdaf docs check
```

## `package validate`

输出：

- package schema；
- paper ID；
- block 数；
- visual 数；
- asset 总大小；
- hash 校验结果；
- validation errors。

## `route run`

输出：

- execution ID；
- cache hit/miss；
- 当前 stage；
-模型 turn/tool usage；
- terminal status；
- handoff 路径。

## `route resume`

只从最后一个未 sealed stage 继续，不重复已完成的付费请求。

## `run inspect`

展示：

- 输入 package hash；
- prompt/schema/model；
-所有 attempt；
-错误；
-token；
-估算成本；
-耗时；
-输出 artifact；
-review/release 状态。

---

# 15. 文档与项目记忆架构

不建立重复维护的“手工进度看板”，也不移动 work item 文件。

## 15.1 根入口

`AGENTS.md` 控制在新成员两分钟可以读完，包含：

- 项目目标；
- 必读顺序；
- 代码架构边界；
-当前 focus work item；
-测试命令；
-付费调用规则；
-禁止事项；
-状态信息的唯一来源。

## 15.2 核心文档

- `docs/PROJECT_CHARTER.md`：WHY、范围和科学目标。
- `docs/ROADMAP.md`：阶段依赖与长期路线。
- `docs/architecture/`：当前架构事实。
- `docs/decisions/`：不可变 ADR。
- `docs/work/`：任务生命周期。
- `docs/guides/`：开发、运行、发布、安全和 provenance。
- `docs/research/`：论文主线与 Harness Engineering。
- `docs/STATUS.md`：由脚本生成。

## 15.3 初始 ADR

建议第一批 ADR：

- `ADR-0001-single-distribution-multiple-lanes.md`
- `ADR-0002-standard-paper-package-boundary.md`
- `ADR-0003-immutable-artifacts-sqlite-index.md`
- `ADR-0004-local-runner-before-prefect.md`
- `ADR-0005-text-first-router-isolated-visual-triage.md`
- `ADR-0006-immutable-handoff-and-release-receipt.md`
- `ADR-0007-pinned-provider-profile.md`

ADR 只能被新 ADR supersede，不能悄悄重写历史决策。

## 15.4 Work item frontmatter

```yaml
---
id: WI-0001
title: Weak Article Router v1
status: active
focus: true
owner: codex
depends_on: []
acceptance:
  - five-paper gold passes
  - all artifacts sealed
  - no paid calls in CI
---
```

正文必须包含：

- 背景；
-范围；
-非目标；
-计划；
-checkpoint；
-blocker；
-next action；
-验收；
-completion receipt。

`docs/STATUS.md` 从这些 metadata 自动生成，并强制：

- work item ID 唯一；
-最多一个 `focus: true`；
-本地链接有效；
-ADR supersession 合法；
-文件没有超出尺寸预算；
-generated status 没有漂移。

---

# 16. 详细实施任务

以下任务按 TDD 执行。每个生产行为都先建立失败测试，再实现最小代码。

## Task 0：保护旧仓库并建立基线

涉及：

- 当前所有 tracked 文件
- `tests/legacy_harness/`
- `pyproject.toml`
- `.gitignore`

工作：

1. 记录当前 git status、branch、remote 和测试结果。
2. 使用 `git mv` 将旧实验 harness 整体迁移到 `tests/legacy_harness/`。
3. 保留旧 fixture、实验记录和文档。
4. 新 pytest 默认排除 legacy。
5. 增加测试，禁止 `src/tsdaf` 导入 legacy 模块。
6. 增加 `.tsdaf-workspace/`、`.env` 忽略规则。
7. 不删除旧历史，不执行自动 commit。

验收：

- Git 能识别为 rename。
- legacy 资产完整。
- production import graph 不包含 legacy。
- 当前旧测试可作为独立 legacy suite 执行。

## Task 1：项目文档和 ADR 基础

新增：

- `AGENTS.md`
- `docs/PROJECT_CHARTER.md`
- `docs/ROADMAP.md`
- `docs/work/WI-0001-weak-article-router-v1.md`
- 七个初始 ADR
- `scripts/validate_docs.py`
- `scripts/generate_status.py`

测试：

- 重复 work item ID 失败。
- 0 个或多个 focus 时失败。
- 本地断链失败。
- 修改 work item 后未更新 STATUS 时失败。
- ADR supersession 指向不存在 ID 时失败。

验收：

```text
tsdaf docs check
```

完全通过。

## Task 2：Python 包和依赖边界

新增：

- `src/tsdaf/`
- CLI entrypoint
- Ruff、mypy、pytest 配置
- dependency extras

建议 extras：

```toml
router = ["google-genai", "pydantic", "pydantic-settings"]
image = []
fusion = []
dev = ["pytest", "pytest-cov", "ruff", "mypy"]
```

Python：

```text
>=3.11,<3.14
```

测试：

- Python 3.11 和 3.13。
- CLI 可启动。
- `contracts` 不包含 SDK import。
- `apps` 之外不读取环境变量。
- import cycle 检查通过。

Task 2 同时拥有首次 active-repository Ruff baseline：按 `line-length = 100`、`select = ["B", "E", "F", "I", "UP"]` 清零非 legacy Python 的 lint/format 问题。该工作只允许机械等价整理，必须以 Task 1 全量回归和确定性文档输出证明没有行为变化。

## Task 3：核心 contracts

实现：

- `ArtifactRef`
- `SourceLocator`
- canonical package
- article taxonomy
- visual taxonomy
- route groups
- text roles
- handoff
- receipt
- stage result
- typed failure

重点测试：

- strict model 禁止未知字段。
- enum 序列化稳定。
- schema JSON hash 稳定。
- handoff 不允许 crop/SMILES 等越权字段。
- receipt 必须绑定 handoff hash。
- job 无 release receipt 时构造失败。

## Task 4：五篇自包含 fixture

每篇 fixture 包含：

- origin PDF；
- normalized document；
- blocks；
-被引用图片；
- manifest；
-hash 清单。

目标约 16 MB。

测试：

- fixture 不依赖工作区外部路径。
- 所有 manifest hash 正确。
- 从不同目录复制后仍可加载。
- 修改一个字节能够触发 `HASH_MISMATCH`。
- 路径逃逸被拒绝。

## Task 5：Canonicalization

实现：

- package loader；
-stable ID；
-clean view；
-section map；
-caption linker；
-visual group；
-decorative asset suppression；
-search index。

重点测试：

- 同 package 多次运行 ID 相同。
- 不同 execution ID 不影响 canonical ID。
- suppression 可逆。
- References 不进入默认正文上下文。
- Figure/Scheme caption 可跨相邻 block 恢复。
- 一个 Scheme label 可以绑定多个 visual member。
- 不确定 caption association 不被强制确认。
- ACS 小图标被标记为 decorative。
- 小尺寸分子结构图不能仅因尺寸小而删除。
- 五篇 fixture 中所有 37 个 asset reference 均进入 inventory。

## Task 6：ArtifactStore、SQLite 和 Runner

实现：

- 内容寻址文件存储；
-原子写；
-seal；
-run manifest；
-event log；
-SQLite index；
-content-key cache；
-cross-platform lock；
-resume。

测试：

- 相同 content key 只执行一次。
- 并发请求不能生成两份不一致结果。
-中途失败不产生 sealed artifact。
- artifact 被修改后 verify 失败。
-删除 SQLite 后能从 manifest 重建。
-resume 不重复已成功 stage。
-失败 attempt 不会被原位改写。

## Task 7：Gemini adapter

实现：

- official SDK client；
-Interactions API 2.x request 与 `store=False` stateless continuation；
-tool schema serializer；
-response normalization；
-usage/timing；
-error mapping；
-redaction；
-transport retry。

离线测试全部使用 recorded provider fixture，不调用网络。

测试：

- Gemini SDK 类型不会进入内层 contract。
- 429 尊重 Retry-After。
- 5xx 最多重试三次。
- 400/provider rejected 不做盲目重试。
- API key 不出现在日志和 artifact。
- `store=False` 永远存在。
-非固定模型配置启动失败。

## Task 8：Main Router Harness

实现：

- context compiler；
-token preflight；
-tool loop；
-search/read tools；
-submit tool；
-turn/tool budget；
-prompt loader；
-output validator；
-medium-thinking second attempt。

测试：

- 主 Router 请求不包含图片。
- 完整 visual inventory 被包含。
- 不允许静默截断。
- `search_blocks` limit 上限生效。
- `read_blocks` block 数和邻居数上限生效。
- 超预算后终止。
- 未 submit 返回 `ROUTER_NO_SUBMIT`。
-未知 block/visual ID 返回 `ROUTER_REFERENCE_INVALID`。
-重复 visual disposition 被拒绝。
-漏掉任一 visual 被拒绝。
- route group 不闭合被拒绝。
- valid semantic output 不被 host 改写。

## Task 9：Visual Triage

实现：

-只收集 `NEEDS_VISUAL_VERIFICATION`；
-按最多 4 张分 batch；
-最小化 context；
-medium media resolution；
-unresolved 转 review。

测试：

-无 flagged visual 时零 triage 调用。
-普通 visual 不进入 triage。
-每批最多四张。
-triage request 不含全文。
-不允许输出 crop、SMILES 或 reaction。
-图片读取和 base64 编码不改变源 hash。
-unresolved 结果进入 `REVIEW_REQUIRED`。

## Task 10：Handoff builder

实现：

- host 派生 forward-route selection；
-构建 route group order；
-复制精确原始段落；
-导出完整图片资产；
-生成 PROPOSED handoff；
-验证 release gate。

测试：

-只有 `FORWARD_ROUTE` 进入 `RouteImageItem`。
-每个 RouteImageItem 指向原始资产 hash。
-不产生 crop。
-每组至少一个 `ROUTE_CORE`。
-Coarse text 等于 canonical raw text。
-`NOT_IN_SCOPE` 不产生 route image。
-无 receipt 时不能创建 image job。
-receipt hash 不匹配时不能创建 job。

## Task 11：CLI、观察性与文档

实现固定 CLI 和结构化日志。

每次执行记录：

- execution/attempt ID；
-stage；
-cache；
-模型、prompt、schema；
-turn/tool count；
-input/output tokens；
-视觉 token；
-延迟；
-估算费用；
-错误类型；
-artifact hash。

成本只能标记为 estimate，并关联 pricing snapshot version，不能假设价格永久不变。

## Task 12：五篇 live gold 验证

五篇论文预期：

- 全部为 `TOTAL_SYNTHESIS`
- Alchivemycin A 包含 `CHEMOENZYMATIC`
- 正好选择 9 个完整图片资产

Gold 图片：

- Alchivemycin A：Fig. 3、Fig. 4、完整 Fig. 5
- Hyperfirin：Scheme 2 route asset、Scheme 3 route asset
- Ineleganolide：Scheme 1
- Kalmanol：Scheme 2、Scheme 3
- Principinol B：Scheme 2

要求：

- 不选择 retrosynthesis。
- 不选择纯 mechanism。
- 不选择优化表。
- 不选择 failed-only route。
- 不选择背景图、assay、icon、cover。
- visual escalation 为 0。
-每个路线组至少一个 `ROUTE_CORE`。
-所有输出图片 hash 与源资产一致。
-完整保存交互、工具、usage、timing 和 provenance。

在第一次 live run 前，先冻结人工 gold seal，避免用模型结果反向修改答案。

---

# 17. 测试分层

## 单元测试

覆盖：

- contract；
-ID；
-cleaning；
-caption stitching；
-search；
-validator；
-state transition；
-cache key；
-error mapping。

要求快速、无网络、无付费调用。

## Contract 测试

覆盖：

- Pydantic schema；
-prompt/tool schema 一致性；
-adapter boundary；
-handoff/release/job 兼容性。

## 集成测试

覆盖：

- package → canonicalization → fake Router → handoff；
-失败恢复；
-artifact seal；
-SQLite rebuild；
-CLI。

## Live 测试

放在 `tests/live/`：

-默认 skip；
-必须具备 API key；
-必须显式传入 live 标记；
-CI 永不执行。

## 文档测试

覆盖：

-断链；
-metadata；
-STATUS 漂移；
-ADR；
-唯一 focus；
-文件尺寸。

## CI gate

建议：

```text
ruff check
ruff format --check
mypy src/tsdaf
pytest tests/unit tests/integration
tsdaf docs check
```

Python matrix：

- 3.11
- 3.13

---

# 18. 第一阶段完成标准

只有同时满足以下条件，Slice 1 才算完成：

1. 新 `tsdaf` 架构建立，legacy 完整隔离。
2. 五篇 fixture 可在任意目录离线加载。
3. package、canonical、routing、handoff contract 固定。
4. 所有 37 个 visual asset 都被且仅被分类一次。
5. Gold 中正好选出 9 张正向路线图。
6. 所有路线图保持完整 MinerU 原图 hash。
7. Visual Triage escalation 为 0。
8. 每个 route group 有 `ROUTE_CORE`。
9. 没有 silent truncation。
10. 支持 cache、resume、retry 和 seal。
11. 删除 SQLite 后可以重建。
12. CI 无付费调用。
13. API key 不进入日志、artifact 或 Git。
14. `docs check` 通过。
15. 人工 gold seal 和 live execution receipt 均归档。
16. 旧项目没有被生产模块导入。
17. 未经明确授权不自动 commit。

---

# 19. 后续路线图

## Slice 2：Review + Gold-200

- Review UI/API；
-人工修订 handoff；
-冲突和 adjudication；
-Gold-200；
-误差分类；
-caption/routing prompt 校准；
-review receipt 正式生产化。

## Slice 3：Strong Text Packaging

-路线级文本包；
-反应叙事边界；
-段落检索；
-路线步骤候选；
-与图片 route group 对齐。

## Slice 4：Supporting Information Lane

- SI package；
-实验步骤；
-化合物编号；
-分析数据；
-main/SI entity linking；
-来源和证据链。

## Slice 5：规模化运行

- Prefect adapter；
-独立 image worker；
-YOLO → MolDet → OCSR；
-Gold-100 blind；
-前 100 篇成本与延迟审计；
-1k canary；
-10k rollout。

放量停止规则：

- 前 100 篇 visual escalation 目标 `<=5%`
- 若 `>10%`，停止 rollout
- 先修 canonicalization、caption linking、prompt 或 taxonomy
- Gold-100 blind 通过前，只允许 `HUMAN_APPROVED`
- 通过后才能引入受限 `POLICY_APPROVED`

这版计划的核心变化，是把“调用一次模型完成任务”提升为一个有清晰领域边界、不可变数据契约、可审计运行记录、受控模型循环和下游发布门禁的生产系统。它既能支撑当前五篇验证，也不会在扩大到 1k、10k 文献时推翻重做。

## Work Item Record

### Background

The repository is establishing Slice 1 as an auditable weak-article routing system while preserving this plan as the authoritative implementation body.

### Scope

Slice 1 covers the normalized-package boundary, canonicalization, text-first routing, isolated visual triage, immutable artifacts and handoffs, local execution, receipts, and offline validation.

### Non-goals

This work item does not authorize PDF or raw MinerU ZIP parsing, image cropping, panel localization, SMILES extraction, paid calls in CI, or unreceipted image jobs.

### Plan

Execute Tasks 0 through 12 in dependency order using RED, expected failure, GREEN, verification, and review checkpoints.

### Checkpoint

Tasks 0 through 11 are complete，Task 12 的离线 gold/安全门禁也已通过。Task 2 checker 仍是冻结工具债务，Task 3 contracts/schema 保持冻结。五篇自包含 fixture、canonical package、immutable runtime、recorded-compatible Gemini 边界、text-first Router、flagged-only Visual Triage、两个 `PROPOSED` handoff、release hash gate 和五个默认离线安全 CLI 命令已经形成完整生产骨架。人工 gold 已在任何预测前冻结为 SHA-256 `6af5e09151bd2b904ceefc28337655d5e14e28188824bc6c033f3801b22705af`。第二套远程 API 的三次首篇执行均已安全封存失败且没有读取 gold 或生成 receipt；其后的原 probe 与一次明确授权重试又都以 `PROVIDER_TRANSIENT` 未通过 exact-schema 门禁。用户已暂停 API/live 工作；生产代码和测试未因 probe 改变。

### Blockers

The third provider run repeated the same schema-invention root even though the exact contract schema and submit `strict: true` reached the Chat wire. Both second-API no-paper exact-schema invocations then terminated with safe `PROVIDER_TRANSIENT` before returning normalized output. The user identifies this as an API issue and has paused further live work.

### Next action

保留并推送当前工程状态；Task 12 live 保持暂停，待用户未来确认 endpoint/provider/model 可用后再单独恢复。恢复前不进入 response-format TDD、不重试 API、不运行论文；frozen gold/contracts/fixtures、prompt、adapter、Router 测试和 `5/6` 预算保持不变。

### Acceptance

Slice 1 requires the five-paper gold set to pass, all artifacts to be sealed, and CI to make no paid calls.

### Completion receipt

Pending.
