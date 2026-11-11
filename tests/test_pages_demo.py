"""The public export builds offline and preserves identity and excerpt offsets."""
import importlib.util
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_static_demo_build_has_only_public_assets(tmp_path):
    spec = importlib.util.spec_from_file_location('build_pages', ROOT / 'scripts/build_pages.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build(tmp_path)
    html = (tmp_path / 'index.html').read_text(encoding='utf-8')
    assert 'src="/assets/' not in html and 'href="/assets/' not in html
    assert html.index('assets/demo.js') < html.index('assets/library.js')
    for asset in re.findall(r'(?:src|href)="(assets/[^"]+)"', html):
        assert (tmp_path / asset).is_file()
    raw = (tmp_path / 'demo.json').read_text(encoding='utf-8')
    assert not re.search(r'"[A-Za-z]:[\\/]|[\\/]Users[\\/]|password|api_key', raw, re.I)
    data = json.loads(raw)
    sources = {s['key']: s for s in data['sources']}
    assert data['default_source'] in sources
    for source in sources.values():
        assert isinstance(source['source_id'], str)
        assert source['key'] == f"{source['release']}:{source['source_id']}"
    for key, records in data['evidence'].items():
        assert key in sources
        for record in records:
            a = record['annotation']
            spans = list(a['identity_evidence'])
            for group in a['relations'] + a.get('results', []):
                spans.extend(group['evidence'])
            for span in spans:
                context = data['contexts'][record['paper']['paper_version_id'] + ':' + span['block_id']]
                unit = next(u for u in context['units'] if all(u.get(k) == span.get(k) for k in ('block_id','field','row','column','note_index')))
                assert unit['text'][span['start']-unit['start']:span['end']-unit['start']] == span['quote']
    assert not list(tmp_path.rglob('*.sqlite3'))
