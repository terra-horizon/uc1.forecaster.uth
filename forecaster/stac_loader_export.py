"""Derived per-execution tile snapshots for the partner STAC loader.

This module writes only the new stac-loader prefix. It does not alter the
canonical collector, features, forecasts, or existing local STAC catalog.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shapely.geometry import shape

from forecaster.core.global_preprocessor import prepare_raw_frame
from forecaster.stac_exporter import TARGET_VARIABLES, bbox_geometry


def _variable(name: str, units: str, maximum: float, limits: tuple[float, float]) -> dict[str, Any]:
    low, high = limits
    return {
        "long_name": name, "units": units, "min": 0, "max": maximum,
        "threshold": {
            "good": {"color": "#639922", "interval": [0, low]},
            "warning": {"color": "#EF9F27", "interval": [low, high]},
            "critical": {"color": "#E24B4A", "interval": [high, maximum]},
        },
    }


# Display metadata, not constraints: exported measurements are never clipped.
# Units match the collector's Se2WaQ formulas, including Cya's 10^3 scale.
VARIABLES = {
    "CDOM": _variable("Colored Dissolved Organic Matter", "mg/L", 5, (2, 4)),
    "Chl_a": _variable("Chlorophyll-a", "µg/L", 50, (12, 30)),
    "Color": _variable("Water Color", "Pt-Co", 50, (20, 40)),
    "Cya": _variable("Cyanobacteria", "10^3 cells/mL", 100, (20, 50)),
    "DOC": _variable("Dissolved Organic Carbon", "mg/L", 40, (10, 30)),
    "Turb": _variable("Turbidity", "NTU", 20, (8, 16)),
    "WQI": {"long_name": "Water Quality Index", "min": -1, "max": 1},
}


def _values(record: dict[str, Any]) -> dict[str, float | None]:
    result = {}
    for variable in TARGET_VARIABLES:
        value = record.get(variable)
        numeric = float(value) if value is not None else None
        result[variable] = numeric if numeric is not None and math.isfinite(numeric) else None
    return result


def _timestamp(value: str) -> str:
    # These products represent calendar days; timestamps use the existing UTC
    # day convention rather than pretending to know a scene acquisition time.
    return f"{str(value)[:10]}T00:00:00Z"


def _encode(payload: dict[str, Any]) -> bytes:
    # Match MongoMinioStore.upload_json serialization for checksum verification.
    return json.dumps(payload, indent=2, allow_nan=False).encode("utf-8")


class StacLoaderExporter:
    def export(
        self, *, output_dir: Path, aoi_id: str, execution_id: str,
        forecast_run_id: str, anchor_date: str, tiles: list[dict[str, Any]],
        observations: list[dict[str, Any]], feature_csvs: dict[str, str],
        inference_tiles: dict[str, Any], forecast_rows: list[dict[str, Any]],
        model_name: str, geometries: dict[str, Any] | None = None,
        aoi_definition_hash: str | None = None, storage: Any = None,
    ) -> dict[str, Any]:
        root = output_dir / execution_id
        root.mkdir(parents=True, exist_ok=True)
        prefix = f"terra-uc1/{aoi_id}/stac-loader/runs/{execution_id}/"
        geometry_map = {
            str(feature["id"]): feature["geometry"]
            for feature in (geometries or {}).get("features", [])
        }
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        observed_by_tile: dict[str, list[dict[str, Any]]] = {}
        forecasts_by_tile: dict[str, list[dict[str, Any]]] = {}
        for record in observations:
            if str(record.get("observation_date", ""))[:10] <= anchor_date:
                observed_by_tile.setdefault(str(record["tile_id"]), []).append(record)
        for record in forecast_rows:
            forecasts_by_tile.setdefault(str(record["tile_id"]), []).append(record)

        files = []
        prepared = []
        seen = set()
        for tile in sorted(tiles, key=lambda t: str(t.get("name") or t.get("tile_id"))):
            tile_id = str(tile.get("name") or tile.get("tile_id") or "")
            if not tile_id or tile_id in seen or any(c in tile_id for c in ("/", "\\")) or tile_id in (".", ".."):
                raise ValueError(f"Invalid or duplicate loader geometry ID: {tile_id!r}")
            seen.add(tile_id)
            geometry = geometry_map.get(tile_id) or tile.get("geometry")
            geometry_source = "tile_geojson"
            if geometry is None:
                geometry = bbox_geometry(tile["bbox"])
                geometry_source = "tile_bbox_fallback"
            geom = shape(geometry)
            if geom.is_empty or not geom.is_valid or geom.geom_type not in ("Polygon", "MultiPolygon"):
                raise ValueError(f"Invalid loader geometry for {tile_id}")
            west, south, east, north = geom.bounds
            if not all(math.isfinite(value) for value in geom.bounds) or not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
                raise ValueError(f"Loader geometry must use WGS84 longitude/latitude for {tile_id}")

            data = []
            source_values = {}
            source_keys = set()
            for record in sorted(observed_by_tile.get(tile_id, []), key=lambda r: str(r["observation_date"])):
                values = _values(record)
                date = str(record["observation_date"])[:10]
                row = {
                    "date": _timestamp(date), "label": "collection", "stage": "observed", "values": values,
                    "quality": {key: record[key] for key in ("collection_status", "water_status", "quality_flags") if key in record},
                }
                if record.get("stac_item_ids"):
                    row["source_stac_item_ids"] = record["stac_item_ids"]
                data.append(row)
                source_values[date] = values
                if isinstance(record.get("artifact"), dict) and record["artifact"].get("key"):
                    source_keys.add(record["artifact"]["key"])

            if tile_id in feature_csvs:
                # Reuse the exact physical-unit preparation used by inference;
                # do not export normalized tensors or reprocess the collector.
                frame, _, _, _, _ = prepare_raw_frame(feature_csvs[tile_id], target_cols=TARGET_VARIABLES)
                for record in frame.to_dict(orient="records"):
                    date = record["date"].date().isoformat()
                    values = _values(record)
                    original = source_values.get(date, {})
                    provenance = {
                        key: "collection" if values[key] is not None and original.get(key) is not None
                        and math.isclose(values[key], original[key], rel_tol=1e-6, abs_tol=1e-8) else "synthetic"
                        for key in TARGET_VARIABLES
                    }
                    data.append({
                        "date": _timestamp(date), "label": "synthetic" if "synthetic" in provenance.values() else "collection",
                        "stage": "preprocessed", "values": values,
                        "processing": {"method": "prepare_raw_frame", "value_origins": provenance},
                    })

            predictions = forecasts_by_tile.get(tile_id, [])
            tile_reference = inference_tiles.get(tile_id, {}).get("last_input_date") or anchor_date
            for record in predictions:
                data.append({
                    "date": _timestamp(record["forecast_date"]), "label": "forecast", "stage": "forecasted",
                    "values": _values(record),
                    "forecast": {"step": record["step"], "reference_datetime": _timestamp(tile_reference)},
                })
            order = {"observed": 0, "preprocessed": 1, "forecasted": 2}
            data.sort(key=lambda row: (row["date"], order[row["stage"]]))
            status = "success" if predictions else "failed" if inference_tiles.get(tile_id, {}).get("error") else "insufficient_history"
            metadata = {
                "aoi_id": aoi_id, "collection_id": "water-pollution", "execution_id": execution_id,
                "forecast_run_id": forecast_run_id, "forecast_reference_datetime": _timestamp(anchor_date),
                "generated_at": generated_at, "model": model_name, "geometry_source": geometry_source,
                "forecast_status": status, "source_observation_keys": sorted(source_keys),
                "last_input_date": inference_tiles.get(tile_id, {}).get("last_input_date"),
                "start": data[0]["date"] if data else None, "end": data[-1]["date"] if data else None,
                "aoi_definition_hash": aoi_definition_hash,
            }
            if inference_tiles.get(tile_id, {}).get("error"):
                metadata["forecast_error"] = str(inference_tiles[tile_id]["error"])[:2000]
            payload = {"geom_id": tile_id, "geometry": geometry, "variables": VARIABLES, "data": data, "metadata": metadata}
            encoded = _encode(payload)
            relative = f"tiles/{tile_id}.json"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encoded)
            files.append({"geom_id": tile_id, "file": relative, "key": prefix + relative,
                          "sha256": hashlib.sha256(encoded).hexdigest(), "forecast_status": status})
            prepared.append((prefix + relative, payload))

        if not files:
            raise ValueError("No geometries available for STAC loader export")
        manifest = {
            "run_id": execution_id, "aoi_id": aoi_id, "collection_id": "water-pollution",
            "forecast_run_id": forecast_run_id, "forecast_reference_datetime": _timestamp(anchor_date),
            "generated_at": generated_at, "status": "ready", "tile_count": len(files),
            "forecast_tile_count": sum(file["forecast_status"] == "success" for file in files),
            "files": files,
        }
        # Upload this completion marker LAST, only after every tile succeeds.
        if storage is not None:
            for key, payload in prepared:
                storage.upload_json_if_changed(payload, key=key)
            storage.upload_json_if_changed(manifest, key=prefix + "manifest.json")
        (root / "manifest.json").write_bytes(_encode(manifest))
        return {"status": "published" if storage is not None else "local_only", "tile_count": len(files),
                "prefix": prefix, "manifest_key": prefix + "manifest.json" if storage is not None else None,
                "local_directory": str(root)}
