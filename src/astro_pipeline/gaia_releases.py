"""Gaia data-release profiles and stable release identity helpers.

Only facts that are public and sufficiently stable belong here.  In
particular, the DR4 TAP schema and final column model are deliberately left
unset until they can be discovered from the live Archive or confirmed from
the published data model.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


COMMON_DATALINK_FORMATS = (
    "votable",
    "votable_plain",
    "fits",
    "csv",
    "ecsv",
)
COMMON_DATALINK_STRUCTURES = ("INDIVIDUAL", "RAW")


@dataclass(frozen=True)
class GaiaReleaseProfile:
    """Known capabilities and safety boundaries for one Gaia release."""

    key: str
    display_name: str
    archive_tag: str
    status: str
    reference_epoch: float
    tap_schema: str | None
    schema_candidates: tuple[str, ...]
    main_table: str
    expected_source_tables: tuple[str, ...]
    datalink_products: tuple[str, ...]
    datalink_formats: tuple[str, ...] = COMMON_DATALINK_FORMATS
    datalink_structures: tuple[str, ...] = COMMON_DATALINK_STRUCTURES
    source_id_partition: str | None = None
    source_id_partition_confirmed: bool = False
    notes: tuple[str, ...] = ()

    @property
    def executable_tap_schema(self) -> str | None:
        """Static schema only; provisional releases require discovery."""
        return self.tap_schema

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DR3_PROFILE = GaiaReleaseProfile(
    key="dr3",
    display_name="Gaia DR3",
    archive_tag="Gaia DR3",
    status="stable",
    reference_epoch=2016.0,
    tap_schema="gaiadr3",
    schema_candidates=("gaiadr3",),
    main_table="gaia_source",
    expected_source_tables=("gaia_source",),
    datalink_products=(
        "EPOCH_PHOTOMETRY",
        "XP_CONTINUOUS",
        "XP_SAMPLED",
        "RVS",
        "MCMC_GSPPHOT",
        "MCMC_MSC",
    ),
    source_id_partition="source_id_healpix12",
    source_id_partition_confirmed=True,
    notes=(
        "source_id level-12 HEALPix partition is documented for DR3",
        "array products are served separately through DataLink",
    ),
)


# Expected DR4 product names are already declared by astroquery 0.4.11 and
# Gaia Archive preparation material.  They are client-side capability hints,
# not proof that a product is currently published by the Archive.
DR4_PROFILE = GaiaReleaseProfile(
    key="dr4",
    display_name="Gaia DR4",
    archive_tag="Gaia DR4",
    status="provisional",
    reference_epoch=2017.5,
    tap_schema=None,
    schema_candidates=("gaiadr4", "gaia_dr4"),
    main_table="gaia_source",
    expected_source_tables=(
        "gaia_source",
        "all_source_astrometry",
        "all_source_photometry",
        "all_source_rvs",
        "all_source_flags",
        "crowded_field_source",
        "gaia_source_environment",
    ),
    datalink_products=(
        "EPOCH_PHOTOMETRY",
        "MEAN_SPECTRUM_RVS",
        "MCMC_GSP_PHOT",
        "EPOCH_ASTROMETRY",
        "EPOCH_SPECTRUM_RVS",
        "EPOCH_ASTROMETRY_CROWDED_FIELD",
        "EPOCH_PHOTOMETRY_CROWDED_FIELD",
        "EPOCH_IMAGE",
        "EPOCH_PHOTOMETRY_CCD",
        "EPOCH_SPECTRUM_XP_SSO",
        "EPOCH_SPECTRUM_XP_CROWDING",
        "MEAN_SPECTRUM_XP",
        "EPOCH_SPECTRUM_XP",
        "CROWDED_FIELD_IMAGE",
        "EPOCH_ASTROMETRY_BRIGHT",
        "MEAN_SPECTRUM_XP_GRAVLENS",
        "EPOCH_FLAGS_NSS",
        "EPOCH_PARAMETERS_RVS_SINGLE",
        "EPOCH_PARAMETERS_RVS_DOUBLE",
        "EPOCH_FLAGS_VARI",
    ),
    source_id_partition=None,
    source_id_partition_confirmed=False,
    notes=(
        "final TAP schema and column model must be discovered or published",
        "gaia_source is expected to be a high-quality subset, not all processed sources",
        "DR4 source identifiers must be treated as independent from DR3 identifiers",
        "HEALPix source_id partitioning is disabled until confirmed for DR4",
    ),
)


_PROFILES = {profile.key: profile for profile in (DR3_PROFILE, DR4_PROFILE)}
_ALIASES = {
    "3": "dr3",
    "dr3": "dr3",
    "gaiadr3": "dr3",
    "gaia dr3": "dr3",
    "gaia_dr3": "dr3",
    "4": "dr4",
    "dr4": "dr4",
    "gaiadr4": "dr4",
    "gaia dr4": "dr4",
    "gaia_dr4": "dr4",
}


def normalize_gaia_release(release: str) -> str:
    normalized = str(release).strip().lower().replace("-", " ")
    try:
        return _ALIASES[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(_PROFILES))
        raise ValueError(
            f"unsupported Gaia release {release!r}; supported profiles: {supported}"
        ) from exc


def get_gaia_release_profile(release: str = "dr3") -> GaiaReleaseProfile:
    return _PROFILES[normalize_gaia_release(release)]


def validate_gaia_schema(release: str, schema: str) -> str:
    """Reject a schema that is known to belong to another release."""
    profile = get_gaia_release_profile(release)
    schema = str(schema).strip()
    for other in _PROFILES.values():
        if (
            other.key != profile.key
            and schema.lower() in {item.lower() for item in other.schema_candidates}
        ):
            raise ValueError(
                f"db_schema {schema!r} belongs to {other.display_name}, "
                f"not {profile.display_name}"
            )
    return schema


def list_gaia_release_profiles() -> list[dict[str, Any]]:
    return [profile.to_dict() for profile in _PROFILES.values()]
