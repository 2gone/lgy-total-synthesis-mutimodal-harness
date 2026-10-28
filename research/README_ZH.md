# 全合成文献语料流程（2026-10 版）

这里是 2026 年 10 月跑全合成语料时实际使用的整套流程，包括 v4 提示词、调用转接层和调度器，可以直接拿来复现，也可以拿来测试网关。它是研究用的语料流程，不是 `src/tsdaf` 里的生产工作流：它带一份自己的框架代码快照，不受生产工作流的分层约束。

## 快速上手

需要：Python 3.12 或更高版本，以及一个 OpenAI 兼容接口的网关地址和密钥。

```bash
python -m venv .venv
.venv/bin/pip install -r research/requirements.txt
```

凭据只通过环境变量传给程序。代码不读 `.env`，也不会把凭据写进任何文件或日志：

```bash
export OPENAI_BASE_URL=https://<网关地址>/v1
export OPENAI_API_KEY=<密钥>
```

把文献标准包集合（格式见下文“输入”）放在任意目录，一条命令跑完整个流程：

```bash
.venv/bin/python research/problem-profile-20261009/runner/run_pipeline.py --packages <标准包集合目录> --name test1 --model gemini-3.8-flash-high --papers-at-once 4 --budget 60 --hard-cap 70
```

中途停了，用同一条命令重跑即可：已经完成的论文和阶段不会再跑。

## 流程

`run_pipeline.py` 依次执行四步，每步也可以用 `--only` 单独跑：

| 步骤 | 工具 | 做什么 | 并发 |
|---|---|---|---|
| 1 导入 `import` | `tools/import_ready200.py` | 每篇的正文标准包放到 `tests/fixtures/paper_packages/<论文>`；全部 SI 依次拼接、正文接在最后，拼成一份“实验文档”放到 `tests/fixtures/si_packages/<论文>`（重复的副本自动去掉） | 不调模型 |
| 2 前处理 `front` | `tools/batch_run.py` | 依次三步：读图 `prepare`（从 PDF 页面切出图，视觉模型逐图转写编号、箭头、条件、表格），页面归属 `owners`（没有成段文字的页面，例如谱图页，判断属于哪个化合物），SI 文本核定 `prestage`（解析文字和 PDF 文字层对不上的地方，切出原文行让模型核对） | 每次 `--papers-at-once` 篇，每篇最多 16 个调用 |
| 3 全链 `chain` | `runner/gm_corpus.py` | 每篇依次：切分实验条目，逐条写实验记录（动作、用量、结果），写论文级记录（目标、路线、总产率），组装，绑定结构 | 在途调用不超过 `--budget` |
| 4 统计 `stats` | `runner/corpus_stats.py` | 按步骤和按论文统计调用次数、token（输入、缓存命中、答案、思考）和用时 | 不调模型 |

前处理每一步结束时会检查失败情况：如果网关拒掉的调用超过一成（SI 文本核定这一步看未读的位置是否超过四分之一），这一步不算完成，等 15 分钟乘以已尝试次数后自动重试。内容过滤造成的拒绝不计入，因为重试也一样会被拒。

## 输入：文献标准包集合

```
<集合>/
  INDEX.csv                  每篇一行：position, legacy_rank, paper_id, doi, title, year, main_sha256, main_pages, package_path（相对集合目录）
  packages/paper_NNN/
    article.json             来源、哈希、SI 清单
    main/                    正文标准包（tsdaf.paper-package.v1：source/paper.pdf、normalized/document.json、normalized/blocks.jsonl、images/）
    si_001/ ...              每份 SI 一个标准包，结构同上
  SHA256SUMS
```

标准包是原始 PDF 加上 MinerU 解析出的分块和图片，不含任何前处理结果。`paper_id` 就是论文在运行中的名字。

## 输出

都写在 `research/problem-profile-20261009/` 下，并且已被 `.gitignore` 排除：

| 位置 | 内容 |
|---|---|
| `whole-paper/chain-r<名字>-<论文>/result.json` | 一篇的完整结果：实验记录、论文级记录、表征记录、作者分歧、未决项、结构绑定 |
| `whole-paper/chain-r<名字>-status.json` | 全链账本：每篇的切分、写记录、补跑用时和状态 |
| `batch/<名字>/status.json` | 前处理账本：每篇每步的状态、用时、失败原因 |
| `thoughts/*.jsonl` | 每次模型调用一行：用时、步骤、思考强度、思考文本、答案、用量；网关返回空响应时也记一行，并附上网关的原始报错 |
| `stats-<名字>.json` | 第 4 步的统计结果 |

## 模型和网关

- **网关要求：** OpenAI 兼容的 chat completions 接口，支持 `reasoning_effort`、图片输入（data URL），能返回 `json_object` 格式。
- **模型名：** 全链用 `--model`，前处理的读图用 `--vision-model`，不给时和 `--model` 一样。网关的模型名可能不同，例如有的叫 `gemini-3.8-flash`，没有 `-high` 后缀。
- **思考强度：** 每一步用什么思考强度，只写在 `tools/run_config.json` 一处，由程序通过 `reasoning_effort` 传给网关。
- **结构化输出和提示词：** 由 `runner/shim/sitecustomize.py` 处理。它把 JSON Schema 约束转成提示词加校验，套用 `runner/prompts/` 里的 v4 提示词改写，并记录每次调用的思考。全链和前处理都通过 `PYTHONPATH` 加载它，不需要手动设置。
- **token 计数：** 各网关的口径不同。有的 `completion_tokens` 已经包含思考，有的把思考单独记在 `reasoning_tokens` 里。统计脚本会按 `total_tokens` 自动判断是哪一种。

## 并发怎么定

- **前处理：** 每篇最多 16 个调用，所以在途调用不超过 16 乘以 `--papers-at-once`。
- **全链：** 在途调用严格不超过 `--budget`。每篇在切分时占 1 个，写记录时占“条目数加 1”个，单篇最多 `--max-parallel` 个条目同时写。`--hard-cap` 是硬上限，`--budget` 不能超过它。
- **网关的冷却：** 账号池类网关在高并发下会冷却，冷却期间调用会失败，表现为 429 或报“冷却中”。流程会自动重试，账本里不会把失败的步骤标成完成。参考 2026-10-28 的测试：一个账号池网关在在途约 60 时，40 分钟后进入冷却，冷却时间逐次变长。

## 可选：分子检测器

检测器给图里的每个结构式画框，后面的结构绑定按框旁边的编号把结构对到化合物。没有检测器时，图照样读，只是不画框，结构绑定为空。它不影响对网关的调用量。

如果要用，准备一个装好 torch 和 ultralytics 的 Python，以及自己的权重文件（型号和 sha256 见 `run_config.json`），然后写一个设置文件，内容如下：

```
TSDAF_DETECTOR_PYTHON=<那个 Python 的路径>
TSDAF_DETECTOR_WEIGHTS=<权重文件路径>
TSDAF_DETECTOR_DEVICE=cuda
```

再用环境变量 `TSDAF_DETECTOR_ENV` 指向这个文件。权重不在仓库里。

## 目录

```
research/
  requirements.txt
  problem-profile-20261009/
    runner/
      run_pipeline.py           一条命令跑完四步
      gm_corpus.py              全链调度：统一的并发预算、断点续跑、自动补跑
      corpus_stats.py           调用、token 和时间统计
      shim/sitecustomize.py     调用转接：结构化输出、提示词改写、思考记录
      prompts/                  final4 提示词改写、record-o3 附加段、v4 的三段改写（切分、实验记录、论文级任务）
    tools/                      流程工具和 run_config.json
      src-units/                运行时用的框架代码快照（切分、分发、结构绑定等）。冻结，不随 src/tsdaf 更新
    .gitignore                  运行时产生的数据目录不进仓库
  fullscore-handoff-4papers-20261011/
    tools/build_review_pack.py  用量标记的起草规则（封存版，不要改）
```

代码靠“工具目录的上一层是工作区，再上一层是仓库根”来定位数据和 `tests/fixtures/`，所以这两处的目录名和层级都不能改。导入会往仓库的 `tests/fixtures/` 里写入各篇的标准包；用完如果不想保留，删掉新增的目录就行。

## 其他工具（OCSR 支路和交付）

| 工具 | 做什么 |
|---|---|
| `page_boxes.py`、`box_check.py`、`rebox.py`、`rebox_apply.py`、`rebox_eval.py` | 结构框：谱图页框选、质检、修补、评估 |
| `corpus_pack.py`、`ocsr_pack.py`、`ocsr_return.py`、`si_review_stage.py` | 交付：语料交付包、OCSR 交付包及其回传、SI 审核服务目录 |

## 常见问题

- **没有设凭据：** 程序会直接退出并提示设置 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。
- **系统代理有问题：** 设 `TSDAF_DIRECT=1`，全链会绕过系统代理直连。
- **某篇卡在前处理：** 看 `batch/<名字>/logs/<论文>.<步骤>.<次数>.log` 和账本里的失败原因 `why`。
- **全链某篇一直不完整：** 看 `chain-logs/` 下的日志和 `thoughts/` 里对应的空响应记录。网关不给内容的情况（例如被内容过滤）会附上网关的原始报错。
