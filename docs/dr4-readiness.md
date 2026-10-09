# Gaia DR4 interface readiness

Official interface assumptions last reviewed: 2026-08-24.

The runtime probe at **2026-09-29 11:35:24 UTC** reached the Archive but returned
`published_in_tap=false`, `resolved_schema=null`, and no DR4 tables. The local
record is `data/collection_validation/dr4-probe-delivery.json` (generated, not committed).
This is an observation, not a prediction of current or future Archive state.
Probe again immediately before any DR4 activation.

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
- that equal numeric DR3 and DR4 source IDs identify the same physical source;
- that a DR4 product name can be parsed with a similarly named DR3 parser;
- that the DR3 source-ID HEALPix divisor remains valid for DR4;
- that binary image products can be reconstructed from astroquery's returned
  table dictionary.

## Implemented boundary

`gaia_releases.py` is the local facts/hints layer. DR3 is stable. DR4 is marked
`provisional`, has no static TAP schema, and has no executable partition
strategy. Ordinary DR4 corpus normalization remains disabled until the live
schema and canonical column mapping are confirmed. Every source and product key
is release-qualified as `(release, source_id)` or by a release-bearing product
contract; numeric IDs are never shared across releases.

`gaia_archive.py` is the live facts layer. It discovers schemas, tables and
columns from `TAP_SCHEMA`, and can inspect DataLink links for one source. MCP
results keep local client hints separate from live Archive confirmation.
Client hints are read from literal declarations in the installed astroquery
package without importing its Gaia singleton (which fetches server notifications).
Unrecognized declarations produce unknown client acceptance, not an assumed
capability. A request without a source ID performs no network I/O.
Metadata probes use a separate 30-second total network budget per query,
including retries, so an unavailable Archive does not block an agent as long
as a catalogue/bulk job may legitimately run. TAP transport and cleanup bounds
are documented in the [operator runbook](operator-runbook.md).

The read-only spectrum adapter registry is fail-closed. It currently executes
only the exact DR3 `XP_CONTINUOUS` and `XP_SAMPLED` FITS contracts. Expected DR4
names, including `MEAN_SPECTRUM_XP` and `EPOCH_SPECTRUM_XP`, remain displayable
client hints but return `unsupported`: source-specific live links, real product
samples, serialization, units, time dimensions, quality fields, and independent
parser tests are required before registration. Product-name similarity alone is
not activation evidence.

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

Bulk DR4 and ordinary DR4 corpus normalization remain blocked. Once the
official data model and live services are available, the activation checklist is:

1. rerun `probe_gaia_archive` and verify the discovered schema rather than
   supplying an assumed one;
2. inspect live table columns, units, nullability, and source-list relationships,
   then add release-specific canonical mappings;
3. confirm source-ID construction and partition behavior before enabling bulk;
4. inspect source-specific DataLink links and test every enabled product/format
   on a few public sources;
5. validate each spectrum parser against real products, including serialization,
   wavelength/time axes, units, uncertainties, and quality flags;
6. implement and verify binary blob transport for image products;
7. only then mark the DR4 profile stable and enable the corresponding corpus,
   spectrum, or bulk path.
