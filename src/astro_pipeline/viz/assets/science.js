/* Versioned scientific records and open research tasks. All content is text, never executable. */
(() => {
  "use strict";
  const kinds = { scientific_record: "科研记录", research_task: "研究任务" };
  const origins = { paper_report: "论文报告", prior_work: "引用前人", catalog_record: "星表记录", agent_calculation: "Agent 复算", agent_synthesis: "Agent 综合", synthetic_example: "模拟示例" };
  const units = { observation: "观测", method: "方法", analysis: "分析步骤", result: "结果", limitation: "限制" };
  const statuses = { planned: "待开展", in_progress: "进行中", completed: "已记录结果" };
  const el = (tag, text = "", cls = "") => { const n = document.createElement(tag); n.textContent = text; n.className = cls; return n; };
  const button = (label, action) => { const n = el("button", label, "quiet"); n.type = "button"; n.addEventListener("click", action); return n; };
  const detail = (label, cls) => { const n = el("details", "", cls); n.append(el("summary", label)); return n; };
  const key = t => `${t.release}:${t.source_id}`;
  function link(label, url) {
    const n = el("a", label);
    try { const u = new URL(url); if (u.protocol === "https:") n.href = u.href; } catch (_) {}
    n.target = "_blank"; n.rel = "noopener noreferrer"; return n;
  }
  function mount(root, { load, loadContext, selectSource }) {
    let selected = null, current = [], request = 0, offset = 0, contextRequest = 0;
    const summary = el("p", "正在读取科研语料…", "r-summary"); summary.id = "science-summary"; summary.setAttribute("role", "status");
    const toolbar = el("div", "", "r-toolbar");
    const kind = el("select"); kind.id = "science-kind";
    kind.append(new Option("全部内容", ""), ...Object.entries(kinds).map(([k, v]) => new Option(v, k)));
    const search = el("input"); search.id = "science-search"; search.type = "search"; search.placeholder = "主题、方法或结果关键词";
    for (const [label, control] of [["内容类型", kind], ["搜索语料", search]]) { const n = el("label", label); n.append(control); toolbar.append(n); }
    toolbar.append(button("查看全部科研内容", () => { selected = null; kind.value = ""; search.value = ""; reload(); }));
    const list = el("div", "", "sci-grid"); list.id = "science-list";
    const more = button("加载更多科研记录", () => reload(true)); more.hidden = true;
    const dialog = el("dialog", "", "evidence-dialog sci-dialog"), context = el("div", "", "e-context");
    dialog.append(button("关闭科研证据", () => dialog.close()), context);
    dialog.addEventListener("close", () => { ++contextRequest; });
    root.replaceChildren(summary, toolbar, list, more, dialog);

    async function showEvidence(evidence) {
      const token = ++contextRequest;
      context.replaceChildren(el("p", "正在定位证据…"));
      if (!dialog.open) dialog.showModal();
      const paper = evidence.paper, span = evidence.span;
      let result, notice = "";
      try {
        result = await loadContext(paper.paper_version_id, span.block_id);
        if (!result.available || result.paper.document_sha256 !== paper.document_sha256) throw new Error("原文不可用或版本不匹配");
        const matching = result.units.filter(u => u.block_id === span.block_id && u.field === span.field
          && ["row", "column", "note_index"].every(k => (u[k] ?? null) === (span[k] ?? null))).sort((a,b) => a.start-b.start);
        let end = span.start, quote = "";
        for (const u of matching) {
          if (u.start > end || u.end <= end || end >= span.end) continue;
          const next = Math.min(u.end, span.end);
          quote += Array.from(u.text).slice(end-u.start, next-u.start).join(""); end = next;
        }
        if (end !== span.end || quote !== span.quote) throw new Error("当前快照不含这段证据");
      } catch (_) {
        result = { available: true, paper, units: [{ ...span, text: span.quote }], coverage: "saved_excerpt" };
        notice = "展示入库时校验并保存的固定摘录；完整上下文请查看论文原文。";
      }
      if (token !== contextRequest) return;
      window.AstroEvidence.renderContext(context, result, span);
      if (notice) context.prepend(el("p", notice, "caption"));
    }

    function card(record, withHistory = true) {
      const item = record.item, node = el("article", "", "sci-record"); node.dataset.recordId = record.record_id;
      const badges = el("div", "", "r-task-top");
      badges.append(el("span", kinds[item.kind], "badge"), el("span", item.topic, "badge"), el("span", `版本 ${record.ordinal}`, "badge"), el("span", "待科学复核", "badge warning"));
      if (item.task) badges.append(el("span", statuses[item.task.status], "badge"));
      node.append(badges, el("h3", item.title, "r-question"), el("p", item.question, "sci-question"), el("p", item.summary, "sci-summary"));
      const targets = el("div", "", "r-targets");
      for (const t of item.targets) targets.append(button(`${t.name} · ${t.release.toUpperCase()} ↗`, () => {
        selectSource(t.release, t.source_id); document.getElementById("observations").scrollIntoView({ block: "start" });
      }));
      node.append(targets);
      if (item.task) {
        const task = detail("任务目标、步骤与预期产物", "sci-task-plan");
        task.append(el("p", item.task.objective));
        const plan = el("ol"); item.task.procedure.forEach(s => plan.append(el("li", s)));
        const outputs = el("ul"); item.task.expected_outputs.forEach(s => outputs.append(el("li", s)));
        task.append(plan, el("h4", "预期产物"), outputs); node.append(task);
      }
      const content = detail(`科研内容与分析过程（${item.units.length}）`, "sci-content");
      for (const unit of item.units) {
        const block = el("article", "", "sci-unit");
        block.append(el("h4", `${unit.id} · ${units[unit.kind]}`), el("span", origins[unit.origin], `badge sci-origin-${unit.origin}`),
          el("p", unit.statement), el("p", `归属：${unit.attribution}`, "caption"));
        if (unit.measurement) {
          const m = unit.measurement;
          block.append(el("p", `${m.quantity}：${m.reported_text} · 单位 ${m.unit}`, "sci-measurement"));
          const conditions = [m.uncertainty && `不确定度 ${m.uncertainty}`, m.limit_type !== "measurement" && m.limit_type,
            m.observed_at && `观测时间 ${m.observed_at}`, m.instrument && `仪器 ${m.instrument}`, m.band && `波段 ${m.band}`].filter(Boolean);
          if (conditions.length) block.append(el("p", conditions.join(" · "), "caption"));
        }
        if (unit.assumptions.length) block.append(el("p", `条件：${unit.assumptions.join("；")}`));
        if (unit.calculation) block.append(el("pre", unit.calculation, "sci-calculation"));
        block.append(el("p", `证据：${unit.evidence_ids.join("、") || "见引用记录"}${unit.depends_on.length ? `；依赖步骤：${unit.depends_on.join(" → ")}` : ""}`, "caption"));
        content.append(block);
      }
      if (!item.units.length) content.append(el("p", "任务尚未记录分析结果。", "caption"));
      node.append(content);
      const evidence = detail(`原文与数据证据（${record.evidence.length}）`, "sci-evidence");
      for (const e of record.evidence) {
        const box = el("article", "", "r-material"); box.append(el("h4", e.id));
        if (e.kind === "paper") {
          box.append(el("p", `${e.paper.title} · ${e.paper.versioned_id}`, "caption"), el("blockquote", e.span.quote, "r-quote"),
            el("p", `${e.span.block_id} · ${e.span.field} · 字符 ${e.span.start}–${e.span.end}`, "caption"),
            button("定位科研证据", () => showEvidence(e)), link("论文原文 ↗", e.paper.url));
        } else {
          box.append(el("p", `${e.release}:${e.source_id} · run ${e.run_id} · 行 ${e.member_ordinal}`, "caption"));
          for (const f of e.fields) box.append(el("p", `${f.column}：${f.value ?? "缺失"} ${f.unit}`));
        }
        evidence.append(box);
      }
      node.append(evidence);
      if (item.related_revision_ids.length) {
        const refs = detail(`引用的科研记录版本（${item.related_revision_ids.length}）`, "sci-related");
        for (const ref of item.related_revision_ids) {
          const row = el("div");
          row.append(button(`读取版本 ${ref}`, async () => {
            row.replaceChildren(el("p", "正在读取固定版本…"));
            try {
              const result = await load({ revision_id: ref });
              row.replaceChildren(result.items.length ? card(result.items[0], false) : el("p", "此版本未包含在当前快照中。"));
            } catch (error) { row.replaceChildren(el("p", error.message)); }
          })); refs.append(row);
        }
        node.append(refs);
      }
      const provenance = detail("记录来源与修订历史", "sci-history");
      provenance.append(el("p", `记录 ${record.record_id}\n版本 ${record.revision_id}\n作者 ${item.author.name} · ${item.author.version}\n修订说明：${record.change_note}`, "r-hash"));
      if (withHistory) {
        const history = el("div"); let historyOffset = 0;
        const historyButton = button("读取历史版本", async () => {
          historyButton.disabled = true;
          try {
            const result = await load({ record_id: record.record_id, include_history: "true", limit: 20, offset: historyOffset });
            for (const r of result.items) {
              const old = detail(`版本 ${r.ordinal} · ${r.change_note}`, "sci-old-version");
              old.append(card(r, false)); history.append(old);
            }
            historyOffset += result.items.length;
            historyButton.hidden = !result.page.has_more;
            if (window.AstroDemo) history.prepend(el("p", "在线页面仅包含本次显式导出的版本。", "caption"));
          } catch (error) { history.append(el("p", error.message)); }
          historyButton.disabled = false;
        });
        provenance.append(historyButton, history);
      }
      node.append(provenance); return node;
    }

    async function reload(append = false) {
      const token = ++request;
      if (!append) { offset = 0; current = []; list.replaceChildren(); }
      more.disabled = true;
      try {
        const result = await load({ kind: kind.value, text: search.value, limit: 20, offset,
          ...(selected ? { source_id: selected.source_id, release: selected.release } : {}) });
        if (token !== request) return;
        current.push(...result.items); offset += result.items.length;
        for (const r of result.items) list.append(card(r));
        summary.textContent = `${selected ? `${selected.release.toUpperCase()} · ${selected.source_id} · ` : ""}${result.page.total} 条科研内容 · 已显示 ${current.length} 条`;
        if (!current.length) list.append(el("p", "暂无符合条件的科研记录或任务。可选择其他天体或查看全部科研内容。", "empty"));
        more.hidden = !result.page.has_more;
      } catch (error) { if (token === request) summary.textContent = `科研内容读取失败：${error.message}`; }
      if (token === request) more.disabled = false;
    }
    kind.addEventListener("change", () => reload());
    let timer; search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => reload(), 300); });
    return { reload, setSource(value) { selected = value; return reload(); },
      bibliographyFor(release, sourceId) {
        const entries = current.filter(r => r.item.targets.some(t => key(t) === `${release}:${sourceId}`))
          .flatMap(r => r.evidence.filter(e => e.kind === "paper").map(e => [e.paper.versioned_id,
            { title: e.paper.title, arxiv_id: e.paper.versioned_id, versioned_id: e.paper.versioned_id, association_origin: "scientific_record" }]));
        return [...new Map(entries).values()];
      } };
  }
  window.AstroScience = { mount };
})();
