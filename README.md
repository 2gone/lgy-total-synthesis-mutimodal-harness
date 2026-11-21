# TS-DAF Harness

TS-DAF 是一个小而逻辑清晰的总合成研究证据 Harness。复杂性应来自科学数据、溯源、不可变证据与可恢复性，而不是内部工具链或推测性的框架。

## 语料流程：协作者从这里开始

2026 年 10 月跑全合成文献语料用的整套流程在 [`research/`](research/README_ZH.md)，包括导入、前处理、v4 提示词的全链和统计，可以直接复现或拿来测试网关。输入是文献标准包集合（原始 PDF 加 MinerU 分块），只要配一个 OpenAI 兼容网关，就能一条命令跑完：

```bash
python -m venv .venv && .venv/bin/pip install -r research/requirements.txt
export OPENAI_BASE_URL=<网关地址>  OPENAI_API_KEY=<密钥>
.venv/bin/python research/problem-profile-20261009/runner/run_pipeline.py --packages <标准包集合> --name test1 --model <模型名> --papers-at-once 4 --budget 60 --hard-cap 70
```

输入格式、并发设置、输出位置和常见问题见 [research/README_ZH.md](research/README_ZH.md)。这套流程是研究用的，不是下文的生产工作流。

## 共同原则

- 合同和 schema 只为真实消费者而存在；每条规则只有一个事实源，Host 校验与 provider 可见 schema 必须一致，绝不静默改写无效模型结果。
- 原始 PDF 与原始源资产是不可变证据；规范化产物是派生物，拥有自己的 hash，不能宣称与源文件字节相同。
- 内容寻址产物、seal、event 和 manifest 才是真相；SQLite 只是可在删除后重建的派生投影。
- 默认命令和 CI 均离线。live 调用必须有明确授权；凭据只经进程环境进入，绝不进入日志、产物或文档。
- commit、push 等 Git 写操作必须由用户明确授权；具体执行约束见 [AGENTS.md](AGENTS.md)。
- Task 2 import checker 是冻结的工具债务，不是生产依赖、架构示例或 Python 解释器项目。
- 新 abstraction 至少要有两个当前消费者；若两轮 review/fix 暴露同一根因，停止打补丁并请求 scope/design adjudication。

## 两条生产工作流

文章路由是文本优先的：Main Router 不接收图像字节，只有显式标记的资产进入隔离的 visual triage。其输入是自包含的规范化 TS-DAF paper package，不是 PDF 或原始 MinerU ZIP；`route` CLI 只产出两个 `PROPOSED` handoff，既不签发 release receipt，也不创建 image job。详见[文章路由工作流](docs/guides/ARTICLE_ROUTING_WORKFLOW_ZH.md)。

图像处理只消费已授权的 `ImageProcessingJob` 与原始 image package，不消费 PDF/MinerU package。crop、SMILES 与 detector 细节都留在 image-lane 私有模型中，不扩张冻结的全局文档合同。详见[图像处理工作流](docs/guides/IMAGE_PROCESSING_WORKFLOW_ZH.md)。

两者的严格衔接是：handoff content hash、release receipt 和 image job 必须绑定；没有有效 release receipt 就没有 image job。

## 已验证状态

- 五个固定 fixture 与冻结的人类 gold 已就绪；五篇论文的首次本地、显式 live 验证为 `1 passed in 260.30s`。
- 全部 37 个 visual decision 与 gold 相同，零 escalation；九个原始资产的精确分布为 `3/2/1/2/1`。
- receipt artifact SHA-256：`05c3b902035008218c1f1fec722a7db9e4dc5df801b1facc303a082af42a5019`。
- 图像处理的已接受证据仅限 unit/fake/recorded；尚无已接受的真实权重/GPU run，也无已接受的 live Gemini OCSR canary。文章五篇 live 并不验证 image OCSR。

## 快速开始（离线）

```powershell
python -m pytest -q
python -m tsdaf docs check
```

常用 CLI、离线/live 边界和恢复行为见 [CLI guide](docs/guides/CLI.md)。`docs/STATUS.md` 是生成文件，只能由生成器更新。

可选的本地网页操作层见 [Web 工作台指南](docs/guides/WEB_WORKBENCH_ZH.md)。它调用现有 CLI，
不重新实现解析逻辑，也不把原始 PDF/MinerU ZIP 当作生产输入。

## 阅读地图

1. 协作者和其 agent 先读 [AGENTS.md](AGENTS.md)。
2. 读[中文代码库指南](docs/guides/CODEBASE_GUIDE_ZH.md)了解目录与调用边界。
3. 按任务选择[文章路由](docs/guides/ARTICLE_ROUTING_WORKFLOW_ZH.md)或[图像处理](docs/guides/IMAGE_PROCESSING_WORKFLOW_ZH.md)，再查 CLI 与 [live gold](docs/guides/LIVE_GOLD.md)。
4. 治理范围与后续门槛见 [roadmap](docs/ROADMAP.md) 和当前 work item。

[`tests/legacy_harness/`](tests/legacy_harness/) 是保存的历史实验测试项目，拥有独立配置、工具、数据和测试；它不是生产依赖，也不是生产 Python 解释器项目。
