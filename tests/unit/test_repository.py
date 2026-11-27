import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    DeviceMode,
    ItemStatus,
    PhysicalCheck,
    PhysicalCheckMethod,
    QueueItem,
    ScanEvidence,
    ScanOutcome,
    ScanResult,
    TaskCounts,
    TaskSnapshot,
    TaskState,
    VerifiedSession,
)
from tt_auto_bag_scanner.infrastructure.repository import RepositoryError, TaskRepository

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def make_task(task_id: str = "task-synthetic") -> TaskSnapshot:
    return TaskSnapshot(
        id=task_id,
        name="Synthetic task",
        state=TaskState.READY,
        item_ids=(),
        counts=TaskCounts.from_statuses([]),
        created_at_utc=NOW,
        updated_at_utc=NOW,
        device_mode=DeviceMode.SIMULATED,
    )


def make_item(item_id: str = "item-synthetic", tracking: str = "SYNTHETIC-0001") -> QueueItem:
    return QueueItem(
        id=item_id,
        task_id="task-synthetic",
        tracking=tracking,
        carrier_key="SYNTHETIC",
        sort_code="SORT-SYNTHETIC",
        container_no="BAG-SYNTHETIC",
        outbound_at_utc=NOW,
        source_row_numbers=(2,),
        status=ItemStatus.WAIT_PHYSICAL,
    )


def make_check(item: QueueItem, check_id: str = "check-synthetic") -> PhysicalCheck:
    return PhysicalCheck(
        id=check_id,
        item_id=item.id,
        tracking=item.tracking,
        container_no=item.container_no,
        operator="synthetic-operator",
        checked_at_utc=NOW,
        method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
        evidence_ref="synthetic-evidence",
    )


def make_session() -> VerifiedSession:
    return VerifiedSession(
        serial="SYNTHETIC-DEVICE",
        warehouse="SYNTHETIC-WAREHOUSE",
        container_no="BAG-SYNTHETIC",
        sort_code="SORT-SYNTHETIC",
        session_id="session-synthetic",
        verified_at_utc=NOW,
    )


def prepare_repository(path, *, tracking: str = "SYNTHETIC-0001"):
    repo = TaskRepository(path)
    task = make_task()
    item = make_item(tracking=tracking)
    repo.create_task(task, [item])
    check = make_check(item)
    repo.record_physical_check(check)
    return repo, task, item, check


def test_schema_uses_wal_and_enforces_same_task_tracking_uniqueness(tmp_path) -> None:
    repo = TaskRepository(tmp_path / "scanner.sqlite3")
    repo.create_task(make_task(), [make_item()])

    assert repo.journal_mode.lower() == "wal"
    with pytest.raises(RepositoryError, match="unique|duplicate"):
        repo.add_items("task-synthetic", [make_item("item-dup-row")])


def test_begin_attempt_commits_in_flight_and_attempt_before_external_write(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    repo.transition_task("task-synthetic", TaskState.RUNNING, actor="operator", reason="test start")

    attempt_id = repo.begin_attempt(item.id, make_session(), check)

    assert repo.get_item(item.id).status is ItemStatus.IN_FLIGHT
    assert repo.get_attempt(attempt_id)["status"] == "in_flight"
    assert repo.events(task_id=item.task_id)[-1]["event_type"] == "attempt_started"


def test_begin_attempt_rejects_missing_or_stale_physical_check(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    repo.revoke_physical_check(check.id, actor="synthetic-operator", reason="test invalidation")
    repo.transition_task("task-synthetic", TaskState.RUNNING, actor="operator", reason="test start")

    with pytest.raises(RepositoryError, match="physical"):
        repo.begin_attempt(item.id, make_session(), check)


def test_active_physical_check_cannot_be_revoked_during_submission(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    repo.transition_task("task-synthetic", TaskState.RUNNING, actor="operator", reason="test start")
    repo.begin_attempt(item.id, make_session(), check)

    with pytest.raises(RepositoryError, match="in flight"):
        repo.revoke_physical_check(
            check.id,
            actor="operator",
            reason="late invalidation request",
        )


def test_repository_allows_only_one_in_flight_attempt_per_task(tmp_path) -> None:
    repo, _, first_item, first_check = prepare_repository(tmp_path / "scanner.sqlite3")
    second_item = make_item("item-second", tracking="SYNTHETIC-0002")
    repo.add_items("task-synthetic", [second_item])
    second_check = make_check(second_item, check_id="check-second")
    repo.record_physical_check(second_check)
    repo.transition_task("task-synthetic", TaskState.RUNNING, actor="operator", reason="test start")

    repo.begin_attempt(first_item.id, make_session(), first_check)
    with pytest.raises(RepositoryError, match="unresolved"):
        repo.begin_attempt(second_item.id, make_session(), second_check)

    assert repo.statistics("task-synthetic")["attempts"] == 1


def test_task_cannot_complete_while_ready_or_unverified_items_remain(tmp_path) -> None:
    repo, _, _, _ = prepare_repository(tmp_path / "scanner.sqlite3")
    repo.transition_task("task-synthetic", TaskState.RUNNING, actor="operator", reason="test start")

    with pytest.raises(RepositoryError, match="unresolved"):
        repo.transition_task(
            "task-synthetic",
            TaskState.COMPLETED,
            actor="operator",
            reason="premature completion",
        )


def test_initial_migration_backs_up_preexisting_database_before_schema_changes(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as legacy_db:
        legacy_db.execute("CREATE TABLE legacy_record(value TEXT NOT NULL)")
        legacy_db.execute("INSERT INTO legacy_record(value) VALUES ('preserve-me')")

    repository = TaskRepository(db_path)
    backups = list(tmp_path.glob("legacy.pre-migration-*.sqlite3"))

    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup_db:
        assert backup_db.execute("SELECT value FROM legacy_record").fetchone()[0] == "preserve-me"
    assert repository.journal_mode.lower() == "wal"


def test_finish_attempt_updates_status_audit_and_new_success_statistics(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    repo.transition_task("task-synthetic", TaskState.RUNNING, actor="operator", reason="test start")
    attempt_id = repo.begin_attempt(item.id, make_session(), check)
    result = ScanResult(
        attempt_id=attempt_id,
        item_id=item.id,
        tracking=item.tracking,
        outcome=ScanOutcome.SUCCESS,
        evidence=ScanEvidence(
            observed_at_utc=NOW,
            latest_tracking=item.tracking,
            container_no=item.container_no,
            sort_code=item.sort_code,
        ),
    )

    repo.finish_attempt(attempt_id, result)

    assert repo.get_item(item.id).status is ItemStatus.SUCCESS
    assert repo.statistics(item.task_id)["success"] == 1
    assert repo.events(task_id=item.task_id)[-1]["event_type"] == "attempt_finished"


def test_duplicate_is_not_counted_as_new_success_and_blocks_until_recovery(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    repo.transition_task("task-synthetic", TaskState.RUNNING, actor="operator", reason="test start")
    attempt_id = repo.begin_attempt(item.id, make_session(), check)
    duplicate = ScanResult(
        attempt_id=attempt_id,
        item_id=item.id,
        tracking=item.tracking,
        outcome=ScanOutcome.ALREADY_IN_BAG,
        evidence=ScanEvidence(
            observed_at_utc=NOW,
            container_no=item.container_no,
            sort_code=item.sort_code,
            logid="LOGID-SYNTHETIC",
        ),
    )
    repo.finish_attempt(attempt_id, duplicate)

    counts = repo.statistics(item.task_id)
    assert counts["success"] == 0
    assert counts["already_in_bag"] == 1
    assert repo.has_unresolved_recovery(item.task_id)


def test_operator_can_reconcile_duplicate_recovery_and_unblock_task(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    attempt_id = _create_blocked_duplicate(repo, item, check)

    repo.reconcile_duplicate_recovery(
        attempt_id,
        tracking=item.tracking,
        container_no=item.container_no,
        logid="LOGID-SYNTHETIC",
        actor="operator-synthetic",
        reason="checked the WMS bag record and confirmed the dialog was cleared",
        evidence_ref="wms-record:synthetic-check",
    )

    assert repo.get_attempt(attempt_id)["recovery_status"] == "recovered"
    assert not repo.has_unresolved_recovery(item.task_id)
    assert repo.get_task(item.task_id).state is TaskState.PAUSED
    event = repo.events(task_id=item.task_id)[-2]
    assert event["event_type"] == "duplicate_recovery_manually_reconciled"
    assert event["actor"] == "operator-synthetic"
    assert event["detail"]["evidence_ref"] == "wms-record:synthetic-check"


@pytest.mark.parametrize("recovery_status", ["pending", "in_flight", "failed", "unknown"])
def test_manual_reconciliation_resolves_each_unresolved_recovery_status(
    tmp_path, recovery_status
) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    attempt_id = _create_blocked_duplicate(repo, item, check)
    with repo._transaction() as connection:
        connection.execute(
            "UPDATE duplicate_recoveries SET status = ? WHERE attempt_id = ?",
            (recovery_status, attempt_id),
        )
        connection.execute(
            "UPDATE attempts SET recovery_status = ? WHERE id = ?",
            (recovery_status, attempt_id),
        )

    result = repo.reconcile_duplicate_recovery(
        attempt_id,
        tracking=item.tracking,
        container_no=item.container_no,
        logid="LOGID-SYNTHETIC",
        actor="operator-synthetic",
        reason="confirmed the shipment in WMS",
        evidence_ref="wms-record:synthetic-check",
    )

    assert result is None
    assert repo.get_attempt(attempt_id)["recovery_status"] == "recovered"
    assert repo.get_task(item.task_id).state is TaskState.PAUSED


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("actor", " ", "actor"),
        ("actor", None, "actor"),
        ("reason", " ", "reason"),
        ("reason", None, "reason"),
        ("evidence_ref", " ", "evidence"),
        ("evidence_ref", None, "evidence"),
    ],
)
def test_manual_duplicate_reconciliation_requires_operator_audit_fields(
    tmp_path, field, value, error
) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    attempt_id = _create_blocked_duplicate(repo, item, check)
    details = {
        "tracking": item.tracking,
        "container_no": item.container_no,
        "logid": "LOGID-SYNTHETIC",
        "actor": "operator-synthetic",
        "reason": "confirmed from WMS",
        "evidence_ref": "wms-record:synthetic-check",
    }
    details[field] = value

    with pytest.raises(RepositoryError, match=error):
        repo.reconcile_duplicate_recovery(attempt_id, **details)

    assert repo.get_attempt(attempt_id)["recovery_status"] == "pending"
    assert repo.get_task(item.task_id).state is TaskState.BLOCKED


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("tracking", "OTHER-TRACKING", "tracking"),
        ("container_no", "OTHER-CONTAINER", "container"),
        ("logid", "OTHER-LOGID", "Logid"),
    ],
)
def test_manual_duplicate_reconciliation_requires_exact_attempt_match(
    tmp_path, field, value, error
) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    attempt_id = _create_blocked_duplicate(repo, item, check)
    details = {
        "tracking": item.tracking,
        "container_no": item.container_no,
        "logid": "LOGID-SYNTHETIC",
        "actor": "operator-synthetic",
        "reason": "confirmed from WMS",
        "evidence_ref": "wms-record:synthetic-check",
    }
    details[field] = value

    with pytest.raises(RepositoryError, match=error):
        repo.reconcile_duplicate_recovery(attempt_id, **details)

    assert repo.get_attempt(attempt_id)["recovery_status"] == "pending"
    assert repo.get_task(item.task_id).state is TaskState.BLOCKED


def test_manual_duplicate_reconciliation_keeps_task_blocked_for_other_unknown_items(
    tmp_path,
) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    other_item = make_item("item-other", tracking="TRACK-OTHER-SYNTHETIC")
    repo.add_items(item.task_id, [other_item])
    repo.record_physical_check(make_check(other_item, check_id="check-other"))
    attempt_id = _create_blocked_duplicate(repo, item, check)
    with repo._transaction() as connection:
        connection.execute(
            "UPDATE task_items SET status = 'unknown' WHERE id = ?",
            (other_item.id,),
        )

    repo.reconcile_duplicate_recovery(
        attempt_id,
        tracking=item.tracking,
        container_no=item.container_no,
        logid="LOGID-SYNTHETIC",
        actor="operator-synthetic",
        reason="checked the WMS bag record and confirmed the dialog was cleared",
        evidence_ref="wms-record:synthetic-check",
    )

    assert not repo.has_unresolved_recovery(item.task_id)
    assert repo.has_unresolved(item.task_id)
    assert repo.get_task(item.task_id).state is TaskState.BLOCKED


@pytest.mark.parametrize(
    ("column", "value"),
    [("container_no", "OTHER-CONTAINER"), ("logid", "OTHER-LOGID")],
)
def test_manual_duplicate_reconciliation_requires_original_attempt_values(
    tmp_path, column, value
) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    attempt_id = _create_blocked_duplicate(repo, item, check)
    with repo._transaction() as connection:
        connection.execute(f"UPDATE attempts SET {column} = ? WHERE id = ?", (value, attempt_id))

    with pytest.raises(RepositoryError, match="container|Logid"):
        repo.reconcile_duplicate_recovery(
            attempt_id,
            tracking=item.tracking,
            container_no=item.container_no,
            logid="LOGID-SYNTHETIC",
            actor="operator-synthetic",
            reason="confirmed the shipment in WMS",
            evidence_ref="wms-record:synthetic-check",
        )

    assert repo.get_attempt(attempt_id)["recovery_status"] == "pending"
    assert repo.get_task(item.task_id).state is TaskState.BLOCKED


def _create_blocked_duplicate(repo, item, check) -> str:
    repo.transition_task(item.task_id, TaskState.RUNNING, actor="operator", reason="test start")
    attempt_id = repo.begin_attempt(item.id, make_session(), check)
    repo.finish_attempt(
        attempt_id,
        ScanResult(
            attempt_id=attempt_id,
            item_id=item.id,
            tracking=item.tracking,
            outcome=ScanOutcome.ALREADY_IN_BAG,
            evidence=ScanEvidence(
                observed_at_utc=NOW,
                container_no=item.container_no,
                sort_code=item.sort_code,
                logid="LOGID-SYNTHETIC",
            ),
        ),
    )
    repo.transition_task(
        item.task_id,
        TaskState.BLOCKED,
        actor="scanner",
        reason="duplicate recovery is pending",
    )
    return attempt_id


def test_target_change_revokes_old_physical_evidence_atomically(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")

    repo.update_item_target(
        item.id,
        container_no="BAG-OTHER-SYNTHETIC",
        actor="synthetic-operator",
        reason="configuration changed",
    )

    updated = repo.get_item(item.id)
    assert updated.status is ItemStatus.WAIT_PHYSICAL
    assert updated.container_no == "BAG-OTHER-SYNTHETIC"
    assert repo.get_physical_check(item.id).revoked_at_utc is not None
    assert repo.get_physical_check(item.id).id == check.id


def test_backup_and_restore_preserve_task_attempts_and_events(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    repo, _, item, check = prepare_repository(source)
    repo.transition_task("task-synthetic", TaskState.RUNNING, actor="operator", reason="test start")
    attempt_id = repo.begin_attempt(item.id, make_session(), check)
    repo.backup_to(backup)

    TaskRepository.restore_from(backup, restored)
    copy = TaskRepository(restored)

    assert copy.get_item(item.id).status is ItemStatus.IN_FLIGHT
    assert copy.get_attempt(attempt_id)["status"] == "in_flight"
    assert copy.events(task_id=item.task_id)


def test_import_snapshot_preserves_raw_rows_and_source_hash(tmp_path) -> None:
    repo = TaskRepository(tmp_path / "scanner.sqlite3")
    repo.save_import_snapshot(
        import_id="import-synthetic",
        file_name="synthetic-report.xlsx",
        source_hash="hash-synthetic",
        sheet_name="Synthetic",
        mapping={"tracking_column": "Tracking"},
        timezone="Synthetic/Zone",
        rows=[
            {
                "row_number": 2,
                "raw": {"Tracking": "0000123456789012", "Status": "ready"},
                "tracking": "0000123456789012",
                "carrier": "SYNTHETIC",
                "status_raw": "ready",
                "outbound_utc": None,
                "exclude_reason": None,
            }
        ],
        imported_at_utc=NOW,
        row_count=1,
    )

    snapshot = repo.get_import_snapshot("import-synthetic")

    assert snapshot["sha256"] == "hash-synthetic"
    assert snapshot["sheet_name"] == "Synthetic"
    assert snapshot["rows"][0]["tracking"] == "0000123456789012"
    assert snapshot["rows"][0]["raw"]["Tracking"] == "0000123456789012"
    source_item = QueueItem(
        id="item-from-import",
        task_id="task-from-import",
        tracking="0000123456789012",
        carrier_key="SYNTHETIC",
        sort_code="SORT-SYNTHETIC",
        container_no="BAG-SYNTHETIC",
        outbound_at_utc=NOW,
        source_row_numbers=(2,),
    )
    repo.create_task(
        make_task("task-from-import"),
        [source_item],
        import_id="import-synthetic",
    )
    assert repo.get_item_sources(source_item.id)[0]["raw"]["Status"] == "ready"


def test_saving_changed_container_revokes_stale_ready_item_evidence(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    config = CarrierConfig(
        carrier_key=item.carrier_key,
        display_name=item.carrier_key,
        aliases=(),
        sort_code=item.sort_code,
        container_no="BAG-NEW-SYNTHETIC",
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )

    repo.save_carrier_configs([config], actor="synthetic-operator")

    updated = repo.get_item(item.id)
    revoked = repo.get_physical_check(item.id)
    assert updated.status is ItemStatus.WAIT_PHYSICAL
    assert updated.physical_check_id is None
    assert revoked is not None and revoked.id == check.id
    assert revoked.revoked_at_utc is not None
    assert repo.events(task_id=item.task_id)[-1]["event_type"] == "physical_check_revoked"


def test_saving_matching_container_preserves_physical_evidence(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    config = CarrierConfig(
        carrier_key=item.carrier_key,
        display_name=item.carrier_key,
        aliases=("synthetic alias",),
        sort_code=item.sort_code,
        container_no=item.container_no,
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )

    repo.save_carrier_configs([config], actor="synthetic-operator")

    assert repo.get_item(item.id).status is ItemStatus.READY
    active = repo.get_physical_check(item.id)
    assert active is not None and active.id == check.id
    assert active.revoked_at_utc is None


def test_cancelling_ready_item_revokes_evidence_and_audits_actor_reason(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")

    repo.cancel_unsubmitted_item(
        item.id,
        actor="synthetic-operator",
        reason="parcel removed from work area",
    )

    cancelled = repo.get_item(item.id)
    revoked_check = repo.get_physical_check(item.id)
    event = repo.events(task_id=item.task_id, item_id=item.id)[-1]
    assert cancelled.status is ItemStatus.CANCELLED
    assert cancelled.physical_check_id is None
    assert revoked_check is not None and revoked_check.id == check.id
    assert revoked_check.revoked_at_utc is not None
    assert event["event_type"] == "item_cancelled"
    assert event["actor"] == "synthetic-operator"
    assert event["detail"]["reason"] == "parcel removed from work area"


def test_cancelling_wait_physical_item_is_allowed(tmp_path) -> None:
    repo = TaskRepository(tmp_path / "scanner.sqlite3")
    item = make_item()
    repo.create_task(make_task(), [item])

    repo.cancel_unsubmitted_item(
        item.id,
        actor="synthetic-operator",
        reason="parcel excluded before verification",
    )

    assert repo.get_item(item.id).status is ItemStatus.CANCELLED


def test_cancelling_in_flight_or_unknown_item_is_rejected(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    repo.transition_task(
        item.task_id,
        TaskState.RUNNING,
        actor="synthetic-operator",
        reason="test start",
    )
    attempt_id = repo.begin_attempt(item.id, make_session(), check)
    with pytest.raises(RepositoryError, match="only unsubmitted"):
        repo.cancel_unsubmitted_item(
            item.id,
            actor="synthetic-operator",
            reason="must not interrupt an in-flight submission",
        )
    repo.mark_attempt_unknown(
        attempt_id,
        "uncertain synthetic result",
        actor="synthetic-worker",
    )

    with pytest.raises(RepositoryError, match="only unsubmitted"):
        repo.cancel_unsubmitted_item(
            item.id,
            actor="synthetic-operator",
            reason="must reconcile first",
        )

    assert repo.get_item(item.id).status is ItemStatus.UNKNOWN


def test_cancelling_ready_item_requires_a_safe_task_state(tmp_path) -> None:
    repo, _, item, _ = prepare_repository(tmp_path / "scanner.sqlite3")
    repo.transition_task(
        item.task_id,
        TaskState.RUNNING,
        actor="synthetic-operator",
        reason="test start",
    )

    with pytest.raises(RepositoryError, match="pause the task"):
        repo.cancel_unsubmitted_item(
            item.id,
            actor="synthetic-operator",
            reason="must wait for a safe boundary",
        )

    assert repo.get_item(item.id).status is ItemStatus.READY


def test_reconciling_last_unknown_atomically_unblocks_task(tmp_path) -> None:
    repo, _, item, check = prepare_repository(tmp_path / "scanner.sqlite3")
    repo.transition_task(
        item.task_id,
        TaskState.RUNNING,
        actor="synthetic-operator",
        reason="test start",
    )
    attempt_id = repo.begin_attempt(item.id, make_session(), check)
    repo.mark_attempt_unknown(
        attempt_id,
        "synthetic outcome could not be read",
        actor="synthetic-worker",
    )

    repo.reconcile_item(
        item.id,
        ItemStatus.SUCCESS,
        actor="synthetic-operator",
        reason="WMS history verified",
    )

    assert repo.get_item(item.id).status is ItemStatus.SUCCESS
    assert repo.get_task(item.task_id).state is TaskState.PAUSED
    assert repo.events(task_id=item.task_id)[-1]["detail"]["to"] == TaskState.PAUSED.value


def test_restore_rejects_backup_from_a_different_device_mode(tmp_path) -> None:
    source = tmp_path / "simulated.sqlite3"
    destination = tmp_path / "real-restore.sqlite3"
    repo, _, _, _ = prepare_repository(source)
    repo.backup_to(tmp_path / "backup.sqlite3")

    with pytest.raises(RepositoryError, match="different scanner mode"):
        TaskRepository.restore_from(
            tmp_path / "backup.sqlite3",
            destination,
            expected_mode=DeviceMode.REAL,
        )

    assert not destination.exists()


def test_restore_rejects_backup_with_broken_foreign_keys(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "restored.sqlite3"
    repo, _, _, _ = prepare_repository(source)
    repo.close()
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM tasks WHERE id = 'task-synthetic'")
    connection.commit()
    connection.close()

    with pytest.raises(RepositoryError, match="foreign-key"):
        TaskRepository.restore_from(source, destination)

    assert not destination.exists()


def test_carrier_config_versions_are_persisted_and_read_back(tmp_path) -> None:
    repo = TaskRepository(tmp_path / "scanner.sqlite3")
    config = CarrierConfig(
        carrier_key="SYNTHETIC_CARRIER",
        display_name="Synthetic Carrier",
        aliases=("Synthetic",),
        sort_code="SORT-SYNTHETIC",
        container_no="BAG-SYNTHETIC",
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )

    repo.save_carrier_configs([config], actor="synthetic-operator")

    assert repo.list_carrier_configs() == (config,)
    updated = replace(
        config,
        container_no="BAG-UPDATED-SYNTHETIC",
        version=2,
        updated_at_utc=datetime(2026, 11, 26, 12, 1, tzinfo=UTC),
    )
    repo.save_carrier_configs([updated], actor="synthetic-operator")

    assert repo.list_carrier_configs() == (updated,)
    history = repo.carrier_config_history(config.carrier_key, config.sort_code)
    assert len(history) == 2
    assert history[-1]["before"]["container_no"] == config.container_no
    assert history[-1]["after"]["container_no"] == updated.container_no
