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
    for (const [id, name] of [["search", "q"], ["release", "release"], ["dataset", "corpus_id"], ["spectrum-filter", "spectrum_state"], ["evidence-filter", "evidence_state"]]) {
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
      row.append(identity, position, magnitude, spectrum, datasets, text("td", `${source.annotation_paper_count || 0} 篇`, "numeric"));
      const select = () => selectSource(source.release, String(source.source_id));
      row.addEventListener("click", select);
      row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } });
      body.append(row);
    }
    if (!sources.length) {
      const cell = text("td", "当前筛选没有源。可调整筛选，或等待采集提交新记录。", "empty");
      cell.colSpan = 6;
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
    $("paper-heading").textContent = "文献与证据";
    $("source-papers").replaceChildren(text("p", "正在读取关系与证据…"));
    $("paper-section").hidden = false;
  }
  async function loadEvidence(data, token) {
    const selected = state.selected;
    const records = [];
    try {
      let offset = 0, result;
      do {
        result = await api(`/api/v1/source/${selected.release}/${selected.source_id}/evidence`, { limit: 100, offset, revision: data.revision });
        records.push(...result.assessments); offset += result.page.returned;
      } while (result.page.has_more && token === state.detailToken);
      if (token !== state.detailToken) return;
      window.AstroEvidence.mount($("source-papers"), {
        records, bibliography: data.active_bibliography?.papers || data.papers || [],
        loadContext: async (version, block) => {
          let result, offset = 0; const units = [];
          do {
            result = await api(`/api/v1/paper/${encodeURIComponent(version)}/context`, { block_id: block, offset, limit: 100, revision: data.revision });
            units.push(...result.units); offset += result.page?.returned || 0;
          } while (result.page?.has_more);
          return { ...result, units };
        },
      });
    } catch (error) {
      if (token !== state.detailToken) return;
      // Keep the independent bibliography readable when evidence cannot be loaded.
      window.AstroEvidence.mount($("source-papers"), { bibliography: data.active_bibliography?.papers || data.papers || [] });
      $("source-papers").prepend(text("p", `证据读取失败：${error.message}`, "caption"));
    }
  }

  function clearDetail(message) {
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
      await Promise.all([loadSpectrum(), loadEvidence(data, token)]);
    } catch (error) {
      if (token !== state.detailToken) return;
      clearDetail(`读取失败：${error.message}`);
      $("source-title").textContent = "无法读取源";
      $("source-identity").textContent = error.message;
    }
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
    for (const id of ["release", "dataset", "scope", "spectrum-filter", "evidence-filter", "page-size"]) $(id).addEventListener("change", changeFilters);
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
