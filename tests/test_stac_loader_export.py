from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from forecaster.from_storage import StoredForecastConfig, StoredForecastPipeline
from forecaster.stac_loader_export import StacLoaderExporter, VARIABLES
from tests.test_forecast_from_storage import FakeStorage, observations


def prediction():
    return {"tile_id": "tile_0", "forecast_date": "2026-01-29", "step": 1,
            "CDOM": 37, "Chl_a": 2, "Color": 3, "Cya": 4, "DOC": 5, "Turb": 6, "WQI": 0.5}


def export_inputs(tmp_path):
    csv = tmp_path / "input.csv"
    csv.write_text("date,CDOM,Chl_a,Color,Cya,DOC,Turb,WQI\n"
                   "2026-01-01,1,2,3,4,5,6,0.5\n"
                   "2026-01-02,,2,3,4,5,6,0.5\n"
                   "2026-01-03,3,2,3,4,5,6,0.5\n", encoding="utf-8")
    records = [{"tile_id": "tile_0", "observation_date": f"2026-01-0{day}",
                "CDOM": cdom, "Chl_a": 2, "Color": 3, "Cya": 4, "DOC": 5, "Turb": 6, "WQI": 0.5}
               for day, cdom in ((1, 1), (2, None), (3, 3))]
    return dict(output_dir=tmp_path / "export", aoi_id="test", execution_id="run-1",
                forecast_run_id="forecast_2026-01-03", anchor_date="2026-01-03",
                tiles=[{"name": "tile_0", "bbox": [22, 38, 23, 39]},
                       {"name": "tile_1", "bbox": [22, 38, 23, 39]}],
                observations=records, feature_csvs={"tile_0": str(csv)},
                inference_tiles={}, forecast_rows=[prediction()], model_name="test-model")


def test_snapshot_preserves_geometry_stages_units_and_uploads_manifest_last(tmp_path):
    inputs = export_inputs(tmp_path)
    # Deliberately not the bbox polygon: preserve the actual spatial feature.
    geometry = {"type": "Polygon", "coordinates": [[[22, 38], [22.1, 38], [22.05, 38.1], [22, 38]]]}
    inputs["geometries"] = {"features": [{"id": "tile_0", "geometry": geometry}]}
    storage = FakeStorage([])
    result = StacLoaderExporter().export(**inputs, storage=storage)
    keys = list(storage.uploads)
    assert keys[-1].endswith("manifest.json")
    assert all("/stac-loader/runs/run-1/" in key for key in keys)
    tile = storage.uploads[keys[0]]
    assert "schema_version" not in tile
    assert tile["geometry"] == geometry
    assert tile["variables"]["CDOM"]["units"] == "mg/L"
    assert tile["variables"]["Cya"]["units"] == "10^3 cells/mL"
    assert len([row for row in tile["data"] if row["date"] == "2026-01-01T00:00:00Z"]) == 2
    synthesized = next(row for row in tile["data"] if row["stage"] == "preprocessed" and row["label"] == "synthetic")
    assert synthesized["values"]["CDOM"] == 2
    assert synthesized["processing"]["value_origins"]["Chl_a"] == "collection"
    assert next(row for row in tile["data"] if row["label"] == "forecast")["values"]["CDOM"] == 37
    assert storage.uploads[keys[1]]["metadata"]["forecast_status"] == "insufficient_history"
    assert storage.uploads[keys[1]]["data"] == []
    manifest = storage.uploads[keys[-1]]
    assert manifest["geometries_count"] == 2
    assert manifest["forecast_geometries_count"] == 1
    assert manifest["loaded-files"] == ["geometries/tile_0.json", "geometries/tile_1.json"]
    assert manifest["original_runs"]["forecaster_run_id"] == "run-1"
    for file in manifest["files"]:
        encoded = (Path(result["local_directory"]) / file["file"]).read_bytes()
        assert hashlib.sha256(encoded).hexdigest() == file["sha256"]
        assert json.loads(encoded) == storage.uploads[file["key"]]


def test_failed_upload_never_writes_completion_marker(tmp_path):
    class FailingStorage(FakeStorage):
        def upload_json_if_changed(self, value, *, key):
            if key.endswith("tile_1.json"):
                raise OSError("Upload interrupted")
            return super().upload_json_if_changed(value, key=key)

    storage = FailingStorage([])
    with pytest.raises(OSError, match="interrupted"):
        StacLoaderExporter().export(**export_inputs(tmp_path), storage=storage)
    assert not any(key.endswith("manifest.json") for key in storage.uploads)
    assert not (tmp_path / "export" / "run-1" / "manifest.json").exists()


def test_manifest_links_recorded_original_files_and_runs(tmp_path):
    inputs = export_inputs(tmp_path)
    inputs["observations"][0].update(last_run_id="collector-1", artifact={
        "bucket": "terra-bucket", "key": "terra-uc1/test/observations/2026-01-01.json", "sha256": "observed-checksum"})
    inputs["observations"][1]["last_run_id"] = "collector-2"
    # A later record is outside this snapshot and must not appear as provenance.
    inputs["observations"].append({"tile_id": "tile_0", "observation_date": "2026-02-01", "last_run_id": "future-run"})
    inputs["source_files"] = [{"role": "forecast", "bucket": "terra-bucket",
                               "key": "terra-uc1/test/forecasts/forecast_2026-01-03.json", "sha256": "forecast-checksum"}]
    inputs["run_context"] = {"api_job_id": "job-1", "api_run_job_id": "caller-1"}
    result = StacLoaderExporter().export(**inputs)
    manifest = json.loads((Path(result["local_directory"]) / "manifest.json").read_text())
    assert "s3://terra-bucket/terra-uc1/test/observations/2026-01-01.json" in manifest["original-files"]
    assert "s3://terra-bucket/terra-uc1/test/forecasts/forecast_2026-01-03.json" in manifest["original-files"]
    assert str(Path(inputs["feature_csvs"]["tile_0"]).resolve()) in manifest["original-files"]
    assert manifest["original_runs"]["collector_last_run_ids"] == ["collector-1", "collector-2"]
    assert manifest["original_runs"]["api_run_job_id"] == "caller-1"
    tile = json.loads((Path(result["local_directory"]) / "geometries/tile_0.json").read_text())
    assert tile["data"][0]["source_run_id"] == "collector-1"
    assert tile["data"][0]["source_artifact"]["sha256"] == "observed-checksum"


def test_invalid_geometry_rejects_package_before_s3_upload(tmp_path):
    inputs = export_inputs(tmp_path)
    inputs["geometries"] = {"features": [{"id": "tile_1", "geometry": {"type": "Polygon", "coordinates": []}}]}
    storage = FakeStorage([])
    with pytest.raises(ValueError, match="Invalid loader geometry"):
        StacLoaderExporter().export(**inputs, storage=storage)
    assert not storage.uploads


def _pipeline(tmp_path, monkeypatch, storage, *, enabled=True):
    monkeypatch.setattr("forecaster.from_storage.AOIInferencePipeline.run_model_inference",
                        lambda *args, **kwargs: {"tiles": {"tile_0": {"forecast": {"model": [
                            {"date": "2026-01-29", **prediction()}
                        ]}}}})
    return StoredForecastPipeline(StoredForecastConfig(aoi_id="partner-aoi", run_name="run",
                                   output_root=tmp_path, export_stac_loader=enabled), storage=storage).execute()


def test_pipeline_exports_after_canonical_forecast_without_changing_it(tmp_path, monkeypatch):
    storage = FakeStorage(observations())
    storage.settings.minio_bucket = "terra-bucket"
    storage.settings.mongo_database = "terra-db"
    for record in storage.records:
        record["last_run_id"] = "original-collector-run"
        record["artifact"] = {"bucket": "terra-bucket", "key": f"terra-uc1/partner-aoi/observations/{record['observation_date']}.json"}
    original_upload = storage.upload_json_if_changed
    def upload(value, *, key):
        original_upload(value, key=key)
        return {"bucket": "terra-bucket", "key": key, "sha256": "source-checksum"}
    storage.upload_json_if_changed = upload
    result = _pipeline(tmp_path, monkeypatch, storage)
    assert result["status"] == "success"
    assert result["stac_loader_export"]["status"] == "published"
    keys = list(storage.uploads)
    canonical = "terra-uc1/partner-aoi/forecasts/forecast_2026-01-24.json"
    assert keys.index(canonical) < next(i for i, key in enumerate(keys) if "/stac-loader/" in key)
    assert storage.uploads[canonical][0]["CDOM"] == 37
    assert storage.runs[-1]["stac_loader_export"]["status"] == "published"
    manifest = storage.uploads[result["stac_loader_export"]["manifest_key"]]
    assert manifest["run_id"] == storage.runs[-1]["run_id"]
    assert manifest["original_runs"]["mongo_forecaster_run_filter"]["run_id"] == manifest["run_id"]
    assert manifest["original_runs"]["collector_last_run_ids"] == ["original-collector-run"]
    assert f"s3://terra-bucket/{canonical}" in manifest["original-files"]
    assert "s3://terra-bucket/terra-uc1/partner-aoi/preprocessed/features/tile_0.json" in manifest["original-files"]


def test_export_failure_does_not_fail_successful_forecast(tmp_path, monkeypatch):
    storage = FakeStorage(observations())
    monkeypatch.setattr("forecaster.from_storage.StacLoaderExporter.export",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("S3 unavailable")))
    result = _pipeline(tmp_path, monkeypatch, storage)
    assert result["status"] == "success"
    assert result["stac_loader_export"]["status"] == "failed"
    assert storage.forecasts
    assert storage.runs[-1]["status"] == "success"


@pytest.mark.parametrize("environment", [False, True])
def test_export_can_be_disabled_without_disabling_forecast(tmp_path, monkeypatch, environment):
    storage = FakeStorage(observations())
    if environment:
        monkeypatch.setenv("STAC_LOADER_EXPORT_ENABLED", "false")
    result = _pipeline(tmp_path, monkeypatch, storage, enabled=environment)
    assert result["stac_loader_export"]["status"] == "disabled"
    assert storage.forecasts
    assert not any("/stac-loader/" in key for key in storage.uploads)
