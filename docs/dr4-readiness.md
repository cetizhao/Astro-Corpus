# Gaia DR4 interface readiness

Last verified against official Gaia/Archive material: 2026-08-24.

## What is stable enough to implement now

Gaia catalogue tables continue to be accessed through the ESA Gaia Archive and
ADQL/TAP. Query results can be serialized as VOTable, FITS, CSV, or ECSV. Large
array/time-series products use DataLink. Current Archive tooling accepts
`INDIVIDUAL` and `RAW` structures; `COMBINED` was deprecated because it does not
scale to DR4.

The expected DR4 content is much broader than DR3. In particular,
`gaia_source` is expected to be a high-quality subset, while source-level
results for all processed sources are distributed across tables such as
`all_source_astrometry`, `all_source_photometry`, `all_source_rvs`, and
`all_source_flags`. Epoch astrometry, photometry, spectra, and images introduce
large DataLink products. The reference epoch is J2017.5.

Official references:

- [Gaia DR4 expected content](https://www.cosmos.esa.int/web/gaia/dr4)
- [Gaia Archive programmatic TAP/DataLink access](https://www.cosmos.esa.int/web/gaia-users/archive/programmatic-access)
- [Gaia Archive release notes](https://www.cosmos.esa.int/web/gaia-users/archive/release-notes)
- [Archive preparation for DR4 DataLink serialization](https://www.cosmos.esa.int/web/gaia/home/-/asset_publisher/wEXKCCPdRxv0/content/2022-12-02-xxx)

## What remains provisional

The project does not assume:

- that the TAP schema will be named `gaiadr4`;
- that preliminary table names/columns are final;
- that an astroquery-declared retrieval type is already served by the Archive;
- that DR3 and DR4 source IDs identify the same physical source;
- that the DR3 source-ID HEALPix divisor remains valid for DR4;
- that binary image products can be reconstructed from astroquery's returned
  table dictionary.

## Implemented boundary

`gaia_releases.py` is the local facts/hints layer. DR3 is stable. DR4 is marked
`provisional`, has no static TAP schema, and has no executable partition
strategy.

`gaia_archive.py` is the live facts layer. It discovers schemas, tables and
columns from `TAP_SCHEMA`, and can inspect DataLink links for one source. MCP
results keep local client hints separate from live Archive confirmation.
Metadata probes use a separate 10-second per-request timeout so an unavailable
Archive does not block an agent as long as a catalogue/bulk job may legitimately
run.

`gaia_products.py` defines the request identity:

```text
release + retrieval_type + identifiers + linking_parameter
        + data_structure + output_format + valid_data
```

Each output directory contains:

```text
data/gaia_products/<label>/
  product_request.json
  checkpoint.json
  artifacts.json
  archive_tables/
    batch-*.fits|vot|csv|ecsv
  <identifier>.fits|vot|csv|ecsv
```

`archive_tables` preserves the table and metadata returned by astroquery, then
re-serializes it locally. It is not advertised as an exact copy of the remote
HTTP bytes. A future blob transport will stream binary image/archive responses
and record response MIME type, length and checksum without table conversion.

## Safe agent sequence

```text
probe_gaia_archive("dr4")
  → describe_gaia_table("dr4", selected_table)
  → list_gaia_products("dr4", optional_source_id)
  → plan_gaia_product_download(...)
  → user/agent reviews scope and storage
  → download_gaia_product(explicit IDs only)
  → gaia_product_status(label)
```

Bulk DR4 remains blocked. Once the official data model is published, the
activation checklist is:

1. verify the discovered schema and column metadata;
2. confirm source-ID construction and partition behavior;
3. add release-specific canonical column mappings;
4. test every enabled DataLink product/format on a few public sources;
5. implement and verify binary blob transport for image products;
6. only then mark the DR4 profile stable and enable an executable bulk
   partition strategy.
