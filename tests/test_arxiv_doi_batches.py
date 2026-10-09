"""Offline tests for verified, serialized arXiv DOI batches."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from astro_pipeline.library.papers import DOI_ARXIV_NAMESPACE, cached_doi_arxiv
from astro_pipeline.library.store import LibraryStore
from astro_pipeline.sources import simbad_refs
from astro_pipeline.sources.arxiv_doi import arxiv_ids_from_dois

ATOM = 'http://www.w3.org/2005/Atom'
OPEN = 'http://a9.com/-/spec/opensearch/1.1/'
ARXIV = 'http://arxiv.org/schemas/atom'


@pytest.fixture(autouse=True)
def no_real_request_delay(monkeypatch):
    monkeypatch.setattr(simbad_refs, '_arxiv_last_request_started', None)
    monkeypatch.setattr(simbad_refs, '_ARXIV_REQUEST_INTERVAL', 0.0)


def _entry(aid: str | None, *, doi: str | None = None, href: str | None = None, title: str = 'A paper') -> str:
    identifier = f'<id>http://arxiv.org/abs/{aid}</id>' if aid is not None else ''
    doi_tag = f'<arxiv:doi>{doi}</arxiv:doi>' if doi is not None else ''
    link = f'<link title="doi" href="{href}" />' if href is not None else ''
    return f'<entry>{identifier}<title>{title}</title>{doi_tag}{link}</entry>'


def _feed(entries: list[str], *, total: int | None = None, start: int = 0, pagination: bool = True) -> str:
    counts = (f'<opensearch:totalResults>{len(entries) if total is None else total}</opensearch:totalResults>'
              f'<opensearch:startIndex>{start}</opensearch:startIndex>') if pagination else ''
    return (f'<feed xmlns="{ATOM}" xmlns:opensearch="{OPEN}" xmlns:arxiv="{ARXIV}">'
            f'{counts}{"".join(entries)}</feed>')


def _reply(url: str, xml: str, *, status: int = 200, headers: dict | None = None):
    return httpx.Response(status, text=xml, headers=headers or {}, request=httpx.Request('GET', url))


def test_batched_or_maps_only_exact_normalized_dois_regardless_of_entry_order(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return _reply(url, _feed([
            _entry('2401.04567v2', doi='10.1234/SECOND'),
            _entry('astro-ph/0603456v1', href='https://dx.doi.org/10.1234%2FFirst'),
            _entry('2411.12345', doi='10.1234/unrequested'),
        ]))

    monkeypatch.setattr(httpx, 'get', get)
    result = arxiv_ids_from_dois([
        'https://doi.org/10.1234/FIRST', '10.1234/second', '10.1234/missing',
        'DOI: 10.1234/FIRST',
    ])
    assert result.resolved == {
        '10.1234/first': 'astro-ph/0603456',
        '10.1234/second': '2401.04567',
        '10.1234/missing': None,
    }
    assert result.failed == {} and result.deferred == [] and result.status == 'completed'
    assert len(calls) == 1
    args = parse_qs(urlsplit(calls[0][0]).query)
    assert args['search_query'] == [
        'doi:"10.1234/first" OR doi:"10.1234/second" OR doi:"10.1234/missing"'
    ]
    assert args['start'] == ['0']
    assert calls[0][0].startswith('https://export.arxiv.org/api/query?')


def test_bounded_twenty_per_batch_and_shared_legacy_rate_lock(monkeypatch):
    class Clock:
        current = 1000.0

        def __call__(self):
            return self.current

        def sleep(self, seconds):
            self.current += seconds

    clock = Clock()
    starts = []
    monkeypatch.setattr(simbad_refs, '_ARXIV_REQUEST_INTERVAL', 3.0)
    monkeypatch.setattr(simbad_refs, '_arxiv_last_request_started', None)
    monkeypatch.setattr(simbad_refs.time, 'monotonic', clock)
    monkeypatch.setattr(simbad_refs.time, 'sleep', clock.sleep)

    def get(url, **kwargs):
        starts.append(clock.current)
        query = parse_qs(urlsplit(url).query)['search_query'][0]
        assert query.count('doi:') <= 20
        return _reply(url, _feed([]))

    monkeypatch.setattr(httpx, 'get', get)
    assert simbad_refs.arxiv_id_from_doi('10.1234/legacy') is None
    result = arxiv_ids_from_dois([f'10.1234/{n}' for n in range(21)])
    assert starts == [1000.0, 1003.0, 1006.0]
    assert len(result.resolved) == 21 and set(result.resolved.values()) == {None}


@pytest.mark.parametrize('xml', [
    _feed([_entry('2401.01234', doi='10.1234/first')], total=2),
    _feed([_entry('2401.01234', doi='10.1234/first')], start=1),
    _feed([], pagination=False),
    _feed([_entry(None, doi='10.1234/first')]),
    _feed([_entry('2401.01234')]),
    _feed(['<entry><id>http://arxiv.org/api/errors#unknown</id><title>Error</title></entry>']),
    _feed([_entry('2401.01234', href='https://example.invalid/doi/10.1234/first')]),
    _feed([_entry('2401.01234', href='https://doi.org/10.1234/first?tracking=1')]),
])
def test_incomplete_or_unidentifiable_feed_never_yields_negative(monkeypatch, xml):
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: _reply(url, xml))
    result = arxiv_ids_from_dois(['10.1234/first', '10.1234/second'])
    assert result.resolved == {}
    assert set(result.failed) == {'10.1234/first', '10.1234/second'}
    assert result.status == 'partial'


def test_ambiguous_doi_fails_only_it_without_expected_title(monkeypatch):
    feed = _feed([
        _entry('2401.00001', doi='10.1234/example', title='The first paper'),
        _entry('2401.00002', href='https://doi.org/10.1234/EXAMPLE', title='The second paper'),
        _entry('2401.00003', doi='10.1234/another', title='An unrelated valid paper'),
    ])
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: _reply(url, feed))
    result = arxiv_ids_from_dois(['10.1234/example', '10.1234/another', '10.1234/missing'])
    assert result.resolved == {'10.1234/another': '2401.00003', '10.1234/missing': None}
    assert list(result.failed) == ['10.1234/example']
    assert result.deferred == []
    assert 'multiple article IDs' in result.failed['10.1234/example']
    assert result.evidence['10.1234/example']['selection'] is None
    assert len(result.evidence['10.1234/example']['candidates']) == 2


def test_unique_exact_title_can_select_real_duplicate_doi_case(monkeypatch):
    doi = '10.1002/asna.200710773'
    target_title = (
        'Evolution of Magnetic Fields in Stars Across the Upper Main Sequence: '
        'II. Observed distribution of the magnetic field geometry'
    )
    feed = _feed([
        _entry('astro-ph/0408233v4', doi=doi,
               title='The applications of the MHD Alfven wave oscillation model for kHz quasi-periodic oscillations'),
        _entry('0704.1502v1', doi=doi, title=target_title),
    ])
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: _reply(url, feed))
    result = arxiv_ids_from_dois(
        [doi, '10.1002/empty'],
        expected_titles={
            'HTTPS://DOI.ORG/10.1002/ASNA.200710773': [
                ' evolution of magnetic fields in stars across the upper main sequence '
                'II - observed distribution of the magnetic field geometry ',
                target_title,  # Duplicate SIMBAD metadata should not change the unique match.
            ]
        },
    )
    assert result.resolved == {doi: '0704.1502', '10.1002/empty': None}
    assert result.failed == {} and result.status == 'completed'
    evidence = result.evidence[doi]
    assert evidence['selection'] == {'method': 'exact_normalized_title', 'arxiv_id': '0704.1502'}
    assert {item['arxiv_id'] for item in evidence['candidates']} == {'astro-ph/0408233', '0704.1502'}
    assert target_title in evidence['expected_titles']


@pytest.mark.parametrize(('expected', 'candidate_titles'), [
    (['A third paper'], ('The first paper', 'The second paper')),
    (['The first paper', 'The second paper'], ('The first paper', 'The second paper')),
    (['The first paper', 'The first paper'], ('The first paper', 'The first paper!')),
    (['The first'], ('The first paper', 'The second paper')),  # no substring selection
    (['The first paper'], ('The first paper', '')),  # unknown other candidate title
])
def test_title_does_not_select_ambiguous_or_unverified_candidate(
    monkeypatch, expected, candidate_titles,
):
    feed = _feed([
        _entry('2401.00001', doi='10.1234/dup', title=candidate_titles[0]),
        _entry('2401.00002', doi='10.1234/dup', title=candidate_titles[1]),
        _entry('2401.00003', doi='10.1234/other', title='A verified other paper'),
    ])
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: _reply(url, feed))
    result = arxiv_ids_from_dois(
        ['10.1234/dup', '10.1234/other'],
        expected_titles={'10.1234/dup': expected},
    )
    assert result.resolved == {'10.1234/other': '2401.00003'}
    assert list(result.failed) == ['10.1234/dup']
    assert result.evidence['10.1234/dup']['selection'] is None
    assert result.deferred == []


def test_ambiguous_doi_does_not_stop_later_batches(monkeypatch):
    feeds = [
        _feed([
            _entry('2401.00001', doi='10.1234/dup', title='First paper'),
            _entry('2401.00002', doi='10.1234/dup', title='Another paper'),
            _entry('2401.00003', doi='10.1234/clear', title='Clear match'),
        ]),
        _feed([_entry('2401.00004', doi='10.1234/later', title='Later valid paper')]),
    ]
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return _reply(url, feeds[len(calls) - 1])

    monkeypatch.setattr(httpx, 'get', get)
    result = arxiv_ids_from_dois(
        ['10.1234/dup', '10.1234/clear', '10.1234/later'], batch_size=2,
    )
    assert result.resolved == {'10.1234/clear': '2401.00003', '10.1234/later': '2401.00004'}
    assert list(result.failed) == ['10.1234/dup']
    assert result.deferred == [] and len(calls) == 2


def test_failure_preserves_completed_batch_and_defers_unrequested(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return _reply(url, _feed([_entry('2501.01010', doi='10.1234/a')]))
        return _reply(url, _feed([], total=3))

    monkeypatch.setattr(httpx, 'get', get)
    result = arxiv_ids_from_dois(['10.1234/a', '10.1234/b', '10.1234/c'], batch_size=1)
    assert result.resolved == {'10.1234/a': '2501.01010'}
    assert list(result.failed) == ['10.1234/b']
    assert result.deferred == ['10.1234/c']
    assert len(calls) == 2


def test_retry_after_is_bounded_and_429_defers_future_batches(monkeypatch):
    class Clock:
        current = 1000.0

        def __call__(self):
            return self.current

        def sleep(self, seconds):
            self.current += seconds

    clock = Clock()
    starts = []
    monkeypatch.setattr(simbad_refs, '_ARXIV_REQUEST_INTERVAL', 3.0)
    monkeypatch.setattr(simbad_refs, '_arxiv_last_request_started', None)
    monkeypatch.setattr(simbad_refs.time, 'monotonic', clock)
    monkeypatch.setattr(simbad_refs.time, 'sleep', clock.sleep)

    def throttled(url, **kwargs):
        starts.append(clock.current)
        return _reply(url, '', status=429, headers={'Retry-After': '12'})

    monkeypatch.setattr(httpx, 'get', throttled)
    result = arxiv_ids_from_dois(['10.1234/a', '10.1234/b'], batch_size=1)
    assert starts == [1000.0, 1012.0]
    assert result.rate_limited and result.status == 'rate_limited'
    assert list(result.failed) == ['10.1234/a']
    assert result.deferred == ['10.1234/b']
    assert result.resolved == {}

    starts.clear()
    monkeypatch.setattr(simbad_refs, '_arxiv_last_request_started', None)
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: (
        starts.append(clock.current) or _reply(url, '', status=429, headers={'Retry-After': '3600'})
    ))
    result = arxiv_ids_from_dois(['10.1234/a'])
    assert starts == [1012.0] and result.rate_limited and result.resolved == {}


def test_completed_values_can_prefill_fenced_cache_without_negative_failures(monkeypatch, tmp_path):
    store = LibraryStore(tmp_path / 'library')
    feed = _feed([_entry('2503.01234', doi='10.1234/found')])
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: _reply(url, feed))
    result = arxiv_ids_from_dois(['10.1234/found', '10.1234/absent'])
    for doi, aid in result.resolved.items():
        returned = cached_doi_arxiv(store, doi=doi, resolve=lambda aid=aid: aid)
        assert returned.value == aid
    assert store.get_cache(DOI_ARXIV_NAMESPACE, '10.1234/found')['outcome'] == 'found'
    assert store.get_cache(DOI_ARXIV_NAMESPACE, '10.1234/absent')['outcome'] == 'not_found'
    assert result.failed == {} and result.deferred == []


def test_single_doi_fallback_uses_complete_feed_and_exact_doi(monkeypatch):
    feed = _feed([
        _entry('2401.00001', doi='10.1234/unrelated', title='Unrelated first result'),
        _entry('2401.00002v2', href='https://doi.org/10.1234/EXAMPLE', title='Correct paper'),
    ])
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: _reply(url, feed))
    # Calling through simbad_refs imports the strict resolver lazily: no cycle.
    assert simbad_refs.arxiv_id_from_doi('10.1234/example') == '2401.00002'
    assert simbad_refs.arxiv_id_from_doi('10.1234/absent') is None


def test_single_doi_fallback_rejects_ambiguous_id_without_negative_cache(monkeypatch, tmp_path):
    doi = '10.1002/asna.200710773'
    feed = _feed([
        _entry('astro-ph/0408233v4', doi=doi, title='A mis-entered DOI'),
        _entry('0704.1502v1', doi=doi, title='The published paper'),
    ])
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: _reply(url, feed))
    store = LibraryStore(tmp_path / 'library')
    prefetched = arxiv_ids_from_dois([doi], expected_titles={doi: ['not either paper']})
    assert list(prefetched.failed) == [doi] and prefetched.resolved == {}
    assert store.get_cache(DOI_ARXIV_NAMESPACE, doi, include_stale=True) is None
    with pytest.raises(RuntimeError, match='multiple article IDs'):
        cached_doi_arxiv(store, doi=doi, resolve=lambda: simbad_refs.arxiv_id_from_doi(doi))
    assert store.get_cache(DOI_ARXIV_NAMESPACE, doi, include_stale=True) is None
    assert [row['outcome'] for row in store.read(
        'SELECT outcome FROM cache_observations WHERE namespace=?', (DOI_ARXIV_NAMESPACE,),
    )] == ['error']
    resolved = cached_doi_arxiv(
        store, doi=doi,
        resolve=lambda: simbad_refs.arxiv_id_from_doi(
            doi, expected_titles=['the published paper'],
        ),
    )
    assert resolved.value == '0704.1502'
    assert store.get_cache(DOI_ARXIV_NAMESPACE, doi)['outcome'] == 'found'


def test_enrich_bibcode_passes_simbad_title_to_strict_fallback(monkeypatch):
    import astroquery.simbad as astro_simbad
    from astropy.table import Table

    doi = '10.1234/paper'
    target_title = 'A verified SIMBAD paper: magnetic fields'
    feed = _feed([
        _entry('2401.00001', doi=doi, title='A different paper'),
        _entry('2401.00002', doi=doi, title='A verified SIMBAD paper - magnetic fields'),
    ])

    class FakeSimbad:
        def query_bibcode(self, bibcode):
            return Table({
                'title': [target_title], 'doi': [doi],
                'journal': ['AJ'], 'year': [2024],
            })

    monkeypatch.setattr(astro_simbad, 'Simbad', FakeSimbad)
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: _reply(url, feed))
    record = simbad_refs.enrich_bibcode('2024AJ...999A', strict=True)
    assert record.title == target_title
    assert record.arxiv_id == '2401.00002'


def test_invalid_doi_or_batch_contract_cannot_make_negative_observations(monkeypatch):
    monkeypatch.setattr(httpx, 'get', lambda *args, **kwargs: pytest.fail('no network expected'))
    with pytest.raises(ValueError, match='Invalid DOI'):
        arxiv_ids_from_dois(['10.1234/ok', 'not-a-doi'])
    with pytest.raises(ValueError, match='batch_size'):
        arxiv_ids_from_dois(['10.1234/ok'], batch_size=21)
