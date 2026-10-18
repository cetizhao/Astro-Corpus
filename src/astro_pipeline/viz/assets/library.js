(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = {
    meta: null, revision: null, page: null, cursor: null, history: [],
    sky: [], selected: null, detail: null, spectrum: null,
    refreshing: false, refreshQueued: false, detailToken: 0, spectrumToken: 0,
    loadingToken: 0, lastSync: null,
  };
  const charts = { sky: null, spectrum: null };
  const labels = {
    ready: ["✓ 已登记", "good"], unavailable: ["— 档案库无产品", ""],
    not_downloaded: ["↓ 未下载", ""], pending: ["◷ 获取中", "warning"],
    failed: ["! 获取失败", "error"], corrupt: ["! 文件异常", "error"],
    unsupported: ["○ 待适配", "warning"], unknown: ["? 未知", ""],
    completed: ["✓ 完成", "good"], complete: ["✓ 完成", "good"], partial: ["! 部分完成", "warning"],
    non_dataset: ["尚未发布", "warning"], unpublished: ["尚未发布", ""],
    running: ["◷ 进行中", "warning"], finalizing: ["◷ 发布中", "warning"],
    current: ["当前已发布", "good"], historical: ["历史", ""],
    working: ["工作中", "warning"], prepared: ["待发布", "warning"],
    legacy_unverified: ["历史来源未验证", "warning"],
  };
  const finite = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
  const number = (value, digits = 3) => finite(value) ? Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits }) : "—";
  const integer = (value) => finite(value) ? Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 }) : "—";
  const scientific = (value) => finite(value) ? Number(value).toExponential(3) : "—";
  const keyOf = (source) => typeof source.key === "string" ? source.key : `${source.release}:${source.source_id}`;
  const colors = () => {
    const css = getComputedStyle(document.documentElement);
    return Object.fromEntries(["surface", "ink", "secondary", "line", "blue", "orange"].map((name) => [name, css.getPropertyValue(`--${name}`).trim()]));
  };
  const object = (value) => {
    if (value && typeof value === "object") return value;
    if (typeof value === "string") { try { return JSON.parse(value); } catch (_) { return {}; } }
    return {};
  };
  const text = (tag, value, className) => {
    const node = document.createElement(tag);
    node.textContent = value ?? "";
    if (className) node.className = className;
    return node;
  };
  function badge(value, fallback) {
    const pair = labels[value] || [fallback || value || "未知", ""];
    return text("span", pair[0], `badge ${pair[1]}`);
  }
  function notice(message = "", error = false) {
    $("notice").textContent = message;
    $("notice").hidden = !message;
    $("notice").classList.toggle("error", error);
  }
  function connection(message, kind = "") {
    $("connection").textContent = message;
    $("connection").className = `status ${kind}`;
  }
  function filters() {
    const result = { scope: $("scope").value };
    for (const [id, name] of [["search", "q"], ["release", "release"], ["dataset", "corpus_id"], ["spectrum-filter", "spectrum_state"]]) {
      const value = $(id).value.trim();
      if (value) result[name] = value;
    }
    return result;
  }
  async function api(path, params = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== "") query.set(key, String(value));
    }
    const response = await fetch(path + (query.size ? `?${query}` : ""), { cache: "no-store", credentials: "same-origin" });
    const data = await response.json();
    if (!response.ok) {
      const message = data.message || (typeof data.error === "string" ? data.error : data.error?.message);
      const error = new Error(message || `HTTP ${response.status}`);
      error.status = response.status;
      error.payload = data;
      throw error;
    }
    return data;
  }
  function updateSelect(select, entries, allText) {
    const selected = select.value;
    select.replaceChildren(new Option(allText, ""));
    for (const entry of entries) select.add(new Option(entry.label, entry.value));
    if ([...select.options].some((option) => option.value === selected)) select.value = selected;
  }
  function renderMeta(meta) {
    const releases = (meta.releases || []).map((item) => typeof item === "string"
      ? { value: item, label: `Gaia ${item.toUpperCase()}` }
      : { value: item.release || item.key, label: item.display_name || item.label || `Gaia ${(item.release || item.key || "").toUpperCase()}` });
    updateSelect($("release"), releases, "全部版本");
    const datasets = (meta.datasets || []).map((item) => ({ value: item.corpus_id || item.id, label: item.label || item.name || item.corpus_id }));
    updateSelect($("dataset"), datasets, "全部样本");
    $("library-summary").textContent = `全库 ${integer(meta.total_sources)} 个独立源 · ${(meta.datasets || []).length} 个来源样本`;
    const rows = $("runs");
    rows.replaceChildren();
    const runs = [...(meta.runs || [])].sort((left, right) => Number(right.started_at || 0) - Number(left.started_at || 0));
    if (!runs.length) rows.append(text("p", "尚无采集运行；旧数据导入会保留其来源标记。", "empty"));
    for (const run of runs.slice(0, 18)) {
      const info = object(run.metadata || run.metadata_json);
      const card = text("article", "", "run-card");
      card.append(text("div", run.label || run.corpus_label || run.corpus_id || "运行", "run-title"));
      const snapshot = run.snapshot_state || run.role || (info.published ? "current" : "unpublished");
      card.append(badge(run.status), document.createTextNode(" "), badge(snapshot === "pending" ? "unpublished" : snapshot));
      if (run.provenance_status === "legacy_unverified") card.append(badge("legacy_unverified"));
      card.append(text("div", `源 ${integer(info.n_unique_sources ?? run.n_unique_sources)} · 样本行 ${integer(info.n_sample_members ?? run.n_sample_members)}`, "run-meta"));
      const stages = run.stages || info.stages;
      if (stages) {
        const parts = Object.entries(stages).map(([name, stage]) => `${name}: ${stage.status || "—"}${stage.completed !== undefined ? ` ${stage.completed}/${stage.total ?? "?"}` : ""}`);
        card.append(text("div", parts.join(" · "), "run-meta"));
      }
      if (run.error || info.publication_error) card.append(text("div", run.error || info.publication_error, "run-meta"));
      card.append(text("div", `run ${String(run.run_id || "—").slice(0, 12)}`, "run-meta"));
      rows.append(card);
    }
    if (runs.length > 18) rows.append(text("p", `显示最近 18 次运行；另有 ${runs.length - 18} 次历史记录。源目录不受此展示数量限制。`, "caption"));
  }
  function renderTable(page) {
    const body = $("source-rows");
    body.replaceChildren();
    const sources = page.sources || [];
    for (const source of sources) {
      const row = document.createElement("tr");
      row.dataset.key = keyOf(source);
      row.tabIndex = 0;
      row.setAttribute("aria-selected", String(state.selected?.key === keyOf(source)));
      row.classList.toggle("selected", state.selected?.key === keyOf(source));
      const identity = document.createElement("td");
      identity.append(text("span", source.name || `Gaia ${String(source.release).toUpperCase()}`, "cell-primary"));
      identity.append(text("span", `${source.release} · ${source.source_id}`, "cell-secondary"));
      const pos = source.position || {};
      const position = document.createElement("td");
      position.className = "numeric";
      position.append(text("span", `${number(pos.ra_deg, 4)} / ${number(pos.dec_deg, 4)}`));
      position.append(text("span", pos.epoch !== null && pos.epoch !== undefined ? `J${Number(pos.epoch).toFixed(1)}` : "历元未知", "cell-secondary"));
      const magnitude = text("td", number(source.astrometry?.g_mag, 2), "numeric");
      const spectrum = document.createElement("td");
      spectrum.append(badge(source.spectrum_status || "unknown"));
      const datasets = document.createElement("td");
      const current = source.datasets || [];
      const working = source.working_datasets || [];
      datasets.append(text("span", current.map((item) => item.label || item.name || item).join("、") || "—"));
      const activeWork = working.filter((item) => item.snapshot_state !== "historical");
      const history = working.filter((item) => item.snapshot_state === "historical");
      if (activeWork.length) datasets.append(text("span", `${activeWork.length} 个未发布关联`, "cell-secondary"));
      if (history.length) datasets.append(text("span", `${history.length} 个历史关联`, "cell-secondary"));
      if (source.provenance_status === "legacy_unverified") datasets.append(badge("legacy_unverified"));
      row.append(identity, position, magnitude, spectrum, datasets);
      const select = () => selectSource(source.release, String(source.source_id));
      row.addEventListener("click", select);
      row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } });
      body.append(row);
    }
    if (!sources.length) {
      const cell = text("td", "当前筛选没有源。可调整筛选，或等待采集提交新记录。", "empty");
      cell.colSpan = 5;
      const row = document.createElement("tr"); row.append(cell); body.append(row);
    }
    $("table-total").textContent = `共 ${integer(page.total)} 个源 · 本页 ${sources.length} 个`;
    $("page-label").textContent = `第 ${state.history.length + 1} 页`;
    $("previous").disabled = !state.history.length;
    $("next").disabled = !page.has_more;
    $("count-sources").textContent = integer(page.total);
    $("count-spectra").textContent = integer(page.registered_xp_ready_sources ?? page.total_ready_spectra);
  }
  function hammer(ra, dec) {
    const longitude = ((((Number(ra) - 180) + 180) % 360 + 360) % 360 - 180) * Math.PI / 180;
    const latitude = Number(dec) * Math.PI / 180;
    const denominator = Math.sqrt(1 + Math.cos(latitude) * Math.cos(longitude / 2));
    return [-2 * Math.SQRT2 * Math.cos(latitude) * Math.sin(longitude / 2) / denominator,
      Math.SQRT2 * Math.sin(latitude) / denominator];
  }
  function graticule(color) {
    const lines = [];
    for (const ra of [0.00001, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330, 359.99999]) {
      const points = [];
      for (let dec = -90; dec <= 90; dec += 3) points.push(hammer(ra, dec));
      lines.push(points);
    }
    for (let dec = -60; dec <= 60; dec += 30) {
      const points = [];
      for (let ra = .01; ra < 360; ra += 3) points.push(hammer(ra, dec));
      lines.push(points);
    }
    return lines.map((data) => ({ type: "line", data, symbol: "none", silent: true, animation: false, lineStyle: { width: 1, color }, tooltip: { show: false }, z: 0 }));
  }
  function renderSky(reset = false) {
    if (!window.echarts) return;
    const zoom = !reset && charts.sky ? charts.sky.getOption()?.dataZoom : null;
    const theme = colors();
    if (!charts.sky) {
      charts.sky = echarts.init($("sky-chart"));
      charts.sky.on("click", (event) => {
        if (event.data?.source) selectSource(event.data.source.release, String(event.data.source.source_id));
      });
    }
    const points = state.sky.filter((point) => finite(point.ra_deg) && finite(point.dec_deg) && Number(point.dec_deg) >= -90 && Number(point.dec_deg) <= 90);
    const data = points.map((point) => ({ value: hammer(point.ra_deg, point.dec_deg), source: point, name: keyOf(point) }));
    const selected = data.filter((point) => point.name === state.selected?.key);
    const tooltip = (event) => {
      const point = event.data?.source;
      return point ? `${point.name || "Gaia"}\n${point.release} ${point.source_id}\nRA ${number(point.ra_deg, 5)}°\nDec ${number(point.dec_deg, 5)}°` : "";
    };
    const width = $("sky-chart").clientWidth;
    const height = $("sky-chart").clientHeight;
    const plotWidth = Math.min(width - 48, 2 * (height - 36));
    const plotHeight = plotWidth / 2;
    charts.sky.setOption({
      animation: false, backgroundColor: theme.surface,
      grid: { left: (width - plotWidth) / 2, top: (height - plotHeight) / 2, width: plotWidth, height: plotHeight },
      xAxis: { type: "value", min: -3.05, max: 3.05, show: false },
      yAxis: { type: "value", min: -1.52, max: 1.52, show: false },
      tooltip: { trigger: "item", renderMode: "richText", formatter: tooltip, confine: true, backgroundColor: theme.surface, textStyle: { color: theme.ink } },
      dataZoom: [{ type: "inside", xAxisIndex: 0, filterMode: "none", start: zoom?.[0]?.start ?? 0, end: zoom?.[0]?.end ?? 100 }, { type: "inside", yAxisIndex: 0, filterMode: "none", start: zoom?.[1]?.start ?? 0, end: zoom?.[1]?.end ?? 100 }],
      series: [...graticule(theme.line),
        { type: "scatter", silent: true, symbolSize: 0, z: 1,
          label: { show: true, formatter: "{b}", position: "top", color: theme.secondary, fontSize: 10 },
          data: [90, 180, 270].map((ra) => ({ value: hammer(ra, 0), name: `${ra}°` })) },
        { type: "scatter", silent: true, symbolSize: 0, z: 1,
          label: { show: true, formatter: "{b}", position: "left", color: theme.secondary, fontSize: 10 },
          data: [-60, -30, 30, 60].map((dec) => ({ value: hammer(180, dec), name: `${dec > 0 ? "+" : ""}${dec}°` })) },
        { name: "已登记源", type: "scatter", data, symbolSize: 8, progressive: 1000, itemStyle: { color: theme.blue, borderColor: theme.surface, borderWidth: 2 }, emphasis: { scale: 1.4 }, z: 2 },
        { name: "选中源", type: "scatter", data: selected, symbolSize: 14, itemStyle: { color: theme.orange, borderColor: theme.surface, borderWidth: 2 }, z: 5 },
        { name: "交互区域", type: "scatter", data, symbolSize: 24, itemStyle: { opacity: 0 }, emphasis: { itemStyle: { opacity: 0 } }, z: 6 },
      ],
    }, { notMerge: true });
  }
  async function loadSky(filter, revision, token) {
    let cursor = null;
    let points = [];
    let summary = null;
    const seen = new Set();
    do {
      const page = await api("/api/v1/sky", { ...filter, revision, limit: 500, cursor });
      if (token !== state.loadingToken) return;
      summary = page;
      points = points.concat(page.points || []);
      $("sky-progress").textContent = `位置载入 ${integer(points.length)} / ${integer(page.total_positions)} · 无坐标 ${integer(page.missing_positions)}`;
      if (!page.has_more) break;
      if (!page.next_cursor || seen.has(page.next_cursor)) throw new Error("天空图分页游标未前进，已停止以避免重复数据。");
      seen.add(page.next_cursor);
      cursor = page.next_cursor;
    } while (true);
    if (token !== state.loadingToken) return;
    const unique = new Map(points.map((point) => [keyOf(point), point]));
    state.sky = [...unique.values()];
    $("count-positions").textContent = integer(summary?.total_positions || 0);
    $("count-missing").textContent = integer(summary?.missing_positions || 0);
    if (finite(summary?.registered_xp_ready_sources)) $("count-spectra").textContent = integer(summary.registered_xp_ready_sources);
    $("sky-progress").textContent = `已载入 ${integer(state.sky.length)} / ${integer(summary?.total_positions || 0)} 个有效位置`;
    if (summary && state.sky.length !== Number(summary.total_positions)) notice("当前坐标加载数量与总数不同；请同步重试。", true);
    renderSky();
  }
  async function refreshView(force = false) {
    if (state.refreshing) { state.refreshQueued ||= force; return; }
    state.refreshing = true;
    const token = ++state.loadingToken;
    document.body.classList.add("is-loading");
    connection("◷ 同步中");
    try {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const meta = await api("/api/v1/meta");
          if (token !== state.loadingToken) return;
          const changed = meta.revision !== state.revision;
          state.meta = meta;
          renderMeta(meta);
          if (!meta.exists) {
            state.revision = meta.revision;
            state.sky = [];
            state.cursor = null;
            state.history = [];
            state.selected = null;
            ++state.detailToken;
            clearDetail("共享库当前不可用，请等待数据后重新选择源。");
            $("source-title").textContent = "请选择一个源";
            state.lastSync = null;
            $("last-sync").textContent = "尚未同步";
            state.page = { sources: [], total: 0, has_more: false, registered_xp_ready_sources: 0 };
            renderTable(state.page);
            $("count-positions").textContent = "0";
            $("count-missing").textContent = "0";
            $("sky-progress").textContent = "共享库尚未建立";
            renderSky();
            notice("当前没有共享库。请通过采集或显式导入建立数据；只读页面不会自动创建数据库。");
            connection("○ 等待数据", "warning");
            return;
          }
          if (!changed && !force) { connection("● 已同步", "good"); return; }
          if (changed) { state.cursor = null; state.history = []; }
          state.revision = meta.revision;
          const filter = filters();
          const page = await api("/api/v1/sources", { ...filter, revision: state.revision, limit: $("page-size").value, cursor: state.cursor });
          state.page = page;
          renderTable(page);
          await loadSky(filter, state.revision, token);
          state.lastSync = new Date();
          $("last-sync").textContent = `最近同步 ${state.lastSync.toLocaleString()}`;
          if (state.selected) await selectSource(state.selected.release, state.selected.source_id, true);
          if (!state.sky.length && Number(page.total) > 0) notice("当前源没有可绘制的坐标；仍可从目录查看其基本信息和产品状态。");
          else notice();
          connection("● 已同步", "good");
          return;
        } catch (error) {
          if (error.status === 409 && attempt < 2) { state.revision = null; state.cursor = null; state.history = []; continue; }
          throw error;
        }
      }
    } catch (error) {
      state.revision = null;
      notice(error.status === 409 ? "库正在持续更新，已保留当前画面，稍后自动重试。" : `同步失败：${error.message}`, true);
      connection("! 同步待重试", "warning");
    } finally {
      state.refreshing = false;
      document.body.classList.remove("is-loading");
      if (state.refreshQueued) { state.refreshQueued = false; refreshView(true); }
    }
  }
  function property(label, value) {
    const wrapper = text("div", "", "property");
    wrapper.append(text("dt", label), text("dd", value));
    return wrapper;
  }
  function renderDetail(data) {
    const source = data.dto || data.source || {};
    const basic = source.astrometry || {};
    const pos = source.position || {};
    $("source-title").textContent = source.name || `Gaia ${String(source.release || state.selected.release).toUpperCase()}`;
    $("source-identity").textContent = `${source.release || state.selected.release} · ${source.source_id || state.selected.source_id}`;
    const badges = $("source-badges"); badges.replaceChildren();
    badges.append(badge(source.spectrum_status || "unknown"));
    if (source.provenance_status) badges.append(badge(source.provenance_status));
    if ((source.working_datasets || data.working_datasets || []).some((item) => item.snapshot_state !== "historical" && ["running", "finalizing"].includes(item.status))) badges.append(badge("working"));
    $("source-properties").replaceChildren(
      property("赤经 RA / 赤纬 Dec (deg)", `${number(pos.ra_deg, 6)} / ${number(pos.dec_deg, 6)}`),
      property("坐标框架 / 参考历元", `${pos.frame || "未知"} / ${finite(pos.epoch) ? `J${Number(pos.epoch).toFixed(1)}` : "未知"}`),
      property("视差 (mas)", number(basic.parallax_mas)),
      property("自行 μα* / μδ (mas yr⁻¹)", `${number(basic.pmra_masyr)} / ${number(basic.pmdec_masyr)}`),
      property("G / BP / RP (mag)", `${number(basic.g_mag, 2)} / ${number(basic.bp_mag, 2)} / ${number(basic.rp_mag, 2)}`),
      property("径向速度 (km s⁻¹)", number(basic.radial_velocity_kms, 2)),
      property("RUWE", number(basic.ruwe)),
      property("身份", "Gaia release + source_id"),
    );
    const provenance = $("source-provenance"); provenance.replaceChildren();
    const datasets = data.datasets || source.datasets || [];
    const working = data.working_datasets || source.working_datasets || [];
    const scopes = [{ value: "global", label: "全库当前缓存" }];
    for (const item of [...datasets, ...working]) {
      const row = text("div", "", "provenance-item");
      row.append(text("strong", item.label || item.corpus_label || item.corpus_id));
      const snapshot = item.snapshot_state || item.role || (working.includes(item) ? "unpublished" : "current");
      row.append(badge(item.status), badge(snapshot === "pending" ? "unpublished" : snapshot));
      row.append(text("span", item.provenance_status || "来源见运行记录"));
      const runId = item.run_id || item.current_run_id;
      if (runId) row.append(text("div", `run ${runId}`, "cell-secondary"));
      if (item.match_method) row.append(text("div", `匹配 ${item.match_method} · 距离 ${number(item.match_sep_arcsec)} arcsec`, "cell-secondary"));
      provenance.append(row);
      if (item.corpus_id && runId && (!working.includes(item) || item.snapshot_state === "historical")) scopes.push({ value: `${item.corpus_id}|${runId}`, label: `${item.label || item.corpus_id} · ${item.snapshot_state === "historical" ? "历史" : "当前"} ${runId.slice(0, 8)}` });
    }
    if (!datasets.length && !working.length) provenance.append(text("p", "暂无已发布样本关联；该源可能仅由产品或文献记录登记。"));
    const fields = data.source?.field_provenance || source.field_provenance;
    if (fields && Object.keys(fields).length) provenance.append(text("p", `已记录 ${Object.keys(fields).length} 个字段的来源。位置／参数为空不等于零。`, "caption"));
    $("provenance-section").hidden = false;
    const chosen = $("spectrum-scope").value;
    $("spectrum-scope").replaceChildren(...scopes.map((item) => new Option(item.label, item.value)));
    if (scopes.some((item) => item.value === chosen)) $("spectrum-scope").value = chosen;
    const bibliography = data.active_bibliography;
    const papers = bibliography?.papers || data.papers || [];
    const groups = new Map();
    for (const [i,paper] of papers.entries()) {
      const title=(paper.title||"").normalize("NFKC").trim().replace(/\s+/g," ").toLowerCase();
      const key=title||`untitled:${paper.bibcode||i}`;
      if(!groups.has(key))groups.set(key,[]);
      groups.get(key).push(paper);
    }
    const year=p=>Number(p.year)||0;
    const grouped=[...groups.values()].map(g=>g.sort((a,b)=>year(b)-year(a)))
      .sort((a,b)=>year(b[0])-year(a[0]));
    $("paper-heading").textContent = `文献关联 · ${grouped.length} 项 / ${papers.length} 条来源${bibliography?.stale ? " · 缓存已过期" : ""}`;
    const paperList = $("source-papers"); paperList.replaceChildren();
    for (const group of grouped) {
      const node = text("div", "", "paper-item");
      node.paperRecords=group;
      node.append(text("strong",group[0].title||group[0].bibcode||"论文"));
      for(const paper of group){
      const link = document.createElement("a");
      link.textContent = `${paper.year || "年份未知"} · ${paper.journal || ""} · ${paper.bibcode || ""}${paper.doi ? " · DOI: "+paper.doi : ""}${paper.arxiv_id ? " · arXiv: "+paper.arxiv_id : ""}`;
      link.href = paper.doi ? `https://doi.org/${encodeURIComponent(paper.doi)}` : paper.arxiv_id ? `https://arxiv.org/abs/${encodeURIComponent(paper.arxiv_id)}` : `https://ui.adsabs.harvard.edu/abs/${encodeURIComponent(paper.bibcode || "")}`;
      link.target = "_blank"; link.rel = "noopener noreferrer";
      link.className="cell-secondary";
      node.append(link);
      }
      paperList.append(node);
    }
    if (!papers.length) paperList.append(text("p", bibliography ? "本次文献观测没有关联条目；这与查询失败/尚未查询不同。" : "尚无可用文献观测。"));
    $("paper-section").hidden = false;
  }
  function clearDetail(message) {
    document.getElementById("online-paper-panel")?.remove();
    document.getElementById("paper-evidence-section")?.remove();
    ++state.spectrumToken;
    state.detail = null;
    state.spectrum = null;
    for (const id of ["source-title", "source-identity", "source-badges", "source-properties", "source-provenance", "source-papers", "spectrum-provenance", "spectrum-note", "spectrum-table-head", "spectrum-table-body"]) $(id).replaceChildren();
    for (const id of ["provenance-section", "paper-section", "spectrum-chart", "spectrum-table-section"]) $(id).hidden = true;
    charts.spectrum?.clear();
    $("spectrum-empty").hidden = false;
    $("spectrum-empty").textContent = message;
    $("spectrum-scope").replaceChildren(new Option("全库当前缓存", "global"));
    $("spectrum-product").value = "";
  }
  async function selectSource(release, sourceId, preserve = false) {
    const token = ++state.detailToken;
    const key = `${release}:${sourceId}`;
    if (!preserve && state.selected?.key !== key) {
      clearDetail("正在读取所选源…");
      $("source-title").textContent = "正在读取源";
      $("source-identity").textContent = `${release} · ${sourceId}`;
    }
    state.selected = { key, release, source_id: String(sourceId) };
    renderSky();
    for (const row of $("source-rows").querySelectorAll("tr[data-key]")) {
      row.classList.toggle("selected", row.dataset.key === key);
      row.setAttribute("aria-selected", String(row.dataset.key === key));
    }
    try {
      const data = await api(`/api/v1/source/${encodeURIComponent(release)}/${encodeURIComponent(sourceId)}`);
      if (token !== state.detailToken) return;
      if (data.resolved === false) throw new Error(data.error || "源记录当前不可用");
      state.detail = data;
      renderDetail(data);
      attachPaperActions(release, sourceId, token, data);
      await loadSpectrum();
    } catch (error) {
      if (token !== state.detailToken) return;
      clearDetail(`读取失败：${error.message}`);
      $("source-title").textContent = "无法读取源";
      $("source-identity").textContent = error.message;
    }
  }
  function attachPaperActions(release, sourceId, token, data) {
    const papers=(data.active_bibliography?.papers||data.papers||[]);
    const list=$("source-papers"); const rows=new Map();let timer=null, busy=false;
    const endpoint=`/api/v1/online-evidence/${encodeURIComponent(release)}/${encodeURIComponent(sourceId)}`;
    const tools=text("div","","paper-actions");
    const all=text("button","全选"),none=text("button","清空选择"),selected=text("button","检索所选（0）"),batch=text("button","批量检索未完成"),stop=text("button","停止");
    const progress=text("p","勾选论文后批量检索，或点击每篇旁的“检索关联”。在线读取HTML，不下载PDF。","caption");
    tools.append(all,none,selected,batch,stop,progress);
    const labels={title:"标题",abstract:"摘要",body:"正文",figure_caption:"图注",table:"表格",vizier:"VizieR表"};
    const statuses={identifier_unresolved:"本次未匹配到arXiv编号（不代表不存在）",identifier_candidates:"检索到候选，论文身份待确认",identifier_lookup_failed:"检索失败，可重试",no_arxiv_identifier:"编号未解析，可重试",insufficient_metadata:"论文信息不足",html_unavailable_or_failed:"HTML不可用或请求失败",no_match_in_supplied_materials:"已读取的HTML中未发现命中；图内文字和附表未检查"};
    function update(){selected.textContent=`检索所选（${new Set([...rows.values()].filter(r=>r.check.checked).map(r=>r.check)).size}项）`;selected.disabled=busy;batch.disabled=busy;stop.disabled=!busy;for(const r of rows.values())r.button.disabled=busy;}
    [...list.children].forEach(node=>{const group=(node.paperRecords||[]).filter(p=>p.bibcode);const paper=group[0];if(!paper)return;
      const check=document.createElement("input");check.type="checkbox";check.setAttribute("aria-label",`选择论文 ${paper.title||paper.bibcode}`);check.onchange=update;
      const button=text("button","检索关联");button.onclick=()=>submit(group.map(p=>p.bibcode));
      const controls=text("div","","paper-actions");controls.append(check,button);
      node.prepend(controls);
      for(const source of group){const output=text("div","","paper-evidence-inline");node.append(output);
      rows.set(source.bibcode,{check,button,output,signature:null,done:false,multiple:group.length>1});}
    });list.prepend(tools);$("paper-section").open=true;
    function render(job){busy=job.state==="running";update();
      if(job.state!=="idle")progress.textContent=`${({running:"检索中",completed:"本次完成",cancelled:"已停止",failed:"失败",paused:"暂停"}[job.state]||job.state)} · ${job.processed||0}/${job.total??"待确认"} 篇${job.current?" · "+job.current:""}${job.error?" · "+job.error:""}`;
      for(const item of job.papers||[]){const r=rows.get(item.paper.bibcode);if(!r)continue;const signature=JSON.stringify(item);if(r.signature===signature)continue;r.signature=signature;r.done=item.status==="mentions_found"||item.status==="no_match_in_supplied_materials";r.output.replaceChildren();
        if(r.multiple)r.output.append(text("p",`来源：${item.paper.bibcode}`,"caption"));
        r.output.append(text("p",item.groups?.length?"出现在："+item.groups.map(k=>labels[k]||k).join("、"):statuses[item.status]||item.status));
        if(item.status==="rate_limited")r.output.firstElementChild.textContent="arXiv限流，待重试（不代表没有论文）";
        if(item.error)r.output.append(text("p",item.error,"caption"));
        if(item.status==="no_match_in_supplied_materials")r.output.firstElementChild.textContent="论文HTML标题、摘要、正文、图注及表格文本中未匹配到别名。";
        if(item.aliases_checked){const d=document.createElement("details");d.append(text("summary",`查看实际匹配的 ${item.aliases_checked.length} 个名称`),text("p",item.aliases_checked.join("；")));r.output.append(d);}
        if(item.vizier){const v=item.vizier;const messages={mentions_found:"在VizieR关联表中找到",no_linked_catalog_found:"未找到该bibcode对应的VizieR目录",no_match_in_linked_tables:"已返回的VizieR关联表文本未命中",partial:"VizieR检查不完整（表格截断或部分请求失败），不能判断未出现",lookup_failed:"VizieR查询失败，不能判断未出现"};r.output.append(text("p",messages[v.status]||v.status));for(const t of v.tables||[])r.output.append(text("p",`${t.id}：检查 ${t.rows_checked} 行${t.possibly_truncated?"（可能截断）":""}`,"caption"));if(v.errors?.length)r.output.append(text("p",v.errors.join("；"),"caption"));}
        if(item.resolution?.errors?.length)r.output.append(text("p",item.resolution.errors.join("；"),"caption"));
        if(item.abstract){const abstract=document.createElement("details");abstract.append(text("summary","摘要"),text("p",item.abstract));r.output.append(abstract);}
        if(item.evidence?.length){const details=document.createElement("details");details.append(text("summary",`查看 ${item.evidence.length} 处原文证据`));for(const e of item.evidence){details.append(text("h4",`${labels[e.location_type]||e.location_type} · ${e.section||e.table_id||e.container_id||""}${e.row_index?" · 行 "+e.row_index:""}`),text("p",e.text));const url=e.locator?.url;if(url&&/^https?:\/\//i.test(url)){const a=text("a","原文位置");a.href=url;a.target="_blank";a.rel="noopener noreferrer";details.append(a);}}r.output.append(details);}
        for(const candidate of item.resolution?.candidates||[]){const a=text("a",`候选：${candidate.title}`);a.href=`https://arxiv.org/abs/${encodeURIComponent(candidate.arxiv_id)}`;a.target="_blank";a.rel="noopener noreferrer";r.output.append(a);}
      }
      for(const button of new Set([...rows.values()].map(r=>r.button))){const entries=[...rows].filter(([,r])=>r.button===button);button.textContent=busy&&entries.some(([bib])=>job.current_bibcode===bib)?"正在检索…":entries.some(([,r])=>r.signature)?"重新检索":"检索关联";}
      if(busy){clearTimeout(timer);timer=setTimeout(poll,2000);}
    }
    async function poll(){if(token!==state.detailToken||!tools.isConnected)return;try{const j=await api(endpoint);if(token===state.detailToken)render(j);}catch(e){progress.textContent="状态查询失败："+e.message;}}
    async function submit(bibcodes,cancel=false){if(!cancel&&!bibcodes.length){progress.textContent="请先选择论文。";return;}clearTimeout(timer);busy=true;update();try{const r=await fetch(endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({bibcodes,cancel})});const j=await r.json();if(!r.ok)throw Error(j.error?.message||"请求失败");if(token===state.detailToken)render(j);}catch(e){busy=false;update();progress.textContent=e.message;}}
    all.onclick=()=>{for(const r of rows.values())r.check.checked=true;update();};none.onclick=()=>{for(const r of rows.values())r.check.checked=false;update();};
    selected.onclick=()=>submit([...rows].filter(([,r])=>r.check.checked).map(([b])=>b));
    batch.onclick=()=>submit([...rows].filter(([,r])=>!r.done).map(([b])=>b));stop.onclick=()=>submit(null,true);update();poll();
  }
  function loadOnlinePanel(release, sourceId, token) {
    document.getElementById("online-paper-panel")?.remove();
    const panel = text("section", ""); panel.id="online-paper-panel";
    const start = text("button", "在线分析该源（10篇/批）");
    const stop = text("button", "停止");stop.disabled=true;
    const progress=text("p", "在线查询SIMBAD全部别名与书目，按需读取arXiv HTML，不下载PDF。无HTML时显示不可用，不误判为没有提及。");
    const output=text("div", "");
    panel.append(text("h3","在线论文分析"),start,stop,progress,output);
    $("paper-section").before(panel);
    const endpoint=`/api/v1/online-evidence/${encodeURIComponent(release)}/${encodeURIComponent(sourceId)}`;
    const labels={title:"标题",abstract:"摘要",body:"正文",figure_caption:"图注",table:"表格"};
    async function show(job) {
      if(token!==state.detailToken || !panel.isConnected)return;
      start.disabled=job.state==="running"||job.state==="completed";stop.disabled=job.state!=="running";
      start.textContent=job.processed ? "继续下一批（10篇）" : "在线分析该源（10篇/批）";
      progress.textContent=`${job.state} · 已处理 ${job.processed||0}/${job.total??"待查询"} 篇 · 别名 ${job.aliases?.length||0} 个${job.current ? " · "+job.current : ""}${job.error ? " · "+job.error : ""}`;
      output.replaceChildren();
      const buckets = {};
      for (const [key,label] of Object.entries({title:"标题出现",abstract:"摘要出现",body:"正文段落",figure_caption:"图注",table:"表格",unchecked:"待解析／检索未完成",absent:"已读HTML未命中"})) {
        const section=document.createElement("details");const summary=text("summary",label+" · 0");section.append(summary);output.append(section);buckets[key]={section,summary,label,count:0};
      }
      function place(key,card){const b=buckets[key]||buckets.unchecked;b.section.append(card);b.count++;b.summary.textContent=`${b.label} · ${b.count} 篇`;}
      if(job.title_matches?.length){const d=document.createElement("details");d.append(text("summary",`标题匹配 ${job.title_matches.length} 篇（仅标题证据）`));for(const p of job.title_matches)d.append(text("p",p.title));output.append(d);}
      for(const p of job.papers||[]){const d=document.createElement("details");d.append(text("summary",`${p.paper.title||p.paper.bibcode} · ${p.groups?.map(k=>labels[k]||k).join("、") || ({no_arxiv_identifier:"旧结果：编号尚未解析",identifier_unresolved:"本次检索未匹配，不代表arXiv不存在",identifier_candidates:"找到候选，身份待确认",identifier_lookup_failed:"检索请求失败，可重试",insufficient_metadata:"缺少检索信息",html_unavailable_or_failed:"HTML不可用或请求失败",no_match_in_supplied_materials:"已读取HTML未命中"}[p.status]||p.status)}`));
        for(const candidate of p.resolution?.candidates||[]){const a=text("a",`候选：${candidate.title}`);a.href=`https://arxiv.org/abs/${encodeURIComponent(candidate.arxiv_id)}`;a.target="_blank";a.rel="noopener noreferrer";d.append(a);}
        if(p.resolution?.errors?.length)d.append(text("p",p.resolution.errors.join("；")));
        if(p.abstract)d.append(text("p","摘要："+p.abstract));if(p.error)d.append(text("p",p.error));
        for(const e of p.evidence||[]){d.append(text("h4",`${labels[e.location_type]||e.location_type} · ${e.section||e.table_id||""}`),text("p",e.text));const url=e.locator?.url;if(url&&/^https?:\/\//i.test(url)){const a=text("a","查看原文位置");a.href=url;a.target="_blank";a.rel="noopener noreferrer";d.append(a);}}
        const categories=p.groups?.length ? p.groups : [p.status==="no_match_in_supplied_materials" ? "absent" : "unchecked"];
        for(const category of categories){const card=d.cloneNode(true);for(const heading of card.querySelectorAll("h4")){if(!heading.textContent.startsWith(labels[category]+" ·")){const paragraph=heading.nextElementSibling;const link=paragraph?.nextElementSibling;if(link?.tagName==="A")link.remove();paragraph?.remove();heading.remove();}}place(category,card);}}
      if(job.state==="running")setTimeout(poll,2000);
    }
    async function poll(){if(token!==state.detailToken||!panel.isConnected)return;try{await show(await api(endpoint));}catch(e){progress.textContent="状态查询失败："+e.message;start.disabled=false;}}
    async function action(cancel){try{start.disabled=true;const r=await fetch(endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({cancel})});const j=await r.json();if(!r.ok)throw Error(j.error?.message||"请求失败");await show(j);}catch(e){progress.textContent=e.message;start.disabled=false;}}
    start.onclick=()=>action(false);stop.onclick=()=>action(true);poll();
  }
  async function loadPaperEvidence(release, sourceId, token) {
    document.getElementById("paper-evidence-section")?.remove();
    const container = document.createElement("section");
    container.id = "paper-evidence-section";
    container.append(text("h3", "论文内容分类"), text("p", "正在匹配关联论文的本地全文…"));
    $("paper-section").before(container);
    const labels = {title:"标题",abstract:"摘要",body:"正文",figure_caption:"图注",table:"表格",page_text:"PDF页文本"};
    try {
      const result = await api(`/api/v1/evidence/${encodeURIComponent(release)}/${encodeURIComponent(sourceId)}`);
      if (token !== state.detailToken) return;
      container.replaceChildren(text("h3", "论文内容分类"),text("p", result.coverage, "caption"),text("p", `匹配别名：${result.aliases.join("、")}`, "caption"));
      const available = result.papers.filter(item => item.status === "mentions_found");
      const pending = result.papers.filter(item => item.status === "fulltext_not_cached");
      container.append(text("p", `本次检查 ${result.papers.length} 篇 · 找到原文证据 ${available.length} 篇 · 未缓存全文 ${pending.length} 篇`));
      const pendingList = document.createElement("details");
      pendingList.append(text("summary", `未缓存全文的论文（${pending.length} 篇，展开查看）`));
      for (const item of result.papers) {
        const card = text("div", "", "paper-item");
        card.append(text("h4", item.paper.title || item.paper.bibcode));
        const status = {fulltext_not_cached:"全文尚未下载，无法判断",no_match_in_cached_text:"已缓存文本中未发现（不代表论文未提及）",unavailable_or_invalid_artifact:"全文文件不可用或校验失败"};
        card.append(text("p", item.groups.length ? item.groups.map(k=>labels[k]||k).join(" · ") : status[item.status] || item.status));
        if(item.abstract) card.append(text("p", `摘要：${item.abstract}`));
        for(const e of item.evidence) {
          const details=document.createElement("details");
          details.append(text("summary", `${labels[e.category]||e.category} · ${e.section_path.join(" / ")} ${e.row ? "行 "+e.row : ""}`));
          details.append(text("p",e.text),text("p",`位置：${JSON.stringify(e.locator)} · block ${e.block_id || "title"}`,"caption"));
          card.append(details);
        }
        if (item.status === "fulltext_not_cached") pendingList.append(card);
        else container.append(card);
      }
      if (pending.length) container.append(pendingList);
      if (!result.papers.length) container.append(text("p","当前源尚无已缓存论文关联。"));
      if(result.truncated) container.append(text("p","本次最多检查100篇关联论文。"));
    } catch(error) { if(token===state.detailToken) container.replaceChildren(text("p",`论文分类读取失败：${error.message}`)); }
  }
  async function loadSpectrum() {
    if (!state.selected) return;
    const token = ++state.spectrumToken;
    const selected = state.selected;
    state.spectrum = null;
    $("spectrum-provenance").replaceChildren();
    $("spectrum-note").replaceChildren();
    $("spectrum-empty").textContent = "正在验证本地产品并读取光谱…";
    $("spectrum-empty").hidden = false;
    $("spectrum-chart").hidden = true;
    $("spectrum-table-section").hidden = true;
    const params = { scope: "global", retrieval_type: $("spectrum-product").value };
    if ($("spectrum-scope").value !== "global") {
      const [corpusId, runId] = $("spectrum-scope").value.split("|");
      Object.assign(params, { scope: "corpus", corpus_id: corpusId, run_id: runId });
    }
    try {
      const data = await api(`/api/v1/spectrum/${encodeURIComponent(selected.release)}/${encodeURIComponent(selected.source_id)}`, params);
      if (token !== state.spectrumToken || state.selected?.key !== selected.key) return;
      state.spectrum = data;
      renderSpectrum();
    } catch (error) {
      if (token !== state.spectrumToken) return;
      $("spectrum-empty").textContent = `读取失败：${error.message}`;
      $("spectrum-provenance").textContent = "未回退到其他版本或远程下载。";
    }
  }
  function renderSpectrum() {
    const data = state.spectrum;
    if (!data) return;
    $("spectrum-provenance").textContent = [
      data.product_type, data.scope === "corpus" ? "样本固定版本" : "全库当前版本",
      data.version_id ? `version ${data.version_id}` : "",
      data.artifact_sha256 ? `SHA-256 ${data.artifact_sha256}` : "",
      data.parser_version ? `parser ${data.parser_version}` : "",
    ].filter(Boolean).join(" · ");
    $("spectrum-note").textContent = data.warning || "仅显示已验证的本地产品；不会在浏览器点击时触发远程下载。";
    if (data.status !== "ready" || !data.series?.length) {
      $("spectrum-empty").hidden = false;
      $("spectrum-chart").hidden = true;
      $("spectrum-table-section").hidden = true;
      $("spectrum-empty").textContent = `${labels[data.status]?.[0] || data.status || "未知"}${data.error ? `：${data.error}` : ""}`;
      return;
    }
    $("spectrum-empty").hidden = true;
    $("spectrum-chart").hidden = false;
    const theme = colors();
    if (!charts.spectrum) charts.spectrum = echarts.init($("spectrum-chart"));
    const values = data.series.flatMap((item) => item.flux).filter(finite).map((value) => Math.abs(Number(value)));
    const largest = values.reduce((maximum, value) => Math.max(maximum, value), 0);
    // ECharts rounds astrophysical SI fluxes near 1e-17 to a degenerate axis.
    const exponent = largest > 0 ? Math.floor(Math.log10(largest)) : 0;
    const fluxScale = 10 ** exponent;
    const series = data.series.map((item, index) => ({
      name: item.name || "XP combined", type: "line", showSymbol: false, connectNulls: false,
      lineStyle: { width: 2, color: index === 0 ? theme.blue : theme.orange },
      itemStyle: { color: index === 0 ? theme.blue : theme.orange },
      data: item.wavelength.map((wave, position) => [finite(wave) ? Number(wave) : null, finite(item.flux[position]) ? Number(item.flux[position]) / fluxScale : null, finite(item.flux_error?.[position]) ? Number(item.flux_error[position]) / fluxScale : null]),
    }));
    charts.spectrum.setOption({
      animation: false, backgroundColor: theme.surface,
      color: [theme.blue, theme.orange],
      legend: { show: series.length >= 2, top: 0, textStyle: { color: theme.ink } },
      grid: { left: 70, right: 22, top: 40, bottom: 52 },
      xAxis: { type: "value", name: `波长 (${data.wavelength_unit || "nm"})`, nameLocation: "middle", nameGap: 29, axisLabel: { color: theme.secondary }, nameTextStyle: { color: theme.secondary }, splitLine: { lineStyle: { color: theme.line } } },
      yAxis: { type: "value", name: `通量 / 10^${exponent}`, scale: true, axisLabel: { color: theme.secondary, formatter: (value) => number(value, 2) }, nameTextStyle: { color: theme.secondary }, splitLine: { lineStyle: { color: theme.line } } },
      tooltip: { trigger: "axis", renderMode: "richText", confine: true, axisPointer: { type: "cross" }, backgroundColor: theme.surface, textStyle: { color: theme.ink }, formatter: (points) => {
        if (!points.length) return "";
        return [`λ ${number(points[0].value[0], 2)} ${data.wavelength_unit}`, ...points.map((point) => `${point.seriesName}: ${scientific(finite(point.value[1]) ? point.value[1] * fluxScale : null)}${point.value[2] !== null ? ` ± ${scientific(point.value[2] * fluxScale)}` : ""}`), data.flux_unit || "单位未知"].join("\n");
      } },
      dataZoom: [{ type: "inside", xAxisIndex: 0, filterMode: "none" }], series,
    }, { notMerge: true });
    charts.spectrum.resize();
    $("spectrum-note").textContent = `${data.warning ? `${data.warning} · ` : ""}${series.map((item) => item.name).join(" / ")} · 通量单位 ${data.flux_unit || "未知"} · 未平滑`;
    const head = document.createElement("tr");
    head.append(text("th", "序列"), text("th", `波长 (${data.wavelength_unit})`), text("th", `通量 (${data.flux_unit})`), text("th", "误差"));
    $("spectrum-table-head").replaceChildren(head);
    const body = $("spectrum-table-body"); body.replaceChildren();
    const fragment = document.createDocumentFragment();
    for (const item of data.series) {
      item.wavelength.forEach((wave, index) => {
        const row = document.createElement("tr");
        row.append(text("td", item.name || "XP combined"), text("td", number(wave, 3)), text("td", scientific(item.flux[index])), text("td", scientific(item.flux_error?.[index])));
        fragment.append(row);
      });
    }
    body.append(fragment);
    $("spectrum-table-section").hidden = false;
  }
  function changeFilters() {
    state.cursor = null;
    state.history = [];
    state.revision = null;
    refreshView(true);
  }
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $("theme").textContent = theme === "dark" ? "浅色模式" : "深色模式";
    try { localStorage.setItem("astro-library-theme", theme); } catch (_) {}
    renderSky();
    renderSpectrum();
  }
  function start() {
    let theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    try { theme = localStorage.getItem("astro-library-theme") || theme; } catch (_) {}
    applyTheme(theme);
    $("filters").addEventListener("submit", (event) => { event.preventDefault(); changeFilters(); });
    for (const id of ["release", "dataset", "scope", "spectrum-filter", "page-size"]) $(id).addEventListener("change", changeFilters);
    let searchTimer;
    $("search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(changeFilters, 350); });
    $("refresh").addEventListener("click", () => refreshView(true));
    $("theme").addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
    $("fit-sky").addEventListener("click", () => renderSky(true));
    $("previous").addEventListener("click", () => { if (state.history.length) { state.cursor = state.history.pop(); refreshView(true); } });
    $("next").addEventListener("click", () => { if (state.page?.has_more) { state.history.push(state.cursor); state.cursor = state.page.next_cursor; refreshView(true); } });
    $("spectrum-product").addEventListener("change", loadSpectrum);
    $("spectrum-scope").addEventListener("change", loadSpectrum);
    window.addEventListener("resize", () => { charts.sky?.resize(); renderSky(); if (!$("spectrum-chart").hidden) charts.spectrum?.resize(); });
    refreshView(true);
    setInterval(() => { if (!document.hidden) refreshView(); }, 5000);
  }
  window.AstroLibraryView = { hammer, refresh: () => refreshView(true), selectSource, state };
  document.addEventListener("DOMContentLoaded", start);
})();
