"""End-to-end API tests for preview, publish, and day queries."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "test.db")
    with TestClient(app) as test_client:
        yield test_client


def upload(client, text: str, filename: str = "day.csv"):
    return client.post(
        "/api/previews",
        files={"file": (filename, text.encode("utf-8"), "text/csv")},
    )


def sample_rows(date: str = "2026-11-13") -> str:
    return (
        "station,timestamp,revision,value\n"
        f"SY-001,{date}T00:00:00,1,12.4\n"
        f"SY-001,{date}T01:00:00,1,10.8\n"
        f"SY-001,{date}T01:00:00,2,11.2\n"
        f"SY-001,{date}T01:00:00,2,11.2\n"
        f"SY-002,{date}T00:00:00,3,18.0\n"
        f"SY-002,{date}T01:00:00,1,16.5\n"
    )


def publish(client, text: str) -> dict:
    response = upload(client, text)
    assert response.status_code == 201, response.text
    preview = response.json()
    assert preview["status"] == "preview"
    confirm = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert confirm.status_code == 200, confirm.text
    return preview


def test_health_and_index(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    response = client.get("/")
    assert response.status_code == 200
    assert "小时观测发布台" in response.text


def test_preview_classifies_add_replace_ignore(client):
    first = publish(client, sample_rows())
    result = client.get(f"/api/previews/{first['preview_id']}").json()
    assert result["status"] == "published"

    updated = (
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,2,12.9\n"
        "SY-001,2026-11-13T01:00:00,3,11.5\n"
        "SY-002,2026-11-13T00:00:00,3,18.0\n"
        "SY-002,2026-11-13T01:00:00,0,16.5\n"
    )
    response = upload(client, updated)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["counts"] == {
        "added": 0,
        "replaced": 2,
        "ignored": 2,
        "conflicts": 0,
        "pending": 2,
        "rows": 4,
    }
    actions = {(r["station"], r["hour"], r["revision"]): r["action"] for r in data["records"]}
    assert actions[("SY-001", "00:00", 2)] == "replace"
    assert actions[("SY-001", "01:00", 3)] == "replace"
    assert actions[("SY-002", "00:00", 3)] == "ignore"
    assert actions[("SY-002", "01:00", 0)] == "ignore"
    reasons = {r["station"] + r["hour"]: r["reason"] for r in data["records"]}
    assert reasons["SY-00200:00"] == "already_published"
    assert reasons["SY-00201:00"] == "older_than_published"

    confirm = client.post(f"/api/previews/{data['preview_id']}/confirm")
    assert confirm.status_code == 200
    assert confirm.json()["result"]["replaced"] == 2

    day = client.get("/api/days/2026-11-13").json()
    values = {(r["station"], r["hour"]): (r["revision"], r["value"]) for r in day["records"]}
    assert values[("SY-001", "00:00")] == (2, "12.9")
    assert values[("SY-001", "01:00")] == (3, "11.5")
    assert values[("SY-002", "00:00")] == (3, "18")
    assert values[("SY-002", "01:00")] == (1, "16.5")
    assert day["day_version"] == 2
    assert day["count"] == 4


def test_highest_revision_wins_and_repeats_ignored(client):
    preview = publish(client, sample_rows())
    day = client.get("/api/days/2026-11-13").json()
    assert day["count"] == 4
    by_hour = {(r["station"], r["hour"]): r for r in day["records"]}
    assert by_hour[("SY-001", "01:00")]["revision"] == 2
    assert by_hour[("SY-001", "01:00")]["value"] == "11.2"

    result = client.get(f"/api/previews/{preview['preview_id']}").json()
    ignored = [r for r in result["records"] if r["action"] == "ignore"]
    assert any(r["reason"] == "superseded_in_batch" for r in ignored)
    assert any(r["reason"] == "duplicate_in_batch" for r in ignored)


def test_conflict_rejects_entire_batch_without_changing_data(client):
    publish(client, sample_rows())

    conflicting = (
        "station,timestamp,revision,value\n"
        "SY-003,2026-11-13T02:00:00,1,25.0\n"
        "SY-001,2026-11-13T01:00:00,2,11.9\n"
        "SY-001,2026-11-13T01:00:00,2,12.3\n"
    )
    response = upload(client, conflicting)
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "batch_conflict"
    detail = body["detail"]
    assert detail["status"] == "conflict"
    assert detail["preview_id"] is None
    assert detail["counts"]["conflicts"] == 2
    assert detail["counts"]["pending"] == 0
    assert detail["counts"]["added"] == 1  # classified, but never written
    assert detail["conflicts"][0]["revision"] == 2
    assert set(detail["conflicts"][0]["values"]) == {"11.9", "12.3"}

    day = client.get("/api/days/2026-11-13").json()
    assert day["count"] == 4
    assert all(r["station"] != "SY-003" for r in day["records"])
    assert day["day_version"] == 1


def test_same_revision_as_published_with_different_value_conflicts(client):
    publish(client, sample_rows())
    response = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-002,2026-11-13T00:00:00,3,18.5\n",
    )
    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"][0]["reason"] == (
        "revision_value_mismatch_with_published"
    )
    day = client.get("/api/days/2026-11-13").json()
    assert day["count"] == 4
    assert next(r for r in day["records"] if r["station"] == "SY-002")["value"] == "18"


def test_format_errors_never_create_preview_or_touch_published_data(client):
    publish(client, sample_rows())

    bad_cases = [
        ("missing column", "station,timestamp,revision\nSY-001,2026-11-13T00:00:00,1\n"),
        ("bad hour", "station,timestamp,revision,value\nSY-001,2026-11-13T00:30:00,1,9\n"),
        ("bad value", "station,timestamp,revision,value\nSY-001,2026-11-13T00:00:00,1,abc\n"),
        ("negative value", "station,timestamp,revision,value\nSY-001,2026-11-13T00:00:00,1,-1\n"),
        ("bad revision", "station,timestamp,revision,value\nSY-001,2026-11-13T00:00:00,1.5,9\n"),
        ("two days", "station,timestamp,revision,value\n"
                     "SY-001,2026-11-13T00:00:00,1,9\n"
                     "SY-001,2026-11-14T00:00:00,1,9\n"),
        ("empty file", ""),
        ("header only", "station,timestamp,revision,value\n"),
    ]
    for label, text in bad_cases:
        response = upload(client, text)
        assert response.status_code == 400, (label, response.text)
        body = response.json()
        assert body["error"] == "csv_format_error", label
        assert body["detail"]["errors"], label
        assert body["detail"]["errors"][0]["message"], label

    day = client.get("/api/days/2026-11-13").json()
    assert day["count"] == 4
    assert day["day_version"] == 1
    assert client.get("/api/days/2026-11-14").json()["count"] == 0


def test_stale_preview_after_someone_else_publishes(client):
    publish(client, sample_rows())

    ours = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,2,12.9\n",
    )
    assert ours.status_code == 201
    our_preview_id = ours.json()["preview_id"]

    other = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-002,2026-11-13T05:00:00,1,8.0\n",
    )
    assert other.status_code == 201
    other_id = other.json()["preview_id"]
    assert client.post(f"/api/previews/{other_id}/confirm").status_code == 200

    confirm = client.post(f"/api/previews/{our_preview_id}/confirm")
    assert confirm.status_code == 409
    assert confirm.json()["error"] == "preview_stale"

    day = client.get("/api/days/2026-11-13").json()
    values = {(r["station"], r["hour"]): r["value"] for r in day["records"]}
    assert values[("SY-001", "00:00")] == "12.4"  # our stale write never applied
    assert values[("SY-002", "05:00")] == "8"
    assert day["day_version"] == 2

    # A stale preview stays stale on later attempts.
    again = client.post(f"/api/previews/{our_preview_id}/confirm")
    assert again.status_code == 409
    assert client.get(f"/api/previews/{our_preview_id}").json()["status"] == "stale"

    # Re-uploading now yields a fresh preview that can be published.
    retry = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,2,12.9\n",
    )
    assert retry.status_code == 201
    retry_id = retry.json()["preview_id"]
    assert client.post(f"/api/previews/{retry_id}/confirm").status_code == 200
    assert client.get("/api/days/2026-11-13").json()["day_version"] == 3


def test_repeated_confirm_does_not_republish(client):
    publish(client, sample_rows())
    preview = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-003,2026-11-13T03:00:00,1,21.0\n",
    ).json()

    first = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert first.status_code == 200
    assert first.json()["result"]["published"] == 1

    second = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert second.status_code == 200
    assert second.json()["already_published"] is True
    assert second.json()["result"] == first.json()["result"]

    day = client.get("/api/days/2026-11-13").json()
    assert day["count"] == 5
    assert day["day_version"] == 2


def test_unknown_preview_is_404(client):
    response = client.post("/api/previews/does-not-exist/confirm")
    assert response.status_code == 404
    assert response.json()["error"] == "preview_not_found"
    assert client.get("/api/previews/does-not-exist").status_code == 404


def test_day_query_validation_and_empty_day(client):
    assert client.get("/api/days/2026-11-13").json() == {
        "date": "2026-11-13", "day_version": 0, "count": 0, "records": [],
    }
    assert client.get("/api/days/2026-13-13").status_code == 400
    assert client.get("/api/days/not-a-date").status_code == 400
    download = client.get("/api/days/2026-11-13.json")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]


def test_reuploaded_identical_rows_are_ignored_and_create_no_version(client):
    publish(client, sample_rows())
    response = upload(client, sample_rows())
    assert response.status_code == 201
    data = response.json()
    assert data["counts"]["pending"] == 0
    assert data["counts"]["ignored"] == 6
    assert data["day_version"] == 1


def test_value_canonicalization(client):
    publish(client, sample_rows())
    response = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,2,12.40\n",
    )
    assert response.status_code == 201
    assert response.json()["counts"]["replaced"] == 1
    assert client.post(
        f"/api/previews/{response.json()['preview_id']}/confirm"
    ).status_code == 200
    day = client.get("/api/days/2026-11-13").json()
    record = next(r for r in day["records"] if (r["station"], r["hour"]) == ("SY-001", "00:00"))
    assert record["value"] == "12.4"


def test_publish_failure_rolls_back_all_writes(client, monkeypatch):
    publish(client, sample_rows())
    preview = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,5,99\n"
        "SY-002,2026-11-13T06:00:00,1,7\n",
    ).json()

    import app.service as service

    original = service._current_version

    def fail_mid_transaction(connection, date):
        # Simulate a storage failure after the first write has been staged.
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(service, "_current_version", fail_mid_transaction)
    response = client.post(f"/api/previews/{preview['preview_id']}/confirm")
    assert response.status_code == 500
    assert response.json()["error"] == "storage_error"
    monkeypatch.setattr(service, "_current_version", original)

    day = client.get("/api/days/2026-11-13").json()
    assert day["day_version"] == 1
    assert day["count"] == 4
    assert client.get(f"/api/previews/{preview['preview_id']}").json()["status"] == "preview"

    # The same preview can still be published once storage recovers.
    assert client.post(f"/api/previews/{preview['preview_id']}/confirm").status_code == 200


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-11-13T00:00",
        "2026-11-13 00:00:00Z",
        "2026-11-13T00:00:00.000",
        "2026-11-13T00:00:00+00:00",
    ],
)
def test_supported_timestamp_variants(client, timestamp):
    response = upload(
        client,
        f"station,timestamp,revision,value\nSY-001,{timestamp},1,9.5\n",
    )
    assert response.status_code == 201, response.text
    assert response.json()["date"] == "2026-11-13"
    assert response.json()["records"][0]["timestamp"] == "2026-11-13T00:00:00"


def test_date_column_with_time_only_cells(client):
    response = upload(
        client,
        "station,date,time,revision,value\n"
        "SY-001,2026-11-13,00:00,1,9.5\n"
        "SY-001,2026-11-13,05:00,1,10.5\n",
    )
    assert response.status_code == 201, response.text
    assert response.json()["counts"]["added"] == 2


def test_chinese_headers_semicolon_and_gb18030(client):
    text = (
        "站点;整点时间;修订号;数值\n"
        "SY-001;2026-11-13T00:00:00;1;9.5\n"
        "SY-002;2026-11-13T01:00:00;2;11\n"
    )
    response = client.post(
        "/api/previews",
        files={"file": ("day.csv", text.encode("gb18030"), "text/csv")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["counts"]["added"] == 2


def test_row_level_errors_are_all_reported(client):
    response = upload(
        client,
        "station,timestamp,revision,value\n"
        ",2026-11-13T00:00:00,1,9.5\n"
        "SY-001,2026-11-13T01:00:00,,9.5\n"
        "SY-002,2026-11-13T02:00:00,1,notanumber\n",
    )
    assert response.status_code == 400
    errors = response.json()["detail"]["errors"]
    assert response.json()["detail"]["error_count"] == 3
    codes = {item["code"] for item in errors}
    assert codes == {"empty_station", "empty_revision", "invalid_value"}


def test_tabs_and_ignore_of_identical_revision_inside_batch(client):
    response = upload(
        client,
        "station,timestamp,revision,value\n"
        "SY-001,2026-11-13T00:00:00,2,9.5\n"
        "SY-001,2026-11-13T00:00:00,2,9.50\n"
        "SY-001,2026-11-13T00:00:00,1,9.0\n",
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["counts"] == {
        "added": 1, "replaced": 0, "ignored": 2, "conflicts": 0,
        "pending": 1, "rows": 3,
    }
    reasons = [record["reason"] for record in data["records"]]
    # 9.50 canonicalises to 9.5, so the second row is a pure duplicate.
    assert reasons == ["new", "duplicate_in_batch", "superseded_in_batch"]


def test_two_previews_for_same_day_cannot_both_commit(client):
    from concurrent.futures import ThreadPoolExecutor

    publish(client, sample_rows())
    first = upload(
        client,
        "station,timestamp,revision,value\nSY-001,2026-11-13T00:00:00,2,12.9\n",
    ).json()
    second = upload(
        client,
        "station,timestamp,revision,value\nSY-002,2026-11-13T00:00:00,4,18.5\n",
    ).json()

    def confirm(preview_id):
        response = client.post(f"/api/previews/{preview_id}/confirm")
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(confirm, [first["preview_id"], second["preview_id"]]))

    assert sorted(statuses) == [200, 409]
    day = client.get("/api/days/2026-11-13").json()
    assert day["day_version"] == 2  # exactly one batch was applied
    applied = [
        sid for sid, status in zip(
            [first["preview_id"], second["preview_id"]], statuses
        ) if status == 200
    ]
    assert client.get(f"/api/previews/{applied[0]}").json()["status"] == "published"


# ---------------------------------------------------------------------------
# Station renumbering / merge
# ---------------------------------------------------------------------------


def merge(client, old: str, new: str):
    return client.post(
        "/api/merges", json={"old_station": old, "new_station": new}
    )


def confirm_merge(client, merge_id: str):
    return client.post(f"/api/merges/{merge_id}/confirm")


def publish_station(client, station: str, rows: list[tuple[str, int, str]]) -> None:
    """rows = [(hour, revision, value), ...] all on 2026-11-13."""
    body = "station,timestamp,revision,value\n" + "".join(
        f"{station},2026-11-13T{hour}:00,{revision},{value}\n"
        for hour, revision, value in rows
    )
    publish(client, body)


def test_merge_preview_keeps_higher_revision_per_hour(client):
    publish_station(client, "OLD-1", [("00:00", 1, "10"), ("01:00", 2, "11.5")])
    publish_station(client, "NEW-1", [("00:00", 3, "12.5"), ("02:00", 1, "9")])

    response = merge(client, "OLD-1", "NEW-1")
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "preview"
    assert data["current_station"] == "NEW-1"
    assert data["counts"] == {
        "cells": 3, "from_old": 1, "from_new": 2,
        "from_both": 0, "conflicts": 0,
    }
    by_hour = {cell["hour"]: cell for cell in data["cells"]}
    assert by_hour["00:00"]["value"] == "12.5"
    assert by_hour["00:00"]["revision"] == 3
    assert by_hour["01:00"]["value"] == "11.5"
    assert by_hour["02:00"]["value"] == "9"
    assert data["dates"] == ["2026-11-13"]

    confirmed = confirm_merge(client, data["merge_id"])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "merged"
    assert confirmed.json()["already_merged"] is False

    day = client.get("/api/merged/NEW-1/days/2026-11-13")
    assert day.status_code == 200, day.text
    merged = day.json()
    assert merged["count"] == 3
    # Two publishes (v1, v2) plus one merge invalidation (v3).
    assert merged["day_version"] == 3
    values = {record["hour"]: record["value"] for record in merged["records"]}
    assert values == {"00:00": "12.5", "01:00": "11.5", "02:00": "9"}
    # Per-hour origin is retained even though the reading is under NEW-1.
    sources = {record["hour"]: record["sources"] for record in merged["records"]}
    assert sources["01:00"] == ["OLD-1"]
    assert sources["00:00"] == ["NEW-1"]

    # Original published records are untouched.
    original = client.get("/api/days/2026-11-13").json()
    originals = {(r["station"], r["hour"]): r["value"] for r in original["records"]}
    assert originals[("OLD-1", "01:00")] == "11.5"
    assert originals[("NEW-1", "00:00")] == "12.5"


def test_merge_refuses_same_revision_different_value_and_names_hours(client):
    publish_station(client, "OLD-2", [("00:00", 4, "10"), ("03:00", 1, "8")])
    publish_station(client, "NEW-2", [("00:00", 4, "10"), ("03:00", 2, "8.5")])

    # 00:00 agrees (same revision and value); 03:00 has different revisions.
    # Add a genuine clash at 04:00 with equal revision, different value.
    publish_station(client, "OLD-2", [("04:00", 5, "20")])
    publish_station(client, "NEW-2", [("04:00", 5, "21")])

    response = merge(client, "OLD-2", "NEW-2")
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"] == "merge_conflict"
    detail = body["detail"]
    assert detail["status"] == "conflict"
    assert detail["merge_id"] is None
    assert detail["counts"]["conflicts"] == 1
    conflict = detail["conflicts"][0]
    assert conflict["hour"] == "04:00"
    assert conflict["revision"] == 5
    assert set(conflict["values"]) == {"20", "21"}
    assert conflict["detail"] == {"OLD-2": "20", "NEW-2": "21"}

    # Nothing was linked; each station still queries on its own.
    assert merge(client, "OLD-2", "NEW-2").status_code == 409
    assert client.get("/api/merged/NEW-2/days/2026-11-13").status_code == 404
    day = client.get("/api/days/2026-11-13").json()
    assert day["count"] == 6  # all original rows still there


def test_merge_rejects_same_or_already_merged_station(client):
    # Distinct hours so no same-revision conflict between the identifiers.
    publish_station(client, "A", [("00:00", 1, "5")])
    publish_station(client, "B", [("01:00", 1, "6")])
    publish_station(client, "C", [("02:00", 1, "7")])

    assert merge(client, "A", "A").status_code == 400

    first = merge(client, "A", "B")
    assert first.status_code == 201
    assert confirm_merge(client, first.json()["merge_id"]).status_code == 200

    again = merge(client, "A", "C")
    assert again.status_code == 409
    assert again.json()["error"] == "station_already_merged"
    assert again.json()["detail"]["stations"] == ["A"]

    # The current identifier is also a merged identity.
    current = merge(client, "B", "C")
    assert current.status_code == 409
    assert current.json()["detail"]["stations"] == ["B"]

    stations = client.get("/api/stations").json()
    assert {item["station"] for item in stations["available"]} == {"C"}
    assert stations["merges"][0]["current_station"] == "B"
    assert stations["merges"][0]["former_stations"] == ["A"]


def test_supplement_under_either_identifier_previews_and_publishes_merged(client):
    publish_station(client, "OLD-3", [("00:00", 1, "10")])
    publish_station(client, "NEW-3", [("01:00", 1, "11")])
    created = merge(client, "OLD-3", "NEW-3").json()
    assert confirm_merge(client, created["merge_id"]).status_code == 200

    # A supplement still filed under the retired number is planned as NEW-3.
    preview = upload(
        client,
        "station,timestamp,revision,value\n"
        "OLD-3,2026-11-13T02:00:00,1,12\n",
    )
    assert preview.status_code == 201, preview.text
    data = preview.json()
    assert data["station_identity"] == {"OLD-3": "NEW-3"}
    assert data["records"][0]["station"] == "NEW-3"
    assert data["records"][0]["submitted_station"] == "OLD-3"
    assert data["counts"]["added"] == 1
    assert client.post(
        f"/api/previews/{data['preview_id']}/confirm"
    ).status_code == 200

    merged = client.get("/api/merged/OLD-3/days/2026-11-13").json()
    assert merged["count"] == 3
    assert merged["current_station"] == "NEW-3"
    assert merged["former_stations"] == ["OLD-3"]
    # v1, v2 from the two publishes, v3 from the merge, v4 from the supplement.
    assert merged["day_version"] == 4
    # The supplement was stored under the current identity but its origin is
    # still reported as the identifier the analyst actually submitted.
    supplement = next(r for r in merged["records"] if r["hour"] == "02:00")
    assert supplement["station"] == "NEW-3"
    assert supplement["source_station"] == "OLD-3"


def test_supplement_under_new_identifier_also_merges(client):
    publish_station(client, "OLD-4", [("00:00", 1, "10")])
    publish_station(client, "NEW-4", [("01:00", 1, "11")])
    confirmed = merge(client, "OLD-4", "NEW-4").json()
    assert confirm_merge(client, confirmed["merge_id"]).status_code == 200

    preview = upload(
        client,
        "station,timestamp,revision,value\n"
        "NEW-4,2026-11-13T01:00:00,2,11.5\n",
    ).json()
    assert preview["records"][0]["action"] == "replace"
    assert client.post(
        f"/api/previews/{preview['preview_id']}/confirm"
    ).status_code == 200

    merged = client.get("/api/merged/NEW-4/days/2026-11-13").json()
    assert merged["count"] == 2
    values = {r["hour"]: r["value"] for r in merged["records"]}
    assert values == {"00:00": "10", "01:00": "11.5"}


def test_preview_built_before_merge_cannot_publish_after_it(client):
    publish_station(client, "OLD-11", [("00:00", 1, "10")])
    publish_station(client, "NEW-11", [("01:00", 1, "11")])

    pending = upload(
        client,
        "station,timestamp,revision,value\n"
        "OLD-11,2026-11-13T02:00:00,1,12\n",
    ).json()
    merge_preview = merge(client, "OLD-11", "NEW-11").json()
    assert confirm_merge(client, merge_preview["merge_id"]).status_code == 200

    assert client.post(
        f"/api/previews/{pending['preview_id']}/confirm"
    ).status_code == 409
    merged = client.get("/api/merged/NEW-11/days/2026-11-13").json()
    # The pre-merge supplement never landed in the merged projection.
    assert [r["hour"] for r in merged["records"]] == ["00:00", "01:00"]

    # Re-previewing now plans the supplement against the merged identity.
    retry = upload(
        client,
        "station,timestamp,revision,value\n"
        "OLD-11,2026-11-13T02:00:00,1,12\n",
    ).json()
    assert retry["station_identity"] == {"OLD-11": "NEW-11"}
    assert client.post(
        f"/api/previews/{retry['preview_id']}/confirm"
    ).status_code == 200
    merged = client.get("/api/merged/NEW-11/days/2026-11-13").json()
    assert [r["hour"] for r in merged["records"]] == ["00:00", "01:00", "02:00"]


def test_merge_invalidates_open_previews_of_affected_dates(client):
    publish_station(client, "OLD-5", [("00:00", 1, "10")])
    publish_station(client, "NEW-5", [("01:00", 1, "11")])

    pending = upload(
        client,
        "station,timestamp,revision,value\n"
        "OLD-5,2026-11-13T05:00:00,1,15\n",
    ).json()
    pending_id = pending["preview_id"]
    assert pending["status"] == "preview"

    merge_preview = merge(client, "OLD-5", "NEW-5").json()
    assert confirm_merge(client, merge_preview["merge_id"]).status_code == 200

    stale = client.post(f"/api/previews/{pending_id}/confirm")
    assert stale.status_code == 409
    assert stale.json()["error"] == "preview_stale"
    assert client.get(f"/api/previews/{pending_id}").json()["status"] == "stale"

    merged = client.get("/api/merged/NEW-5/days/2026-11-13").json()
    assert [r["hour"] for r in merged["records"]] == ["00:00", "01:00"]


def test_merge_invalidated_by_supplement_published_after_preview(client):
    publish_station(client, "OLD-6", [("00:00", 1, "10")])
    publish_station(client, "NEW-6", [("01:00", 1, "11")])

    merge_preview = merge(client, "OLD-6", "NEW-6").json()
    merge_id = merge_preview["merge_id"]

    supplement = upload(
        client,
        "station,timestamp,revision,value\n"
        "NEW-6,2026-11-13T01:00:00,2,11.5\n",
    ).json()
    assert client.post(
        f"/api/previews/{supplement['preview_id']}/confirm"
    ).status_code == 200

    stale = confirm_merge(client, merge_id)
    assert stale.status_code == 409
    assert stale.json()["error"] == "merge_stale"
    assert client.get(f"/api/merges/{merge_id}").json()["status"] == "stale"

    # Re-previewing after the supplement gives the merged projection.
    fresh = merge(client, "OLD-6", "NEW-6").json()
    assert confirm_merge(client, fresh["merge_id"]).status_code == 200
    merged = client.get("/api/merged/NEW-6/days/2026-11-13").json()
    values = {r["hour"]: r["value"] for r in merged["records"]}
    assert values == {"00:00": "10", "01:00": "11.5"}


def test_repeated_merge_confirm_is_idempotent(client):
    publish_station(client, "OLD-7", [("00:00", 1, "10")])
    publish_station(client, "NEW-7", [("01:00", 1, "11")])
    merge_id = merge(client, "OLD-7", "NEW-7").json()["merge_id"]

    first = confirm_merge(client, merge_id)
    assert first.status_code == 200
    assert first.json()["already_merged"] is False
    second = confirm_merge(client, merge_id)
    assert second.status_code == 200
    assert second.json()["already_merged"] is True
    assert second.json()["result"] == first.json()["result"]

    # The merge advanced each affected date exactly once.
    merged = client.get("/api/merged/NEW-7/days/2026-11-13").json()
    assert merged["day_version"] == 3
    assert len(client.get("/api/stations").json()["merges"]) == 1


def test_merge_requires_both_identifiers_to_have_records(client):
    publish_station(client, "ONLY", [("00:00", 1, "10")])
    response = merge(client, "ONLY", "MISSING")
    assert response.status_code == 400
    assert "MISSING" in response.json()["message"]


def test_supplement_after_merge_competes_by_revision_with_old_rows(client):
    publish_station(client, "OLD-12", [("00:00", 5, "10")])
    publish_station(client, "NEW-12", [("01:00", 1, "11")])
    confirmed = merge(client, "OLD-12", "NEW-12").json()
    assert confirm_merge(client, confirmed["merge_id"]).status_code == 200

    # A lower supplement under the retired number is ignored, not applied.
    low = upload(
        client,
        "station,timestamp,revision,value\n"
        "OLD-12,2026-11-13T00:00:00,4,9.5\n",
    ).json()
    assert low["records"][0]["action"] == "ignore"
    assert low["records"][0]["reason"] == "older_than_published"
    assert low["counts"]["pending"] == 0

    # A higher supplement replaces the merged reading.
    high = upload(
        client,
        "station,timestamp,revision,value\n"
        "OLD-12,2026-11-13T00:00:00,6,10.5\n",
    ).json()
    assert high["records"][0]["action"] == "replace"
    assert client.post(
        f"/api/previews/{high['preview_id']}/confirm"
    ).status_code == 200

    merged = client.get("/api/merged/NEW-12/days/2026-11-13").json()
    by_hour = {r["hour"]: r for r in merged["records"]}
    assert by_hour["00:00"]["value"] == "10.5"
    assert by_hour["00:00"]["revision"] == 6
    assert by_hour["00:00"]["source_station"] == "OLD-12"


def test_concurrent_merges_on_overlapping_stations_serialise(client):
    from concurrent.futures import ThreadPoolExecutor

    for name in ("S-A", "S-B", "S-C"):
        publish_station(client, name, [("00:00", 1, "10")])

    first = merge(client, "S-A", "S-B")
    second = merge(client, "S-B", "S-C")
    assert first.status_code == 201 and second.status_code == 201

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(
            pool.map(
                confirm_merge,
                [client, client],
                [first.json()["merge_id"], second.json()["merge_id"]],
            )
        )
    codes = sorted(response.status_code for response in statuses)
    assert codes == [200, 409], [r.text for r in statuses]

    merges = client.get("/api/stations").json()["merges"]
    assert len(merges) == 1
    current = merges[0]["current_station"]
    former = merges[0]["former_stations"]
    # Exactly one of the overlapping pairs won, and it is fully linked.
    assert sorted({current, *former}) in (["S-A", "S-B"], ["S-B", "S-C"])


def test_concurrent_confirms_of_same_merge_give_one_identity(client):
    from concurrent.futures import ThreadPoolExecutor

    publish_station(client, "OLD-8", [("00:00", 1, "10")])
    publish_station(client, "NEW-8", [("01:00", 1, "11")])
    merge_id = merge(client, "OLD-8", "NEW-8").json()["merge_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: confirm_merge(client, merge_id), range(4))
        )
    assert all(response.status_code == 200 for response in responses)
    created = [r.json() for r in responses if r.json()["already_merged"] is False]
    assert len(created) == 1
    assert all(r.json()["result"] == created[0]["result"] for r in responses)

    merged = client.get("/api/merged/NEW-8/days/2026-11-13").json()
    assert merged["day_version"] == 3


def test_merged_day_unknown_or_unmerged_station(client):
    assert client.get("/api/merged/OLD-9/days/2026-11-13").status_code == 404
    assert client.get("/api/merged/OLD-9/days/2026-13-01").status_code == 400


def test_merged_day_mapping_and_readings_share_one_snapshot(client):
    publish_station(client, "OLD-10", [("00:00", 1, "10")])
    publish_station(client, "NEW-10", [("01:00", 1, "11")])
    merge_id = merge(client, "OLD-10", "NEW-10").json()["merge_id"]
    assert confirm_merge(client, merge_id).status_code == 200

    # Any identifier converges on the same current identity and readings.
    for station in ("OLD-10", "NEW-10"):
        response = client.get(f"/api/merged/{station}/days/2026-11-13")
        assert response.status_code == 200
        data = response.json()
        assert data["current_station"] == "NEW-10"
        assert [r["hour"] for r in data["records"]] == ["00:00", "01:00"]

    station_view = client.get("/api/stations/OLD-10").json()
    assert station_view["canonical_station"] == "NEW-10"
    assert station_view["stations"] == ["NEW-10", "OLD-10"]
    assert {(r["station"], r["hour"]) for r in station_view["records"]} == {
        ("OLD-10", "00:00"), ("NEW-10", "01:00"),
    }

    download = client.get("/api/merged/NEW-10/days/2026-11-13.json")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]
