/* Research tasks share source selection and pinned evidence with the observatory. */
(() => {
  "use strict";
  const categories = {
    time_and_sampling: "时间与采样", measurement_extraction: "测量读取",
    result_attribution: "结果归属", scientific_scope: "结论适用范围",
    numerical_reasoning: "数值推理", insufficient_information: "信息不足判断",
    physical_inference: "物理量推导", cross_paper_comparison: "跨论文比较",
    uncertainty_analysis: "不确定度分析", catalog_literature_integration: "星表与论文联合分析",
    observing_strategy: "观测与标定", data_quality: "数据质量", calibration_transfer: "标定适用范围",
  };
  const answers = {
    prior_work: "引用前人结果", not_simultaneous: "并非同期观测",
    compatible_timescales: "不同时间尺度上的结论可以相容", not_guaranteed: "不能保证逐历元精度",
    insufficient_information: "信息不足，无法确定",
  };
  const fieldNames = { RAICRS: "赤经", DEICRS: "赤纬", Plx_gdr3: "视差", e_Plx_gdr3: "视差误差",
    pmRA_gdr3: "赤经自行", pmDE_gdr3: "赤纬自行", RUWE_gdr3: "RUWE" };
  const keyOf = t => `${t.release}:${t.source_id}`;
  const el = (tag, value, cls = "") => {
    const node = document.createElement(tag); node.textContent = value ?? ""; node.className = cls; return node;
  };
  const button = (label, action, cls = "") => {
    const node = el("button", label, cls); node.type = "button"; node.addEventListener("click", action); return node;
  };
  function external(label, url) {
    const link = el("a", label);
    try { const u = new URL(url); if (u.protocol === "https:") link.href = u.href; } catch (_) {}
    link.target = "_blank"; link.rel = "noopener noreferrer"; return link;
  }
  function details(label, cls = "") {
    const node = el("details", "", cls); node.append(el("summary", label)); return node;
  }
  function mount(root, { load, loadContext, selectSource }) {
    let data = { suites: [], warnings: [] }, selected = null, request = 0, contextRequest = 0;
    const summary = el("p", "正在读取科研任务…", "r-summary"); summary.setAttribute("role", "status");
    const warning = el("p", "", "r-warning"); warning.hidden = true;
    const tools = el("div", "", "r-toolbar");
    const target = el("select"); target.id = "research-target";
    const category = el("select"); category.id = "research-category";
    const level = el("select"); level.id = "research-level";
    level.append(new Option("全部任务", ""), new Option("综合案例", "integrated_case"), new Option("基础题", "basic"));
    const count = el("span", "", "caption"); count.id = "research-count"; count.setAttribute("aria-live", "polite");
    for (const [label, control] of [["任务关联天体", target], ["任务层次", level], ["任务类型", category]]) {
      const wrapper = el("label", label); wrapper.append(control); tools.append(wrapper);
    }
    category.append(new Option("全部类型", ""), ...Object.entries(categories).map(([key, name]) => new Option(name, key)));
    tools.append(count, button("查看全部任务", () => { target.value = ""; category.value = ""; level.value = ""; draw(); }, "quiet"));
    const list = el("div", "", "r-grid"); list.id = "research-list";
    const provenance = details("任务包版本与来源", "r-provenance");
    const dialog = el("dialog", "", "evidence-dialog r-dialog");
    const context = el("div", "", "e-context");
    dialog.append(button("关闭任务原文", () => dialog.close()), context);
    dialog.addEventListener("close", () => { ++contextRequest; });
    root.replaceChildren(summary, warning, tools, list, provenance, dialog);

    function targets() {
      return new Map(data.suites.flatMap(s => s.targets).map(t => [keyOf(t), t]));
    }
    function updateTargets(value = target.value) {
      const all = targets();
      target.replaceChildren(new Option("全部天体", ""));
      for (const [key, t] of all) target.add(new Option(t.name, key));
      if (selected && !all.has(keyOf(selected))) target.add(new Option(`当前所选源 · ${selected.source_id}（无关联任务）`, keyOf(selected)));
      if ([...target.options].some(o => o.value === value)) target.value = value;
    }
    async function openMaterial(material, suite) {
      const token = ++contextRequest;
      const p = material.provenance, loc = p.location;
      const paper = suite.papers.find(v => v.versioned_id === p.paper);
      const span = { ...loc, field: loc.field === "cell" ? "table_cell" : loc.field, quote: material.text };
      context.replaceChildren(el("p", "正在读取固定版本原文…"));
      if (!dialog.open) dialog.showModal();
      try {
        let result;
        if (loc.field === "caption") {
          // Captions have their own offsets in the research bundle.
          result = { available: true, paper: { ...paper, paper_version_id: paper.library_version_id,
            document_sha256: paper.artifact_sha256, parser_version: "任务包固定摘录" }, units: [{
            ...span, text: material.text, section_path: loc.section_path, source_url: p.url,
          }] };
        } else {
          result = await loadContext(paper.library_version_id, loc.block_id);
          if (result.available && result.paper.document_sha256 !== paper.artifact_sha256) throw new Error("原文版本与任务包不一致");
        }
        if (token !== contextRequest) return;
        window.AstroEvidence.renderContext(context, result, span);
        if (window.AstroDemo || loc.field === "caption") context.prepend(el("p", "此处展示固定版本摘录；完整上下文可通过论文链接查看。", "e-meta"));
      } catch (error) {
        if (token === contextRequest) context.replaceChildren(el("p", `原文读取失败：${error.message}`));
      }
    }
    function renderMaterial(material, suite) {
      const node = el("article", "", "r-material");
      const p = material.provenance;
      if (material.kind === "gaia_fields") {
        node.append(el("h4", `${material.id} · ${p.target} · Gaia ${p.release.toUpperCase()}`));
        const table = el("table");
        const head = el("tr"); ["字段", "数值", "单位"].forEach(v => head.append(el("th", v))); table.append(head);
        for (const f of material.fields) {
          const row = el("tr");
          [fieldNames[f.column] || f.column, f.value ?? "缺失", f.unit].forEach(v => row.append(el("td", v))); table.append(row);
        }
        const wrap = el("div", "", "r-table-wrap"); wrap.append(table); node.append(wrap);
        node.append(el("p", `${p.release} · ${p.source_id}`, "caption"));
      } else {
        const loc = p.location;
        node.append(el("h4", `${material.id} · ${p.paper}`), el("p", p.title, "caption"), el("blockquote", material.text, "r-quote"));
        const address = loc.field === "cell" ? `单元格 · ${loc.row_label} / ${loc.column_label} · 行 ${loc.row} 列 ${loc.column}（从 0 起）`
          : loc.field === "caption" ? "表注" : "正文";
        node.append(el("p", `${address} · ${loc.block_id} · 字符 ${loc.start}–${loc.end}`, "caption"));
        if (loc.row_context && loc.row_context.length) node.append(el("p", loc.row_context.map(f => `${f.label}: ${f.value}`).join(" · "), "caption"));
        const actions = el("div", "", "r-links");
        actions.append(button("查看原文位置", () => openMaterial(material, suite), "quiet"), external("打开论文 ↗", p.url));
        node.append(actions);
      }
      return node;
    }
    function card(task, suite) {
      const node = el("article", "", "r-task"); node.dataset.taskId = task.id;
      const top = el("div", "", "r-task-top");
      top.append(el("span", task.id, "r-task-id"), el("span", categories[task.category] || task.category, "badge"),
        el("span", task.task_level === "integrated_case" ? "综合案例" : "基础题", "badge"),
        el("span", task.review_status === "pending" ? "待复核" : task.review_status, "badge warning"));
      node.append(top, el("h3", task.title || task.question, "r-question"));
      if (task.title) node.append(el("p", task.question, "r-scenario"));
      if (task.response_fields) {
        const fields = details(`需要回答的检查点（${task.response_fields.length}）`, "r-fields");
        const steps = el("ol");
        for (const f of task.response_fields) {
          const row = el("li", `${f.request}${f.unit ? ` 单位：${f.unit}。` : ""}`);
          row.append(el("p", `${f.id}${f.choices ? ` · 选项：${f.choices.join(" / ")}` : ""}`, "caption"));
          steps.append(row);
        }
        fields.append(steps); node.append(fields);
      }
      if (task.choices) node.append(el("p", `选项：${task.choices.join(" / ")}`, "caption"));
      const links = el("div", "", "r-targets");
      for (const name of task.targets) {
        const t = suite.targets.find(t => t.name === name);
        if (t) links.append(button(`${name} ↗`, () => {
          selectSource(t.release, t.source_id);
          document.getElementById("observations").scrollIntoView({ block: "start" });
        }, "r-target"));
      }
      node.append(links);
      const materials = details(`材料与证据（${task.material_ids.length}）`, "r-materials");
      for (const id of task.material_ids) {
        const m = suite.materials.find(m => m.id === id);
        if (m) materials.append(renderMaterial(m, suite));
      }
      const solution = details("参考解答与推导（待复核）", "r-answer");
      const ref = task.reference;
      if (ref.kind === "structured") {
        const checks = el("ol", "", "r-checkpoints");
        for (const check of ref.checkpoints) {
          const field = task.response_fields.find(f => f.id === check.id);
          const row = el("li", field ? field.request : check.id);
          row.append(el("p", `${answers[check.answer] || check.answer}${check.unit ? ` ${check.unit}` : ""}`, "r-answer-value"),
            el("p", `证据：${check.required_evidence_ids.join("、")}`, "caption"));
          checks.append(row);
        }
        solution.append(checks);
      } else solution.append(el("p", `${answers[ref.answer] || ref.answer}${ref.unit ? ` ${ref.unit}` : ""}`, "r-answer-value"));
      const steps = el("ol"); (ref.worked_solution || []).forEach(step => steps.append(el("li", step))); solution.append(steps);
      solution.append(el("p", `参考证据：${ref.required_evidence_ids.join("、")}`, "caption"));
      node.append(materials, solution); return node;
    }
    function draw() {
      list.replaceChildren(); let shown = 0, total = 0;
      for (const suite of data.suites) {
        const wanted = suite.targets.find(t => keyOf(t) === target.value);
        total += suite.tasks.length;
        for (const task of suite.tasks) {
          if (target.value && (!wanted || !task.targets.includes(wanted.name))) continue;
          if (category.value && task.category !== category.value) continue;
          if (level.value && (task.task_level || "basic") !== level.value) continue;
          list.append(card(task, suite)); shown++;
        }
      }
      count.textContent = `${shown} / ${total} 道任务`;
      if (!shown) list.append(el("p", total ? "当前天体或类型没有关联任务。可选择“全部天体”或查看全部任务。"
        : window.AstroDemo ? "本次在线快照尚未包含科研任务。" : "暂无已生成的科研任务包。", "empty"));
    }
    async function reload() {
      const token = ++request;
      try {
        const incoming = await load();
        if (token !== request) return;
        data = incoming; updateTargets();
        const tasks = data.suites.flatMap(s => s.tasks);
        const integrated = tasks.filter(t => t.task_level === "integrated_case");
        summary.textContent = `${targets().size} 颗天体 · ${new Set(data.suites.flatMap(s => s.papers.map(p => p.versioned_id))).size} 篇论文 · ${integrated.length} 个综合案例 / ${tasks.length - integrated.length} 道基础题 · ${integrated.reduce((n, t) => n + t.response_fields.length, 0)} 个综合评分检查点。参考解答待复核；尚未进行模型试答。`;
        warning.textContent = (data.warnings || []).join("\n"); warning.hidden = !warning.textContent;
        provenance.replaceChildren(el("summary", "任务包版本与来源"));
        for (const suite of data.suites) provenance.append(el("p", `${suite.suite_id} · ${suite.created_at}\nSHA-256 ${suite.bundle_sha256}\n开发集 · 参考解答待复核 · 不代表模型评测成绩`, "r-hash"));
        provenance.hidden = !data.suites.length;
        draw();
      } catch (error) {
        if (token === request) { warning.hidden = false; warning.textContent = `科研任务读取失败：${error.message}`; summary.textContent = "科研任务暂时不可用"; }
      }
    }
    target.addEventListener("change", draw); category.addEventListener("change", draw); level.addEventListener("change", draw);
    return { reload, setSource(source) { selected = source; updateTargets(source ? keyOf(source) : ""); draw(); },
      bibliographyFor(release, sourceId) {
        const papers = data.suites.flatMap(suite => {
          const t = suite.targets.find(t => keyOf(t) === `${release}:${sourceId}`);
          return t ? suite.papers.filter(p => p.targets.includes(t.name)).map(p => ({
            title: p.title, arxiv_id: p.versioned_id, versioned_id: p.versioned_id, association_origin: "research_task",
          })) : [];
        });
        return [...new Map(papers.map(p => [p.versioned_id, p])).values()];
      },
    };
  }
  window.AstroResearch = { mount };
})();
