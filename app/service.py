"""Preview planning and atomic publication of daily PM2.5 batches."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .database import connect
from .parsing import CsvFormatError, Observation, ParsedBatch, parse_csv

STATUS_PREVIEW = "preview"
STATUS_PUBLISHED = "published"
STATUS_STALE = "stale"
STATUS_CONFLICT = "conflict"

# Station merges use their own lifecycle.  "merged" is the terminal success
# state and mirrors "published" for batches.
STATUS_MERGED = "merged"
MERGE_PREVIEW = "preview"
MERGE_SOURCE_OLD = "old"
MERGE_SOURCE_NEW = "new"
MERGE_SOURCE_BOTH = "both"


class AppError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details


class PreviewNotFound(AppError):
    status_code = 404
    code = "preview_not_found"


class PreviewStale(AppError):
    status_code = 409
    code = "preview_stale"


class BatchConflict(AppError):
    status_code = 409
    code = "batch_conflict"


class PreviewRejected(AppError):
    status_code = 409
    code = "preview_rejected"


class PreviewNotPublishable(AppError):
    status_code = 400
    code = "preview_not_publishable"


class MergeNotFound(AppError):
    status_code = 404
    code = "merge_not_found"


class MergeStale(AppError):
    status_code = 409
    code = "merge_stale"


class MergeRejected(AppError):
    status_code = 409
    code = "merge_rejected"


class MergeConflict(AppError):
    status_code = 409
    code = "merge_conflict"


class MergeNotConfirmable(AppError):
    status_code = 400
    code = "merge_not_confirmable"


class StationAlreadyMerged(AppError):
    status_code = 409
    code = "station_already_merged"


class StationNotMerged(AppError):
    status_code = 404
    code = "station_not_merged"


@dataclass(frozen=True)
class ExistingRecord:
    revision: int
    value: str

    def as_dict(self) -> dict:
        return {"revision": self.revision, "value": self.value}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _current_version(connection: sqlite3.Connection, date: str) -> int:
    row = connection.execute(
        "SELECT version FROM day_versions WHERE date = ?", (date,)
    ).fetchone()
    return int(row["version"]) if row else 0


def _existing_records(
    connection: sqlite3.Connection, date: str
) -> dict[tuple[str, str], ExistingRecord]:
    """Published records of the day, keyed by their current station identity.

    When two physical rows collapse onto one merged identity, the highest
    revision wins.  Same-revision rows must agree on the value (the merge
    preview refuses anything else), and the current identifier is preferred
    for determinism.
    """
    identity = _identity_map(connection)
    rows = connection.execute(
        """
        SELECT station, hour, revision, value FROM published_records
        WHERE date = ? ORDER BY revision, station
        """,
        (date,),
    ).fetchall()
    records: dict[tuple[str, str], ExistingRecord] = {}
    for row in rows:
        canonical = identity.get(row["station"], row["station"])
        key = (canonical, row["hour"])
        candidate = ExistingRecord(revision=int(row["revision"]), value=row["value"])
        current = records.get(key)
        if current is None or candidate.revision > current.revision:
            records[key] = candidate
        # Same revision for the same merged hour already agreed on the value
        # (the merge preview refuses mismatches), so keep the first.
    return records


# ---------------------------------------------------------------------------
# Station identity / merge
# ---------------------------------------------------------------------------

# Source labels for a merged hourly reading.
SOURCE_OLD = "old"
SOURCE_NEW = "new"
SOURCE_BOTH = "both"


def _identity_map(connection: sqlite3.Connection) -> dict[str, str]:
    """Every merged identifier (including the current one) -> current name."""
    rows = connection.execute(
        "SELECT station, canonical_station FROM station_identities"
    ).fetchall()
    return {row["station"]: row["canonical_station"] for row in rows}


def _member_stations(identity: dict[str, str], canonical: str) -> list[str]:
    return sorted(name for name, current in identity.items() if current == canonical)


def list_stations(db_path: str | Path) -> dict:
    connection = connect(db_path)
    try:
        identity = _identity_map(connection)
        stats = {
            row["station"]: row
            for row in connection.execute(
                """
                SELECT station, MIN(date) AS first_date, MAX(date) AS last_date,
                       COUNT(*) AS hour_count
                FROM published_records GROUP BY station
                """
            ).fetchall()
        }
        available = [
            {
                "station": station,
                "first_date": stats[station]["first_date"],
                "last_date": stats[station]["last_date"],
                "hour_count": int(stats[station]["hour_count"]),
            }
            for station in sorted(stats)
            if station not in identity
        ]

        merges: dict[str, dict] = {}
        rows = connection.execute(
            """
            SELECT station, canonical_station, merge_id, merged_at
            FROM station_identities ORDER BY merged_at, station
            """
        ).fetchall()
        for row in rows:
            group = merges.setdefault(
                row["merge_id"],
                {
                    "merge_id": row["merge_id"],
                    "current_station": row["canonical_station"],
                    "former_stations": [],
                    "merged_at": row["merged_at"],
                },
            )
            if row["station"] != row["canonical_station"]:
                group["former_stations"].append(row["station"])
        ordered = sorted(merges.values(), key=lambda group: group["merged_at"])
        for group in ordered:
            group["former_stations"].sort()
        return {"available": available, "merges": ordered}
    finally:
        connection.close()


def station_detail(db_path: str | Path, station: str) -> dict:
    """Every published row for one identity, under either of its identifiers."""
    connection = connect(db_path)
    try:
        identity = _identity_map(connection)
        canonical = identity.get(station, station)
        members = _member_stations(identity, canonical)
        if canonical not in members:
            members = sorted({station, *members})
        placeholders = ", ".join("?" for _ in members)
        rows = connection.execute(
            f"""
            SELECT date, hour, station, revision, value, batch_id, updated_at,
                   source_station
            FROM published_records WHERE station IN ({placeholders})
            ORDER BY date, hour, station
            """,
            members,
        ).fetchall()
        return {
            "station": station,
            "canonical_station": canonical,
            "merged": canonical != station,
            "stations": sorted({station, canonical, *members}),
            "dates": sorted({row["date"] for row in rows}),
            "records": [
                {
                    "date": row["date"],
                    "hour": row["hour"],
                    "timestamp": f"{row['date']}T{row['hour']}:00",
                    "station": row["station"],
                    "revision": int(row["revision"]),
                    "value": row["value"],
                    "batch_id": row["batch_id"],
                    "updated_at": row["updated_at"],
                    "source_station": row["source_station"],
                }
                for row in rows
            ],
        }
    finally:
        connection.close()


def build_merge_preview(
    old_station: str,
    new_station: str,
    records: list[dict],
) -> dict:
    """Resolve the two identifiers' hourly readings into one reading per hour.

    * Per date/hour the highest revision wins.
    * If the highest revision exists under both identifiers with different
      values the merge is refused, and the conflicting hours are reported.
    * Every value is labelled with the identifier it came from.  Identical
      values under both identifiers are labelled ``both``.
    * Published rows are never changed; this is a read-time projection.
    """
    by_cell: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        by_cell[(record["date"], record["hour"])].append(record)

    cells: list[dict] = []
    conflicts: list[dict] = []
    for (day, hour) in sorted(by_cell):
        rows = by_cell[(day, hour)]
        highest = max(int(row["revision"]) for row in rows)
        top = [row for row in rows if int(row["revision"]) == highest]
        values = sorted({row["value"] for row in top})

        if len(values) > 1:
            conflicts.append(
                {
                    "date": day,
                    "hour": hour,
                    "timestamp": f"{day}T{hour}:00",
                    "revision": highest,
                    "reason": "revision_value_mismatch",
                    "values": values,
                    "stations": sorted({row["station"] for row in top}),
                    "detail": {
                        row["station"]: row["value"] for row in top
                    },
                }
            )
            cells.append(
                {
                    "date": day,
                    "hour": hour,
                    "timestamp": f"{day}T{hour}:00",
                    "revision": highest,
                    "value": None,
                    "source": SOURCE_BOTH,
                    "stations": sorted({row["station"] for row in top}),
                    "conflict": True,
                }
            )
            continue

        # A later submission at the same revision is authoritative.
        winner = max(top, key=lambda row: (row["updated_at"], row["station"]))
        reported = sorted(
            {
                row.get("source_station") or row["station"]
                for row in top
            }
        )
        if len(reported) > 1:
            label, source = "、".join(reported), SOURCE_BOTH
        else:
            label = reported[0]
            source = SOURCE_OLD if label == old_station else SOURCE_NEW
        # ``source_stations`` always reports the actual published identifiers,
        # which may differ from the old/new pair once prior merges happened.
        source_stations = sorted({row["station"] for row in top})
        cells.append(
            {
                "date": day,
                "hour": hour,
                "timestamp": f"{day}T{hour}:00",
                "revision": highest,
                "value": winner["value"],
                "source": source,
                "source_station": label,
                "stations": source_stations,
                "conflict": False,
            }
        )

    counts = {
        "cells": len(cells),
        "from_old": sum(1 for cell in cells if cell["source"] == SOURCE_OLD),
        "from_new": sum(1 for cell in cells if cell["source"] == SOURCE_NEW),
        "from_both": sum(1 for cell in cells if cell["source"] == SOURCE_BOTH),
        "conflicts": len(conflicts),
    }
    return {
        "old_station": old_station,
        "new_station": new_station,
        "current_station": new_station,
        "cells": cells,
        "conflicts": conflicts,
        "dates": sorted({cell["date"] for cell in cells}),
        "counts": counts,
    }


def _merge_guard(
    connection: sqlite3.Connection, old_station: str, new_station: str
) -> dict:
    """Snapshot underpinning a merge preview: daily versions plus row set.

    A later publish touching either identifier changes a date's version, and a
    re-uploaded identical row may leave versions untouched, so the row count
    and maximum update timestamp are recorded too.
    """
    rows = connection.execute(
        """
        SELECT date, COUNT(*) AS row_count, MAX(updated_at) AS last_update
        FROM published_records WHERE station IN (?, ?) GROUP BY date
        """,
        (old_station, new_station),
    ).fetchall()
    return {
        "days": [
            {
                "date": row["date"],
                "day_version": _current_version(connection, row["date"]),
                "row_count": int(row["row_count"]),
                "last_update": row["last_update"],
            }
            for row in rows
        ]
    }


def _public_merge(
    merge_id: str | None,
    plan: dict,
    *,
    status: str,
    created_at: str,
    result: dict | None = None,
) -> dict:
    payload = {
        "merge_id": merge_id,
        "old_station": plan["old_station"],
        "new_station": plan["new_station"],
        "current_station": plan["current_station"],
        "status": status,
        "counts": plan["counts"],
        "cells": plan["cells"],
        "conflicts": plan["conflicts"],
        "dates": plan["dates"],
        "created_at": created_at,
    }
    if result is not None:
        payload["result"] = result
    return payload


def create_merge_preview(
    db_path: str | Path, old_station: str, new_station: str
) -> dict:
    old_station = old_station.strip()
    new_station = new_station.strip()
    if not old_station or not new_station:
        raise AppError("请选择两个站点编号")
    if old_station == new_station:
        raise AppError("旧编号与新编号不能相同")
    if len(old_station) > 120 or len(new_station) > 120:
        raise AppError("站点编号不得超过 120 个字符")

    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            identity = _identity_map(connection)
            retired = sorted(
                station for station in (old_station, new_station) if station in identity
            )
            if retired:
                connection.commit()
                raise StationAlreadyMerged(
                    f"站点编号 {'、'.join(retired)} 已参与归并，不能再次归并",
                    details={
                        "stations": retired,
                        "canonical_station": identity[retired[0]],
                    },
                )

            for station in (old_station, new_station):
                if connection.execute(
                    "SELECT 1 FROM published_records WHERE station = ? LIMIT 1",
                    (station,),
                ).fetchone() is None:
                    connection.commit()
                    raise AppError(f"站点编号 {station} 没有已发布记录，无法归并")

            rows = connection.execute(
                """
                SELECT date, hour, station, revision, value, batch_id, updated_at,
                       source_station
                FROM published_records
                WHERE station IN (?, ?)
                ORDER BY date, hour, station
                """,
                (old_station, new_station),
            ).fetchall()
            records = [
                {
                    "date": row["date"],
                    "hour": row["hour"],
                    "station": row["station"],
                    "revision": int(row["revision"]),
                    "value": row["value"],
                    "batch_id": row["batch_id"],
                    "updated_at": row["updated_at"],
                    "source_station": row["source_station"],
                }
                for row in rows
            ]
            plan = build_merge_preview(old_station, new_station, records)
            created_at = _utc_now()

            if plan["conflicts"]:
                connection.commit()
                raise MergeConflict(
                    "旧、新编号在同一小时的最高修订号数值不一致，归并已拒绝",
                    details=_public_merge(
                        None, plan, status=STATUS_CONFLICT, created_at=created_at
                    ),
                )

            merge_id = uuid.uuid4().hex
            public = _public_merge(
                merge_id, plan, status=MERGE_PREVIEW, created_at=created_at
            )
            connection.execute(
                """
                INSERT INTO merge_previews
                    (id, old_station, new_station, status, preview_json,
                     guard_json, result_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    merge_id,
                    old_station,
                    new_station,
                    MERGE_PREVIEW,
                    json.dumps(public, ensure_ascii=False),
                    json.dumps(
                        _merge_guard(connection, old_station, new_station),
                        ensure_ascii=False,
                    ),
                    created_at,
                    created_at,
                ),
            )
            connection.commit()
            return public
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def _load_merge(connection: sqlite3.Connection, merge_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM merge_previews WHERE id = ?", (merge_id,)
    ).fetchone()
    if row is None:
        raise MergeNotFound("归并预览不存在或已过期，请重新选择站点")
    return row


def _refresh_merge_status(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> sqlite3.Row:
    """Persistently invalidate a merge preview whose snapshot moved on."""
    if row["status"] != MERGE_PREVIEW:
        return row
    guard = json.loads(row["guard_json"])
    current = _merge_guard(connection, row["old_station"], row["new_station"])
    if current != guard:
        connection.execute(
            "UPDATE merge_previews SET status = ?, updated_at = ? WHERE id = ?",
            (STATUS_STALE, _utc_now(), row["id"]),
        )
        connection.commit()
        return _load_merge(connection, row["id"])
    return row


def get_merge(db_path: str | Path, merge_id: str) -> dict:
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _refresh_merge_status(connection, _load_merge(connection, merge_id))
            connection.commit()
            public = json.loads(row["preview_json"])
            public["status"] = row["status"]
            if row["result_json"]:
                public["result"] = json.loads(row["result_json"])
            return public
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def confirm_merge(db_path: str | Path, merge_id: str) -> dict:
    """Record a station identity change once, atomically.

    The guard snapshot (daily versions and row counts) must be unchanged.  A
    batch confirmed after the merge preview was built, or another merge that
    claimed either identifier first, invalidates this one.  Two concurrent
    confirms serialise on BEGIN IMMEDIATE; the first writer wins and the loser
    observes the identity rows / stale guard.
    """
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _refresh_merge_status(connection, _load_merge(connection, merge_id))
            status = row["status"]
            if status == STATUS_MERGED:
                connection.commit()
                return {
                    "merge_id": row["id"],
                    "status": STATUS_MERGED,
                    "already_merged": True,
                    "result": json.loads(row["result_json"]) if row["result_json"] else None,
                }
            if status == STATUS_STALE:
                connection.commit()
                raise MergeStale(
                    "该归并预览已失效，相关日期数据已被更新，请重新选择站点"
                )
            if status == STATUS_CONFLICT:
                connection.commit()
                raise MergeRejected("该站点对存在冲突，归并已被拒绝")
            if status != MERGE_PREVIEW:
                connection.rollback()
                raise MergeNotConfirmable("该归并预览状态不可确认")

            identity = _identity_map(connection)
            for station in (row["old_station"], row["new_station"]):
                if station in identity:
                    connection.rollback()
                    raise StationAlreadyMerged(
                        f"站点编号 {station} 已参与归并，不能再次归并"
                    )

            now = _utc_now()
            for station in (row["old_station"], row["new_station"]):
                connection.execute(
                    """
                    INSERT INTO station_identities
                        (station, canonical_station, merge_id, merged_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (station, row["new_station"], merge_id, now),
                )

            # The merge changes how every date under either identifier is read
            # at its current identity, so previews built before it must not be
            # able to publish against the old projection.  Relevant dates are
            # those with published data under either identifier plus those an
            # open batch preview still intends to write there.
            affected = {
                item["date"]
                for item in json.loads(row["guard_json"]).get("days", [])
            }
            open_previews = connection.execute(
                "SELECT date, plan_json FROM previews WHERE status = ?",
                (STATUS_PREVIEW,),
            ).fetchall()
            for pending in open_previews:
                plan = json.loads(pending["plan_json"])
                targets = {
                    item.get("station") for item in plan.get("writes", [])
                } | {
                    item.get("station") for item in plan.get("records", [])
                }
                if targets & {row["old_station"], row["new_station"]}:
                    affected.add(pending["date"])

            for day in sorted(affected):
                connection.execute(
                    """
                    INSERT INTO day_versions (date, version, updated_at)
                    VALUES (?, 1, ?)
                    ON CONFLICT (date) DO UPDATE SET
                        version = version + 1,
                        updated_at = excluded.updated_at
                    """,
                    (day, now),
                )

            result = {
                "merge_id": merge_id,
                "old_station": row["old_station"],
                "new_station": row["new_station"],
                "current_station": row["new_station"],
                "merged_at": now,
            }
            connection.execute(
                """
                UPDATE merge_previews
                SET status = ?, result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (STATUS_MERGED, json.dumps(result, ensure_ascii=False), now, merge_id),
            )
            connection.commit()
            return {
                "merge_id": merge_id,
                "status": STATUS_MERGED,
                "already_merged": False,
                "result": result,
            }
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def get_merged_day(db_path: str | Path, station: str, day: str) -> dict:
    """Merged readings for one day, under the current identifier.

    The identity mapping and the readings selected by it are read in one
    transaction, so a merge confirmed midway cannot mix two mappings.
    """
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            identity = _identity_map(connection)
            if station not in identity:
                connection.commit()
                raise StationNotMerged(
                    f"站点编号 {station} 未参与归并，请直接按站点查询已发布日数据"
                )
            canonical = identity[station]
            rows = connection.execute(
                """
                SELECT station, hour, revision, value, batch_id, updated_at,
                       source_station
                FROM published_records
                WHERE date = ? AND station IN (
                    SELECT station FROM station_identities
                    WHERE canonical_station = ?
                )
                ORDER BY hour, station
                """,
                (day, canonical),
            ).fetchall()
            plan = build_merge_preview(
                canonical,
                canonical,
                [
                    {
                        "date": day,
                        "hour": row["hour"],
                        "station": row["station"],
                        "revision": int(row["revision"]),
                        "value": row["value"],
                        "batch_id": row["batch_id"],
                        "updated_at": row["updated_at"],
                        "source_station": row["source_station"],
                    }
                    for row in rows
                ],
            )
            records = [
                {
                    "station": canonical,
                    "hour": cell["hour"],
                    "timestamp": cell["timestamp"],
                    "revision": cell["revision"],
                    "value": cell["value"],
                    "source_station": cell.get("source_station"),
                    "sources": cell["stations"],
                }
                for cell in plan["cells"]
                if cell["value"] is not None
            ]
            version = _current_version(connection, day)
            connection.commit()
            return {
                "station": station,
                "current_station": canonical,
                "former_stations": [
                    name
                    for name in _member_stations(identity, canonical)
                    if name != canonical
                ],
                "date": day,
                "day_version": version,
                "count": len(records),
                "records": records,
                "conflicts": plan["conflicts"],
            }
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def _record(
    observation: Observation,
    action: str,
    *,
    reason: str,
    existing: ExistingRecord | None = None,
) -> dict:
    return {
        "station": observation.station,
        "hour": observation.hour,
        "timestamp": observation.timestamp,
        "revision": observation.revision,
        "value": observation.value,
        "action": action,
        "reason": reason,
        "row": observation.row_number,
        "existing": existing.as_dict() if existing else None,
    }


def _conflict(
    *,
    station: str,
    hour: str,
    revision: int,
    reason: str,
    values: list[str],
    rows: list[int],
    existing_value: str | None = None,
) -> dict:
    return {
        "station": station,
        "hour": hour,
        "revision": revision,
        "reason": reason,
        "values": values,
        "rows": rows,
        "existing_value": existing_value,
    }


def build_plan(
    batch: ParsedBatch,
    existing: dict[tuple[str, str], ExistingRecord],
    identity: dict[str, str] | None = None,
) -> dict:
    """Classify every uploaded row and build the set of writes to apply.

    Rules:
      * highest revision per (station, hour) wins;
      * duplicate revisions with different values reject the whole batch;
      * rows never overwrite a higher published revision;
      * a revision equal to the published one is only acceptable with the
        identical value (otherwise it is a conflict).

    ``identity`` maps every merged station identifier to its current name, so
    a supplement filed under the old or the new number is planned against the
    merged station.
    """
    identity = identity or {}
    submitted = {
        observation.row_number: observation.station
        for observation in batch.observations
    }
    batch = ParsedBatch(
        date=batch.date,
        observations=[
            Observation(
                row_number=observation.row_number,
                station=identity.get(observation.station, observation.station),
                date=observation.date,
                hour=observation.hour,
                revision=observation.revision,
                value=observation.value,
            )
            for observation in batch.observations
        ],
    )
    by_key: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for observation in batch.observations:
        by_key[observation.key].append(observation)

    conflicts: list[dict] = []
    records: list[dict] = []
    plan_writes: dict[tuple[str, str], Observation] = {}
    conflict_row_numbers: set[int] = set()

    for key in sorted(by_key):
        rows = sorted(by_key[key], key=lambda item: item.row_number)
        station, hour = key
        published = existing.get(key)

        # 1. Same revision, different values inside the batch: whole batch fails.
        revisions: dict[int, list[Observation]] = defaultdict(list)
        for observation in rows:
            revisions[observation.revision].append(observation)
        for revision, revision_rows in sorted(revisions.items()):
            values = sorted({item.value for item in revision_rows})
            if len(values) > 1:
                involved = [item.row_number for item in revision_rows]
                conflict_row_numbers.update(involved)
                conflicts.append(
                    _conflict(
                        station=station,
                        hour=hour,
                        revision=revision,
                        reason="revision_value_mismatch_in_batch",
                        values=values,
                        rows=involved,
                    )
                )

        # 2. Same revision as the published record with a different value is
        #    equally irreconcilable, even if another row in this batch is newer.
        if published is not None:
            clashing = [
                item for item in rows
                if item.revision == published.revision and item.value != published.value
            ]
            if clashing:
                involved = [item.row_number for item in clashing]
                conflict_row_numbers.update(involved)
                conflicts.append(
                    _conflict(
                        station=station,
                        hour=hour,
                        revision=published.revision,
                        reason="revision_value_mismatch_with_published",
                        values=sorted({item.value for item in clashing}),
                        rows=involved,
                        existing_value=published.value,
                    )
                )

        winner_revision = max(revisions)
        winner_rows = revisions[winner_revision]
        winner = winner_rows[0]

        for observation in rows:
            if observation.row_number in conflict_row_numbers:
                records.append(
                    _record(
                        observation,
                        "conflict",
                        reason="revision_value_mismatch",
                        existing=published,
                    )
                )
                continue

            if observation.revision < winner_revision:
                records.append(
                    _record(
                        observation,
                        "ignore",
                        reason="superseded_in_batch",
                        existing=published,
                    )
                )
                continue

            if observation.row_number != winner.row_number:
                records.append(
                    _record(
                        observation,
                        "ignore",
                        reason="duplicate_in_batch",
                        existing=published,
                    )
                )
                continue

            if published is None:
                plan_writes[key] = observation
                records.append(_record(observation, "add", reason="new"))
            elif winner_revision > published.revision:
                plan_writes[key] = observation
                records.append(
                    _record(observation, "replace", reason="newer_revision",
                            existing=published)
                )
            elif winner_revision == published.revision:  # values are equal here
                records.append(
                    _record(observation, "ignore", reason="already_published",
                            existing=published)
                )
            else:
                records.append(
                    _record(observation, "ignore", reason="older_than_published",
                            existing=published)
                )

    records.sort(key=lambda item: item["row"])

    counts = {"added": 0, "replaced": 0, "ignored": 0, "conflicts": 0}
    for item in records:
        if item["action"] == "add":
            counts["added"] += 1
        elif item["action"] == "replace":
            counts["replaced"] += 1
        elif item["action"] == "ignore":
            counts["ignored"] += 1
        else:
            counts["conflicts"] += 1
    counts["pending"] = 0 if conflicts else counts["added"] + counts["replaced"]
    counts["rows"] = len(records)

    return {
        "records": records,
        "conflicts": conflicts,
        "counts": counts,
        # A rejected batch must never carry executable writes, even though the
        # per-row classification is still shown to explain the decision.
        "writes": [] if conflicts else [
            {
                "station": observation.station,
                "source_station": submitted.get(
                    observation.row_number, observation.station
                ),
                "hour": observation.hour,
                "revision": observation.revision,
                "value": observation.value,
            }
            for _, observation in sorted(plan_writes.items())
        ],
        "has_conflicts": bool(conflicts),
    }


def _public_preview(
    preview_id: str | None,
    batch_date: str,
    version: int,
    plan: dict,
    *,
    status: str,
    created_at: str,
    result: dict | None = None,
) -> dict:
    payload = {
        "preview_id": preview_id,
        "date": batch_date,
        "day_version": version,
        "status": status,
        "counts": plan["counts"],
        "records": plan["records"],
        "conflicts": plan["conflicts"],
        "station_identity": plan.get("station_identity", {}),
        "created_at": created_at,
    }
    if result is not None:
        payload["result"] = result
    return payload


def create_preview(db_path: str | Path, raw: bytes) -> dict:
    batch = parse_csv(raw)

    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            identity = _identity_map(connection)
            submitted = {
                observation.row_number: observation.station
                for observation in batch.observations
            }
            version = _current_version(connection, batch.date)
            existing = _existing_records(connection, batch.date)
            plan = build_plan(batch, existing, identity)
            for record in plan["records"]:
                record["submitted_station"] = submitted.get(
                    record["row"], record["station"]
                )
            plan["station_identity"] = {
                station: identity.get(station, station)
                for station in sorted(set(submitted.values()))
                if identity.get(station, station) != station
            }

            if plan["has_conflicts"]:
                connection.rollback()
                raise BatchConflict(
                    "同站同小时存在修订号相同但数值不一致的记录，整批已拒绝",
                    details=_public_preview(
                        None,
                        batch.date,
                        version,
                        plan,
                        status=STATUS_CONFLICT,
                        created_at=_utc_now(),
                    ),
                )

            preview_id = uuid.uuid4().hex
            created_at = _utc_now()
            public = _public_preview(
                preview_id,
                batch.date,
                version,
                plan,
                status=STATUS_PREVIEW,
                created_at=created_at,
            )
            connection.execute(
                """
                INSERT INTO previews
                    (id, date, day_version, status, preview_json, plan_json,
                     result_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    preview_id,
                    batch.date,
                    version,
                    STATUS_PREVIEW,
                    json.dumps(public, ensure_ascii=False),
                    json.dumps(plan, ensure_ascii=False),
                    created_at,
                    created_at,
                ),
            )
            connection.commit()
            return public
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def _load_preview(connection: sqlite3.Connection, preview_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM previews WHERE id = ?", (preview_id,)
    ).fetchone()
    if row is None:
        raise PreviewNotFound("预览不存在或已过期，请重新上传 CSV")
    return row


def get_preview(db_path: str | Path, preview_id: str) -> dict:
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _load_preview(connection, preview_id)
            # Surface invalidation as soon as it is observed, without ever
            # touching published records.
            if row["status"] == STATUS_PREVIEW:
                current_version = _current_version(connection, row["date"])
                if current_version != int(row["day_version"]):
                    connection.execute(
                        "UPDATE previews SET status = ?, updated_at = ? WHERE id = ?",
                        (STATUS_STALE, _utc_now(), preview_id),
                    )
                    connection.commit()
                    row = _load_preview(connection, preview_id)
            connection.commit()
            public = json.loads(row["preview_json"])
            public["status"] = row["status"]
            if row["result_json"]:
                public["result"] = json.loads(row["result_json"])
            return public
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def confirm_preview(db_path: str | Path, preview_id: str) -> dict:
    """Apply a preview once, atomically, and only if the day is unchanged."""
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _load_preview(connection, preview_id)
            status = row["status"]

            # Repeated confirmation is a no-op and returns the original result.
            if status == STATUS_PUBLISHED:
                connection.commit()
                return {
                    "preview_id": row["id"],
                    "date": row["date"],
                    "status": STATUS_PUBLISHED,
                    "already_published": True,
                    "counts": json.loads(row["preview_json"])["counts"],
                    "result": json.loads(row["result_json"]) if row["result_json"] else None,
                }

            if status == STATUS_STALE:
                connection.commit()
                raise PreviewStale("该预览已失效，该日数据已被更新，请重新上传 CSV")

            if status == STATUS_CONFLICT:
                connection.commit()
                raise PreviewRejected("该批次存在冲突，已被整批拒绝，请修正后重新上传")

            if status != STATUS_PREVIEW:
                connection.rollback()
                raise PreviewNotPublishable("该预览状态不可确认")

            current_version = _current_version(connection, row["date"])
            if current_version != int(row["day_version"]):
                now = _utc_now()
                connection.execute(
                    "UPDATE previews SET status = ?, updated_at = ? WHERE id = ?",
                    (STATUS_STALE, now, preview_id),
                )
                connection.commit()
                raise PreviewStale(
                    "该预览已失效，该日数据已被他人更新，请重新上传 CSV 获取最新预览"
                )

            plan = json.loads(row["plan_json"])
            writes = plan["writes"]
            now = _utc_now()
            batch_id = row["id"]

            # Verify planned writes are still consistent with the published
            # snapshot; the version guard should make this redundant.
            planned_writes = {
                (item["station"], item["hour"]): item for item in writes
            }
            for (station, hour), existing in _existing_records(connection, row["date"]).items():
                planned = planned_writes.get((station, hour))
                if planned and planned["revision"] <= existing.revision:
                    connection.rollback()
                    raise PreviewStale("该预览已失效，所需写入不再适用，请重新上传 CSV")

            for item in writes:
                connection.execute(
                    """
                    INSERT INTO published_records
                        (date, station, hour, revision, value, batch_id,
                         updated_at, source_station)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (date, station, hour) DO UPDATE SET
                        revision = excluded.revision,
                        value = excluded.value,
                        batch_id = excluded.batch_id,
                        updated_at = excluded.updated_at,
                        source_station = excluded.source_station
                    """,
                    (
                        row["date"],
                        item["station"],
                        item["hour"],
                        item["revision"],
                        item["value"],
                        batch_id,
                        now,
                        item.get("source_station", item["station"]),
                    ),
                )

            new_version = current_version
            if writes:
                new_version = current_version + 1
                connection.execute(
                    """
                    INSERT INTO day_versions (date, version, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (date) DO UPDATE SET
                        version = excluded.version,
                        updated_at = excluded.updated_at
                    """,
                    (row["date"], new_version, now),
                )

            result = {
                "date": row["date"],
                "published": len(writes),
                "added": plan["counts"]["added"],
                "replaced": plan["counts"]["replaced"],
                "ignored": plan["counts"]["ignored"],
                "published_at": now,
                "day_version": new_version,
                "batch_id": batch_id,
            }
            public = json.loads(row["preview_json"])
            public["day_version"] = new_version
            public["counts"] = plan["counts"]
            connection.execute(
                """
                UPDATE previews
                SET status = ?, day_version = ?, preview_json = ?,
                    result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    STATUS_PUBLISHED,
                    new_version,
                    json.dumps(public, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    now,
                    preview_id,
                ),
            )
            connection.commit()
            return {
                "preview_id": preview_id,
                "date": row["date"],
                "status": STATUS_PUBLISHED,
                "already_published": False,
                "counts": plan["counts"],
                "result": result,
            }
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()


def get_day(db_path: str | Path, day: str) -> dict:
    connection = connect(db_path)
    try:
        version = _current_version(connection, day)
        rows = connection.execute(
            """
            SELECT station, hour, revision, value, batch_id, updated_at
            FROM published_records
            WHERE date = ?
            ORDER BY station, hour
            """,
            (day,),
        ).fetchall()
        records = [
            {
                "station": row["station"],
                "hour": row["hour"],
                "timestamp": f"{day}T{row['hour']}:00",
                "revision": int(row["revision"]),
                "value": row["value"],
                "batch_id": row["batch_id"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
        return {
            "date": day,
            "day_version": version,
            "count": len(records),
            "records": records,
        }
    finally:
        connection.close()


def list_days(db_path: str | Path) -> dict:
    connection = connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT p.date AS date,
                   COUNT(*) AS record_count,
                   COALESCE(v.version, 0) AS version
            FROM published_records p
            LEFT JOIN day_versions v ON v.date = p.date
            GROUP BY p.date
            ORDER BY p.date DESC
            """
        ).fetchall()
        return {
            "days": [
                {
                    "date": row["date"],
                    "record_count": int(row["record_count"]),
                    "day_version": int(row["version"]),
                }
                for row in rows
            ]
        }
    finally:
        connection.close()


__all__ = [
    "AppError",
    "BatchConflict",
    "CsvFormatError",
    "MergeConflict",
    "MergeNotFound",
    "MergeRejected",
    "MergeStale",
    "PreviewNotFound",
    "PreviewRejected",
    "PreviewStale",
    "StationAlreadyMerged",
    "StationNotMerged",
    "confirm_merge",
    "confirm_preview",
    "create_merge_preview",
    "create_preview",
    "get_day",
    "get_merge",
    "get_merged_day",
    "get_preview",
    "list_days",
    "list_stations",
    "station_detail",
]
