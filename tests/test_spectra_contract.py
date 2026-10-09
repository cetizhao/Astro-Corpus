from astro_pipeline import spectra


def test_invalid_png_identity_is_an_error_payload(tmp_path):
    for source_id, release in [("not-a-source", "dr3"), ("123", "unsupported")]:
        result = spectra.get_xp_spectrum_png(source_id, tmp_path, release=release)
        assert "error" in result
    assert not list(tmp_path.iterdir())


def test_fresh_download_includes_release(monkeypatch, tmp_path):
    downloaded = False

    def download(*args, **kwargs):
        nonlocal downloaded
        downloaded = True

    def resolve(*args, **kwargs):
        if not downloaded:
            raise FileNotFoundError("not yet cached")
        return {"path": str(tmp_path / "fixture.fits"), "sha256": "fixture"}

    monkeypatch.setattr(spectra, "_download_via_datalink", download)
    monkeypatch.setattr(spectra, "resolve_product", resolve)
    result = spectra.fetch_xp_spectrum("123", tmp_path, allow_download=True)
    assert result["release"] == "dr3"
