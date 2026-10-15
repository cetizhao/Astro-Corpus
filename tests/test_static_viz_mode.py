import pandas as pd

from astro_pipeline import spectra
from astro_pipeline.viz.source_paper import build_overview_html


def test_status_only_html_reports_cached_spectra_without_parsing(monkeypatch, tmp_path):
    catalog = tmp_path / "members.csv"
    edges = tmp_path / "edges.parquet"
    pd.DataFrame({"Gaia": ["123"], "RAICRS": [10.], "DEICRS": [-20.]}).to_csv(catalog, index=False)
    pd.DataFrame(columns=["source_id", "bibcode"]).to_parquet(edges, index=False)
    monkeypatch.setattr(spectra, "fetch_xp_spectrum", lambda *a, **k: {"format": "continuous"})

    def do_not_parse(*args, **kwargs):
        raise AssertionError("status-only rendering must not calibrate XP")

    monkeypatch.setattr(spectra, "read_xp_spectrum", do_not_parse)
    output = build_overview_html(tmp_path / "overview.html", catalog_path=catalog,
                                 edges_path=edges, spectra_dir=tmp_path, include_spectra=False)
    html = output.read_text(encoding="utf-8")
    assert '"spectrum_status":"cached_not_embedded"' in html
    assert '"spectra_embedded":false' in html
    assert '"has_xp":true' in html
