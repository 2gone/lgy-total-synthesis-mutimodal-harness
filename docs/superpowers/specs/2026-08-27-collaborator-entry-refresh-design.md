# TS-DAF 协作者入口与双工作流文档设计

## 目标

让第一次进入仓库的人类协作者及其智能体无需回读聊天记录，就能在几分钟内理解 TS-DAF 的
科学目标、两条生产工作流、不可破坏的领域原则、当前验证状态、代码入口和安全测试方式。

## 当前事实

仓库同时包含 Task1–12 弱文章路由和 image processing v1。文章路由的五篇 first-local live
gold 已通过；image processing v1 已通过离线 fake/recorded 测试，但真实 MolYOLO 权重/GPU
和 live Gemini OCSR 仍未验收。现有中文代码导览仍把 Task 12 描述为暂停，CLI 与 live-gold
指南也没有反映文章路由已经允许受限 loopback HTTP。

## 选定组织方式

采用分层单一事实源，不把完整细节复制到每个文件：

1. 根 `README.md` 是项目入口，说明原则、两条链路、当前能力和最短验证命令。
2. `AGENTS.md` 是智能体执行约束，规定阅读顺序、边界、停止条件和提交规则。
3. `docs/guides/ARTICLE_ROUTING_WORKFLOW_ZH.md` 解释文章路由的输入、输出、运行链、领域不变量、
   live 证据和非目标。
4. `docs/guides/IMAGE_PROCESSING_WORKFLOW_ZH.md` 解释授权 image job 到 MolYOLO、crop 和单 crop
   OCSR 的链路、lane-private 数据和未完成边界。
5. `docs/guides/CODEBASE_GUIDE_ZH.md` 继续承担代码地图责任，并链接两份聚焦指南。
6. `CLI.md`、`LIVE_GOLD.md`、`ROADMAP.md` 和 `CHANGELOG.md` 只记录各自直接负责的事实。

历史 spec/plan 和 `tests/legacy_harness/**` README 是时间点证据，不因当前状态变化而改写。

## 必须显著表达的原则

- 这是逻辑清晰的小型 Harness；复杂度必须来自科学数据、provenance、不可变性和恢复，而不是
  内部工具或没有消费者的通用框架。
- 合同和 schema 只服务真实消费者；同一规则必须有单一事实源，Host 校验与 provider 可见
  schema 同时存在，但不得靠 Host 静默改写模型结果。
- 原始 PDF 和原始资产字节不可改写；normalized artifacts 是派生物，分别记录 hash。
- content-addressed artifact、seal、event 和 manifest 是事实；SQLite 只是可删除重建的派生索引。
- Main Router 文本优先，不读取图片字节；只有 flagged asset 进入隔离 visual triage。
- handoff 内容 hash、release receipt 和 image job 必须严格绑定；无有效 receipt 不创建 image job。
- crop、SMILES 和 detector 细节属于 image lane 私有模型，不回灌冻结的全局文档合同。
- 默认离线、CI 无付费调用；live 必须显式授权，凭据只从进程环境注入且不得进入证据。
- Task 2 checker 是冻结工具债务，不是生产依赖、架构范例或可继续扩展的 Python 解释器。
- 新抽象至少需要两个当前消费者；连续两轮同根因失败时停止补丁并做范围裁决。

## 验证与交付

两条工作流由不同只读 reviewer 检查入口、边界和状态陈述。实施后运行 active 全量测试、
Ruff、format、Mypy、import-boundary、文档治理、legacy 独立测试和敏感路径审计。只有全部
通过且 `main` 可严格快进时，才正常 push；禁止 force-push、历史重写和提交秘密、权重或
vendored runtime。
