/* Shared by the live library and immutable offline exports. Paper text is data. */
(() => {
  "use strict";
  const roles = { mentioned: "提到", in_sample: "纳入样本", analysed: "本研究分析", reports_result: "本研究结果", cites_prior_result: "引用既有结果", comparison: "比较对象", calibrator: "校准对象" };
  const scopes = { source: "单源", component: "分量", system: "系统" };
  const assessments = { evidence_found: "有原文证据", uncertain: "关系不确定", not_found_in_inspected_blocks: "已检查范围未发现" };
  const membership = { member: "成员", candidate: "候选成员", non_member: "非成员", uncertain: "成员资格未定" };
  function el(tag, value, className) {
    const node = document.createElement(tag);
    node.textContent = value ?? "";
    if (className) node.className = className;
    return node;
  }
  function button(label, action, className = "") {
    const node = el("button", label, className); node.type = "button";
    node.addEventListener("click", action); return node;
  }
  function external(label, url) {
    const node = el("a", label);
    try { const parsed = new URL(url); if (parsed.protocol === "https:") node.href = parsed.href; } catch (_) { /* no link */ }
    node.target = "_blank"; node.rel = "noopener noreferrer"; return node;
  }
  const paperKey = (p) => p.arxiv_id ? `arxiv:${p.arxiv_id.replace(/v\d+$/, "")}` : p.doi ? `doi:${p.doi.toLowerCase()}` : `bib:${p.bibcode}`;
  function highlight(node, unit, span) {
    const chars = Array.from(unit.text); // Python offsets count Unicode code points, not UTF-16 units.
    const same = unit.block_id === span.block_id && unit.field === span.field && ["row", "column", "note_index"].every(k => (unit[k] ?? null) === (span[k] ?? null));
    const start = Math.max(span.start, unit.start), end = Math.min(span.end, unit.end);
    if (!same || start >= end) { node.append(document.createTextNode(unit.text)); return; }
    node.append(document.createTextNode(chars.slice(0, start - unit.start).join("")),
      el("mark", chars.slice(start - unit.start, end - unit.start).join("")),
      document.createTextNode(chars.slice(end - unit.start).join("")));
  }
  function renderContext(root, data, span) {
    root.replaceChildren();
    if (!data.available) { root.append(el("p", "固定版本的全文当前不可用。")); return; }
    const paper = data.paper;
    root.append(el("p", `${paper.versioned_id} · ${paper.title || ""}`, "e-meta"));
    const first = data.units[0];
    if (first) {
      root.append(el("p", (first.section_path || []).map(s => typeof s === "string" ? s : s.title || s.id || "").join(" / "), "e-meta"));
      root.append(external("打开原文位置 ↗", first.source_url));
    }
    root.append(el("p", `${span.block_id} · ${span.field}${span.row !== undefined ? ` · 原始行 ${span.row} / 列 ${span.column}（从 0 起）` : ""} · 字符 ${span.start}–${span.end}`, "e-meta"));
    let table = null; const rows = new Map(), cells = new Map();
    for (const unit of data.units) {
      if (unit.field === "table_cell") {
        if (!table) { table = el("table", "", "e-table"); const wrap = el("div", "", "e-table-wrap"); wrap.append(table); root.append(wrap); }
        if (!rows.has(unit.row)) { const tr = el("tr"); rows.set(unit.row, tr); table.append(tr); }
        const key = `${unit.row}:${unit.column}`;
        if (!cells.has(key)) {
          const structure = unit.cell_structure || {};
          const cell = el(structure.header ? "th" : "td");
          cell.rowSpan = structure.rowspan || 1; cell.colSpan = structure.colspan || 1;
          cells.set(key, cell); rows.get(unit.row).append(cell);
        }
        highlight(cells.get(key), unit, span);
      } else {
        const paragraph = el(unit.field === "raw" ? "pre" : "p", "", unit.field === "table_note" ? "e-meta" : "e-passage");
        highlight(paragraph, unit, span); root.append(paragraph);
      }
    }
    const provenance = el("details"); provenance.append(el("summary", "固定版本与完整性"), el("p", `version ${paper.paper_version_id}\nJSON SHA-256 ${paper.document_sha256}\nparser ${paper.parser_version}`, "e-hash"));
    root.append(provenance);
    root.querySelector("mark")?.scrollIntoView({ block: "center" });
  }
  function mount(root, { records = [], bibliography = [], loadContext, snapshotLabel = "" } = {}) {
    root.replaceChildren(); root.classList.add("evidence-view");
    const groups = new Map();
    for (const record of records) {
      const key = paperKey(record.paper);
      if (!groups.has(key)) groups.set(key, { paper: record.paper, records: [] });
      groups.get(key).records.push(record);
    }
    for (const paper of bibliography) {
      // Match known identifiers only: titles alone do not establish work identity.
      let group = groups.get(paperKey(paper)) || [...groups.values()].find(g => paper.doi && g.paper.doi && paper.doi.toLowerCase() === g.paper.doi.toLowerCase());
      if (!group) { group = { paper, records: [] }; groups.set(paperKey(paper), group); }
      group.bibliographic = true;
    }
    root.append(el("p", `${groups.size} 篇论文 · ${records.length} 条标注记录 · 全部待复核${snapshotLabel ? ` · ${snapshotLabel}` : ""}`, "e-summary"));
    const filter = el("select"); filter.setAttribute("aria-label", "筛选论文关系");
    filter.append(new Option("全部关系", ""), ...Object.entries(roles).map(([key, label]) => new Option(label, key)));
    const resultFilter = el("select"); resultFilter.setAttribute("aria-label", "筛选论文成员判断");
    resultFilter.append(new Option("全部成员判断", ""), ...Object.entries(membership).map(([key, label]) => new Option(label, key)));
    const list = el("div", "", "e-cards");
    root.append(filter, resultFilter, list);
    const dialog = el("dialog", "", "evidence-dialog");
    const content = el("div", "", "e-context");
    dialog.append(button("关闭原文", () => dialog.close()), content); root.append(dialog);
    let request = 0;
    dialog.addEventListener("close", () => { ++request; });
    async function openEvidence(record, span) {
      const token = ++request;
      content.replaceChildren(el("p", "正在读取固定版本原文…"));
      if (!dialog.open) dialog.showModal();
      try {
        const data = await loadContext(record.paper.paper_version_id, span.block_id);
        if (token === request && root.isConnected) renderContext(content, data, span);
      } catch (error) { if (token === request) content.replaceChildren(el("p", `原文读取失败：${error.message}`)); }
    }
    function evidenceButtons(container, record, spans) {
      spans.forEach((span, i) => container.append(button(`原文 ${i + 1} ↗`, () => openEvidence(record, span), "e-evidence-link")));
    }
    function draw() {
      list.replaceChildren();
      for (const group of groups.values()) {
        const visible = group.records.filter(r => (!filter.value || r.annotation.relations.some(rel => rel.role === filter.value))
          && (!resultFilter.value || (r.annotation.results || []).some(result => result.status === resultFilter.value)));
        if ((filter.value || resultFilter.value) && !visible.length) continue;
        const paper = group.paper, card = el("article", "", "e-paper");
        const url = paper.arxiv_id ? `https://arxiv.org/abs/${encodeURIComponent(paper.arxiv_id)}` : paper.doi ? `https://doi.org/${encodeURIComponent(paper.doi)}` : `https://ui.adsabs.harvard.edu/abs/${encodeURIComponent(paper.bibcode || "")}`;
        const heading = el("h3"); heading.append(external(paper.title || paper.bibcode || paper.versioned_id, url)); card.append(heading);
        card.append(el("p", `${paper.year || (paper.arxiv_year ? `${paper.arxiv_year} arXiv` : "年份未知")} · ${paper.arxiv_id || paper.bibcode || ""}${group.bibliographic ? paper.association_origin === "research_task" ? " · 科研任务选文" : " · 文献目录关联" : ""}`, "e-meta"));
        if (!group.records.length) card.append(el("p", "尚未进行关系标注；文献目录关联本身不说明研究角色。", "e-meta"));
        visible.forEach((record, index) => {
          const a = record.annotation, history = el("details", "", "e-record"); history.open = index === 0;
          history.append(el("summary", `${assessments[a.assessment]} · ${scopes[a.subject_scope]} · 待复核 · ${record.paper.versioned_id} · ${new Date(record.created_at * 1000).toLocaleDateString()}`));
          const identity = el("p", `${a.paper_name} · ${a.identity_status === "ambiguous" ? "身份有歧义" : "已记录身份依据，未独立复核"}`, "e-meta");
          evidenceButtons(identity, record, a.identity_evidence || []); history.append(identity);
          for (const relation of a.relations) {
            const row = el("div", "", `e-relation role-${relation.role}`);
            row.append(el("span", roles[relation.role], "e-role"), el("p", relation.statement));
            evidenceButtons(row, record, relation.evidence); history.append(row);
          }
          for (const result of a.results || []) {
            const claim = el("section", "", "e-result");
            claim.append(el("h4", `${result.association}${result.population ? ` / ${result.population}` : ""} · ${membership[result.status]}`),
              el("p", `${result.origin === "current_analysis" ? "本文判断" : "沿用前人判断"} · 原标签 ${result.reported_label}`, "e-role"),
              el("p", result.attribution), el("p", `依据：${result.method}`), el("p", `限定：${result.qualification}`));
            evidenceButtons(claim, record, result.evidence); history.append(claim);
          }
          if (a.note) history.append(el("p", a.note, "e-meta"));
          const provenance = el("details"); provenance.append(el("summary", "身份依据与标注批次"), el("p", `${a.identity_basis}\n${record.batch_id}\n${record.extractor.name} ${record.extractor.version} · ${record.extractor.prompt_version}\nversion ${record.paper.paper_version_id}\nJSON SHA-256 ${record.paper.document_sha256}`, "e-hash"));
          history.append(provenance); card.append(history);
        });
        list.append(card);
      }
      if (!list.childElementCount) list.append(el("p", "当前范围没有标注记录；这不表示天体与论文没有关系。", "e-meta"));
    }
    filter.addEventListener("change", draw); resultFilter.addEventListener("change", draw); draw();
  }
  window.AstroEvidence = { mount, renderContext, roles };
})();
