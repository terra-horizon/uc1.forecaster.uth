# Per-run STAC loader export

After a successful forecast, the stored forecasting pipeline generates one
unified JSON per configured tile. The export is derived from the observation
snapshot, physical-unit model inputs, and predictions of that execution.
It does not change existing collector/forecast data or contact the partner
STAC API or MongoDB.

## Storage

```text
terra-bucket/terra-uc1/<aoi_id>/stac-loader/runs/<forecast-execution-id>/
├── tiles/
│   ├── tile_0.json
│   ├── tile_1.json
│   └── ...
└── manifest.json
```

Local copies are under:

```text
<forecast-run-directory>/stac_loader/<forecast-execution-id>/
```

Each execution has its own prefix, including executions with the same forecast
anchor. The loader can choose its STAC Item ID independently. No `latest`
pointer is updated. Historical files at existing prefixes are untouched.

## Tile JSON contract

Top-level keys are `geom_id`, `geometry`, `variables`, `data`, and `metadata`.
There is no `schema_version` field.

- `geom_id`: the existing tile ID, unchanged.
- `geometry`: the original Polygon/MultiPolygon from `river_tiles.geojson`.
  If unavailable, the tile record geometry or bbox polygon is used; metadata
  explicitly identifies the bbox fallback. Coordinates must be WGS84.
- `variables`: the seven water-quality variables with display limits and
  thresholds. Limits do not clip or validate scientific measurements.
- `data`: chronologically sorted timestamped records. Same-date observations
  and preprocessed records are both retained.
- `metadata`: AOI, collection, execution, model, date coverage, geometry source,
  source observation references when available, and per-tile forecast status.

Each data record has `date`, `values`, `label`, and `stage`:

| Stage | Label | Meaning |
|---|---|---|
| `observed` | `collection` | Collector records; supplied quality flags/status are preserved |
| `preprocessed` | `collection` | Prepared physical-unit values retain collected values |
| `preprocessed` | `synthetic` | At least one prepared variable was filled or changed |
| `forecasted` | `forecast` | Model prediction, including step and reference time |

Preprocessed rows include per-variable `processing.value_origins` to distinguish
collected from generated values in a mixed row. Preparation reuses
`prepare_raw_frame`, as inference does. Normalized tensors, rolling differences,
and seasonal features are not included as water-quality measurements.
The current stored pipeline generally retains observed values during
preprocessing; it does not manufacture a daily or five-day observation grid.
Zero-valued `_gpr_std` compatibility columns are not exported as uncertainty.

Dates follow the existing calendar-day convention at UTC midnight, not exact
satellite acquisition timestamps. History covers the observations loaded for
the execution, bounded by its history/as-of settings and forecast anchor.
Forecast dates come from actual model output, not a fabricated fixed range.
The per-tile `last_input_date`, when supplied by inference, identifies its
actual last model input; it can differ from the AOI-level anchor.

## Units and display limits

Units match the collector's Se2WaQ calculations. Values are not rescaled.

| Variable | Unit | Display range | Good / warning / critical boundaries |
|---|---|---|---|
| CDOM | mg/L | 0–5 | 0–2 / 2–4 / 4–5 |
| Chl_a | µg/L | 0–50 | 0–12 / 12–30 / 30–50 |
| Color | Pt-Co | 0–50 | 0–20 / 20–40 / 40–50 |
| Cya | 10^3 cells/mL | 0–100 | 0–20 / 20–50 / 50–100 |
| DOC | mg/L | 0–40 | 0–10 / 10–30 / 30–40 |
| Turb | NTU | 0–20 | 0–8 / 8–16 / 16–20 |
| WQI | dimensionless | -1–1 | No supplied thresholds |

Colors are `#639922`, `#EF9F27`, and `#E24B4A` respectively.
Values outside these ranges remain unchanged. Non-finite measurements are
represented as JSON `null`, never `NaN` or infinity.

## Manifest and completeness

The manifest lists the execution, forecast anchor, collection, expected tile
count, number of forecasted tiles, each relative file path, S3 key, SHA-256
checksum, and forecast status. Its `status` is `ready`.

All tile payloads are built and validated before uploading. The S3 manifest is
uploaded last, after every tile upload succeeds. The loader must process only
prefixes containing this manifest, use its file list, and verify checksums of
the downloaded bytes. Partial prefixes without a manifest are incomplete and
must be ignored. A local manifest is also written only after successful export.

`ready` means the export package is complete; it does not mean all geometries
have forecasts or that the data has already been loaded into STAC. Every tile
gets a JSON. Tiles without predictions have `failed` or `insufficient_history`
forecast status and retain any available history. A tile without any records
has an empty `data` array and null date coverage.

## Failure isolation and controls

The export runs after existing canonical forecast publication. If export
generation or upload fails, the existing forecast remains successful.
`forecast_run_result.json` and the forecaster `pipeline_runs` record expose
`stac_loader_export.status` (`published`, `local_only`, `failed`, or `disabled`).
Here `published` means stored in S3, not loaded into partner STAC. A warning and
error details are recorded on failure; no ready manifest is uploaded.

The export is enabled by default. Disable it with the CLI option:

```text
--no-stac-loader-export
```

Or set `STAC_LOADER_EXPORT_ENABLED=false` in the forecaster process/container
environment. For Compose, an `.env` value alone is not sufficient unless that
variable is passed into the container; use a service environment entry or
`docker compose run -e STAC_LOADER_EXPORT_ENABLED=false ...`.

Local `--no-publish` runs generate local export files without writing to S3.
No new dependencies, Dockerfile changes, or database migrations are required.

## Server rollout

Pull the updated branch and rebuild the existing forecaster image using the
deployment's existing Compose/build procedure. Do not recreate the storage
services. The next forecast execution will create the new prefix automatically.
Check the returned `stac_loader_export` status and its `manifest_key`.

The partner loader must support the new `geom_id`, `stage`, `label`, and
`values` contract. Integrating or executing that loader is a separate step.
