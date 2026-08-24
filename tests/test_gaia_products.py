"""Offline tests for release-aware Gaia DataLink products."""
from __future__ import annotations

import json

import pytest
from astropy.table import Table

from astro_pipeline.gaia_products import (
    GaiaProductRequest,
    download_gaia_product_request,
    gaia_product_status,
    plan_gaia_product_request,
)


def test_product_request_is_release_qualified_and_validated():
    request = GaiaProductRequest(
        release="Gaia DR4",
        retrieval_type="epoch_astrometry",
        identifiers=["123", "123", "456"],
        data_structure="individual",
        output_format="fits",
        batch_size=2,
    )
    assert request.release == "dr4"
    assert request.archive_tag == "Gaia DR4"
    assert request.retrieval_type == "EPOCH_ASTROMETRY"
    assert request.identifiers == ("123", "456")

    scalar = GaiaProductRequest(
        release="dr3", retrieval_type="XP_SAMPLED", identifiers="123"
    )
    assert scalar.identifiers == ("123",)

    with pytest.raises(ValueError, match="not declared"):
        GaiaProductRequest(
            release="dr4", retrieval_type="XP_SAMPLED", identifiers=["123"]
        )
    with pytest.raises(ValueError, match="digits only"):
        GaiaProductRequest(
            release="dr4",
            retrieval_type="EPOCH_ASTROMETRY",
            identifiers=["123,456"],
        )


def test_product_plan_is_read_only_and_warns_for_provisional_release(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = plan_gaia_product_request(
        release="dr4",
        retrieval_type="EPOCH_IMAGE",
        n_identifiers=11,
        output_format="fits",
        batch_size=5,
    )
    assert plan["writes_data"] is False
    assert plan["n_batches"] == 3
    assert plan["profile_status"] == "provisional"
    assert plan["warnings"]
    assert not (tmp_path / "data").exists()


def test_generic_download_preserves_archive_table_normalizes_and_resumes(tmp_path):
    calls = []

    def fake_loader(**kwargs):
        calls.append(kwargs)
        ids = [str(value) for value in kwargs["ids"]]
        return {
            "xp_raw.fits": [
                Table(
                    {
                        "source_id": [int(value) for value in ids],
                        "flux": [[1.0, 2.0] for _ in ids],
                    },
                    meta={"SOL_ID": "42"},
                )
            ]
        }

    request = GaiaProductRequest(
        release="dr3",
        retrieval_type="XP_SAMPLED",
        identifiers=["100", "200", "300"],
        data_structure="RAW",
        output_format="fits",
        batch_size=2,
    )
    artifacts = download_gaia_product_request(
        request, tmp_path, loader=fake_loader
    )

    assert [len(call["ids"]) for call in calls] == [2, 1]
    assert all(call["data_release"] == "Gaia DR3" for call in calls)
    assert {item.identifier for item in artifacts if item.success} == {
        "100",
        "200",
        "300",
    }
    assert (tmp_path / "100.fits").exists()
    assert len(list((tmp_path / "archive_tables").glob("*.fits"))) == 2
    contract = json.loads((tmp_path / "product_request.json").read_text())
    assert contract["contract"]["release"] == "dr3"
    manifest = json.loads((tmp_path / "artifacts.json").read_text())
    assert len(manifest["artifacts"]) == 3
    assert all(
        item["metadata"]["preservation"] == "astroquery_table_reencoded"
        for item in manifest["artifacts"]
    )
    assert all(item["archive_checksum_sha256"] for item in manifest["artifacts"])
    assert all(item["content_type"] == "application/fits" for item in manifest["artifacts"])

    status = gaia_product_status(tmp_path)
    assert status["n_completed_identifiers"] == 3
    assert status["n_failed_identifiers"] == 0
    assert status["n_archive_tables"] == 2
    assert status["resumable"] is True

    resumed = download_gaia_product_request(
        request,
        tmp_path,
        loader=lambda **kwargs: pytest.fail("completed identifiers were re-downloaded"),
    )
    assert resumed == []


def test_output_directory_refuses_different_release_or_product(tmp_path):
    dr3 = GaiaProductRequest(
        release="dr3", retrieval_type="XP_SAMPLED", identifiers=["100"]
    )
    download_gaia_product_request(
        dr3,
        tmp_path,
        loader=lambda **kwargs: {"x": [Table({"source_id": [100]})]},
    )
    dr4 = GaiaProductRequest(
        release="dr4", retrieval_type="EPOCH_ASTROMETRY", identifiers=["100"]
    )
    with pytest.raises(ValueError, match="different request contract"):
        download_gaia_product_request(
            dr4,
            tmp_path,
            loader=lambda **kwargs: {},
        )


def test_individual_product_can_use_unique_identifier_from_archive_key(tmp_path):
    request = GaiaProductRequest(
        release="dr4",
        retrieval_type="EPOCH_ASTROMETRY",
        identifiers=["100", "200"],
        data_structure="INDIVIDUAL",
        output_format="ecsv",
        batch_size=2,
    )
    artifacts = download_gaia_product_request(
        request,
        tmp_path,
        loader=lambda **kwargs: {
            "Gaia_DR4_100_epoch_astrometry.ecsv": [Table({"time": [1.0]})],
            "Gaia_DR4_200_epoch_astrometry.ecsv": [Table({"time": [2.0]})],
        },
    )
    assert {item.identifier for item in artifacts if item.success} == {"100", "200"}
    assert (tmp_path / "100.ecsv").exists()


def test_votable_plain_uses_tabledata_serialization(tmp_path):
    request = GaiaProductRequest(
        release="dr3",
        retrieval_type="XP_SAMPLED",
        identifiers="100",
        output_format="votable_plain",
    )
    artifacts = download_gaia_product_request(
        request,
        tmp_path,
        loader=lambda **kwargs: {"source_100.xml": [Table({"source_id": [100]})]},
    )
    assert artifacts[0].success is True
    payload = (tmp_path / "100.vot").read_text(encoding="utf-8")
    assert "<TABLEDATA>" in payload
