import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from hrl_restoration_pipeline.ingestion import load_submission
from hrl_restoration_pipeline.publication import activate_local_snapshot, merge, publish_local
from hrl_restoration_pipeline.registry import CsvRegistry
from hrl_restoration_pipeline.reporting import write_reports
from hrl_restoration_pipeline.transformation import as_feature_collection, canonicalize, publicize
from hrl_restoration_pipeline.validation import MANIFEST_PATH, SCHEMA_PATH, _resolve_lead_entities, candidate_profile_errors, validate_records
from hrl_restoration_pipeline.models import Report

FIXTURES = Path(__file__).parent / "fixtures"

def registry(): return CsvRegistry(FIXTURES / "registry.csv", ref="2026-08-24-test")

def record(**updates):
    value = {"project_id": " HRL-001 ", "project_name": "Test", "project_description": "Description", "project_stage": "design", "lead_entity": "dwr", "early_implementation": True, "system": "Delta", "project_type": "tidal habitat", "acreage": 1.5, "target_species": "Chinook salmon"}
    value.update(updates); return value

def manifest(): return {"submission_id":"s1", "organization_code":"DWR", "data_as_of":"2026-08-20", "data_steward_name":"Example Steward", "data_steward_email":"steward@example.org"}

def test_snapshot_checksum_matches_manifest():
    manifest_data = json.loads(MANIFEST_PATH.read_text())
    assert manifest_data["tag"] == "v1.3.1"
    assert manifest_data["commit_sha"] == "fe5633a6a0e4dc240483bdfd2787909ff4fce7e3"
    expected = manifest_data["artifacts"][SCHEMA_PATH.name]
    assert hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest() == expected

def test_valid_record_is_repaired_and_awaits_approval():
    values, report = validate_records([record()], registry(), manifest(), [{"project_id":"HRL-001", "crs":"EPSG:3310", "geometry":{"type":"Point", "coordinates":[1,2]}}])
    assert report.status == "AWAITING_APPROVAL"
    assert values[0]["project_id"] == "HRL-001"
    assert report.repairs and report.warnings
    assert report.pipeline_version == "0.3.0"
    assert report.json()["registry"]["version"] == "2026-08-24-test"

def test_nan_optional_values_are_normalized_to_missing():
    values, report = validate_records([record(contractors=float("nan"), funding_sources=float("nan"))], registry(), manifest())
    assert "contractors" not in values[0] and "funding_sources" not in values[0]
    assert any(repair.rule == "nan_to_missing" for repair in report.repairs)

def test_lead_entity_aliases_normalize_to_stable_ids():
    report = Report("s1", {}, {}, "test", "2026-08-24T00:00:00Z")
    value = {"lead_entity": ["YWA", "Yuba Water Agency", "River Partners"]}
    catalog = {
        "ywa": {"yuba_water_agency"},
        "yuba water agency": {"yuba_water_agency"},
        "river partners": {"river_partners"},
    }
    _resolve_lead_entities(value, catalog, report, "HRL-001")
    assert value["lead_entity"] == ["yuba_water_agency", "river_partners"]
    assert {repair.rule for repair in report.repairs} == {"lead_entity_catalog_match"}

@pytest.mark.parametrize("change", [{"project_id":"MISSING"}, {"project_id":"HRL-RETIRED"}, {"project_type":"unknown"}, {"project_name":""}])
def test_invalid_records_need_correction(change):
    _, report = validate_records([record(**change)], registry(), manifest())
    assert report.status == "NEEDS_CORRECTION"

def test_construction_fields_are_required():
    _, report = validate_records([record(project_stage="construction")], registry(), manifest())
    assert any(x.rule == "stage_requiredness" and x.severity == "ERROR" for x in report.findings)

def test_early_stage_missing_acreage_is_a_warning():
    _, report = validate_records([record(acreage=None)], registry(), manifest())
    assert any(x.rule == "acreage_required" and x.severity == "WARNING" for x in report.findings)
    assert not any(x.rule == "acreage_required" and x.severity == "ERROR" for x in report.findings)


def test_construction_missing_acreage_is_an_error():
    _, report = validate_records([record(project_stage="construction", acreage=None)], registry(), manifest())
    assert any(x.rule == "acreage_required" and x.severity == "ERROR" for x in report.findings)


def test_fish_passage_acreage_exception():
    _, report = validate_records([record(project_stage="construction", project_type="fish passage improvement", acreage=None)], registry(), manifest())
    assert not any(x.rule == "acreage_required" for x in report.findings)

def test_spatial_errors_are_separate():
    _, report = validate_records([record()], registry(), manifest(), [{"project_id":"HRL-001", "geometry":{"type":"LineString", "coordinates":[]}}])
    assert {x.stage for x in report.errors} >= {"spatial"}

def test_reports_write_json_and_html_by_default_and_pdf_on_request(tmp_path):
    _, report = validate_records([record()], registry(), manifest(), validation_timestamp="2026-08-24T00:00:00Z")
    report.submission_metadata = {"organization": "Example organization", "primary_file": "projects.gpkg"}
    report.input_file = {"filename": "projects.gpkg", "feature_count": "1"}
    report.add("business", "WARNING", "stage_requiredness", "funding_secured is not yet supplied", "HRL-001")
    write_reports(report, tmp_path)
    assert json.loads((tmp_path / "validation-report.json").read_text())["status"] == "AWAITING_APPROVAL"
    html = (tmp_path / "validation-report.html").read_text()
    assert "<meta charset=\"utf-8\">" in html
    assert "PASSED WITH" in html and "Example organization" in html
    assert "<th>Check</th>" in html and "<th>What needs review</th>" in html
    assert "<strong>HRL-001</strong><br><span>Test</span>" in html
    assert "Required fields for the project stage" in html
    assert not (tmp_path / "validation-report.pdf").exists()
    with_pdf = tmp_path / "with-pdf"; write_reports(report, with_pdf, pdf=True)
    assert (with_pdf / "validation-report.pdf").read_bytes().startswith(b"%PDF")
    second = tmp_path / "second"; write_reports(report, second, pdf=True)
    assert (with_pdf / "validation-report.pdf").read_bytes() == (second / "validation-report.pdf").read_bytes()

def test_invalid_geometry_and_reprojection_are_reported():
    _, report = validate_records([record()], registry(), manifest(), [{"project_id":"HRL-001", "crs":"EPSG:3310", "source_crs":"EPSG:4326", "reprojected":True, "geometry":{"type":"Polygon", "coordinates":[[[0,0],[1,1],[1,0],[0,1],[0,0]]]}}])
    assert any(x.rule == "valid_geometry" for x in report.errors)
    assert any(x.rule == "reproject_to_epsg_3310" for x in report.repairs)

def test_geometry_outside_the_expected_extent_is_an_error():
    # A valid EPSG:3310 polygon that lands well outside California once
    # unprojected: the kind of result a wrong source CRS produces.
    far = {"type": "Polygon", "coordinates": [[[2_000_000, 0], [2_000_100, 0], [2_000_100, 100], [2_000_000, 0]]]}
    _, report = validate_records([record()], registry(), manifest(), [{"project_id": "HRL-001", "crs": "EPSG:3310", "geometry": far}])
    assert any(x.rule == "geometry_in_range" and x.severity == "ERROR" for x in report.errors)

def test_geometry_in_range_check_is_skipped_without_an_input_crs():
    _, report = validate_records([record()], registry(), manifest(), [{"project_id": "HRL-001", "geometry": {"type": "Point", "coordinates": [2_000_000, 0]}}])
    assert any(x.rule == "crs_required" for x in report.errors)
    assert not any(x.rule == "geometry_in_range" for x in report.findings)

def test_candidate_profiles_conform_when_complete():
    complete = record(project_id="HRL-001", estimated_budget=100, funding_secured=40, construction_start_year=2026, construction_completion_year=2027, geometry={"type":"Point", "coordinates":[1,2]})
    normalized, _ = validate_records([complete], registry(), manifest())
    canonical = canonicalize(normalized, manifest())
    assert not candidate_profile_errors(canonical, "RestorationProjectCanonicalRecord")
    assert not candidate_profile_errors(publicize(canonical), "RestorationProjectPublicRecord")

def test_legacy_funding_gap_is_preserved_with_warning_when_not_calculable():
    normalized, report = validate_records([record(funding_gap=60)], registry(), manifest())
    normalized[0]["geometry"] = {"type": "Point", "coordinates": [1, 2]}
    canonical = canonicalize(normalized, manifest())
    assert canonical[0]["funding_gap"] == 60
    assert any(x.rule == "funding_gap_passthrough" and x.severity == "WARNING" for x in report.findings)
    assert not candidate_profile_errors(canonical, "RestorationProjectCanonicalRecord")

def test_merge_keeps_absent_records_and_public_excludes_private_fields(tmp_path):
    normalized = {"project_stage": ["design"], "lead_entity": ["dwr"], "project_type": ["tidal habitat"], "target_species": ["Chinook salmon"]}
    old = canonicalize([record(project_id="HRL-OLD", geometry={"type":"Point", "coordinates":[1, 2]}, **normalized)], manifest())
    candidate = canonicalize([record(project_id="HRL-001", geometry={"type":"Point", "coordinates":[2, 1]}, **normalized)], manifest())
    joined = merge(old, candidate)
    assert [x["project_id"] for x in joined] == ["HRL-001", "HRL-OLD"]
    public = publicize(joined)
    assert "contact_email" not in public[0] and "source_organization_code" not in public[0]
    root = tmp_path / "exports"; publish_local(public, root, "v1", {"schema_version":"v1.1.1"})
    before = (root / "current.json").read_text()
    with pytest.raises(FileExistsError): publish_local(public, root, "v1", {})
    assert (root / "current.json").read_text() == before

def test_publicize_filters_non_active_canonical_records():
    active = {**record(project_id="HRL-001"), "record_status": "active"}
    retired = {**record(project_id="HRL-RETIRED"), "record_status": "retired"}
    public = publicize([active, retired])
    assert [value["project_id"] for value in public] == ["HRL-001"]

def test_manifest_and_input_contract(tmp_path):
    manifest_data = {"submission_id":"s", "organization":"DWR", "organization_code":"DWR", "dataset_name":"x", "submission_type":"update", "submission_scope":"partial_update", "data_as_of":"2026-08-20", "data_steward_name":"Example Steward", "data_steward_email":"steward@example.org", "primary_file":"x.geojson"}
    (tmp_path / "submission.json").write_text(json.dumps(manifest_data))
    (tmp_path / "x.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    assert load_submission(tmp_path)[1].name == "x.geojson"
    missing_steward = {key: value for key, value in manifest_data.items() if key != "data_steward_email"}
    (tmp_path / "submission.json").write_text(json.dumps(missing_steward))
    with pytest.raises(ValueError, match="data_steward_email"): load_submission(tmp_path)
    invalid_email = {**manifest_data, "data_steward_email": "not-an-email"}
    (tmp_path / "submission.json").write_text(json.dumps(invalid_email))
    with pytest.raises(ValueError, match="email"): load_submission(tmp_path)
    (tmp_path / "submission.json").write_text(json.dumps(manifest_data))
    (tmp_path / "another.gpkg").write_text("x")
    with pytest.raises(ValueError, match="exactly one"): load_submission(tmp_path)

def test_rejects_corrupt_and_incomplete_shapefile_archives(tmp_path):
    base = {"submission_id":"s", "organization":"DWR", "organization_code":"DWR", "dataset_name":"x", "submission_type":"update", "submission_scope":"partial_update", "data_as_of":"2026-08-20", "data_steward_name":"Example Steward", "data_steward_email":"steward@example.org", "primary_file":"x.zip"}
    (tmp_path / "submission.json").write_text(json.dumps(base)); (tmp_path / "x.zip").write_text("not a zip")
    with pytest.raises(ValueError, match="corrupt"): load_submission(tmp_path)
    with zipfile.ZipFile(tmp_path / "x.zip", "w") as archive: archive.writestr("x.shp", "placeholder")
    with pytest.raises(ValueError, match="incomplete"): load_submission(tmp_path)

def test_csv_registry_reads_the_plain_file_and_records_provenance(tmp_path):
    export = tmp_path / "project-id-registry.csv"
    export.write_text("project_id,status,project_name,assigned_organization_code,assigned_at,superseded_by_project_id\n"
                      "HRL-050,eligible,Example,DWR,2026-08-24,\nHRL-051,retired,Gone,DWR,2026-08-24,\n")
    reg = CsvRegistry(export, ref="abc1234")
    assert reg.eligible("HRL-050")
    assert not reg.eligible("HRL-051")
    assert not reg.eligible("HRL-999")
    provenance = reg.provenance()
    assert provenance["version"] == "abc1234"
    assert provenance["checksum"] == hashlib.sha256(export.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="csv"):
        CsvRegistry(tmp_path / "registry.json")

@pytest.mark.parametrize("suffix", [".geojson", ".gpkg", ".zip"])
def test_each_supported_ingestion_format(tmp_path, suffix):
    import geopandas as gpd
    from shapely.geometry import Point
    data = gpd.GeoDataFrame([{"project_id":"HRL-001"}], geometry=[Point(-121, 38)], crs="EPSG:4326")
    primary = "projects" + suffix
    manifest_data = {"submission_id":"formats", "organization":"DWR", "organization_code":"DWR", "dataset_name":"x", "submission_type":"update", "submission_scope":"partial_update", "data_as_of":"2026-08-20", "data_steward_name":"Example Steward", "data_steward_email":"steward@example.org", "primary_file":primary}
    (tmp_path / "submission.json").write_text(json.dumps(manifest_data))
    if suffix == ".geojson": data.to_file(tmp_path / primary, driver="GeoJSON")
    elif suffix == ".gpkg": data.to_file(tmp_path / primary, driver="GPKG")
    else:
        parts = tmp_path / "parts"; parts.mkdir(); data.to_file(parts / "projects.shp")
        shutil.make_archive(str(tmp_path / "projects"), "zip", parts)
    assert load_submission(tmp_path)[1].name == primary


def _cli_manifest(primary: str, submission_id: str = "cli-test") -> dict[str, str]:
    return {"submission_id": submission_id, "organization": "DWR", "organization_code": "DWR", "dataset_name": "CLI fixture", "submission_type": "update", "submission_scope": "partial_update", "data_as_of": "2026-08-20", "data_steward_name": "Example Steward", "data_steward_email": "steward@example.org", "primary_file": primary}


def _cli_record(project_id: str = "HRL-001", **updates):
    value = {"project_id": project_id, "project_name": "CLI project", "project_description": "End-to-end fixture", "project_stage": "design", "lead_entity": "dwr", "early_implementation": True, "system": "Delta", "project_type": "tidal habitat", "acreage": 1.5, "target_species": "Chinook salmon", "funding_gap": 60}
    value.update(updates)
    return value


def _write_cli_submission(directory: Path, suffix: str, values: dict | None = None, crs: str | None = "EPSG:4326", submission_id: str = "cli-test") -> Path:
    import geopandas as gpd
    from shapely.geometry import Point

    values = values or _cli_record()
    primary = f"projects{suffix}"
    (directory / "submission.json").write_text(json.dumps(_cli_manifest(primary, submission_id)))
    frame = gpd.GeoDataFrame([values], geometry=[Point(-121, 38)], crs=crs)
    if suffix == ".geojson":
        frame.to_file(directory / primary, driver="GeoJSON")
    elif suffix == ".gpkg":
        frame.to_file(directory / primary, driver="GPKG")
    else:
        parts = directory / "parts"; parts.mkdir()
        frame.to_file(parts / "projects.shp")
        shutil.make_archive(str(directory / "projects"), "zip", parts)
        shutil.rmtree(parts)
    return directory / primary


def _run_cli(submission: Path, output: Path, *, pdf: bool = False) -> subprocess.CompletedProcess[str]:
    command = ["hrl-pipeline", str(submission), "--registry", str(FIXTURES / "registry.csv"),
               "--registry-ref", "2026-08-24-test", "--output", str(output)]
    if pdf:
        command.append("--pdf")
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _run_promote(candidate: Path, canonical: Path, public_root: Path, version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["hrl-pipeline", "promote", str(candidate), "--canonical", str(canonical), "--public-root", str(public_root), "--version", version], text=True, capture_output=True, check=False)


def _assert_candidate_contract(path: Path, profile: str) -> None:
    import geopandas as gpd

    payload = json.loads(path.read_text())
    assert payload["crs"]["properties"]["name"] == "EPSG:3310"
    for feature in payload["features"]:
        record = {**feature["properties"], "geometry": feature["geometry"]}
        assert not candidate_profile_errors([record], profile)
    frame = gpd.read_file(path)
    assert frame.crs.to_epsg() == 3310
    assert frame.geometry.is_valid.all()


@pytest.mark.parametrize("suffix", [".geojson", ".gpkg", ".zip"])
def test_cli_valid_submissions_write_all_acceptance_artifacts_without_mutating_sources(tmp_path, suffix):
    submission = tmp_path / f"submission-{suffix[1:]}"; submission.mkdir()
    _write_cli_submission(submission, suffix)
    before = {path.relative_to(submission): hashlib.sha256(path.read_bytes()).hexdigest() for path in submission.rglob("*") if path.is_file()}
    output = tmp_path / "output"
    completed = _run_cli(submission, output)
    assert completed.returncode == 0, completed.stderr
    report = json.loads((output / "validation-report.json").read_text())
    assert report["status"] == "AWAITING_APPROVAL"
    assert report["submission_metadata"]["submission_id"] == "cli-test"
    assert report["input_file"]["filename"] == f"projects{suffix}"
    assert report["input_file"]["feature_count"] == "1"
    assert report["record_names"]["HRL-001"] == "CLI project"
    if suffix == ".gpkg":
        assert report["input_file"]["layer"] == "projects"
    assert (output / "validation-report.html").is_file()
    assert not (output / "validation-report.pdf").exists()
    assert json.loads((output / "status.json").read_text())["status"] == "AWAITING_APPROVAL"
    canonical = output / "canonical-candidate.geojson"; public = output / "public-candidate.geojson"
    assert canonical.is_file() and public.is_file()
    candidate_manifest = json.loads((output / "candidate-manifest.json").read_text())
    assert candidate_manifest["status"] == "AWAITING_APPROVAL"
    assert candidate_manifest["schema"]["version"] == "v1.3.1"
    assert candidate_manifest["artifacts"]["canonical-candidate.geojson"] == hashlib.sha256(canonical.read_bytes()).hexdigest()
    _assert_candidate_contract(canonical, "RestorationProjectCanonicalRecord")
    _assert_candidate_contract(public, "RestorationProjectPublicRecord")
    public_properties = json.loads(public.read_text())["features"][0]["properties"]
    forbidden = {"contact_name", "contact_email", "contractors", "funding_secured", "funding_gap", "estimated_budget_comments", "construction_completion_year_comments", "source_project_id", "source_organization_code", "last_submission_id", "source_data_as_of", "update_date"}
    assert not forbidden & set(public_properties)
    after = {path.relative_to(submission): hashlib.sha256(path.read_bytes()).hexdigest() for path in submission.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("kind", ["malformed_manifest", "ambiguous_input", "corrupt_geojson", "corrupt_gpkg", "bad_zip", "missing_crs", "invalid_geometry", "unknown_id", "retired_id", "candidate_profile"])
def test_cli_invalid_cases_write_reports_and_do_not_publish_candidates(tmp_path, kind):
    import geopandas as gpd
    from shapely.geometry import Polygon

    submission = tmp_path / kind; submission.mkdir(); output = tmp_path / "output"
    if kind == "malformed_manifest":
        (submission / "submission.json").write_text("{")
    elif kind == "ambiguous_input":
        _write_cli_submission(submission, ".geojson")
        (submission / "other.gpkg").write_bytes(b"not a geopackage")
    elif kind == "corrupt_geojson":
        (submission / "submission.json").write_text(json.dumps(_cli_manifest("projects.geojson")))
        (submission / "projects.geojson").write_text("not geojson")
    elif kind == "corrupt_gpkg":
        (submission / "submission.json").write_text(json.dumps(_cli_manifest("projects.gpkg")))
        (submission / "projects.gpkg").write_bytes(b"not a geopackage")
    elif kind == "bad_zip":
        (submission / "submission.json").write_text(json.dumps(_cli_manifest("projects.zip")))
        with zipfile.ZipFile(submission / "projects.zip", "w") as archive: archive.writestr("projects.shp", "bad")
    elif kind == "missing_crs":
        _write_cli_submission(submission, ".gpkg", crs=None)
    elif kind == "invalid_geometry":
        values = _cli_record()
        (submission / "submission.json").write_text(json.dumps(_cli_manifest("projects.geojson")))
        gpd.GeoDataFrame([values], geometry=[Polygon([(-121, 38), (-120, 39), (-120, 38), (-121, 39), (-121, 38)])], crs="EPSG:4326").to_file(submission / "projects.geojson", driver="GeoJSON")
    elif kind == "unknown_id":
        _write_cli_submission(submission, ".geojson", _cli_record("HRL-UNKNOWN"))
    elif kind == "retired_id":
        _write_cli_submission(submission, ".geojson", _cli_record("HRL-RETIRED"))
    else:
        _write_cli_submission(submission, ".geojson", _cli_record(project_description=""))
    completed = _run_cli(submission, output)
    assert completed.returncode == 2, completed.stderr
    report = json.loads((output / "validation-report.json").read_text())
    assert report["status"] == "NEEDS_CORRECTION"
    if report["input_file"]:
        assert report["submission_metadata"]["primary_file"] == report["input_file"]["filename"]
    assert (output / "validation-report.html").is_file() and not (output / "validation-report.pdf").exists()
    assert not (output / "canonical-candidate.geojson").exists()
    assert not (output / "public-candidate.geojson").exists()


def test_cli_pdf_flag_emits_a_pdf_companion(tmp_path):
    submission = tmp_path / "submission"; submission.mkdir()
    _write_cli_submission(submission, ".geojson")
    output = tmp_path / "output"
    assert _run_cli(submission, output, pdf=True).returncode == 0
    assert (output / "validation-report.pdf").read_bytes().startswith(b"%PDF")


def test_cli_legacy_funding_gap_warns_and_passes_through_to_canonical_only(tmp_path):
    submission = tmp_path / "legacy"; submission.mkdir(); output = tmp_path / "output"
    _write_cli_submission(submission, ".geojson", _cli_record(funding_gap=73))
    completed = _run_cli(submission, output)
    assert completed.returncode == 0
    report = json.loads((output / "validation-report.json").read_text())
    assert any(item["rule"] == "funding_gap_passthrough" for item in report["findings"])
    assert json.loads((output / "canonical-candidate.geojson").read_text())["features"][0]["properties"]["funding_gap"] == 73
    assert "funding_gap" not in json.loads((output / "public-candidate.geojson").read_text())["features"][0]["properties"]


def test_publication_writes_validated_artifacts_and_preserves_pointer_on_failure(tmp_path, monkeypatch):
    import geopandas as gpd

    normalized = {"project_stage": ["design"], "lead_entity": ["dwr"], "project_type": ["tidal habitat"], "target_species": ["Chinook salmon"]}
    public = publicize(canonicalize([_cli_record("HRL-001", geometry={"type": "Point", "coordinates": [100, 200]}, **normalized), _cli_record("HRL-OLD", geometry={"type": "Point", "coordinates": [200, 100]}, **normalized)], manifest()))
    root = tmp_path / "exports"
    target = publish_local(public[::-1], root, "v1", {"schema_version": "v1.2.0"})
    assert {path.name for path in target.iterdir()} == {"projects.geojson", "projects.gpkg", "projects.csv", "metadata.json"}
    pointer = json.loads((root / "current.json").read_text())
    assert set(pointer["artifacts"]) == {"projects.geojson", "projects.gpkg", "projects.csv", "metadata.json"}
    assert pointer["snapshot_version"] == "v1" and set(pointer["output_checksums"]) == {"projects.geojson", "projects.gpkg", "projects.csv"}
    assert [item["properties"]["project_id"] for item in json.loads((target / "projects.geojson").read_text())["features"]] == ["HRL-001", "HRL-OLD"]
    assert [row["project_id"] for row in __import__("csv").DictReader((target / "projects.csv").open())] == ["HRL-001", "HRL-OLD"]
    assert gpd.read_file(target / "projects.gpkg").crs.to_epsg() == 3310
    before = (root / "current.json").read_text()
    monkeypatch.setattr("hrl_restoration_pipeline.publication._validate_snapshot", lambda *_: (_ for _ in ()).throw(ValueError("forced failure")))
    with pytest.raises(ValueError, match="forced failure"):
        publish_local(public, root, "v2")
    assert (root / "current.json").read_text() == before
    assert not (root / "v2").exists()


def test_publication_output_conventions_and_sparse_multivalue_fields(tmp_path):
    import csv as _csv
    import math

    import geopandas as gpd

    # A many-vertex footprint plus a multivalue field populated for only one of
    # two records: both used to break _validate_snapshot's GeoPackage/CSV
    # round-trip (giant CSV geometry cell; NaN read back for the empty list).
    n = 4000
    ring = [[math.cos(t / n * 2 * math.pi) * 100.0, math.sin(t / n * 2 * math.pi) * 100.0] for t in range(n)]
    ring.append(ring[0])
    footprint = {"type": "Polygon", "coordinates": [ring]}
    normalized = {"project_stage": ["design"], "lead_entity": ["dwr"], "project_type": ["tidal habitat", "tributary floodplain habitat"], "target_species": ["Chinook salmon"]}
    public = publicize(canonicalize([
        _cli_record("HRL-001", geometry=footprint, funding_sources=["WCB grant", "USBR grant"], **normalized),
        _cli_record("HRL-002", geometry=footprint, **normalized),
    ], manifest()))
    target = publish_local(public, tmp_path / "exports", "2026-08-28", {"schema_version": "v1.3.1"})
    assert {path.name for path in target.iterdir()} == {"projects.geojson", "projects.gpkg", "projects.csv", "metadata.json"}

    # GeoJSON keeps arrays.
    gj = json.loads((target / "projects.geojson").read_text())["features"]
    assert gj[0]["properties"]["project_type"] == ["tidal habitat", "tributary floodplain habitat"]

    # CSV: attributes only, "; "-joined multivalue, empty where the list was empty.
    rows = list(_csv.DictReader((target / "projects.csv").open()))
    assert "geometry" not in rows[0]
    assert [row["project_id"] for row in rows] == ["HRL-001", "HRL-002"]
    assert rows[0]["project_type"] == "tidal habitat; tributary floodplain habitat"
    assert rows[0]["funding_sources"] == "WCB grant; USBR grant"
    assert rows[1]["funding_sources"] == ""

    # GeoPackage: same "; " convention, no unset-list artifacts.
    frame = gpd.read_file(target / "projects.gpkg")
    assert frame.crs.to_epsg() == 3310
    assert frame.sort_values("project_id").iloc[0]["target_species"] == "Chinook salmon"


def test_public_geojson_is_wgs84_lonlat_while_geopackage_keeps_the_working_crs(tmp_path):
    import geopandas as gpd

    from hrl_restoration_pipeline.validation import EXPECTED_WGS84_BOUNDS, PUBLIC_GEOJSON_CRS, positions_outside

    normalized = {"project_stage": ["design"], "lead_entity": ["dwr"], "project_type": ["tidal habitat"], "target_species": ["Chinook salmon"]}
    # EPSG:3310 coordinates near HRL's real footprint.
    footprint = {"type": "Polygon", "coordinates": [[[-127701.0, 28177.2], [-127714.0, 27974.2], [-127919.1, 28009.9], [-127701.0, 28177.2]]]}
    public = publicize(canonicalize([_cli_record("HRL-001", geometry=footprint, **normalized)], manifest()))
    target = publish_local(public, tmp_path / "exports", "2026-08-31", {"schema_version": "v1.3.1"})

    payload = json.loads((target / "projects.geojson").read_text())
    assert payload["crs"]["properties"]["name"] == PUBLIC_GEOJSON_CRS
    lon, lat = payload["features"][0]["geometry"]["coordinates"][0][0]
    assert -122.0 < lon < -121.0 and 38.0 < lat < 39.0
    assert positions_outside(payload["features"][0]["geometry"], EXPECTED_WGS84_BOUNDS) == []

    assert gpd.read_file(target / "projects.gpkg").crs.to_epsg() == 3310
    assert json.loads((target / "metadata.json").read_text())["artifact_crs"] == {"projects.geojson": "EPSG:4326", "projects.gpkg": "EPSG:3310"}


def test_publish_rejects_a_snapshot_that_is_not_lonlat_and_preserves_the_pointer(tmp_path, monkeypatch):
    normalized = {"project_stage": ["design"], "lead_entity": ["dwr"], "project_type": ["tidal habitat"], "target_species": ["Chinook salmon"]}
    public = publicize(canonicalize([_cli_record("HRL-001", geometry={"type": "Point", "coordinates": [-127701.0, 28177.2]}, **normalized)], manifest()))
    root = tmp_path / "exports"
    publish_local(public, root, "good")
    pointer_before = (root / "current.json").read_text()
    # Simulate the projected-meters regression: skip the reprojection step.
    monkeypatch.setattr("hrl_restoration_pipeline.transformation.reproject_geometry", lambda geometry, *_: geometry)
    with pytest.raises(ValueError, match="lon/lat"):
        publish_local(public, root, "bad")
    assert (root / "current.json").read_text() == pointer_before
    assert not (root / "bad").exists()


def test_publication_conditional_pointer_preserves_competing_current_version(tmp_path):
    normalized = {"project_stage": ["design"], "lead_entity": ["dwr"], "project_type": ["tidal habitat"], "target_species": ["Chinook salmon"]}
    public = publicize(canonicalize([_cli_record("HRL-001", geometry={"type": "Point", "coordinates": [100, 200]}, **normalized)], manifest()))
    root = tmp_path / "exports"
    publish_local(public, root, "v1")
    v1_checksum = hashlib.sha256((root / "current.json").read_bytes()).hexdigest()
    publish_local(public, root, "v2", update_pointer=False)
    publish_local(public, root, "v3")
    before = (root / "current.json").read_text()
    with pytest.raises(ValueError, match="changed during promotion"):
        activate_local_snapshot(root, "v2", v1_checksum)
    assert (root / "current.json").read_text() == before


def test_cli_promotion_requires_explicit_matching_approval_and_upserts_local_master(tmp_path):
    submission = tmp_path / "submission"; submission.mkdir()
    _write_cli_submission(submission, ".geojson", _cli_record("HRL-001"))
    candidate = tmp_path / "candidate"
    assert _run_cli(submission, candidate).returncode == 0
    canonical = tmp_path / "standardized" / "canonical-restoration-projects.geojson"; public_root = tmp_path / "public"
    normalized = {"project_stage": ["design"], "lead_entity": ["dwr"], "project_type": ["tidal habitat"], "target_species": ["Chinook salmon"]}
    old = canonicalize([_cli_record("HRL-OLD", project_name="Kept because absent", geometry={"type": "Point", "coordinates": [50, 50]}, **normalized)], manifest())
    canonical.parent.mkdir()
    canonical.write_text(json.dumps(as_feature_collection(old), indent=2, sort_keys=True))
    assert _run_promote(candidate, canonical, public_root, "2026-08-24").returncode == 2
    assert not (public_root / "current.json").exists()
    approval = {"submission_id": "cli-test", "publication_version": "2026-08-24", "approved_by": "local-reviewer", "approved_at": "2026-08-24T20:00:00Z", "candidate_manifest_sha256": hashlib.sha256((candidate / "candidate-manifest.json").read_bytes()).hexdigest()}
    (candidate / "_APPROVE").write_text(json.dumps(approval, sort_keys=True))
    before = {path.relative_to(candidate): hashlib.sha256(path.read_bytes()).hexdigest() for path in candidate.rglob("*") if path.is_file()}
    completed = _run_promote(candidate, canonical, public_root, "2026-08-24")
    assert completed.returncode == 0, completed.stderr
    canonical_records = json.loads(canonical.read_text())["features"]
    assert [feature["properties"]["project_id"] for feature in canonical_records] == ["HRL-001", "HRL-OLD"]
    assert json.loads((public_root / "current.json").read_text())["snapshot_version"] == "2026-08-24"
    metadata = json.loads((public_root / "2026-08-24" / "metadata.json").read_text())
    assert metadata["source_submission_id"] == "cli-test" and metadata["approved_at"] == approval["approved_at"]
    assert metadata["candidate_manifest_sha256"] == approval["candidate_manifest_sha256"]
    audit = json.loads((canonical.parent / "promotion-audits" / "2026-08-24.json").read_text())
    assert audit["submission_id"] == "cli-test"
    assert audit["approved_by"] == approval["approved_by"]
    assert audit["data_steward"]["email"] == "steward@example.org"
    after = {path.relative_to(candidate): hashlib.sha256(path.read_bytes()).hexdigest() for path in candidate.rglob("*") if path.is_file()}
    assert after == before
    (candidate / "public-candidate.geojson").write_text("tampered")
    assert _run_promote(candidate, canonical, public_root, "2026-08-24-r2").returncode == 2
    assert not (public_root / "2026-08-24-r2").exists()


def test_cli_promotion_rejects_repeated_candidate_and_preserves_current_pointer(tmp_path):
    submission = tmp_path / "submission"; submission.mkdir()
    _write_cli_submission(submission, ".geojson")
    candidate = tmp_path / "candidate"
    assert _run_cli(submission, candidate).returncode == 0
    approval = {"submission_id": "cli-test", "publication_version": "2026-08-24", "approved_by": "local-reviewer", "approved_at": "2026-08-24T20:00:00Z", "candidate_manifest_sha256": hashlib.sha256((candidate / "candidate-manifest.json").read_bytes()).hexdigest()}
    (candidate / "_APPROVE").write_text(json.dumps(approval))
    canonical = tmp_path / "standardized" / "canonical-restoration-projects.geojson"; public_root = tmp_path / "public"
    assert _run_promote(candidate, canonical, public_root, "2026-08-24").returncode == 0
    pointer_before = (public_root / "current.json").read_text()
    canonical_before = canonical.read_text()
    repeated = _run_promote(candidate, canonical, public_root, "2026-08-24-r2")
    assert repeated.returncode == 2
    assert "publication_version does not match requested promotion version" in repeated.stderr
    assert (public_root / "current.json").read_text() == pointer_before
    assert canonical.read_text() == canonical_before
    assert not (public_root / "2026-08-24-r2").exists()


def test_cli_promotion_requires_complete_checksum_manifest(tmp_path):
    submission = tmp_path / "submission"; submission.mkdir()
    _write_cli_submission(submission, ".geojson")
    candidate = tmp_path / "candidate"
    assert _run_cli(submission, candidate).returncode == 0
    candidate_manifest = json.loads((candidate / "candidate-manifest.json").read_text())
    del candidate_manifest["artifacts"]["public-candidate.geojson"]
    (candidate / "candidate-manifest.json").write_text(json.dumps(candidate_manifest, sort_keys=True))
    approval = {"submission_id": "cli-test", "publication_version": "2026-08-24", "approved_by": "local-reviewer", "approved_at": "2026-08-24T20:00:00Z", "candidate_manifest_sha256": hashlib.sha256((candidate / "candidate-manifest.json").read_bytes()).hexdigest()}
    (candidate / "_APPROVE").write_text(json.dumps(approval))
    completed = _run_promote(candidate, tmp_path / "canonical-restoration-projects.geojson", tmp_path / "public", "2026-08-24")
    assert completed.returncode == 2
    assert "candidate-manifest.json is incomplete" in completed.stderr
    assert not (tmp_path / "public" / "current.json").exists()


@pytest.mark.parametrize("change", [
    lambda approval: {**approval, "candidate_manifest_sha256": "0" * 64},
    lambda approval: {**approval, "submission_id": "another-submission"},
])
def test_cli_promotion_rejects_mismatched_approval_without_changing_current_pointer(tmp_path, change):
    submission = tmp_path / "submission"; submission.mkdir()
    _write_cli_submission(submission, ".geojson")
    candidate = tmp_path / "candidate"
    assert _run_cli(submission, candidate).returncode == 0
    canonical = tmp_path / "standardized" / "canonical-restoration-projects.geojson"; public_root = tmp_path / "public"
    approval = {"submission_id": "cli-test", "publication_version": "2026-08-24", "approved_by": "local-reviewer", "approved_at": "2026-08-24T20:00:00Z", "candidate_manifest_sha256": hashlib.sha256((candidate / "candidate-manifest.json").read_bytes()).hexdigest()}
    (candidate / "_APPROVE").write_text(json.dumps(approval))
    assert _run_promote(candidate, canonical, public_root, "2026-08-24").returncode == 0
    pointer_before = (public_root / "current.json").read_text()
    (candidate / "_APPROVE").write_text(json.dumps(change(approval)))
    failed = _run_promote(candidate, canonical, public_root, "2026-08-24-r2")
    assert failed.returncode == 2
    assert (public_root / "current.json").read_text() == pointer_before
    assert not (public_root / "2026-08-24-r2").exists()


@pytest.mark.parametrize("approval", ["not json", json.dumps({"submission_id": "other", "publication_version": "2026-08-24", "approved_by": "reviewer", "approved_at": "2026-08-24T20:00:00Z", "candidate_manifest_sha256": "x"}), json.dumps({"submission_id": "cli-test", "publication_version": "2026-08-24", "approved_by": "reviewer", "approved_at": "not-a-date", "candidate_manifest_sha256": "x"})])
def test_cli_promotion_rejects_invalid_approval_markers_without_writing(tmp_path, approval):
    candidate = tmp_path / "candidate"; candidate.mkdir()
    (candidate / "_APPROVE").write_text(approval)
    completed = _run_promote(candidate, tmp_path / "canonical-restoration-projects.geojson", tmp_path / "public", "2026-08-24")
    assert completed.returncode == 2
    assert not (tmp_path / "canonical-restoration-projects.geojson").exists() and not (tmp_path / "public" / "current.json").exists()
