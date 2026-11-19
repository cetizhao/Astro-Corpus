/* Fixed public example data; shares the local observatory renderer. */
(() => {
  'use strict';
  const snapshot = fetch('demo.json').then(r => { if (!r.ok) throw new Error('示例数据加载失败'); return r.json(); });
  const norm = s => String(s ?? '').normalize('NFKC').replace(/\s/g, '').toLowerCase();
  window.AstroDemo = {
    async request(path, p = {}) {
      const d = await snapshot, revision = d.meta.revision;
      if (path === '/api/v1/meta') return d.meta;
      if (path === '/api/v1/research-tasks') return d.research_tasks || {suites:[],warnings:[]};
      if (path === '/api/v1/research-items') {
        const all = (p.revision_id || p.include_history === 'true') ? (d.science?.archive || []) : (d.science?.items || []);
        const rows = all.filter(r => (!p.record_id || r.record_id === p.record_id) && (!p.revision_id || r.revision_id === p.revision_id)
          && (!p.kind || r.item.kind === p.kind) && (!p.topic || r.item.topic === p.topic)
          && (!p.paper_version_id || r.evidence.some(e => e.kind === 'paper' && e.paper.paper_version_id === p.paper_version_id))
          && (!p.source_id || r.item.targets.some(t => t.source_id === p.source_id && t.release === (p.release || 'dr3')))
          && (!p.text || JSON.stringify(r).toLowerCase().includes(p.text.toLowerCase())))
          .sort((a,b) => b.created_at-a.created_at || b.ordinal-a.ordinal);
        const offset = Number(p.offset || 0), limit = Number(p.limit || 20), items = rows.slice(offset, offset+limit);
        return { schema_version: 'research-query-1', schema_ready: !!d.science, read_only: true, items,
          page: { total: rows.length, returned: items.length, offset, limit, has_more: offset+items.length<rows.length } };
      }
      if (path === '/api/v1/sources' || path === '/api/v1/sky') {
        const rows = d.sources.filter(s => (!p.release || s.release === p.release)
          && (!p.corpus_id || s.datasets.some(v => v.corpus_id === p.corpus_id))
          && (p.scope !== 'current_datasets' || s.datasets.length)
          && (!p.spectrum_state || p.spectrum_state === 'all' || s.spectrum_status === p.spectrum_state)
          && (!p.evidence_state || p.evidence_state === 'all' || (p.evidence_state === 'recorded') === (s.annotation_count > 0))
          && (!p.q || [s.source_id, s.name, ...(s.catalog_names || []).map(v => v.name), ...s.datasets.map(v => v.label)].some(v => norm(v).includes(norm(p.q)))));
        const sky = rows.filter(s => s.position.valid).map(s => ({...s, ...s.position}));
        const offset = Number(p.cursor || 0), limit = Number(p.limit || 50);
        const items = path.endsWith('/sky') ? sky : rows;
        const end = offset + limit;
        return { revision, total: rows.length, total_positions: sky.length, missing_positions: rows.length - sky.length,
          registered_xp_ready_sources: rows.filter(s => s.spectrum_status === 'ready').length,
          sources: items.slice(offset,end), points: items.slice(offset,end), returned: items.slice(offset,end).length,
          has_more: end < items.length, next_cursor: end < items.length ? String(end) : null };
      }
      const parts = path.split('/').map(decodeURIComponent);
      const key = `${parts[4]}:${parts[5]}`;
      if (parts[3] === 'source' && parts[6] === 'evidence') {
        const all = d.evidence[key] || [], offset = Number(p.offset || 0), records = all.slice(offset,offset+Number(p.limit || 100));
        return {revision, assessments:records, page:{returned:records.length,has_more:false}};
      }
      if (parts[3] === 'source') {
        const dto = d.sources.find(s => s.key === key);
        return {revision, resolved:!!dto, dto, datasets:dto?.datasets || [], papers:[]};
      }
      if (parts[3] === 'spectrum') {
        const products = d.spectra[key] || {};
        const product = products[p.retrieval_type] || Object.values(products).find(v => v.status === 'ready') || Object.values(products)[0];
        return product?.status === 'ready' ? product : {status:'not_downloaded',warning:'本次公开快照未包含 XP 光谱文件。',error:'此示例没有可绘制的光谱数据。'};
      }
      if (parts[3] === 'paper') {
        const context = d.contexts[`${parts[4]}:${p.block_id}`];
        if (!context) throw new Error('此段未包含在公开示例中，请查看论文原文。');
        return {...context,page:{returned:context.units.length,has_more:false}};
      }
      throw new Error('示例中不支持此查询');
    },
    ready: snapshot,
  };
})();
