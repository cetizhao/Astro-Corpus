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
