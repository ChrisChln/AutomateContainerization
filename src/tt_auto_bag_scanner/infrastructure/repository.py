"""Transactional SQLite persistence for tasks, attempts, and audit history."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    DeviceMode,
    ItemStatus,
    PhysicalCheck,
    PhysicalCheckMethod,
    QueueItem,
    RecoveryResult,
    ScanOutcome,
    ScanResult,
    TaskCounts,
    TaskSnapshot,
    TaskState,
    VerifiedSession,
)
from tt_auto_bag_scanner.domain.states import StateTransitionError, transition_task


class RepositoryError(RuntimeError):
    """Raised when a persistence operation cannot preserve scanner invariants."""


class TaskRepository:
    SCHEMA_VERSION = 1
    _MIGRATION_FILE = "001_initial.sql"

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._connection_lock = threading.Lock()
        self._connections: set[sqlite3.Connection] = set()
        self._initialized = False
        self._migrate()
        self._initialized = True

    @property
    def journal_mode(self) -> str:
        return str(self._connection().execute("PRAGMA journal_mode").fetchone()[0])

    def _migration_sql(self) -> str:
        candidates = [
            Path(__file__).resolve().parents[3] / "migrations" / self._MIGRATION_FILE,
            Path(getattr(sys, "_MEIPASS", "")) / "migrations" / self._MIGRATION_FILE,
            Path(sys.executable).resolve().parent / "migrations" / self._MIGRATION_FILE,
        ]
        for path in candidates:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        raise RepositoryError("database migration file is missing from the application package")

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if self._initialized:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        with self._connection_lock:
            self._connections.add(connection)
        return connection

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._new_connection()
            self._local.connection = connection
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            connection = self._connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _migrate(self) -> None:
        connection = self._connection()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > self.SCHEMA_VERSION:
            raise RepositoryError(
                f"database schema version {version} is newer than this application"
            )
        if version == self.SCHEMA_VERSION:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            return
        has_schema = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if version == 0 and has_schema is not None:
            backup_path = self.path.with_name(
                f"{self.path.stem}.pre-migration-{_utc_now():%Y%m%dT%H%M%SZ}.sqlite3"
            )
            if backup_path.exists():
                backup_path = backup_path.with_name(
                    f"{backup_path.stem}-{uuid.uuid4().hex[:8]}{backup_path.suffix}"
                )
            self.backup_to(backup_path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        sql = self._migration_sql()
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + sql
                + f"\nPRAGMA user_version = {self.SCHEMA_VERSION};\nCOMMIT;"
            )
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise RepositoryError(
                f"database migration failed; source was not replaced: {exc}"
            ) from exc

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            with self._connection_lock:
                self._connections.discard(connection)
            self._local.connection = None

    def create_task(
        self,
        task: TaskSnapshot,
        items: Sequence[QueueItem],
        *,
        import_id: str | None = None,
        config_snapshot: Mapping[str, Any] | None = None,
        actor: str = "system",
    ) -> None:
        if any(item.task_id != task.id for item in items):
            raise RepositoryError("each queue item must belong to the created task")
        if len({item.id for item in items}) != len(items):
            raise RepositoryError("duplicate item id in task")
        try:
            with self._transaction() as connection:
                now = _iso(task.created_at_utc)
                connection.execute(
                    """INSERT INTO tasks
                       (id, import_id, name, state, device_mode, real_mode_authorized,
                        source_hash, config_snapshot_json, created_at_utc, updated_at_utc)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task.id,
                        import_id,
                        task.name,
                        task.state.value,
                        task.device_mode.value,
                        int(task.real_mode_authorized),
                        task.source_hash,
                        _json(config_snapshot or {}),
                        now,
                        _iso(task.updated_at_utc),
                    ),
                )
                self._insert_items(connection, task.id, items)
                if import_id is not None:
                    for item in items:
                        for row_no in item.source_row_numbers:
                            connection.execute(
                                """INSERT INTO item_sources(item_id, import_id, row_no)
                                   VALUES (?, ?, ?)""",
                                (item.id, import_id, row_no),
                            )
                self._event(
                    connection,
                    task.id,
                    "task_created",
                    actor,
                    {"item_count": len(items), "device_mode": task.device_mode.value},
                )
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(
                f"task violates a database uniqueness or foreign-key constraint: {exc}"
            ) from exc

    def _insert_items(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        items: Sequence[QueueItem],
        *,
        start_index: int | None = None,
    ) -> None:
        if start_index is None:
            row = connection.execute(
                "SELECT COALESCE(MAX(queue_index) + 1, 0) FROM task_items WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            start_index = int(row[0])
        for offset, item in enumerate(items):
            if item.task_id != task_id:
                raise RepositoryError("queue item belongs to a different task")
            created_at = _iso(item.created_at_utc or _utc_now())
            connection.execute(
                """INSERT INTO task_items
                   (id, task_id, tracking, carrier_key, sort_code, container_no, outbound_at_utc,
                    source_row_numbers_json, queue_index, status, physical_check_id, source_hash,
                    created_at_utc, updated_at_utc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.id,
                    task_id,
                    item.tracking,
                    item.carrier_key,
                    item.sort_code,
                    item.container_no,
                    _iso(item.outbound_at_utc),
                    _json(list(item.source_row_numbers)),
                    start_index + offset,
                    item.status.value,
                    item.physical_check_id,
                    item.source_hash,
                    created_at,
                    created_at,
                ),
            )

    def add_items(self, task_id: str, items: Sequence[QueueItem], *, actor: str = "system") -> None:
        try:
            with self._transaction() as connection:
                task = connection.execute(
                    "SELECT state, import_id FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if task is None:
                    raise RepositoryError(f"unknown task: {task_id}")
                if TaskState(task["state"]) not in {TaskState.DRAFT, TaskState.READY}:
                    raise RepositoryError("queue order is frozen after a task starts")
                self._insert_items(connection, task_id, items)
                if task["import_id"] is not None:
                    for item in items:
                        for row_no in item.source_row_numbers:
                            connection.execute(
                                """INSERT INTO item_sources(item_id, import_id, row_no)
                                   VALUES (?, ?, ?)""",
                                (item.id, task["import_id"], row_no),
                            )
                self._touch_task(connection, task_id)
                self._event(connection, task_id, "items_added", actor, {"item_count": len(items)})
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(f"duplicate item or invalid queue row: {exc}") from exc

    def get_task(self, task_id: str) -> TaskSnapshot:
        row = self._connection().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise RepositoryError(f"unknown task: {task_id}")
        items = self.list_items(task_id)
        counts = TaskCounts.from_statuses([item.status for item in items])
        return TaskSnapshot(
            id=row["id"],
            name=row["name"],
            state=TaskState(row["state"]),
            item_ids=tuple(item.id for item in items),
            counts=counts,
            created_at_utc=_datetime(row["created_at_utc"]),
            updated_at_utc=_datetime(row["updated_at_utc"]),
            device_mode=DeviceMode(row["device_mode"]),
            real_mode_authorized=bool(row["real_mode_authorized"]),
            source_hash=row["source_hash"],
        )

    def get_task_config_snapshot(self, task_id: str) -> dict[str, Any]:
        row = (
            self._connection()
            .execute("SELECT config_snapshot_json FROM tasks WHERE id = ?", (task_id,))
            .fetchone()
        )
        if row is None:
            raise RepositoryError(f"unknown task: {task_id}")
        return json.loads(row["config_snapshot_json"])

    def list_tasks(self, *, device_mode: DeviceMode | None = None) -> tuple[TaskSnapshot, ...]:
        if device_mode is None:
            rows = (
                self._connection()
                .execute("SELECT id FROM tasks ORDER BY created_at_utc, id")
                .fetchall()
            )
        else:
            rows = (
                self._connection()
                .execute(
                    "SELECT id FROM tasks WHERE device_mode = ? ORDER BY created_at_utc, id",
                    (device_mode.value,),
                )
                .fetchall()
            )
        return tuple(self.get_task(row["id"]) for row in rows)

    def list_task_summaries(self) -> tuple[dict[str, Any], ...]:
        status_columns = ", ".join(
            f"COALESCE(SUM(CASE WHEN i.status = '{status.value}' THEN 1 ELSE 0 END), 0) "
            f"AS {status.value}"
            for status in ItemStatus
        )
        rows = self._connection().execute(
            f"""SELECT t.id, t.name, t.state, t.created_at_utc, t.updated_at_utc,
                       t.device_mode, t.real_mode_authorized, t.source_hash,
                       COUNT(i.id) AS total, {status_columns}
                FROM tasks t LEFT JOIN task_items i ON i.task_id = t.id
                GROUP BY t.id ORDER BY t.created_at_utc, t.id"""
        ).fetchall()
        summaries = []
        for row in rows:
            counts = TaskCounts(
                total=int(row["total"]),
                by_status={status: int(row[status.value]) for status in ItemStatus},
            )
            summaries.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "state": TaskState(row["state"]),
                    "created_at_utc": _datetime(row["created_at_utc"]),
                    "updated_at_utc": _datetime(row["updated_at_utc"]),
                    "device_mode": DeviceMode(row["device_mode"]),
                    "real_mode_authorized": bool(row["real_mode_authorized"]),
                    "source_hash": row["source_hash"],
                    "counts": counts,
                }
            )
        return tuple(summaries)

    def task_ids_matching_tracking(self, query: str) -> frozenset[str]:
        needle = query.strip().casefold()
        if not needle:
            return frozenset()
        rows = self._connection().execute(
            "SELECT DISTINCT task_id FROM task_items WHERE instr(lower(tracking), ?) > 0",
            (needle,),
        ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def unresolved_count(self, task_id: str) -> int:
        return int(
            self._connection()
            .execute(
                """SELECT COUNT(*) FROM (
                     SELECT id AS item_id FROM task_items
                     WHERE task_id = ? AND status IN ('unknown','in_flight')
                     UNION
                     SELECT i.id AS item_id FROM duplicate_recoveries r
                     JOIN attempts a ON a.id = r.attempt_id
                     JOIN task_items i ON i.id = a.item_id
                     WHERE i.task_id = ? AND r.status IN ('pending','in_flight','failed','unknown')
                   )""",
                (task_id, task_id),
            )
            .fetchone()[0]
        )

    def has_import_hash(self, source_hash: str) -> bool:
        return bool(
            self._connection()
            .execute("SELECT EXISTS(SELECT 1 FROM imports WHERE sha256 = ?)", (source_hash,))
            .fetchone()[0]
        )

    def list_items(self, task_id: str) -> tuple[QueueItem, ...]:
        rows = (
            self._connection()
            .execute(
                "SELECT * FROM task_items WHERE task_id = ? ORDER BY queue_index",
                (task_id,),
            )
            .fetchall()
        )
        return tuple(_item_from_row(row) for row in rows)

    def get_item(self, item_id: str) -> QueueItem:
        row = (
            self._connection()
            .execute("SELECT * FROM task_items WHERE id = ?", (item_id,))
            .fetchone()
        )
        if row is None:
            raise RepositoryError(f"unknown queue item: {item_id}")
        return _item_from_row(row)

    def get_item_sources(self, item_id: str) -> tuple[dict[str, Any], ...]:
        rows = (
            self._connection()
            .execute(
                """SELECT s.import_id, s.row_no, r.raw_json, r.tracking, r.carrier,
                      r.status_raw, r.outbound_utc, r.exclude_reason
               FROM item_sources s JOIN import_rows r
                 ON r.import_id = s.import_id AND r.row_no = s.row_no
               WHERE s.item_id = ? ORDER BY s.import_id, s.row_no""",
                (item_id,),
            )
            .fetchall()
        )
        return tuple({**dict(row), "raw": json.loads(row["raw_json"])} for row in rows)

    def record_physical_check(self, check: PhysicalCheck, *, actor: str | None = None) -> None:
        if check.revoked_at_utc is not None:
            raise RepositoryError("a revoked physical check cannot be recorded as current evidence")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM task_items WHERE id = ?", (check.item_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown queue item: {check.item_id}")
            item = _item_from_row(row)
            if not check.is_valid_for(item):
                raise RepositoryError("physical check does not match shipment and target container")
            if item.status not in {ItemStatus.WAIT_PHYSICAL, ItemStatus.READY}:
                raise RepositoryError(
                    "physical check cannot change an item after submission starts"
                )
            connection.execute(
                "UPDATE physical_checks SET revoked_at_utc = ? "
                "WHERE item_id = ? AND revoked_at_utc IS NULL",
                (_iso(check.checked_at_utc), item.id),
            )
            connection.execute(
                """INSERT INTO physical_checks
                   (id, item_id, tracking, container_no, operator, checked_at_utc, method,
                    evidence_ref, revoked_at_utc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    check.id,
                    check.item_id,
                    check.tracking,
                    check.container_no,
                    check.operator,
                    _iso(check.checked_at_utc),
                    check.method.value,
                    check.evidence_ref,
                ),
            )
            connection.execute(
                "UPDATE task_items SET status = 'ready', physical_check_id = ?, "
                "updated_at_utc = ? WHERE id = ?",
                (check.id, _iso(check.checked_at_utc), item.id),
            )
            self._event(
                connection,
                item.task_id,
                "physical_check_recorded",
                actor or check.operator,
                {
                    "check_id": check.id,
                    "container_no": check.container_no,
                    "method": check.method.value,
                },
                item_id=item.id,
            )

    def get_physical_check(self, item_id: str) -> PhysicalCheck | None:
        row = (
            self._connection()
            .execute(
                "SELECT * FROM physical_checks WHERE item_id = ? "
                "ORDER BY (revoked_at_utc IS NULL) DESC, checked_at_utc DESC, rowid DESC LIMIT 1",
                (item_id,),
            )
            .fetchone()
        )
        return _check_from_row(row) if row is not None else None

    def revoke_physical_check(
        self,
        check_id: str,
        *,
        actor: str,
        reason: str,
        at_utc: datetime | None = None,
    ) -> None:
        if not actor.strip() or not reason.strip():
            raise RepositoryError("actor and reason are required to revoke physical evidence")
        now = _iso(at_utc or _utc_now())
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT c.*, i.task_id, i.status FROM physical_checks c
                   JOIN task_items i ON i.id = c.item_id WHERE c.id = ?""",
                (check_id,),
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown physical check: {check_id}")
            if row["revoked_at_utc"] is not None:
                return
            if ItemStatus(row["status"]) is ItemStatus.IN_FLIGHT:
                raise RepositoryError(
                    "physical evidence cannot be revoked while a submission is in flight"
                )
            connection.execute(
                "UPDATE physical_checks SET revoked_at_utc = ? WHERE id = ?", (now, check_id)
            )
            next_status = (
                "wait_physical" if row["status"] == ItemStatus.READY.value else row["status"]
            )
            connection.execute(
                "UPDATE task_items SET status = ?, physical_check_id = NULL, "
                "updated_at_utc = ? WHERE id = ?",
                (next_status, now, row["item_id"]),
            )
            self._event(
                connection,
                row["task_id"],
                "physical_check_revoked",
                actor,
                {"check_id": check_id, "reason": reason},
                item_id=row["item_id"],
            )

    def cancel_unsubmitted_item(self, item_id: str, *, actor: str, reason: str) -> None:
        if not actor.strip() or not reason.strip():
            raise RepositoryError("actor and reason are required to cancel an item")
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT i.*, t.state AS task_state FROM task_items i
                   JOIN tasks t ON t.id = i.task_id WHERE i.id = ?""",
                (item_id,),
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown queue item: {item_id}")
            item = _item_from_row(row)
            if item.status not in {ItemStatus.WAIT_PHYSICAL, ItemStatus.READY}:
                raise RepositoryError("only unsubmitted items can be cancelled")
            task_state = TaskState(row["task_state"])
            if task_state in {TaskState.RUNNING, TaskState.PAUSING}:
                raise RepositoryError("pause the task before cancelling an item")
            if task_state is TaskState.COMPLETED:
                raise RepositoryError("completed task items cannot be cancelled")

            check_id = item.physical_check_id
            if check_id is not None:
                connection.execute(
                    "UPDATE physical_checks SET revoked_at_utc = ? "
                    "WHERE id = ? AND revoked_at_utc IS NULL",
                    (_iso(now), check_id),
                )
            connection.execute(
                "UPDATE task_items SET status = ?, physical_check_id = NULL, "
                "updated_at_utc = ? WHERE id = ?",
                (ItemStatus.CANCELLED.value, _iso(now), item_id),
            )
            self._touch_task(connection, item.task_id, now)
            self._event(
                connection,
                item.task_id,
                "item_cancelled",
                actor,
                {
                    "from": item.status.value,
                    "to": ItemStatus.CANCELLED.value,
                    "reason": reason,
                    "revoked_physical_check_id": check_id,
                },
                item_id=item_id,
                at_utc=now,
            )

    def update_item_target(
        self,
        item_id: str,
        *,
        actor: str,
        reason: str,
        container_no: str | None = None,
        sort_code: str | None = None,
    ) -> None:
        if not actor.strip() or not reason.strip():
            raise RepositoryError("actor and reason are required to update a frozen target")
        if container_no is not None and not container_no.strip():
            raise RepositoryError("container cannot be empty")
        if sort_code is not None and not sort_code.strip():
            raise RepositoryError("sort code cannot be empty")
        now = _iso(_utc_now())
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM task_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise RepositoryError(f"unknown queue item: {item_id}")
            if ItemStatus(row["status"]) not in {ItemStatus.WAIT_PHYSICAL, ItemStatus.READY}:
                raise RepositoryError("target cannot change after submission starts")
            task = connection.execute(
                "SELECT state FROM tasks WHERE id = ?", (row["task_id"],)
            ).fetchone()
            if TaskState(task["state"]) not in {TaskState.DRAFT, TaskState.READY}:
                raise RepositoryError("task snapshot is frozen after the task starts")
            next_container = container_no if container_no is not None else row["container_no"]
            next_sort = sort_code if sort_code is not None else row["sort_code"]
            connection.execute(
                "UPDATE physical_checks SET revoked_at_utc = ? "
                "WHERE item_id = ? AND revoked_at_utc IS NULL",
                (now, item_id),
            )
            connection.execute(
                """UPDATE task_items SET container_no = ?, sort_code = ?, status = 'wait_physical',
                   physical_check_id = NULL, updated_at_utc = ? WHERE id = ?""",
                (next_container, next_sort, now, item_id),
            )
            self._touch_task(connection, row["task_id"])
            self._event(
                connection,
                row["task_id"],
                "item_target_changed",
                actor,
                {
                    "container_no": next_container,
                    "sort_code": next_sort,
                    "reason": reason,
                    "prior_container_no": row["container_no"],
                    "prior_sort_code": row["sort_code"],
                },
                item_id=item_id,
            )

    def begin_attempt(
        self,
        item_id: str,
        session: VerifiedSession,
        physical_check: PhysicalCheck,
        *,
        started_at_utc: datetime | None = None,
        actor: str = "scanner",
    ) -> str:
        now = started_at_utc or _utc_now()
        attempt_id = str(uuid.uuid4())
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM task_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise RepositoryError(f"unknown queue item: {item_id}")
            item = _item_from_row(row)
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (item.task_id,)
            ).fetchone()
            if TaskState(task["state"]) is not TaskState.RUNNING:
                raise RepositoryError("task must be explicitly running before a submission")
            if item.status is not ItemStatus.READY:
                raise RepositoryError(
                    f"only READY items can start; current status is {item.status.value}"
                )
            if (
                not session.valid
                or session.container_no != item.container_no
                or session.sort_code != item.sort_code
            ):
                raise RepositoryError("verified session does not match the item target")
            check_row = connection.execute(
                "SELECT * FROM physical_checks WHERE id = ? AND item_id = ?",
                (physical_check.id, item.id),
            ).fetchone()
            if check_row is None or item.physical_check_id != physical_check.id:
                raise RepositoryError("persisted physical check is required before submission")
            stored_check = _check_from_row(check_row)
            if stored_check.revoked_at_utc is not None or not stored_check.is_valid_for(item):
                raise RepositoryError("physical check is stale or mismatched")
            if not physical_check.is_valid_for(item) or physical_check.id != stored_check.id:
                raise RepositoryError("provided physical check does not match persisted evidence")
            if self._has_unresolved_locked(connection, item.task_id):
                raise RepositoryError("unresolved submission or duplicate recovery blocks the task")
            previous = int(
                connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE item_id = ?", (item.id,)
                ).fetchone()[0]
            )
            connection.execute(
                """INSERT INTO attempts
                   (id, item_id, attempt_no, status, session_id, device_serial, warehouse,
                    container_no, sort_code, started_at_utc, before_json)
                   VALUES (?, ?, ?, 'in_flight', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt_id,
                    item.id,
                    previous + 1,
                    session.session_id,
                    session.serial,
                    session.warehouse,
                    session.container_no,
                    session.sort_code,
                    _iso(now),
                    _json({"status": item.status.value, "physical_check_id": stored_check.id}),
                ),
            )
            connection.execute(
                "UPDATE task_items SET status = 'in_flight', updated_at_utc = ? WHERE id = ?",
                (_iso(now), item.id),
            )
            self._event(
                connection,
                item.task_id,
                "attempt_started",
                actor,
                {
                    "attempt_no": previous + 1,
                    "session_id": session.session_id,
                    "physical_check_id": stored_check.id,
                },
                item_id=item.id,
                attempt_id=attempt_id,
                at_utc=now,
            )
        return attempt_id

    def record_session_verified(
        self,
        item_id: str,
        session: VerifiedSession,
        *,
        actor: str = "scanner",
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM task_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise RepositoryError(f"unknown queue item: {item_id}")
            item = _item_from_row(row)
            task = connection.execute(
                "SELECT state FROM tasks WHERE id = ?", (item.task_id,)
            ).fetchone()
            if TaskState(task["state"]) is not TaskState.RUNNING:
                raise RepositoryError("session cannot be recorded outside a running task")
            if (
                not session.valid
                or not session.session_id.strip()
                or session.container_no != item.container_no
                or session.sort_code != item.sort_code
            ):
                raise RepositoryError("verified session does not match the item target")
            self._event(
                connection,
                item.task_id,
                "session_verified",
                actor,
                {
                    "session_id": session.session_id,
                    "serial": session.serial,
                    "warehouse": session.warehouse,
                    "container_no": session.container_no,
                    "sort_code": session.sort_code,
                    "verified_at_utc": _iso(session.verified_at_utc),
                },
                item_id=item.id,
                at_utc=session.verified_at_utc,
            )

    def finish_attempt(
        self,
        attempt_id: str,
        result: ScanResult,
        *,
        actor: str = "scanner",
        finished_at_utc: datetime | None = None,
    ) -> None:
        now = finished_at_utc or result.evidence.observed_at_utc
        outcome_status = {
            ScanOutcome.SUCCESS: ItemStatus.SUCCESS,
            ScanOutcome.ALREADY_IN_BAG: ItemStatus.ALREADY_IN_BAG,
            ScanOutcome.UNKNOWN: ItemStatus.UNKNOWN,
            ScanOutcome.REJECTED: ItemStatus.REJECTED,
        }[result.outcome]
        with self._transaction() as connection:
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RepositoryError(f"unknown attempt: {attempt_id}")
            if attempt["status"] != ItemStatus.IN_FLIGHT.value:
                raise RepositoryError("attempt is already finished")
            item_row = connection.execute(
                "SELECT * FROM task_items WHERE id = ?", (attempt["item_id"],)
            ).fetchone()
            item = _item_from_row(item_row)
            if (
                result.attempt_id != attempt_id
                or result.item_id != item.id
                or result.tracking != item.tracking
            ):
                raise RepositoryError("scan result does not match the pending attempt")
            if result.outcome in {ScanOutcome.SUCCESS, ScanOutcome.ALREADY_IN_BAG}:
                if result.evidence.container_no != attempt["container_no"]:
                    raise RepositoryError(
                        "terminal result container does not match the verified session"
                    )
                if result.evidence.sort_code != attempt["sort_code"]:
                    raise RepositoryError(
                        "terminal result sort code does not match the verified session"
                    )
            evidence_payload = {
                "observed_at_utc": _iso(result.evidence.observed_at_utc),
                "latest_tracking": result.evidence.latest_tracking,
                "container_no": result.evidence.container_no,
                "sort_code": result.evidence.sort_code,
                "error_text": result.evidence.error_text,
                "logid": result.evidence.logid,
                "redacted_xml_ref": result.evidence.redacted_xml_ref,
            }
            recovery_status = (
                "pending" if outcome_status is ItemStatus.ALREADY_IN_BAG else "not_required"
            )
            connection.execute(
                """UPDATE attempts SET status = ?, finished_at_utc = ?, outcome = ?, after_json = ?,
                   logid = ?, error_code = ?, recovery_status = ? WHERE id = ?""",
                (
                    outcome_status.value,
                    _iso(now),
                    result.outcome.value,
                    _json(evidence_payload),
                    result.evidence.logid,
                    None
                    if result.outcome in {ScanOutcome.SUCCESS, ScanOutcome.ALREADY_IN_BAG}
                    else result.evidence.error_text,
                    recovery_status,
                    attempt_id,
                ),
            )
            connection.execute(
                "UPDATE task_items SET status = ?, updated_at_utc = ? WHERE id = ?",
                (outcome_status.value, _iso(now), item.id),
            )
            if outcome_status is ItemStatus.ALREADY_IN_BAG:
                connection.execute(
                    """INSERT INTO duplicate_recoveries
                       (attempt_id, status, tracking, container_no, logid, updated_at_utc)
                       VALUES (?, 'pending', ?, ?, ?, ?)""",
                    (
                        attempt_id,
                        item.tracking,
                        item.container_no,
                        result.evidence.logid,
                        _iso(now),
                    ),
                )
            self._event(
                connection,
                item.task_id,
                "attempt_finished",
                actor,
                {"outcome": result.outcome.value, "evidence": evidence_payload},
                item_id=item.id,
                attempt_id=attempt_id,
                at_utc=now,
            )

    def mark_attempt_unknown(
        self,
        attempt_id: str,
        reason: str,
        *,
        actor: str = "recovery",
        at_utc: datetime | None = None,
    ) -> None:
        if not reason.strip():
            raise RepositoryError("an audit reason is required to mark an attempt unknown")
        now = at_utc or _utc_now()
        with self._transaction() as connection:
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise RepositoryError(f"unknown attempt: {attempt_id}")
            if attempt["status"] != ItemStatus.IN_FLIGHT.value:
                if attempt["status"] == ItemStatus.UNKNOWN.value:
                    return
                raise RepositoryError("only an in-flight attempt can become unknown")
            item = connection.execute(
                """SELECT i.task_id, t.state FROM task_items i JOIN tasks t ON t.id = i.task_id
                   WHERE i.id = ?""",
                (attempt["item_id"],),
            ).fetchone()
            connection.execute(
                """UPDATE attempts SET status = 'unknown', outcome = 'unknown', finished_at_utc = ?,
                   error_code = 'ambiguous', after_json = ? WHERE id = ?""",
                (_iso(now), _json({"reason": reason}), attempt_id),
            )
            connection.execute(
                "UPDATE task_items SET status = 'unknown', updated_at_utc = ? WHERE id = ?",
                (_iso(now), attempt["item_id"]),
            )
            connection.execute(
                "UPDATE tasks SET state = 'blocked', updated_at_utc = ? WHERE id = ?",
                (_iso(now), item["task_id"]),
            )
            if item["state"] != TaskState.BLOCKED.value:
                self._event(
                    connection,
                    item["task_id"],
                    "task_state_changed",
                    actor,
                    {"from": item["state"], "to": TaskState.BLOCKED.value, "reason": reason},
                    at_utc=now,
                )
            self._event(
                connection,
                item["task_id"],
                "attempt_marked_unknown",
                actor,
                {"reason": reason},
                item_id=attempt["item_id"],
                attempt_id=attempt_id,
                at_utc=now,
            )

    def record_recovery(
        self,
        result: RecoveryResult,
        *,
        actor: str = "scanner",
    ) -> None:
        now = result.observed_at_utc
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT r.*, a.item_id, i.task_id, i.tracking AS item_tracking,
                          i.container_no AS item_container
                   FROM duplicate_recoveries r JOIN attempts a ON a.id = r.attempt_id
                   JOIN task_items i ON i.id = a.item_id WHERE r.attempt_id = ?""",
                (result.attempt_id,),
            ).fetchone()
            if row is None:
                raise RepositoryError("duplicate recovery is not pending for this attempt")
            if (
                result.tracking != row["item_tracking"]
                or result.container_no != row["item_container"]
            ):
                raise RepositoryError(
                    "duplicate recovery evidence does not match the shipment and bag"
                )
            if result.logid and row["logid"] and result.logid != row["logid"]:
                raise RepositoryError(
                    "duplicate recovery Logid does not match the original duplicate"
                )
            if row["status"] != "in_flight":
                raise RepositoryError("duplicate recovery has already been resolved")
            status = "recovered" if result.recovered else "failed"
            connection.execute(
                """UPDATE duplicate_recoveries SET status = ?, evidence_ref = ?,
                   observed_at_utc = ?, updated_at_utc = ? WHERE attempt_id = ?""",
                (status, result.evidence_ref, _iso(now), _iso(now), result.attempt_id),
            )
            connection.execute(
                "UPDATE attempts SET recovery_status = ? WHERE id = ?",
                (status, result.attempt_id),
            )
            self._event(
                connection,
                row["task_id"],
                "duplicate_recovery_finished",
                actor,
                {
                    "status": status,
                    "tracking": result.tracking,
                    "container_no": result.container_no,
                    "logid": result.logid,
                    "evidence_ref": result.evidence_ref,
                },
                item_id=row["item_id"],
                attempt_id=result.attempt_id,
                at_utc=now,
            )
            if not result.recovered:
                connection.execute(
                    "UPDATE tasks SET state = 'blocked', updated_at_utc = ? WHERE id = ?",
                    (_iso(now), row["task_id"]),
                )

    def reconcile_duplicate_recovery(
        self,
        attempt_id: str,
        *,
        tracking: str,
        container_no: str,
        logid: str,
        actor: str,
        reason: str,
        evidence_ref: str,
        at_utc: datetime | None = None,
    ) -> None:
        """Manually resolve a duplicate recovery after the operator checks WMS.

        This method only updates the local audit record. It never invokes a device
        adapter or the WMS controller.
        """
        if not isinstance(actor, str) or not actor.strip():
            raise RepositoryError("operator actor is required")
        if not isinstance(reason, str) or not reason.strip():
            raise RepositoryError("manual reconciliation reason is required")
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise RepositoryError("manual reconciliation evidence reference is required")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (tracking, container_no, logid)
        ):
            raise RepositoryError(
                "tracking, container, and Logid are required for duplicate reconciliation"
            )
        now = at_utc or _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT r.*, a.status AS attempt_status, a.outcome AS attempt_outcome,
                          a.container_no AS attempt_container, a.logid AS attempt_logid,
                          a.item_id, i.task_id, i.tracking AS item_tracking,
                          i.container_no AS item_container, t.state AS task_state
                   FROM duplicate_recoveries r
                   JOIN attempts a ON a.id = r.attempt_id
                   JOIN task_items i ON i.id = a.item_id
                   JOIN tasks t ON t.id = i.task_id
                   WHERE r.attempt_id = ?""",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise RepositoryError("duplicate recovery record was not found")
            if row["status"] not in {"pending", "in_flight", "failed", "unknown"}:
                raise RepositoryError("duplicate recovery is already resolved")
            if row["attempt_outcome"] != ScanOutcome.ALREADY_IN_BAG.value:
                raise RepositoryError("attempt does not contain a confirmed duplicate outcome")
            if row["attempt_status"] != ItemStatus.ALREADY_IN_BAG.value:
                raise RepositoryError("duplicate attempt state does not match its recovery record")
            if tracking != row["tracking"] or tracking != row["item_tracking"]:
                raise RepositoryError("tracking does not match the original duplicate attempt")
            if (
                container_no != row["container_no"]
                or container_no != row["attempt_container"]
                or container_no != row["item_container"]
            ):
                raise RepositoryError("container does not match the original duplicate attempt")
            if not row["logid"] or logid != row["logid"] or logid != row["attempt_logid"]:
                raise RepositoryError("Logid does not match the original duplicate attempt")

            previous_status = row["status"]
            connection.execute(
                """UPDATE duplicate_recoveries SET status = 'recovered', evidence_ref = ?,
                   observed_at_utc = ?, updated_at_utc = ? WHERE attempt_id = ?""",
                (evidence_ref.strip(), _iso(now), _iso(now), attempt_id),
            )
            connection.execute(
                "UPDATE attempts SET recovery_status = 'recovered' WHERE id = ?",
                (attempt_id,),
            )
            self._event(
                connection,
                row["task_id"],
                "duplicate_recovery_manually_reconciled",
                actor.strip(),
                {
                    "attempt_id": attempt_id,
                    "prior_recovery_status": previous_status,
                    "tracking": tracking,
                    "container_no": container_no,
                    "logid": logid,
                    "reason": reason.strip(),
                    "evidence_ref": evidence_ref.strip(),
                },
                item_id=row["item_id"],
                attempt_id=attempt_id,
                at_utc=now,
            )

            if TaskState(
                row["task_state"]
            ) is TaskState.BLOCKED and not self._has_unresolved_locked(connection, row["task_id"]):
                try:
                    next_state = transition_task(TaskState.BLOCKED, TaskState.PAUSED)
                except StateTransitionError as exc:
                    raise RepositoryError(str(exc)) from exc
                connection.execute(
                    "UPDATE tasks SET state = ?, updated_at_utc = ? WHERE id = ?",
                    (next_state.value, _iso(now), row["task_id"]),
                )
                self._event(
                    connection,
                    row["task_id"],
                    "task_state_changed",
                    actor.strip(),
                    {
                        "from": TaskState.BLOCKED.value,
                        "to": next_state.value,
                        "reason": "manual duplicate recovery reconciliation completed",
                    },
                    at_utc=now,
                )

    def begin_recovery(self, attempt_id: str, *, actor: str = "scanner") -> None:
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT r.status, a.item_id, i.task_id FROM duplicate_recoveries r
                   JOIN attempts a ON a.id = r.attempt_id
                   JOIN task_items i ON i.id = a.item_id WHERE r.attempt_id = ?""",
                (attempt_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                raise RepositoryError("duplicate recovery is not ready for a first attempt")
            connection.execute(
                "UPDATE duplicate_recoveries SET status = 'in_flight', updated_at_utc = ? "
                "WHERE attempt_id = ?",
                (_iso(now), attempt_id),
            )
            connection.execute(
                "UPDATE attempts SET recovery_status = 'in_flight' WHERE id = ?",
                (attempt_id,),
            )
            self._event(
                connection,
                row["task_id"],
                "duplicate_recovery_started",
                actor,
                {"attempt_id": attempt_id},
                item_id=row["item_id"],
                attempt_id=attempt_id,
                at_utc=now,
            )

    def transition_task(
        self,
        task_id: str,
        target: TaskState,
        *,
        actor: str,
        reason: str,
    ) -> None:
        if not actor.strip() or not reason.strip():
            raise RepositoryError("actor and reason are required for task state changes")
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise RepositoryError(f"unknown task: {task_id}")
            current_state = TaskState(row["state"])
            if target is TaskState.COMPLETED:
                unresolved = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM task_items WHERE task_id = ?
                           AND status NOT IN ('success','already_in_bag','rejected','cancelled')""",
                        (task_id,),
                    ).fetchone()[0]
                )
                unresolved += int(
                    connection.execute(
                        """SELECT COUNT(*) FROM duplicate_recoveries r
                           JOIN attempts a ON a.id = r.attempt_id
                           JOIN task_items i ON i.id = a.item_id
                           WHERE i.task_id = ? AND r.status IN
                           ('pending','in_flight','failed','unknown')""",
                        (task_id,),
                    ).fetchone()[0]
                )
            elif current_state is TaskState.STOPPED and target is TaskState.RUNNING:
                unresolved = int(self._has_unresolved_locked(connection, task_id))
            else:
                unresolved = 0
            try:
                next_state = transition_task(current_state, target, unresolved_items=unresolved)
            except StateTransitionError as exc:
                raise RepositoryError(str(exc)) from exc
            connection.execute(
                "UPDATE tasks SET state = ?, updated_at_utc = ? WHERE id = ?",
                (next_state.value, _iso(now), task_id),
            )
            self._event(
                connection,
                task_id,
                "task_state_changed",
                actor,
                {"from": row["state"], "to": next_state.value, "reason": reason},
                at_utc=now,
            )

    def reconcile_item(
        self,
        item_id: str,
        target: ItemStatus,
        *,
        actor: str,
        reason: str,
        physical_check: PhysicalCheck | None = None,
    ) -> None:
        if not actor.strip() or not reason.strip():
            raise RepositoryError("authorized actor and reason are required for reconciliation")
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT i.*, t.state AS task_state FROM task_items i
                   JOIN tasks t ON t.id = i.task_id WHERE i.id = ?""",
                (item_id,),
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown queue item: {item_id}")
            item = _item_from_row(row)
            if item.status is not ItemStatus.UNKNOWN:
                raise RepositoryError("only an UNKNOWN item can be manually reconciled")
            if target is ItemStatus.READY:
                if physical_check is None or not physical_check.is_valid_for(item):
                    raise RepositoryError(
                        "new physical check is required before an unknown item can be retried"
                    )
                connection.execute(
                    "UPDATE physical_checks SET revoked_at_utc = ? "
                    "WHERE item_id = ? AND revoked_at_utc IS NULL",
                    (_iso(now), item_id),
                )
                connection.execute(
                    """INSERT INTO physical_checks
                       (id, item_id, tracking, container_no, operator, checked_at_utc, method,
                        evidence_ref, revoked_at_utc)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (
                        physical_check.id,
                        physical_check.item_id,
                        physical_check.tracking,
                        physical_check.container_no,
                        physical_check.operator,
                        _iso(physical_check.checked_at_utc),
                        physical_check.method.value,
                        physical_check.evidence_ref,
                    ),
                )
            elif target not in {
                ItemStatus.SUCCESS,
                ItemStatus.ALREADY_IN_BAG,
                ItemStatus.REJECTED,
                ItemStatus.CANCELLED,
            }:
                raise RepositoryError("manual reconciliation target is not supported")
            connection.execute(
                "UPDATE task_items SET status = ?, physical_check_id = ?, "
                "updated_at_utc = ? WHERE id = ?",
                (
                    target.value,
                    physical_check.id if target is ItemStatus.READY and physical_check else None,
                    _iso(now),
                    item_id,
                ),
            )
            self._touch_task(connection, item.task_id, now)
            self._event(
                connection,
                item.task_id,
                "item_reconciled",
                actor,
                {"from": ItemStatus.UNKNOWN.value, "to": target.value, "reason": reason},
                item_id=item_id,
                at_utc=now,
            )
            if (
                TaskState(row["task_state"]) is TaskState.BLOCKED
                and not self._has_unresolved_locked(connection, item.task_id)
            ):
                try:
                    next_state = transition_task(TaskState.BLOCKED, TaskState.PAUSED)
                except StateTransitionError as exc:
                    raise RepositoryError(str(exc)) from exc
                connection.execute(
                    "UPDATE tasks SET state = ?, updated_at_utc = ? WHERE id = ?",
                    (next_state.value, _iso(now), item.task_id),
                )
                self._event(
                    connection,
                    item.task_id,
                    "task_state_changed",
                    actor,
                    {
                        "from": TaskState.BLOCKED.value,
                        "to": next_state.value,
                        "reason": "manual reconciliation cleared all unresolved submissions",
                    },
                    at_utc=now,
                )

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        row = (
            self._connection()
            .execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
            .fetchone()
        )
        if row is None:
            raise RepositoryError(f"unknown attempt: {attempt_id}")
        return dict(row)

    def latest_attempt(self, item_id: str) -> dict[str, Any] | None:
        row = (
            self._connection()
            .execute(
                "SELECT * FROM attempts WHERE item_id = ? ORDER BY attempt_no DESC LIMIT 1",
                (item_id,),
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def events(self, *, task_id: str, item_id: str | None = None) -> tuple[dict[str, Any], ...]:
        if item_id is None:
            rows = (
                self._connection()
                .execute("SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,))
                .fetchall()
            )
        else:
            rows = (
                self._connection()
                .execute(
                    "SELECT * FROM events WHERE task_id = ? AND item_id = ? ORDER BY id",
                    (task_id, item_id),
                )
                .fetchall()
            )
        return tuple({**dict(row), "detail": json.loads(row["detail_json"])} for row in rows)

    def append_log(
        self,
        level: str,
        message: str,
        *,
        task_id: str | None = None,
        item_id: str | None = None,
        attempt_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
        at_utc: datetime | None = None,
    ) -> None:
        if not level.strip() or not message.strip():
            raise RepositoryError("log level and message are required")
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO logs(task_id, item_id, attempt_id, level, message, "
                "detail_json, at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    item_id,
                    attempt_id,
                    level,
                    message,
                    _json(detail or {}),
                    _iso(at_utc or _utc_now()),
                ),
            )

    def logs(self, *, task_id: str) -> tuple[dict[str, Any], ...]:
        rows = (
            self._connection()
            .execute("SELECT * FROM logs WHERE task_id = ? ORDER BY id", (task_id,))
            .fetchall()
        )
        return tuple({**dict(row), "detail": json.loads(row["detail_json"])} for row in rows)

    def statistics(self, task_id: str) -> dict[str, int]:
        counts = {status.value: 0 for status in ItemStatus}
        rows = (
            self._connection()
            .execute(
                "SELECT status, COUNT(*) AS count FROM task_items WHERE task_id = ? "
                "GROUP BY status",
                (task_id,),
            )
            .fetchall()
        )
        for row in rows:
            counts[row["status"]] = row["count"]
        attempts = int(
            self._connection()
            .execute(
                "SELECT COUNT(*) FROM attempts a JOIN task_items i ON i.id = a.item_id "
                "WHERE i.task_id = ?",
                (task_id,),
            )
            .fetchone()[0]
        )
        recovered = int(
            self._connection()
            .execute(
                "SELECT COUNT(*) FROM duplicate_recoveries r "
                "JOIN attempts a ON a.id = r.attempt_id "
                "JOIN task_items i ON i.id = a.item_id "
                "WHERE i.task_id = ? AND r.status = 'recovered'",
                (task_id,),
            )
            .fetchone()[0]
        )
        return {
            **counts,
            "attempts": attempts,
            "duplicate_recovered": recovered,
            "total": sum(counts.values()),
        }

    def has_unresolved_recovery(self, task_id: str) -> bool:
        return bool(
            self._connection()
            .execute(
                """SELECT EXISTS(SELECT 1 FROM duplicate_recoveries r
                   JOIN attempts a ON a.id = r.attempt_id JOIN task_items i ON i.id = a.item_id
                   WHERE i.task_id = ? AND r.status IN ('pending','in_flight',
                   'failed','unknown'))""",
                (task_id,),
            )
            .fetchone()[0]
        )

    def has_unresolved(self, task_id: str) -> bool:
        return self._has_unresolved_locked(self._connection(), task_id)

    def _has_unresolved_locked(self, connection: sqlite3.Connection, task_id: str) -> bool:
        return bool(
            connection.execute(
                """SELECT EXISTS(
                     SELECT 1 FROM task_items WHERE task_id = ?
                       AND status IN ('unknown','in_flight')
                     UNION ALL
                     SELECT 1 FROM duplicate_recoveries r JOIN attempts a ON a.id = r.attempt_id
                       JOIN task_items i ON i.id = a.item_id
                       WHERE i.task_id = ? AND r.status IN ('pending','in_flight',
                       'failed','unknown'))""",
                (task_id, task_id),
            ).fetchone()[0]
        )

    def recover_inflight(self, *, at_utc: datetime | None = None) -> tuple[str, ...]:
        now = at_utc or _utc_now()
        recovered: list[str] = []
        blocked: set[str] = set()
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT a.id AS attempt_id, a.item_id, i.task_id, t.state AS task_state
                   FROM attempts a JOIN task_items i ON i.id = a.item_id
                   JOIN tasks t ON t.id = i.task_id
                   WHERE a.status = 'in_flight' ORDER BY a.started_at_utc"""
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE attempts SET status = 'unknown', outcome = 'unknown',
                       finished_at_utc = ?,
                       error_code = 'process_interruption', after_json = ? WHERE id = ?""",
                    (
                        _iso(now),
                        _json({"reason": "process restarted with an in-flight attempt"}),
                        row["attempt_id"],
                    ),
                )
                connection.execute(
                    "UPDATE task_items SET status = 'unknown', updated_at_utc = ? WHERE id = ?",
                    (_iso(now), row["item_id"]),
                )
                connection.execute(
                    "UPDATE tasks SET state = 'blocked', updated_at_utc = ? WHERE id = ?",
                    (_iso(now), row["task_id"]),
                )
                if row["task_state"] != TaskState.BLOCKED.value and row["task_id"] not in blocked:
                    self._event(
                        connection,
                        row["task_id"],
                        "task_state_changed",
                        "startup-recovery",
                        {
                            "from": row["task_state"],
                            "to": TaskState.BLOCKED.value,
                            "reason": "process restarted with an in-flight attempt",
                        },
                        at_utc=now,
                    )
                blocked.add(row["task_id"])
                self._event(
                    connection,
                    row["task_id"],
                    "startup_recovery_unknown",
                    "startup-recovery",
                    {"reason": "process restarted with an in-flight attempt"},
                    item_id=row["item_id"],
                    attempt_id=row["attempt_id"],
                    at_utc=now,
                )
                recovered.append(row["attempt_id"])
            interrupted_recoveries = connection.execute(
                """SELECT r.attempt_id, a.item_id, i.task_id FROM duplicate_recoveries r
                   JOIN attempts a ON a.id = r.attempt_id
                   JOIN task_items i ON i.id = a.item_id WHERE r.status = 'in_flight'"""
            ).fetchall()
            for row in interrupted_recoveries:
                connection.execute(
                    "UPDATE duplicate_recoveries SET status = 'unknown', updated_at_utc = ? "
                    "WHERE attempt_id = ?",
                    (_iso(now), row["attempt_id"]),
                )
                connection.execute(
                    "UPDATE attempts SET recovery_status = 'unknown' WHERE id = ?",
                    (row["attempt_id"],),
                )
                connection.execute(
                    "UPDATE tasks SET state = 'blocked', updated_at_utc = ? WHERE id = ?",
                    (_iso(now), row["task_id"]),
                )
                self._event(
                    connection,
                    row["task_id"],
                    "duplicate_recovery_interrupted",
                    "startup-recovery",
                    {"reason": "process restarted during duplicate recovery"},
                    item_id=row["item_id"],
                    attempt_id=row["attempt_id"],
                    at_utc=now,
                )
            unresolved_tasks = connection.execute(
                """SELECT DISTINCT task_id FROM task_items
                   WHERE status = 'unknown'"""
            ).fetchall()
            for row in unresolved_tasks:
                connection.execute(
                    "UPDATE tasks SET state = 'blocked', updated_at_utc = ? WHERE id = ?",
                    (_iso(now), row["task_id"]),
                )
            safe_active_tasks = connection.execute(
                """SELECT t.id FROM tasks t WHERE t.state IN ('running','pausing')
                   AND NOT EXISTS (
                     SELECT 1 FROM task_items i WHERE i.task_id = t.id
                       AND i.status IN ('in_flight','unknown'))
                   AND NOT EXISTS (
                     SELECT 1 FROM duplicate_recoveries r
                     JOIN attempts a ON a.id = r.attempt_id
                     JOIN task_items i ON i.id = a.item_id
                     WHERE i.task_id = t.id
                       AND r.status IN ('pending','in_flight','failed','unknown'))"""
            ).fetchall()
            for row in safe_active_tasks:
                connection.execute(
                    "UPDATE tasks SET state = 'paused', updated_at_utc = ? WHERE id = ?",
                    (_iso(now), row["id"]),
                )
                self._event(
                    connection,
                    row["id"],
                    "startup_task_paused",
                    "startup-recovery",
                    {"reason": "process restarted without an unresolved submission"},
                    at_utc=now,
                )
            pending_tasks = connection.execute(
                """SELECT DISTINCT i.task_id FROM duplicate_recoveries r
                   JOIN attempts a ON a.id = r.attempt_id JOIN task_items i ON i.id = a.item_id
                   WHERE r.status IN ('pending','in_flight','failed','unknown')"""
            ).fetchall()
            for row in pending_tasks:
                connection.execute(
                    "UPDATE tasks SET state = 'blocked', updated_at_utc = ? WHERE id = ?",
                    (_iso(now), row["task_id"]),
                )
        return tuple(recovered)

    def backup_to(self, destination: str | Path) -> Path:
        target_path = Path(destination).expanduser().resolve()
        if target_path == self.path:
            raise RepositoryError("backup destination must differ from the active database")
        if target_path.exists():
            raise RepositoryError("backup destination already exists; refusing to overwrite it")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            source = self._connection()
            target = sqlite3.connect(target_path)
            try:
                source.backup(target)
                check = target.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    raise RepositoryError(f"backup integrity check failed: {check}")
            finally:
                target.close()
        return target_path

    @classmethod
    def restore_from(
        cls,
        source: str | Path,
        destination: str | Path,
        *,
        expected_mode: DeviceMode | None = None,
    ) -> Path:
        source_path = Path(source).expanduser().resolve()
        target_path = Path(destination).expanduser().resolve()
        if source_path == target_path:
            raise RepositoryError("restore destination must differ from backup source")
        if not source_path.is_file():
            raise RepositoryError("backup source does not exist")
        if target_path.exists():
            raise RepositoryError(
                "restore destination already exists; refusing to overwrite database"
            )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.restore")
        source_db = sqlite3.connect(source_path)
        target_db = sqlite3.connect(temp_path)
        try:
            result = source_db.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise RepositoryError(f"backup integrity check failed: {result}")
            version = int(source_db.execute("PRAGMA user_version").fetchone()[0])
            has_schema = source_db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if version != cls.SCHEMA_VERSION or has_schema is None:
                raise RepositoryError("backup does not contain a supported scanner database schema")
            foreign_key_errors = source_db.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RepositoryError("backup contains broken foreign-key references")
            if expected_mode is not None:
                task_modes = {
                    row[0]
                    for row in source_db.execute("SELECT DISTINCT device_mode FROM tasks")
                }
                if any(mode != expected_mode.value for mode in task_modes):
                    raise RepositoryError("backup belongs to a different scanner mode")
            source_db.backup(target_db)
            target_db.commit()
            result = target_db.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise RepositoryError(f"restored database integrity check failed: {result}")
            if target_db.execute("PRAGMA foreign_key_check").fetchall():
                raise RepositoryError("restored database contains broken foreign-key references")
            target_db.close()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp_path, target_path)
        except Exception:
            target_db.close()
            if temp_path.exists():
                temp_path.unlink()
            raise
        finally:
            source_db.close()
        return target_path

    def _event(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        event_type: str,
        actor: str,
        detail: Mapping[str, Any],
        *,
        item_id: str | None = None,
        attempt_id: str | None = None,
        at_utc: datetime | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events(task_id, item_id, attempt_id, event_type, actor, "
            "at_utc, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                item_id,
                attempt_id,
                event_type,
                actor,
                _iso(at_utc or _utc_now()),
                _json(detail),
            ),
        )

    def _touch_task(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        at_utc: datetime | None = None,
    ) -> None:
        connection.execute(
            "UPDATE tasks SET updated_at_utc = ? WHERE id = ?",
            (_iso(at_utc or _utc_now()), task_id),
        )

    def save_import_snapshot(
        self,
        *,
        import_id: str,
        file_name: str,
        source_hash: str,
        sheet_name: str,
        mapping: Mapping[str, Any],
        timezone: str,
        rows: Sequence[Mapping[str, Any]],
        imported_at_utc: datetime,
        row_count: int,
    ) -> None:
        if not all(
            value.strip() for value in (import_id, file_name, source_hash, sheet_name, timezone)
        ):
            raise RepositoryError("import identity, source hash, sheet, and timezone are required")
        if row_count < 0 or row_count != len(rows):
            raise RepositoryError("import row count must match the frozen source rows")
        try:
            with self._transaction() as connection:
                connection.execute(
                    """INSERT INTO imports
                       (id, file_name, sha256, sheet_name, mapping_json, imported_at_utc,
                        timezone, row_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        import_id,
                        file_name,
                        source_hash,
                        sheet_name,
                        _json(dict(mapping)),
                        _iso(imported_at_utc),
                        timezone,
                        row_count,
                    ),
                )
                seen_rows: set[int] = set()
                for source_row in rows:
                    row_no = int(source_row["row_number"])
                    if row_no < 1 or row_no in seen_rows:
                        raise RepositoryError("source row numbers must be unique positive integers")
                    seen_rows.add(row_no)
                    outbound = source_row.get("outbound_utc")
                    outbound_text = _iso(outbound) if isinstance(outbound, datetime) else outbound
                    connection.execute(
                        """INSERT INTO import_rows
                           (id, import_id, row_no, raw_json, tracking, carrier, status_raw,
                            outbound_utc, exclude_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            f"{import_id}:{row_no}",
                            import_id,
                            row_no,
                            _json(dict(source_row.get("raw", {}))),
                            source_row.get("tracking"),
                            source_row.get("carrier"),
                            source_row.get("status_raw"),
                            outbound_text,
                            source_row.get("exclude_reason"),
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(
                f"import snapshot violates a uniqueness or foreign-key constraint: {exc}"
            ) from exc

    def get_import_snapshot(self, import_id: str) -> dict[str, Any]:
        row = (
            self._connection()
            .execute("SELECT * FROM imports WHERE id = ?", (import_id,))
            .fetchone()
        )
        if row is None:
            raise RepositoryError(f"unknown import: {import_id}")
        rows = (
            self._connection()
            .execute(
                "SELECT * FROM import_rows WHERE import_id = ? ORDER BY row_no",
                (import_id,),
            )
            .fetchall()
        )
        return {
            **dict(row),
            "mapping": json.loads(row["mapping_json"]),
            "rows": tuple(
                {
                    **dict(source_row),
                    "raw": json.loads(source_row["raw_json"]),
                }
                for source_row in rows
            ),
        }

    def list_import_snapshots(self) -> tuple[dict[str, Any], ...]:
        rows = (
            self._connection()
            .execute("SELECT id FROM imports ORDER BY imported_at_utc DESC, id")
            .fetchall()
        )
        return tuple(self.get_import_snapshot(row["id"]) for row in rows)

    def save_carrier_configs(
        self,
        configs: Sequence[CarrierConfig],
        *,
        actor: str,
    ) -> None:
        if not actor.strip():
            raise RepositoryError("actor is required to save carrier configuration")
        pairs = [(config.carrier_key.casefold(), config.sort_code.casefold()) for config in configs]
        if len(set(pairs)) != len(pairs):
            raise RepositoryError("duplicate carrier and sort configuration in update")
        now = _utc_now()
        with self._transaction() as connection:
            for config in configs:
                config_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{config.carrier_key}\0{config.sort_code}")
                )
                current = connection.execute(
                    "SELECT * FROM carrier_configs WHERE carrier_key = ? AND sort_code = ?",
                    (config.carrier_key, config.sort_code),
                ).fetchone()
                before = None
                if current is not None:
                    before = {
                        "display_name": current["display_name"],
                        "container_no": current["bag_no"],
                        "aliases": json.loads(current["aliases_json"]),
                        "enabled": bool(current["enabled"]),
                        "version": current["version"],
                    }
                after = {
                    "display_name": config.display_name,
                    "aliases": list(config.aliases),
                    "container_no": config.container_no,
                    "enabled": config.enabled,
                    "version": config.version,
                }
                connection.execute(
                    """INSERT INTO carrier_configs
                       (id, carrier_key, display_name, sort_code, bag_no, aliases_json, enabled,
                        version, updated_at_utc)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(carrier_key, sort_code) DO UPDATE SET
                         display_name = excluded.display_name,
                         bag_no = excluded.bag_no,
                         aliases_json = excluded.aliases_json,
                         enabled = excluded.enabled,
                         version = excluded.version,
                         updated_at_utc = excluded.updated_at_utc""",
                    (
                        config_id,
                        config.carrier_key,
                        config.display_name,
                        config.sort_code,
                        config.container_no,
                        _json(list(config.aliases)),
                        int(config.enabled),
                        config.version,
                        _iso(config.updated_at_utc),
                    ),
                )
                connection.execute(
                    """INSERT INTO carrier_config_events
                       (config_id, carrier_key, sort_code, actor, before_json, after_json, at_utc)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        config_id,
                        config.carrier_key,
                        config.sort_code,
                        actor,
                        _json(before) if before is not None else None,
                        _json(after),
                        _iso(config.updated_at_utc),
                    ),
                )

            stale_in_flight = connection.execute(
                """SELECT i.id FROM task_items i
                   LEFT JOIN carrier_configs c
                     ON lower(c.carrier_key) = lower(i.carrier_key)
                    AND lower(c.sort_code) = lower(i.sort_code)
                    AND c.bag_no = i.container_no AND c.enabled = 1
                   WHERE i.status = 'in_flight' AND c.id IS NULL LIMIT 1"""
            ).fetchone()
            if stale_in_flight is not None:
                raise RepositoryError(
                    "carrier configuration cannot invalidate an in-flight submission"
                )

            stale_checks = connection.execute(
                """SELECT p.id AS check_id, p.item_id, i.task_id
                   FROM physical_checks p
                   JOIN task_items i ON i.id = p.item_id
                   LEFT JOIN carrier_configs c
                     ON lower(c.carrier_key) = lower(i.carrier_key)
                    AND lower(c.sort_code) = lower(i.sort_code)
                    AND c.bag_no = i.container_no AND c.enabled = 1
                   WHERE p.revoked_at_utc IS NULL AND i.status = 'ready'
                     AND i.physical_check_id = p.id AND c.id IS NULL"""
            ).fetchall()
            for stale in stale_checks:
                connection.execute(
                    "UPDATE physical_checks SET revoked_at_utc = ? WHERE id = ?",
                    (_iso(now), stale["check_id"]),
                )
                connection.execute(
                    "UPDATE task_items SET status = 'wait_physical', physical_check_id = NULL, "
                    "updated_at_utc = ? WHERE id = ?",
                    (_iso(now), stale["item_id"]),
                )
                self._touch_task(connection, stale["task_id"], now)
                self._event(
                    connection,
                    stale["task_id"],
                    "physical_check_revoked",
                    actor,
                    {
                        "check_id": stale["check_id"],
                        "reason": "carrier target or enabled state no longer matches",
                    },
                    item_id=stale["item_id"],
                    at_utc=now,
                )

    def list_carrier_configs(self) -> tuple[CarrierConfig, ...]:
        rows = (
            self._connection()
            .execute("SELECT * FROM carrier_configs ORDER BY carrier_key, sort_code")
            .fetchall()
        )
        return tuple(
            CarrierConfig(
                carrier_key=row["carrier_key"],
                display_name=row["display_name"],
                aliases=tuple(json.loads(row["aliases_json"])),
                sort_code=row["sort_code"],
                container_no=row["bag_no"],
                enabled=bool(row["enabled"]),
                version=row["version"],
                updated_at_utc=_datetime(row["updated_at_utc"]),
            )
            for row in rows
        )

    def carrier_config_history(
        self,
        carrier_key: str,
        sort_code: str,
    ) -> tuple[dict[str, Any], ...]:
        rows = (
            self._connection()
            .execute(
                """SELECT * FROM carrier_config_events
               WHERE lower(carrier_key) = lower(?) AND lower(sort_code) = lower(?)
               ORDER BY id""",
                (carrier_key, sort_code),
            )
            .fetchall()
        )
        return tuple(
            {
                **dict(row),
                "before": json.loads(row["before_json"]) if row["before_json"] else None,
                "after": json.loads(row["after_json"]),
            }
            for row in rows
        )


def _item_from_row(row: sqlite3.Row) -> QueueItem:
    return QueueItem(
        id=row["id"],
        task_id=row["task_id"],
        tracking=row["tracking"],
        carrier_key=row["carrier_key"],
        sort_code=row["sort_code"],
        container_no=row["container_no"],
        outbound_at_utc=_datetime(row["outbound_at_utc"]),
        source_row_numbers=tuple(json.loads(row["source_row_numbers_json"])),
        status=ItemStatus(row["status"]),
        physical_check_id=row["physical_check_id"],
        source_hash=row["source_hash"],
        created_at_utc=_datetime(row["created_at_utc"]),
    )


def _check_from_row(row: sqlite3.Row) -> PhysicalCheck:
    return PhysicalCheck(
        id=row["id"],
        item_id=row["item_id"],
        tracking=row["tracking"],
        container_no=row["container_no"],
        operator=row["operator"],
        checked_at_utc=_datetime(row["checked_at_utc"]),
        method=PhysicalCheckMethod(row["method"]),
        evidence_ref=row["evidence_ref"],
        revoked_at_utc=_datetime(row["revoked_at_utc"]) if row["revoked_at_utc"] else None,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RepositoryError("database timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RepositoryError("stored database timestamp is missing a timezone")
    return parsed.astimezone(UTC)


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
            default=_json_default,
        )
    except (TypeError, ValueError) as exc:
        raise RepositoryError(f"audit detail is not JSON serializable: {exc}") from exc


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers are not valid JSON")
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
