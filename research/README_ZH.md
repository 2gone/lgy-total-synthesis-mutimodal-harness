# 全合成 200 篇语料流程（2026-10 版）

这里是 2026 年 10 月跑完 200 篇全合成论文时实际使用的整套流程代码，原样收录，用于复现和协作。它是研究用的语料流程，不是 `src/tsdaf` 里的生产工作流：它直接读 PDF，自带一份框架代码快照，不受生产工作流的分层约束。

## 目录

```
research/
  problem-profile-20261009/
    tools/                     流程代码（31 个工具）和 run_config.json
      src-units/               运行时用的框架代码快照（切分、分发、结构绑定等），冻结，不随 src/tsdaf 更新
    .gitignore                 运行时产生的数据目录不进仓库
  fullscore-handoff-4papers-20261011/
    tools/build_review_pack.py 数量标记的起草规则（封存版，不要改）
```

代码按"工具目录的上一层是工作区、再上一层是仓库根"定位数据和测试夹具（`tests/fixtures/`），两处目录名和层级都不能改。运行时的数据（读图结果、切分、记录、交付包等）写在 `problem-profile-20261009/` 下，已被 `.gitignore` 排除。

## 流程

一篇论文依次经过四个阶段（`batch_run.py` 调度，可断点续跑，账本在 `batch/<名字>/status.json`）：

| 阶段 | 工具 | 做什么 |
|---|---|---|
| 导入 | `import_ready200.py` | 把正文和全部 SI 拼成一份"实验文档"（去掉重复副本），放进 `tests/fixtures/` |
| 前置 | `prestage_paper.py` | 从 PDF 页面切图，分子检测器框出结构式，视觉模型逐图转写（编号、箭头、条件、表格） |
| 页面归属 | `page_owners.py` | 没有成段文字的页面（谱图页等）整页渲染，判断是哪个化合物的表征 |
| 文字核对 | `prestage_resolve.py` | 解析文字和 PDF 文字层对不上的地方，裁出原文行让模型核对 |
| 主链路 | `whole_paper.py` | 切分（`si_units_probe.py`）→ 文本包（`entry_package.py`、`build_review_pack.py`）→ 逐条实验记录（`record_actions.py`）→ 论文级任务（`paper_task.py`）→ 组装 → 结构绑定（`structure_step.py`） |

结构式的框另有一条支路，给 OCSR 协作方用：

| 工具 | 做什么 |
|---|---|
| `page_boxes.py` | 在整页渲染的谱图页上也框出结构式 |
| `box_check.py` | 视觉模型质检每个框（完整 / 截断 / 一框多个 / 不是结构），并找出漏框 |
| `rebox.py --cascade` | 修补：先用检测器在白边扩展的图上重框，修不好的再交给视觉模型；新框同样经质检 |
| `rebox_apply.py` | 把合格的修补框并入各图，新框读出化合物编号（只读新框附近） |
| `rebox_eval.py` | 比较检测器重框、视觉模型、二者结合各能补上多少漏框 |

交付：

| 工具 | 产出 |
|---|---|
| `corpus_pack.py` | 200 篇交付包：每篇 `source/`（原始 PDF）、`processed/`（前置处理后的文本和图片）、`results/`（机器结果），加总索引和中文说明 |
| `ocsr_pack.py` / `ocsr_return.py` | OCSR 交付包（裁图、清单、回传模板），以及回传结果的校验、收录和重新绑定 |
| `si_review_stage.py` | 整理成 SI 审核服务需要的目录结构 |

## 模型和参数

每一步用哪个模型、什么推理强度，只写在 `tools/run_config.json` 一处。可以用环境变量临时改某一组的模型：`TSDAF_WORKFLOW_MODEL`、`TSDAF_VISION_MODEL`、`TSDAF_OCSR_MODEL`。

## 运行环境

- 凭据只从进程环境变量进入：`OPENAI_BASE_URL`、`OPENAI_API_KEY`（OpenAI 兼容接口）。代码不读 `.env`，任何文件里都不写凭据。
- 分子检测器：`TSDAF_DETECTOR_PYTHON`（装有 torch 和 ultralytics 的 Python）、`TSDAF_DETECTOR_WEIGHTS`（权重文件，型号和 sha256 见 `run_config.json`）、`TSDAF_DETECTOR_DEVICE`（cuda / mps / cpu）。权重不在仓库里。
- `batch_run.py` 默认从 `D:\tsdaf-lpz.env` 读凭据、从 `D:\tsdaf\det\detector.env` 读检测器设置，只传给子进程；可用 `TSDAF_CREDENTIALS` 指定别的凭据文件。
- Python 依赖：openai、pillow、pypdfium2、PyMuPDF（fitz）、pydantic；检测器另需 torch、ultralytics。

## 不在仓库里的东西

论文 PDF、解析结果、读图结果、交付包等数据都不在这里；它们在运行节点上，交付包另有存放位置。
