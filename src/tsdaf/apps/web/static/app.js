const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let currentPackagePath = "";
let currentExecutionId = "";
let parsePollTimer;
let chemistryPollTimer;
let routeMode = "contract-mock";
let recordedChemistryPapers = [];
const MAX_UPLOAD_BYTES = 256 * 1024 * 1024;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function busy(label, active = true) {
  $("#busy-text").textContent = label;
  $("#busy").classList.toggle("hidden", !active);
}

let toastTimer;
function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 4200);
}

function bytes(value) {
  if (!Number.isFinite(value)) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let number = value, unit = 0;
  while (number >= 1024 && unit < units.length - 1) { number /= 1024; unit += 1; }
  return `${number.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function showTaskWorkspace(filename, kind, message) {
  currentPackagePath = "";
  currentExecutionId = "";
  $("#package-path").value = "";
  ["#package-panel", "#run-panel", "#chemistry-job-panel", "#chemistry-panel"].forEach(selector => {
    $(selector).classList.add("hidden");
  });
  $("#parse-progress").className = "parse-progress hidden";
  document.body.classList.remove("landing-mode");
  $("#upload-landing").classList.add("hidden");
  $("#task-workspace").classList.remove("hidden");
  $("#new-task-button").classList.remove("hidden");
  $("#task-file-name").textContent = filename || "文献解析任务";
  $("#task-file-kind").textContent = kind || "DOCUMENT INTAKE";
  $("#task-state-text").textContent = message || "正在处理";
  const historyMethod = window.location.hash === "#/task" ? "replaceState" : "pushState";
  window.history[historyMethod]({ view: "task" }, "", "#/task");
  window.scrollTo({ top: 0, behavior: "instant" });
}

function showUploadLanding() {
  clearTimeout(parsePollTimer);
  clearTimeout(chemistryPollTimer);
  currentPackagePath = "";
  currentExecutionId = "";
  document.body.classList.add("landing-mode");
  $("#upload-landing").classList.remove("hidden");
  $("#task-workspace").classList.add("hidden");
  $("#new-task-button").classList.add("hidden");
  $("#document-input").value = "";
  $("#parse-progress").className = "parse-progress hidden";
  window.history.replaceState({ view: "upload" }, "", "#/upload");
  window.scrollTo({ top: 0, behavior: "instant" });
}

async function handleDocument(file) {
  if (!file) return;
  if (file.size > MAX_UPLOAD_BYTES) return toast("文件超过 256 MB，无法上传。", true);
  const name = file.name.toLowerCase();
  const isPdf = name.endsWith(".pdf") || file.type === "application/pdf";
  const isZip = name.endsWith(".zip") || ["application/zip", "application/x-zip-compressed"].includes(file.type);
  if (!isPdf && !isZip) return toast("仅支持 PDF 原文或 ZIP 标准解析包。", true);
  showTaskWorkspace(file.name, isPdf ? "PDF · MINERU INTAKE" : "ZIP · STANDARD PACKAGE", isPdf ? "正在提交 MinerU" : "正在导入解析包");
  if (isPdf) await uploadPdf(file);
  else await uploadZip(file);
}

function showPackage(payload) {
  $("#package-panel").classList.remove("hidden");
  $("#paper-id").textContent = payload.paper_id || "未命名论文";
  $("#package-hash").textContent = payload.canonical_package_sha256 || "—";
  $("#package-hash").title = payload.canonical_package_sha256 || "";
  $("#block-count").textContent = payload.block_count ?? "—";
  $("#visual-count").textContent = payload.visual_count ?? "—";
  $("#asset-bytes").textContent = bytes(payload.total_source_asset_bytes);
  $("#schema-version").textContent = payload.source_schema_version || "—";
  $("#task-state-text").textContent = "标准解析包已就绪";
  $("#package-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function validatePackage() {
  const packagePath = $("#package-path").value.trim();
  if (!packagePath) return toast("请先选择 ZIP 或填写标准解析包目录。", true);
  if ($("#task-workspace").classList.contains("hidden")) {
    showTaskWorkspace(packagePath.split(/[\\/]/).pop(), "STANDARD PACKAGE", "正在校验解析包");
  }
  busy("正在验证 manifest、资产哈希与规范化内容…");
  try {
    const result = await api("/api/packages/validate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ package_path: packagePath })
    });
    if (!result.ok) throw new Error(result.payload?.message || result.payload?.code || "解析包校验失败");
    currentPackagePath = packagePath;
    showPackage(result.payload);
    toast("标准解析包校验通过。");
  } catch (error) { toast(error.message, true); }
  finally { busy("", false); }
}

async function downloadPackage() {
  if (!currentPackagePath) return toast("请先校验解析包。", true);
  busy("正在重新校验并打包标准解析结果…");
  try {
    const response = await fetch("/api/packages/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ package_path: currentPackagePath })
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `下载失败 (${response.status})`);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
    const filename = filenameMatch?.[1] || `${$("#paper-id").textContent}_tsdaf_package.zip`;
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    toast("标准解析包 ZIP 已开始下载。");
  } catch (error) { toast(error.message, true); }
  finally { busy("", false); }
}

async function uploadZip(file) {
  if (!file || !file.name.toLowerCase().endsWith(".zip")) return toast("请选择 ZIP 文件。", true);
  busy("正在安全导入标准解析包…");
  try {
    const result = await api("/api/packages/import", {
      method: "POST", headers: { "Content-Type": "application/zip", "X-Filename": file.name }, body: file
    });
    $("#task-state-text").textContent = "正在校验标准解析包";
    $("#package-path").value = result.package_path;
    await validatePackage();
  } catch (error) { toast(error.message, true); busy("", false); }
}

function renderParseJob(job) {
  $("#parse-progress").classList.remove("hidden");
  $("#parse-filename").textContent = job.filename || "paper.pdf";
  $("#parse-message").textContent = job.message || job.status;
  $("#parse-percent").textContent = `${job.progress ?? 0}%`;
  $("#parse-bar").style.width = `${job.progress ?? 0}%`;
  $("#task-state-text").textContent = job.message || job.status;
}

async function pollParseJob(jobId) {
  clearTimeout(parsePollTimer);
  try {
    const job = await api(`/api/parsing/jobs/${encodeURIComponent(jobId)}`);
    renderParseJob(job);
    if (!["SUCCEEDED", "FAILED"].includes(job.status)) {
      parsePollTimer = setTimeout(() => pollParseJob(jobId), 1500);
      return;
    }
    if (job.status === "FAILED") throw new Error(`${job.message}（${job.error_code}）`);
    $("#package-path").value = job.package_path;
    currentPackagePath = job.package_path;
    $("#task-state-text").textContent = "MinerU 完成，正在校验解析包";
    toast("上游解析完成，正在校验标准解析包。 ");
    await validatePackage();
  } catch (error) {
    $("#parse-message").textContent = error.message;
    $("#parse-progress").classList.add("failed");
    $("#task-state-text").textContent = "处理失败";
    toast(error.message, true);
  }
}

async function uploadPdf(file) {
  if (!file || !file.name.toLowerCase().endsWith(".pdf")) return toast("请选择 PDF 文件。", true);
  busy("正在安全上传原始 PDF…");
  try {
    const job = await api("/api/parsing/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/pdf", "X-Filename": encodeURIComponent(file.name) },
      body: file
    });
    renderParseJob(job);
    toast("PDF 已进入 MinerU 解析队列。 ");
    pollParseJob(job.job_id);
  } catch (error) { toast(error.message, true); }
  finally { busy("", false); }
}

function executionIdFrom(payload) {
  return payload?.execution_id || payload?.run?.execution_id || payload?.execution?.execution_id || "";
}

function flattenFacts(payload) {
  const status = payload?.status || payload?.execution_status || payload?.run?.status || "UNKNOWN";
  const observations = payload?.observability || payload?.observations || {};
  return { status, observations };
}

function replaceChildren(node, children) {
  node.replaceChildren(...children);
}

function element(tag, text, className = "") {
  const node = document.createElement(tag);
  node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function renderFacts(title, facts) {
  const heading = element("h3", title);
  const list = document.createElement("dl");
  facts.forEach(([key, value]) => {
    list.append(element("dt", key), element("dd", value));
  });
  replaceChildren($("#result-summary"), [heading, list]);
}

function renderRun(result, fallbackTitle = "执行详情") {
  const payload = result.payload || {};
  const contractMock = payload.mode === "contract-mock";
  currentExecutionId = executionIdFrom(payload) || currentExecutionId;
  const { status, observations } = flattenFacts(payload);
  const badge = $("#run-status");
  badge.textContent = contractMock ? "接口已对齐" : (result.exit_code === 3 ? "等待 Live" : (status === "UNKNOWN" ? (payload.code || "已返回") : status));
  badge.className = `status-badge ${result.exit_code === 3 ? "waiting" : (!result.ok && result.exit_code !== 3 ? "failed" : "")}`;
  $("#run-title").textContent = contractMock ? "文章路由接口回执" : (currentExecutionId || fallbackTitle);
  $("#resume-button").classList.toggle("hidden", contractMock || !currentExecutionId);
  const facts = contractMock ? [
    ["模式", "接口模拟"],
    ["协议", payload.provider_protocol || "—"],
    ["候选模型", (payload.model_candidates || []).join(" / ") || "—"],
    ["模型调用", payload.real_model_called ? "是" : "否"],
    ["凭据要求", payload.credentials_required ? "需要" : "不需要"],
    ["结果", payload.code || "—"]
  ] : [
    ["返回码", result.exit_code], ["状态", status],
    ["缓存", observations.cache_used ?? payload.cache_used ?? "—"],
    ["Router turns", observations.router_turns ?? "—"],
    ["Provider tokens", observations.provider_tokens_total ?? "—"],
    ["错误代码", payload.code || observations.error_code || "—"]
  ];
  renderFacts(
    contractMock ? "接口字段验证完成" : (result.exit_code === 3 ? "需要显式 Live 调用" : "本次运行摘要"),
    facts
  );
  $("#result-json").textContent = JSON.stringify(payload, null, 2);
  $("#run-panel").classList.remove("hidden");
  const stages = $$(".stage");
  const successful = result.ok && ["SUCCEEDED", "SUCCESS", "COMPLETED"].includes(String(status).toUpperCase());
  stages.forEach((stage, index) => {
    stage.classList.toggle("done", contractMock ? index < 2 : (successful || (result.exit_code === 3 && index < 2)));
    stage.classList.toggle("active", contractMock ? index === 2 : (!successful && index === (result.exit_code === 3 ? 2 : 0)));
  });
  $("#run-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function runRoute() {
  if (!currentPackagePath) return toast("请先校验解析包。", true);
  const live = routeMode === "cli" && $("#live-toggle").checked;
  busy(routeMode === "contract-mock" ? "正在验证文章路由接口字段…" : (live ? "正在执行显式 Live 路由，请勿关闭页面…" : "正在检查缓存并启动离线路由…"));
  try {
    const result = await api("/api/routes/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ package_path: currentPackagePath, live })
    });
    renderRun(result);
    const mockResult = result.payload?.mode === "contract-mock";
    toast(mockResult ? "接口对接验证完成，本次未调用真实模型。" : (result.exit_code === 3 ? "离线缓存不足；如已在服务器配置凭据，可打开“显式 Live”。" : (result.ok ? "路由执行完成。" : (result.payload?.message || "路由执行失败"))), !result.ok && result.exit_code !== 3);
    loadHistory();
  } catch (error) { toast(error.message, true); }
  finally { busy("", false); }
}

function visualLabel(compound) {
  return (compound.labels || []).join(" / ") || compound.visual_structure?.printed_label || "未编号";
}

function openStructurePreview(compound) {
  const visual = compound.visual_structure;
  if (!visual?.image_url) return;
  const label = visualLabel(compound);
  const name = (compound.names || [])[0] || "未提供名称";
  $("#structure-preview-image").src = visual.image_url;
  $("#structure-preview-image").alt = `${label} 的结构识别裁剪图`;
  $("#structure-preview-title").textContent = `${label} · ${name}`;
  $("#structure-preview-smiles").textContent = visual.smiles || "未提供 SMILES";
  $("#structure-preview-meta").textContent = [
    visual.formula,
    Number.isFinite(visual.reader_confidence) ? `识别置信度 ${Math.round(visual.reader_confidence * 100)}%` : null,
    "结构图来自独立 OCSR 绑定，不覆盖机器事实"
  ].filter(Boolean).join(" · ");
  $("#structure-preview-dialog").showModal();
}

function structureTile(compound, role = "") {
  const visual = compound.visual_structure;
  const label = visualLabel(compound);
  const tile = document.createElement(visual?.image_url ? "button" : "div");
  tile.className = `structure-tile ${visual?.image_url ? "has-visual" : "missing-visual"}`;
  if (visual?.image_url) {
    tile.type = "button";
    const image = document.createElement("img");
    image.src = visual.image_url;
    image.alt = `${label} 的结构识别裁剪图`;
    image.loading = "lazy";
    tile.append(image);
    tile.addEventListener("click", () => openStructurePreview(compound));
  } else {
    tile.append(element("span", "暂无绑定图"));
  }
  tile.append(element("b", role ? `${role} · ${label}` : label));
  return tile;
}

function openReactionPreview(block, visual = block.reaction_visual) {
  const imageUrl = visual?.image_url || visual?.context_image_url;
  if (!imageUrl) return;
  const isContextOnly = !visual.image_url && Boolean(visual.context_image_url);
  $("#structure-preview-image").src = imageUrl;
  $("#structure-preview-image").alt = `反应块 ${block.source_order || "—"} 原文证据图`;
  $("#structure-preview-title").textContent = isContextOnly
    ? `反应块 ${block.source_order || "—"} · 完整原文 Scheme`
    : `反应块 ${block.source_order || "—"} · 原文证据 ${Number(visual.visual_index || 0) + 1}`;
  $("#structure-preview-smiles").textContent = isContextOnly
    ? "当前布局无法可靠裁成单步反应，因此展示完整原文 Scheme 供人工核对；它不改变机器解析事实。"
    : "图片从论文原始 Figure 的局部区域派生，仅用于展示；化学事实仍以机器结果及其证据引用为准。";
  $("#structure-preview-meta").textContent = `原图资产 ${visual.figure_asset_sha256 || "—"}`;
  $("#structure-preview-dialog").showModal();
}

function reactionFigure(block) {
  const primary = block.reaction_visual;
  if (!primary?.image_url) {
    const reasons = {
      MISSING_INPUT_VISUAL_BINDING: "输入化合物缺少原文视觉锚点",
      MISSING_OUTPUT_VISUAL_BINDING: "输出化合物缺少原文视觉锚点",
      MISSING_INPUT_AND_OUTPUT_VISUAL_BINDING: "输入与输出化合物均缺少原文视觉锚点",
      NO_SHARED_FIGURE_VISUAL_BINDING: "输入与输出没有可确认的同图视觉锚点",
      MULTI_STEP_SCHEME_LAYOUT: "原文为跨行或多步骤 Scheme，已停止不可靠的自动单步裁切",
    };
    const message = reasons[primary?.reason_code] || "暂无可可靠定位的原文反应图";
    const panel = element("div", message, primary?.context_image_url ? "reaction-context-only" : "reaction-figure-missing");
    if (primary?.context_image_url) {
      const button = element("button", "查看完整原文 Scheme", "reaction-context-button");
      button.type = "button";
      button.addEventListener("click", () => openReactionPreview(block, primary));
      panel.append(button);
    }
    return panel;
  }
  const gallery = document.createElement("div");
  gallery.className = "reaction-source-gallery";
  [primary, ...(primary.alternatives || [])].forEach((visual, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "reaction-source-figure";
    const image = document.createElement("img");
    image.src = visual.image_url;
    image.alt = `反应块 ${block.source_order || "—"} 原文证据图 ${index + 1}`;
    image.loading = "lazy";
    button.append(image, element("span", index === 0 ? "查看主要原文证据" : `查看补充证据 ${index + 1}`));
    button.addEventListener("click", () => openReactionPreview(block, visual));
    gallery.append(button);
  });
  if (primary.context_image_url) {
    const contextButton = element("button", "查看完整原文 Scheme", "reaction-context-button");
    contextButton.type = "button";
    contextButton.addEventListener("click", () => openReactionPreview(block, {
      ...primary,
      image_url: null,
    }));
    gallery.append(contextButton);
  }
  return gallery;
}

function reactionDetails(block) {
  const details = document.createElement("details");
  details.className = "reaction-details";
  const toggle = document.createElement("summary");
  toggle.textContent = "查看实验步骤、条件、产率与路线位置";
  details.append(toggle);
  const content = document.createElement("div");
  content.className = "reaction-detail-content";

  const routeSection = document.createElement("section");
  routeSection.append(element("h4", "总合成路线"));
  const route = block.route_context || {};
  if (route.included) {
    routeSection.append(
      element("p", `当前为主路线第 ${route.position}/${route.total_reaction_blocks} 个反应块 · ${route.route_id || "—"}`)
    );
    if (route.overview) routeSection.append(element("p", route.overview, "detail-muted"));
  } else {
    routeSection.append(element("p", "该反应块未列入主合成路线，可能属于对照、旁路或补充实验。"));
  }

  const stepSection = document.createElement("section");
  stepSection.append(element("h4", "实验步骤"));
  const steps = block.experimental_steps || [];
  if (steps.length) {
    const list = document.createElement("ol");
    steps.forEach(step => list.append(element("li", step)));
    stepSection.append(list);
  } else {
    stepSection.append(element("p", "解析结果中未提供逐阶段实验记录。", "detail-muted"));
  }

  const conditionSection = document.createElement("section");
  conditionSection.append(element("h4", "反应条件"));
  const conditions = block.conditions || [];
  if (conditions.length) {
    conditions.forEach(condition => {
      const operation = document.createElement("div");
      operation.className = "condition-operation";
      operation.append(element("b", `操作 ${condition.operation_index || "—"}${condition.reaction_class ? ` · ${condition.reaction_class}` : ""}`));
      const facts = [
        ["气氛", condition.atmosphere], ["温度", condition.temperature],
        ["时间", condition.duration], ["压力", condition.pressure],
        ["浓度", condition.concentration], ["关键加料", condition.key_addition],
        ["后处理", condition.key_workup],
      ].filter(([, value]) => value);
      if (facts.length) {
        const factList = document.createElement("dl");
        facts.forEach(([label, value]) => {
          factList.append(element("dt", label), element("dd", value));
        });
        operation.append(factList);
      }
      if ((condition.materials || []).length) {
        const materialList = document.createElement("div");
        materialList.className = "condition-materials";
        condition.materials.forEach(material => {
          const quantity = [material.amount, material.equivalents].filter(Boolean).join(" · ");
          materialList.append(element("span", `${material.role} · ${material.name}${quantity ? ` · ${quantity}` : ""}`));
        });
        operation.append(materialList);
      }
      if ((condition.other_conditions || []).length) {
        const notes = document.createElement("ul");
        notes.className = "condition-notes";
        condition.other_conditions.forEach(note => notes.append(element("li", note)));
        operation.append(notes);
      }
      conditionSection.append(operation);
    });
  } else {
    conditionSection.append(element("p", "解析结果中未提供结构化反应条件。", "detail-muted"));
  }

  const outcomeSection = document.createElement("section");
  outcomeSection.append(element("h4", "产率与实验结果"));
  const outcomeLabels = {
    ISOLATED_YIELD: "分离收率", YIELD: "收率", MASS: "质量",
    AMOUNT_OF_SUBSTANCE: "物质的量", DR: "非对映选择性", ER: "对映体比例",
  };
  const outcomes = block.outcomes || [];
  if (outcomes.length) {
    const list = document.createElement("ul");
    list.className = "outcome-list";
    outcomes.forEach(outcome => {
      const kind = outcomeLabels[outcome.kind] || outcome.kind || "实验结果";
      list.append(element("li", `${kind}：${outcome.value}${outcome.basis ? `（${outcome.basis}）` : ""}`));
    });
    outcomeSection.append(list);
  } else {
    outcomeSection.append(element("p", "解析结果中未提供结构化产率或定量结果。", "detail-muted"));
  }

  content.append(routeSection, stepSection, conditionSection, outcomeSection);
  details.append(content);
  return details;
}

function renderChemistry(payload) {
  const summary = payload.summary || {};
  const counts = summary.counts || {};
  const paperId = summary.paper_id || "unknown-paper";
  $("#chemistry-result-badge").textContent = payload.mode === "research-pipeline" ? "RESEARCH 全链结果" : "真实历史结果";
  $("#chemistry-title").textContent = paperId;
  $("#chemistry-schema").textContent = summary.schema_version || "—";
  $("#chemistry-schema").title = summary.machine_result_id || "";
  $("#chemistry-compounds").textContent = counts.compounds ?? "—";
  $("#chemistry-reactions").textContent = counts.reaction_blocks ?? "—";
  $("#chemistry-routes").textContent = counts.routes ?? "—";
  $("#chemistry-characterization").textContent = counts.characterization_records ?? "—";
  $("#chemistry-conflicts").textContent = counts.field_conflicts ?? "—";
  $("#chemistry-unresolved").textContent = counts.unresolved_items ?? "—";
  const bindingCount = summary.structure_binding?.summary?.compounds_with_a_structure;
  const bindingNote = Number.isFinite(bindingCount)
    ? `已载入 ${bindingCount} 个结构识别裁剪图；识别结果为独立展示层，不覆盖化学事实`
    : "当前结果没有可用的结构图绑定";
  $("#chemistry-audit-note").textContent = `${payload.message || "已读取录制结果。"} · ${summary.coverage || "—"} · ${summary.completeness || "—"} · ${bindingNote}`;
  $("#chemistry-json-download").href = payload.downloads?.json || `/api/chemistry/results/${encodeURIComponent(paperId)}.json`;
  $("#chemistry-zip-download").href = payload.downloads?.zip || `/api/chemistry/results/${encodeURIComponent(paperId)}.zip`;

  const route = (summary.routes || [])[0] || {};
  const overview = $("#chemistry-overview");
  const routeLabel = element("span", "SYNTHESIS ROUTE");
  const routeTitle = element("h3", `${route.reaction_block_count ?? counts.reaction_blocks ?? 0} 个反应块 · ${route.compound_count ?? counts.compounds ?? 0} 个路线化合物`);
  const routeText = element("p", route.overview || "该结果没有提供路线概述，可在完整 JSON 中查看路线合同。 ");
  replaceChildren(overview, [routeLabel, routeTitle, routeText]);

  const reactionCards = (summary.reaction_blocks || []).map((block, index) => {
    const card = document.createElement("article");
    card.className = "reaction-card";
    const number = element("b", block.source_order ?? index + 1);
    const body = document.createElement("div");
    body.append(element("strong", block.summary || "未提供步骤摘要"));
    body.append(element("p", block.summary_zh || "中文展示译文暂未提供", "reaction-translation"));
    const inputs = (block.input_compound_ids || []).length;
    const outputs = (block.output_compound_ids || []).length;
    const meta = document.createElement("div");
    meta.className = "reaction-meta";
    meta.append(
      element("span", `输入 ${inputs}`),
      element("span", `输出 ${outputs}`),
      element("span", `操作 ${block.operation_count ?? 0}`),
      element("span", `结果 ${block.outcome_count ?? 0}`),
      ...(block.primary_yield ? [element("span", `产率 ${block.primary_yield}`, "yield-badge")] : []),
      ...(block.route_context?.included ? [element("span", `主路线 ${block.route_context.position}/${block.route_context.total_reaction_blocks}`, "route-badge")] : [])
    );
    body.append(meta, reactionFigure(block), reactionDetails(block), element("small", block.reaction_block_id || "—"));
    card.append(number, body);
    return card;
  });
  replaceChildren($("#chemistry-reaction-list"), reactionCards.length ? reactionCards : [element("div", "没有路线反应块", "empty-state")]);
  $("#reaction-list-count").textContent = `${reactionCards.length} 条`;

  const compoundCards = (summary.compounds || []).map(compound => {
    const card = document.createElement("article");
    card.className = "compound-card";
    const label = (compound.labels || []).join(" / ") || "未编号";
    const name = (compound.names || [])[0] || "未提供名称";
    card.append(
      structureTile(compound),
      element("strong", label),
      element("span", name),
      element(
        "small",
        `${compound.visual_structure ? "已绑定结构识别图" : (compound.has_structure ? "机器事实含结构" : "暂无结构图")} · ${compound.compound_id || "—"}`
      )
    );
    return card;
  });
  replaceChildren($("#chemistry-compound-list"), compoundCards.length ? compoundCards : [element("div", "没有化合物记录", "empty-state")]);
  $("#compound-list-count").textContent = `${compoundCards.length} 条`;
  $("#chemistry-json").textContent = JSON.stringify(payload.result || {}, null, 2);
  $("#chemistry-panel").classList.remove("hidden");
  $("#chemistry-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderChemistryJob(payload) {
  const panel = $("#chemistry-job-panel");
  panel.classList.remove("hidden");
  $("#chemistry-job-title").textContent = payload.job_id ? `Research 任务 ${payload.job_id}` : "Research 化学解析预检";
  $("#chemistry-job-progress").textContent = `${payload.progress ?? 0}%`;
  $("#chemistry-job-message").textContent = payload.message || "正在处理";
  const badge = $("#chemistry-job-status");
  badge.textContent = payload.status || "PRECHECK";
  badge.className = `status-badge ${["FAILED", "CONFIG_REQUIRED"].includes(payload.status) ? "failed" : (["SUCCEEDED"].includes(payload.status) ? "" : "waiting")}`;
  const order = ["import", "front", "chain", "stats"];
  const current = payload.stage === "complete" ? order.length : order.indexOf(payload.stage);
  $$('[data-chemistry-stage]').forEach((stage, index) => {
    stage.classList.toggle("done", current === order.length || index < current);
    stage.classList.toggle("active", index === current);
  });
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function pollChemistry(jobId) {
  clearTimeout(chemistryPollTimer);
  try {
    const payload = await api(`/api/chemistry/jobs/${encodeURIComponent(jobId)}`);
    renderChemistryJob(payload);
    if (payload.status === "SUCCEEDED") {
      renderChemistry(payload);
      toast("Research 化学解析完成，可以查看并下载结果。");
      return;
    }
    if (payload.status === "FAILED") {
      toast(payload.message || "Research 化学解析失败。", true);
      return;
    }
    chemistryPollTimer = setTimeout(() => pollChemistry(jobId), 3000);
  } catch (error) { toast(error.message, true); }
}

async function runChemistry() {
  if (!currentPackagePath) return toast("请先校验解析包。", true);
  const live = $("#chemistry-live-toggle").checked;
  busy(live ? "正在创建 Research 化学解析任务…" : "正在检查录制结果和 Research 流水线…");
  try {
    const payload = await api("/api/chemistry/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ package_path: currentPackagePath, live })
    });
    if (payload.status === "SUCCEEDED") {
      $("#chemistry-job-panel").classList.add("hidden");
      renderChemistry(payload);
      toast("已载入化学解析结果。");
    } else {
      renderChemistryJob(payload);
      if (payload.job_id) {
        chemistryPollTimer = setTimeout(() => pollChemistry(payload.job_id), 1500);
        toast("Research 化学解析任务已创建。");
      } else {
        toast(payload.message || "Research 流水线预检完成。", payload.status === "CONFIG_REQUIRED");
      }
    }
  } catch (error) { toast(error.message, true); }
  finally { busy("", false); }
}

async function resumeRun() {
  if (!currentExecutionId) return;
  busy("正在验证不可变证据并恢复执行…");
  try {
    const result = await api(`/api/routes/${encodeURIComponent(currentExecutionId)}/resume`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ live: $("#live-toggle").checked })
    });
    renderRun(result);
  } catch (error) { toast(error.message, true); }
  finally { busy("", false); }
}

async function inspectRun(id) {
  busy("正在验证并读取执行证据…");
  try {
    const result = await api(`/api/runs/${encodeURIComponent(id)}`);
    currentExecutionId = id;
    document.querySelector('[data-view="workspace"]').click();
    renderRun(result, id);
  } catch (error) { toast(error.message, true); }
  finally { busy("", false); }
}

async function loadHistory() {
  try {
    const { items } = await api("/api/runs");
    const history = $("#history-list");
    if (!items.length) {
      replaceChildren(history, [element("div", "还没有运行记录", "empty-state")]);
      return;
    }
    const buttons = items.map(item => {
      const button = element("button", "", "history-item");
      button.dataset.run = String(item.execution_id);
      button.append(
        element("span", "", "status-dot"),
        element("b", item.execution_id),
        element("time", new Date(item.updated_at * 1000).toLocaleString()),
        element("span", "查看 →")
      );
      button.addEventListener("click", () => inspectRun(button.dataset.run));
      return button;
    });
    replaceChildren(history, buttons);
  } catch (error) { toast(error.message, true); }
}

async function initialize() {
  try {
    const health = await api("/api/health");
    routeMode = health.route_mode || "contract-mock";
    recordedChemistryPapers = health.chemistry_recorded_papers || [];
    $("#health").classList.add("ok"); $("#health").lastChild.textContent = "本地服务正常";
    const upstreamState = $("#upstream-state");
    upstreamState.className = `upstream-state ${health.upstream_configured ? "ready" : "missing"}`;
    const backendText = ["mineru-local", "mineru-wsl"].includes(health.parse_backend)
      ? `${health.parse_backend === "mineru-wsl" ? "WSL " : ""}本地 MinerU 已就绪 · ${health.mineru_tier || "standard"}`
      : (health.parse_backend === "configured-upstream" ? "远程解析服务已连接" : "尚未安装 MinerU，标准解析包入口仍可使用");
    upstreamState.querySelector("span").textContent = backendText;
    const cliMode = routeMode === "cli";
    $("#live-control").classList.toggle("hidden", !cliMode);
    $("#route-action-title").textContent = cliMode ? "准备启动文章路由" : "准备验证文章路由接口";
    $("#route-action-note").textContent = cliMode ? "默认离线；无缓存时会安全停止并提示需要 live。" : `接口模拟模式 · ${health.route_model_candidates?.join(" / ") || "后端选择模型"} · 不调用真实模型。`;
    $("#run-button").textContent = cliMode ? "开始路由" : "验证接口对接";
    $("#chemistry-action-note").textContent = recordedChemistryPapers.length
      ? `已发现 ${recordedChemistryPapers.length} 篇录制结果；其他论文进入 research 四阶段流水线。`
      : "新论文将进入 research 四阶段流水线。";
    const researchReady = health.chemistry_research_available && !(health.chemistry_missing_modules || []).length;
    $("#chemistry-live-toggle").disabled = !researchReady || !health.chemistry_provider_configured;
    $("#chemistry-live-control").title = !researchReady
      ? `Research 运行依赖未就绪：${(health.chemistry_missing_modules || []).join("、") || "代码缺失"}`
      : (!health.chemistry_provider_configured ? "服务端尚未配置 OpenAI 兼容接口凭据" : `将使用 ${health.chemistry_model || "后端模型"} 进行真实调用`);
  } catch { $("#health").lastChild.textContent = "服务未连接"; }
  try {
    const { items } = await api("/api/fixtures");
    const fixtures = items.map(item => {
      const button = element("button", item.name, "chip");
      button.dataset.path = String(item.path);
      button.addEventListener("click", () => {
        showTaskWorkspace(item.name, "BUILT-IN STANDARD PACKAGE", "正在校验内置样例");
        $("#package-path").value = button.dataset.path;
        validatePackage();
      });
      return button;
    });
    replaceChildren(
      $("#fixture-chips"),
      fixtures.length ? fixtures : [element("span", "没有发现样例", "muted")]
    );
  } catch (error) { toast(error.message, true); }
  loadHistory();
}

$$('[data-view]').forEach(button => button.addEventListener("click", () => {
  document.body.classList.remove("landing-mode");
  $$('[data-view]').forEach(item => item.classList.toggle("active", item === button));
  $$(".view").forEach(view => view.classList.remove("active"));
  $(`#view-${button.dataset.view}`).classList.add("active");
  $("#page-title").textContent = button.textContent.trim();
  $("#new-task-button").classList.toggle("hidden", button.dataset.view !== "workspace");
  if (button.dataset.view === "history") loadHistory();
}));
$("#validate-button").addEventListener("click", validatePackage);
$("#download-package-button").addEventListener("click", downloadPackage);
$("#package-path").addEventListener("keydown", event => { if (event.key === "Enter") validatePackage(); });
$("#run-button").addEventListener("click", runRoute);
$("#chemistry-button").addEventListener("click", runChemistry);
$("#resume-button").addEventListener("click", resumeRun);
$("#refresh-button").addEventListener("click", initialize);
$("#history-refresh").addEventListener("click", loadHistory);
$("#new-task-button").addEventListener("click", showUploadLanding);
$("#home-link").addEventListener("click", event => { event.preventDefault(); showUploadLanding(); });
$("#document-input").addEventListener("change", event => handleDocument(event.target.files[0]));
const unifiedDropZone = $("#unified-drop-zone");
["dragenter", "dragover"].forEach(name => unifiedDropZone.addEventListener(name, event => {
  event.preventDefault();
  unifiedDropZone.classList.add("drag");
}));
["dragleave", "drop"].forEach(name => unifiedDropZone.addEventListener(name, event => {
  event.preventDefault();
  unifiedDropZone.classList.remove("drag");
}));
unifiedDropZone.addEventListener("drop", event => handleDocument(event.dataTransfer.files[0]));
$("#structure-preview-dialog .dialog-close").addEventListener("click", () => $("#structure-preview-dialog").close());
$("#structure-preview-dialog").addEventListener("click", event => {
  if (event.target === $("#structure-preview-dialog")) $("#structure-preview-dialog").close();
});
window.addEventListener("popstate", () => {
  if (window.location.hash !== "#/task" || !currentPackagePath) showUploadLanding();
});
showUploadLanding();
initialize();
