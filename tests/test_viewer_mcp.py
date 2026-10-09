"""Focused MCP forwarding tests for viewer and corpus options."""
from __future__ import annotations

from astro_pipeline import corpus_builder, mcp_server, membership


def test_gaia_xp_spectrum_forwards_release_with_dr3_default(monkeypatch):
    calls: list[dict] = []

    def fake_png(gaia_id, cache_dir, **kwargs):
        calls.append(
            {
                "gaia_id": gaia_id,
                "cache_dir": cache_dir,
                **kwargs,
            }
        )
        return {"gaia_id": gaia_id, "release": kwargs["release"]}

    monkeypatch.setattr(mcp_server, "get_xp_spectrum_png", fake_png)

    default_result = mcp_server.gaia_xp_spectrum("123")
    explicit_result = mcp_server.gaia_xp_spectrum("123", release="dr4")

    assert default_result["release"] == "dr3"
    assert explicit_result["release"] == "dr4"
    assert [call["release"] for call in calls] == ["dr3", "dr4"]
    assert all(call["allow_download"] is False for call in calls)


def test_download_corpus_forwards_static_viz_mode_and_refresh(monkeypatch):
    captured: dict = {}
    sentinel = object()

    def fake_build(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(corpus_builder, "build_corpus_snapshot", fake_build)
    monkeypatch.setattr(
        corpus_builder,
        "result_to_dict",
        lambda result: {"built": result is sentinel},
    )

    result = mcp_server.download_corpus(
        "viewer-test",
        name="TW Hya",
        limit=11,
        refresh=True,
        static_viz_mode="off",
    )

    assert result == {"built": True}
    assert captured["limit"] == 11
    assert captured["refresh"] is True
    assert captured["static_viz_mode"] == "off"


def test_download_membership_corpus_forwards_options_and_zero_limit(monkeypatch):
    captured: dict = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return {"built": True}

    monkeypatch.setattr(membership, "build_membership_corpus", fake_build)

    result = mcp_server.download_membership_corpus(
        "members-test",
        "J/AJ/165/269",
        limit=0,
        refresh=True,
        static_viz_mode="off",
    )

    assert result == {"built": True}
    assert captured["limit"] is None
    assert captured["refresh"] is True
    assert captured["static_viz_mode"] == "off"


def test_membership_forwards_explicit_selection_resume(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        membership, "build_membership_corpus",
        lambda **kwargs: captured.update(kwargs) or {"built": True},
    )
    run_id = "8b0280c0-2f40-4047-b363-41845ac136e0"
    result = mcp_server.download_membership_corpus(
        "members-test", "J/AJ/160/44", resume_selection_run_id=run_id,
    )
    assert result == {"built": True}
    assert captured["resume_selection_run_id"] == run_id
    assert captured["refresh"] is False


def test_download_functions_reject_negative_limits_before_build(monkeypatch):
    calls: list[str] = []

    def unexpected_corpus_build(**kwargs):
        calls.append("corpus")
        raise AssertionError("corpus builder must not run")

    def unexpected_membership_build(**kwargs):
        calls.append("membership")
        raise AssertionError("membership builder must not run")

    monkeypatch.setattr(
        corpus_builder,
        "build_corpus_snapshot",
        unexpected_corpus_build,
    )
    monkeypatch.setattr(
        membership,
        "build_membership_corpus",
        unexpected_membership_build,
    )

    corpus_result = mcp_server.download_corpus(
        "negative-corpus",
        name="TW Hya",
        limit=-1,
    )
    membership_result = mcp_server.download_membership_corpus(
        "negative-members",
        "J/AJ/165/269",
        limit=-1,
    )

    assert corpus_result["error"] == "limit must be zero or a positive integer"
    assert membership_result["error"] == "limit must be zero or a positive integer"
    assert calls == []
