# TS-DAF 主线提升实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Task1–12 弱文章路由与 image processing v1 连同准确的中文协作说明快进到 `main`，同时保持旧 Harness 可独立测试。

**Architecture:** 不迁移或重写生产代码；当前 image 分支已经线性包含 `main` 和 Task1–12。先补齐协作文档并验证 feature 分支，再在现有 main worktree 中执行 `--ff-only` 快进、复验和普通 push。

**Tech Stack:** Python 3.12、Pydantic、Pillow、pytest、Ruff、Mypy、Git、PowerShell。

## Global Constraints

- 不重写 `653f051`、`348cf50` 或远端 `main` 历史，不 rebase、不 squash、不 force-push。
- 旧 Harness 完整保留在 `tests/legacy_harness/`，生产代码不得导入它。
- 不修改冻结的全局合同、schema、Router prompt 或五篇人工 gold。
- Task 2 import checker 保持冻结，不扩展其语言语义。
- 默认测试和 CI 保持离线；本计划不授权付费 API、真实 MolYOLO 权重或 GPU 调用。
- 不提交或 push 真实 `.env`、API 凭据、`MolYOLO.pt` 或 `.vendor/` runtime。
- `docs/STATUS.md` 是生成文件，不手工编辑。

---

### Task 1: 中文协作者代码导览与主线文档校正

**Files:**
- Create: `docs/guides/CODEBASE_GUIDE_ZH.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/architecture/SYSTEM_OVERVIEW.md`
- Modify: `docs/PROJECT_CHARTER.md`
- Modify: `docs/ROADMAP.md`
- Modify: `CHANGELOG.md`
- Test: `scripts/validate_docs.py`

**Interfaces:**
- Consumes: 已批准的 `docs/superpowers/specs/2026-08-27-mainline-promotion-and-collaboration-design.md`，现有 `src/tsdaf` 分层和 CLI。
- Produces: 一个中文协作入口，准确区分生产、legacy、离线已验证能力和 future-live 门禁。

- [ ] **Step 1: 记录当前文档矛盾基线**

Run:

```powershell
rg -n "five fixed commands|does not.*crop|independent image workers|only current focus" README.md AGENTS.md docs
```

Expected: 命中旧的 Slice 1/五命令/图片 worker 尚未来到等陈述，证明需要文档校正。

- [ ] **Step 2: 编写中文代码导览**

`docs/guides/CODEBASE_GUIDE_ZH.md` 必须包含以下固定章节：

```markdown
# TS-DAF 协作者代码导览
## 先看结论
## 仓库目录与 legacy 边界
## 两个生产工作流
## 代码分层和关键入口
## 合同、私有模型和数据衔接
## 外部 MolYOLO 与 Gemini 配置
## Task 2 历史债务
## 当前 API 测试状态
## 测试命令
## 下一步：绑定与 Image Package
```

其中必须写明 Task 12 live 未完成、image v1 未完成真实权重/GPU/远程 OCSR 验证，以及
`CLI -> command -> composition -> runtime -> lane/adapter` 的调用方向。

- [ ] **Step 3: 校正根入口和架构文档**

实现以下精确调整：

```text
README.md                  生产 TS-DAF 身份、六个 CLI、中文导览、legacy 独立命令
AGENTS.md                  把中文导览加入 required reading；列出两个已实现工作流
SYSTEM_OVERVIEW.md         增加 release-gated image-processing v1，不宣称 binding 已实现
PROJECT_CHARTER.md         从 non-goals 移除 crop/SMILES 的过时绝对表述，改为 binding/final package 非目标
ROADMAP.md                 标记 image worker 基础已经前置落地，后续仍需 binding/review/scaling
CHANGELOG.md               记录 Task1–12、image v1 和 legacy relocation
```

- [ ] **Step 4: 验证文档治理和敏感信息边界**

Run:

```powershell
python -m tsdaf docs check
python scripts/generate_status.py --check
git diff --check
git diff --name-only | rg "(^|/)\.env($|\.)|MolYOLO\.pt|(^|/)\.vendor/"
```

Expected: 前三条退出 0；最后一条无输出且退出 1，表示没有敏感路径进入变更。

- [ ] **Step 5: 提交协作文档**

```powershell
git add README.md AGENTS.md CHANGELOG.md docs/guides/CODEBASE_GUIDE_ZH.md docs/architecture/SYSTEM_OVERVIEW.md docs/PROJECT_CHARTER.md docs/ROADMAP.md docs/superpowers/plans/2026-08-27-mainline-promotion.md
git commit -m "docs: add TS-DAF collaborator guide"
```

### Task 2: 提升前双工作流与 legacy 验证

**Files:**
- Verify: `src/tsdaf/**`
- Verify: `tests/unit/**`
- Verify: `tests/integration/**`
- Verify: `tests/live/**`
- Verify: `tests/legacy_harness/**`

**Interfaces:**
- Consumes: Task 1 文档提交和当前生产代码。
- Produces: feature 分支可以进入 `main` 的离线证据；不会产生 live/付费结果。

- [ ] **Step 1: 验证 Task1–12 弱文章工作流**

Run:

```powershell
python -m pytest -q tests/unit/routing tests/unit/document tests/integration/routing tests/integration/test_cli_workflow.py tests/integration/test_five_paper_canonicalization.py tests/integration/test_paper_fixtures.py
```

Expected: 全部通过，覆盖五篇 fixture、Router、visual triage、handoff、cache/resume 和 CLI。

- [ ] **Step 2: 验证 image processing v1**

Run:

```powershell
python -m pytest -q tests/unit/image tests/unit/adapters/test_molyolo_v10.py tests/unit/adapters/test_gemini_interactions.py tests/unit/workflow/test_image_processing_runtime.py tests/integration/image tests/integration/test_cli.py
```

Expected: 全部通过；不加载真实权重，不访问远程 API。

- [ ] **Step 3: 验证 live 安全门默认关闭**

Run:

```powershell
python -m pytest -q tests/live
```

Expected: live gold 测试被跳过；没有付费调用。

- [ ] **Step 4: 验证旧 Harness 可独立运行**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path 'tests/legacy_harness').Path
python -m pytest -q -c tests/legacy_harness/pyproject.toml tests/legacy_harness/tests
$legacyTestExit = $LASTEXITCODE
Remove-Item Env:PYTHONPATH
exit $legacyTestExit
```

Expected: 旧 Harness 全部测试通过，且环境变量在命令结束前清理。

- [ ] **Step 5: 运行 active 全量门禁**

Run:

```powershell
python -m pytest -q
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src/tsdaf
python scripts/check_import_boundaries.py src/tsdaf
python -m tsdaf docs check
git diff --check
```

Expected: 每条命令退出 0。

### Task 3: 推送 feature 分支并严格快进本地 main

**Files:**
- Update Git ref: `origin/codex/image-processing-v1`
- Update Git ref: local `main`

**Interfaces:**
- Consumes: Task 2 全绿的 feature commit。
- Produces: 本地 main 与 feature 完全一致，但尚不推送远端 main。

- [ ] **Step 1: 复核两个 worktree 与远端基线**

Run:

```powershell
git status --short --branch
git -C 'D:/Chengrui Wei/全合成数据库/2026-08-24/total-synthesis-multimodal-harness' status --short --branch
git ls-remote --heads origin main codex/image-processing-v1
git merge-base --is-ancestor origin/main HEAD
```

Expected: 两个 worktree 均无未提交文件；远端 main 仍是本地 main 的对应提交；祖先检查退出 0。

- [ ] **Step 2: 推送完整 feature 分支**

Run:

```powershell
git push origin codex/image-processing-v1
git rev-parse HEAD
git ls-remote --heads origin codex/image-processing-v1
```

Expected: 本地 HEAD 与远端 feature SHA 完全一致。

- [ ] **Step 3: 在现有 main worktree 中执行严格快进**

Run:

```powershell
git -C 'D:/Chengrui Wei/全合成数据库/2026-08-24/total-synthesis-multimodal-harness' fetch origin
git -C 'D:/Chengrui Wei/全合成数据库/2026-08-24/total-synthesis-multimodal-harness' merge --ff-only codex/image-processing-v1
```

Expected: fast-forward 成功，无 merge commit、冲突或历史重写。

### Task 4: main 复验、敏感文件审计与普通 push

**Files:**
- Verify and push Git ref: `main`

**Interfaces:**
- Consumes: Task 3 快进后的本地 main。
- Produces: 经复验的 `origin/main`，保留 Task1–12、image v1 和协作文档提交历史。

- [ ] **Step 1: 从 main worktree 重跑 active 全量验证**

Run in `D:/Chengrui Wei/全合成数据库/2026-08-24/total-synthesis-multimodal-harness`:

```powershell
python -m pytest -q
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src/tsdaf
python scripts/check_import_boundaries.py src/tsdaf
python -m tsdaf docs check
git diff --check
```

Expected: 每条命令退出 0。

- [ ] **Step 2: 从 main worktree 重跑旧 Harness 测试**

```powershell
$env:PYTHONPATH = (Resolve-Path 'tests/legacy_harness').Path
python -m pytest -q -c tests/legacy_harness/pyproject.toml tests/legacy_harness/tests
$legacyMainExit = $LASTEXITCODE
Remove-Item Env:PYTHONPATH
exit $legacyMainExit
```

Expected: 全部通过。

- [ ] **Step 3: 审计 main 提交树中的敏感路径**

```powershell
$sensitiveMainPaths = git diff-tree --no-commit-id --name-only -r HEAD | Where-Object { $_ -match '(^|/)\.env($|\.)|(^|/)\.vendor(/|$)|MolYOLO\.pt$' }
if ($sensitiveMainPaths) { $sensitiveMainPaths; exit 1 }
Write-Output 'MAIN_SENSITIVE_PATHS=0'
```

Expected: `MAIN_SENSITIVE_PATHS=0`。

- [ ] **Step 4: 正常 push main 并核对远端**

```powershell
git push origin main
git rev-parse main
git ls-remote --heads origin main
git log --graph --decorate --oneline --max-count=8 main
```

Expected: 本地 main 与 `origin/main` SHA 完全一致；历史中依次保留 `653f051`、`348cf50` 和协作文档提交。
