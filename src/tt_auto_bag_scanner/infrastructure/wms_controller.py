"""Guarded bag setup, one-shot scan submission, and duplicate recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from time import sleep
from uuid import uuid4

from tt_auto_bag_scanner.domain.device import DeviceAdapter
from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    DeviceInfo,
    DeviceMode,
    ItemStatus,
    PageObservation,
    PageState,
    PhysicalCheck,
    QueueItem,
    RecoveryResult,
    ScanEvidence,
    ScanOutcome,
    ScanResult,
    VerifiedSession,
)
from tt_auto_bag_scanner.infrastructure.exception_handler import (
    AmbiguousSubmission,
    DeviceActionError,
    UnknownPageError,
)
from tt_auto_bag_scanner.infrastructure.state_detector import StateDetector

_DUPLICATE_ERROR = "已扫描包裹已在当前大包中"


class SubmissionBlocked(RuntimeError):
    """A precondition failed before the one-shot scan could be sent."""


class WmsController:
    def __init__(
        self,
        adapter: DeviceAdapter,
        *,
        warehouse: str,
        target_package: str = "com.ss.android.pda",
        real_submission_authorized: bool = False,
        poll_attempts: int = 30,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        if not warehouse.strip():
            raise ValueError("warehouse is required")
        if poll_attempts < 1 or poll_interval_seconds < 0:
            raise ValueError("poll attempts must be positive and interval non-negative")
        self.adapter = adapter
        self.warehouse = warehouse
        self.target_package = target_package
        self.real_submission_authorized = real_submission_authorized
        self.poll_attempts = poll_attempts
        self.poll_interval_seconds = poll_interval_seconds
        self.detector = StateDetector(target_package=target_package)
        self._session: VerifiedSession | None = None
        self._session_generation: int | None = None
        self._blocked = False
        self._pending_duplicate: tuple[str, str, str] | None = None
        self._duplicate_recovery_attempted = False

    @property
    def device_mode(self) -> DeviceMode | None:
        is_simulated = getattr(self.adapter, "is_simulated", None)
        if is_simulated is True:
            return DeviceMode.SIMULATED
        if is_simulated is False:
            return DeviceMode.REAL
        return None

    def observe(self) -> PageObservation:
        current_generation = getattr(self.adapter, "session_generation", None)
        if (
            self._session is not None
            and self._session_generation is not None
            and current_generation != self._session_generation
        ):
            self._session = None
            self._session_generation = None
            self._blocked = True
            raise DeviceActionError("device/app session changed and must be reverified")
        try:
            observation = self.detector.detect(self.adapter.snapshot_xml())
        except Exception as exc:
            if self._session is not None:
                self._session = None
                self._session_generation = None
                self._blocked = True
            raise DeviceActionError("unable to read current WMS page") from exc
        if observation.state in {PageState.UNKNOWN, PageState.LOGIN}:
            if self._session is not None:
                self._blocked = True
            self._session = None
            self._session_generation = None
        return observation

    def open_bag(self, config: CarrierConfig) -> VerifiedSession:
        if self._blocked:
            raise SubmissionBlocked("controller is blocked pending human reconciliation")
        if self._session is not None:
            raise SubmissionBlocked("use verified bag switching for an active session")
        if not config.enabled:
            raise SubmissionBlocked("carrier configuration is disabled")
        if not config.container_no.strip() or not config.sort_code.strip():
            raise SubmissionBlocked("container and sort code are required")
        self._ensure_real_authorized()
        before = self.observe()
        if before.state is PageState.HOME:
            self._require_warehouse(before)
            before = self._tap_navigation_target(
                expected_state=PageState.HOME,
                exact_texts=("组大包", "大包管理"),
                next_state=PageState.CONTAINER_MENU,
            )
        if before.state is PageState.CONTAINER_MENU:
            self._require_warehouse(before)
            before = self._tap_navigation_target(
                expected_state=PageState.CONTAINER_MENU,
                exact_texts=("单排序",),
                next_state=PageState.SINGLE_EMPTY,
            )
        if before.state is not PageState.SINGLE_EMPTY:
            raise UnknownPageError("open bag requires a verified home/menu/single-scan page")
        self._require_warehouse(before)
        field = self._unique_empty_field(before)
        self.adapter.tap_field(field)
        focused = self.observe()
        self._require_warehouse(focused)
        focused_field = self._unique_empty_field(focused)
        self._require_same_field(field, focused_field)
        try:
            self.adapter.enter_text(config.container_no)
            self.adapter.press_key("ENTER")
            after = self._observe_until_known()
        except Exception as exc:
            self._session = None
            self._blocked = True
            raise AmbiguousSubmission("bag-open result could not be verified") from exc
        if after.state is not PageState.BAG_READY:
            self._session = None
            self._blocked = True
            raise UnknownPageError("WMS did not confirm the requested bag")
        actual_bag = after.visible_labels.get("大包号", "")
        actual_sort = after.visible_labels.get("排序码", "")
        if actual_bag != config.container_no or actual_sort != config.sort_code:
            self._session = None
            self._blocked = True
            raise SubmissionBlocked("WMS container or sort code does not match configuration")
        self._require_single_focused_field(after, allow_text=False)
        if after.visible_labels.get("仓库") != self.warehouse:
            self._session = None
            self._blocked = True
            raise SubmissionBlocked("WMS warehouse does not match configuration")
        return self._issue_verified_session(actual_bag, actual_sort)

    def verify_current_or_open_bag(self, config: CarrierConfig) -> VerifiedSession:
        """Read-only verify an existing bag, otherwise use the guarded open flow."""
        if self._blocked:
            raise SubmissionBlocked("controller is blocked pending human reconciliation")
        if not config.enabled:
            raise SubmissionBlocked("carrier configuration is disabled")
        if not config.container_no.strip() or not config.sort_code.strip():
            raise SubmissionBlocked("container and sort code are required")
        observation = self.observe()
        if observation.state in {PageState.BAG_READY, PageState.SCAN_RESULT}:
            self._require_warehouse(observation)
            if observation.visible_labels.get("大包号") != config.container_no:
                raise SubmissionBlocked("visible container does not match configuration")
            if observation.visible_labels.get("排序码") != config.sort_code:
                raise SubmissionBlocked("visible sort code does not match configuration")
            self._require_single_focused_field(observation, allow_text=False)
            self._ensure_real_device_connected()
            if self._session is not None:
                self._validate_session_token(self._session)
                self._require_session_page(observation, self._session)
                return self._session
            return self._issue_verified_session(config.container_no, config.sort_code)

        if self._session is not None:
            self._session = None
            self._session_generation = None
            self._blocked = True
            raise SubmissionBlocked("active session changed; human reconciliation is required")
        if observation.state in {
            PageState.SINGLE_EMPTY,
            PageState.HOME,
            PageState.CONTAINER_MENU,
        }:
            return self.open_bag(config)
        raise UnknownPageError("current page cannot be verified or opened safely")

    def switch_bag(
        self,
        config: CarrierConfig,
        *,
        unresolved_submissions: int,
    ) -> VerifiedSession:
        """Leave the open bag only through verified Back navigation; never close it."""
        if unresolved_submissions != 0:
            raise SubmissionBlocked("cannot switch bags with unresolved submissions")
        if self._blocked:
            raise SubmissionBlocked("controller is blocked pending human reconciliation")
        self._ensure_real_authorized()
        session = self._session
        if session is None:
            raise SubmissionBlocked("a controller-verified session is required to switch bags")
        self._validate_session_token(session)
        current = self.observe()
        self._require_session_page(current, session)
        self._require_single_focused_field(current, allow_text=False)

        for _ in range(2):
            self.adapter.press_key("BACK")
            after_back = self.observe()
            if after_back.state is PageState.SINGLE_EMPTY:
                self._session = None
                self._session_generation = None
                return self.open_bag(config)
            if after_back.state not in {PageState.BAG_READY, PageState.SCAN_RESULT}:
                self._session = None
                self._session_generation = None
                self._blocked = True
                raise UnknownPageError("WMS page is unknown while leaving the current bag")
            if (
                after_back.visible_labels.get("大包号") != session.container_no
                or after_back.visible_labels.get("排序码") != session.sort_code
                or after_back.visible_labels.get("仓库") != self.warehouse
            ):
                self._session = None
                self._session_generation = None
                self._blocked = True
                raise SubmissionBlocked("bag context changed while leaving the current bag")
        self._session = None
        self._session_generation = None
        self._blocked = True
        raise UnknownPageError("Back navigation did not reach a stable empty single-scan page")

    def submit_once(
        self,
        item: QueueItem,
        session: VerifiedSession,
        physical_check: PhysicalCheck,
        attempt_id: str,
    ) -> ScanResult:
        if not attempt_id.strip():
            raise SubmissionBlocked("durable attempt id is required before submission")
        if item.status is not ItemStatus.READY:
            raise SubmissionBlocked("item must be READY before submission")
        if physical_check is None or not physical_check.is_valid_for(item):
            raise SubmissionBlocked("valid physical check is required")
        if not session.valid:
            raise SubmissionBlocked("verified session is invalid")
        if self._blocked:
            raise SubmissionBlocked("controller is blocked pending human reconciliation")
        if session.warehouse != self.warehouse:
            raise SubmissionBlocked("warehouse does not match verified session")
        if session.container_no != item.container_no:
            raise SubmissionBlocked("item container does not match verified session")
        if session.sort_code != item.sort_code:
            raise SubmissionBlocked("item sort code does not match verified session")
        self._validate_session_token(session)
        self._ensure_real_authorized()

        before = self.observe()
        self._require_session_page(before, session)
        if item.tracking in before.latest_tracking_records:
            raise SubmissionBlocked("tracking already appears in the current latest record")
        field = self._require_single_focused_field(before, allow_text=False)
        self.adapter.tap_field(field)
        focused = self.observe()
        focused_field = self._require_single_focused_field(focused, allow_text=False)
        self._require_same_field(field, focused_field)
        if focused.state not in {PageState.BAG_READY, PageState.SCAN_RESULT}:
            raise SubmissionBlocked("page changed before submission")
        try:
            self.adapter.enter_text(item.tracking)
        except Exception as exc:
            # A failed text entry occurs before the only Enter. It remains blocked
            # because field state may have changed and requires a fresh operator check.
            self._blocked = True
            self._session = None
            self._session_generation = None
            raise SubmissionBlocked("tracking entry failed before submission") from exc

        # From this call onward, any error is ambiguous. Never send a second Enter.
        try:
            self.adapter.press_key("ENTER")
            after = self._observe_until_result(before)
        except Exception:
            evidence = self._evidence(None, session, error="submission result could not be read")
            return ScanResult(
                attempt_id=attempt_id,
                item_id=item.id,
                tracking=item.tracking,
                outcome=ScanOutcome.UNKNOWN,
                evidence=evidence,
            )
        return self._classify_scan_result(item, session, attempt_id, after)

    def invalidate_session(self) -> None:
        """Invalidate after manual takeover, app restart, or an external disconnect."""
        self._session = None
        self._session_generation = None
        self._blocked = True

    def recover_duplicate(
        self, result: ScanResult, session: VerifiedSession
    ) -> RecoveryResult:
        if result.outcome is not ScanOutcome.ALREADY_IN_BAG or not result.evidence.logid:
            raise SubmissionBlocked("duplicate Logid evidence is required for recovery")
        expected_duplicate = self._pending_duplicate
        if expected_duplicate != (
            result.attempt_id,
            result.tracking,
            result.evidence.logid,
        ):
            raise SubmissionBlocked("duplicate result was not issued by this controller")
        if (
            result.evidence.container_no != session.container_no
            or result.evidence.sort_code != session.sort_code
        ):
            raise SubmissionBlocked("duplicate result context does not match the current session")
        if self._duplicate_recovery_attempted:
            raise SubmissionBlocked("duplicate recovery was already attempted")
        if not session.valid or session.container_no == "":
            raise SubmissionBlocked("a verified session is required for recovery")
        self._validate_session_token(session, allow_blocked=True)
        current = self.observe()
        if current.state is not PageState.DUPLICATE_DIALOG:
            raise UnknownPageError("duplicate dialog is no longer visible")
        if current.dialog_text != _DUPLICATE_ERROR or current.logid != result.evidence.logid:
            raise SubmissionBlocked("duplicate evidence does not match the current dialog")
        fields = current.focused_inputs
        if len(fields) != 1 or not fields[0].editable or fields[0].text.strip():
            raise UnknownPageError("recovery requires one focused empty enabled field")
        prior_field = fields[0]
        self.adapter.tap_field(prior_field)
        refreshed = self.observe()
        fields = refreshed.focused_inputs
        if refreshed.state is not PageState.DUPLICATE_DIALOG or len(fields) != 1:
            raise UnknownPageError("duplicate dialog changed before recovery")
        self._require_same_field(prior_field, fields[0])
        self._duplicate_recovery_attempted = True
        try:
            self.adapter.enter_text(result.tracking)
            self.adapter.press_key("ENTER")
            after = self._observe_after_recovery(session)
        except Exception:
            self._blocked = True
            self._session = None
            self._session_generation = None
            return RecoveryResult(
                attempt_id=result.attempt_id,
                tracking=result.tracking,
                container_no=session.container_no,
                recovered=False,
                observed_at_utc=datetime.now(UTC),
                logid=result.evidence.logid,
            )
        recovered = (
            after.state in {PageState.BAG_READY, PageState.SCAN_RESULT}
            and after.visible_labels.get("大包号") == session.container_no
            and after.visible_labels.get("排序码") == session.sort_code
            and after.visible_labels.get("仓库") == session.warehouse
            and not after.dialog_text
            and len(after.focused_inputs) == 1
            and not after.focused_inputs[0].text.strip()
        )
        if recovered:
            self._blocked = False
            self._pending_duplicate = None
            self._duplicate_recovery_attempted = False
        else:
            self._blocked = True
            self._session = None
            self._session_generation = None
        return RecoveryResult(
            attempt_id=result.attempt_id,
            tracking=result.tracking,
            container_no=session.container_no,
            recovered=recovered,
            observed_at_utc=datetime.now(UTC),
            logid=result.evidence.logid,
            evidence_ref=after.redacted_xml_ref,
        )

    def _classify_scan_result(
        self,
        item: QueueItem,
        session: VerifiedSession,
        attempt_id: str,
        observation: PageObservation,
    ) -> ScanResult:
        if (
            observation.state is PageState.DUPLICATE_DIALOG
            and observation.logid
            and observation.dialog_text == _DUPLICATE_ERROR
        ):
            self._blocked = True
            self._pending_duplicate = (attempt_id, item.tracking, observation.logid)
            self._duplicate_recovery_attempted = False
            evidence = ScanEvidence(
                observed_at_utc=datetime.now(UTC),
                container_no=session.container_no,
                sort_code=session.sort_code,
                error_text=observation.dialog_text,
                logid=observation.logid,
                redacted_xml_ref=observation.redacted_xml_ref,
            )
            return ScanResult(
                attempt_id=attempt_id,
                item_id=item.id,
                tracking=item.tracking,
                outcome=ScanOutcome.ALREADY_IN_BAG,
                evidence=evidence,
                recovery_status="pending",
            )
        actual_container = observation.visible_labels.get("大包号")
        actual_sort = observation.visible_labels.get("排序码")
        explicit_success = observation.visible_labels.get("扫描状态") in {
            "扫描成功",
            "Scan successful",
        }
        explicit_rejection = observation.visible_labels.get("扫描状态") in {
            "扫描失败",
            "Scan rejected",
        }
        if (
            observation.state is PageState.SCAN_RESULT
            and explicit_rejection
            and observation.logid
            and observation.latest_tracking_records
            and observation.latest_tracking_records[0] == item.tracking
            and actual_container == session.container_no
            and actual_sort == session.sort_code
        ):
            return ScanResult(
                attempt_id=attempt_id,
                item_id=item.id,
                tracking=item.tracking,
                outcome=ScanOutcome.REJECTED,
                evidence=ScanEvidence(
                    observed_at_utc=datetime.now(UTC),
                    container_no=actual_container,
                    sort_code=actual_sort,
                    error_text=observation.dialog_text,
                    logid=observation.logid,
                    redacted_xml_ref=observation.redacted_xml_ref,
                ),
            )
        if (
            observation.state is PageState.SCAN_RESULT
            and observation.latest_tracking_records
            and observation.latest_tracking_records[0] == item.tracking
            and actual_container == session.container_no
            and actual_sort == session.sort_code
            and explicit_success
        ):
            return ScanResult(
                attempt_id=attempt_id,
                item_id=item.id,
                tracking=item.tracking,
                outcome=ScanOutcome.SUCCESS,
                evidence=ScanEvidence(
                    observed_at_utc=datetime.now(UTC),
                    latest_tracking=observation.latest_tracking_records[0],
                    container_no=actual_container,
                    sort_code=actual_sort,
                    redacted_xml_ref=observation.redacted_xml_ref,
                ),
            )
        self._blocked = True
        self._session = None
        self._session_generation = None
        return ScanResult(
            attempt_id=attempt_id,
            item_id=item.id,
            tracking=item.tracking,
            outcome=ScanOutcome.UNKNOWN,
            evidence=ScanEvidence(
                observed_at_utc=datetime.now(UTC),
                latest_tracking=(
                    observation.latest_tracking_records[0]
                    if observation.latest_tracking_records
                    else None
                ),
                container_no=actual_container,
                sort_code=actual_sort,
                error_text=observation.dialog_text or "result evidence is incomplete or mismatched",
                logid=observation.logid,
                redacted_xml_ref=observation.redacted_xml_ref,
            ),
        )

    def _require_session_page(
        self, observation: PageObservation, session: VerifiedSession
    ) -> None:
        if observation.state not in {PageState.BAG_READY, PageState.SCAN_RESULT}:
            self._session = None
            self._session_generation = None
            self._blocked = True
            raise UnknownPageError("current WMS page is not a known open bag")
        if observation.visible_labels.get("大包号") != session.container_no:
            self._session = None
            self._session_generation = None
            self._blocked = True
            raise SubmissionBlocked("visible container does not match verified session")
        if observation.visible_labels.get("排序码") != session.sort_code:
            self._session = None
            self._session_generation = None
            self._blocked = True
            raise SubmissionBlocked("visible sort code does not match verified session")
        if observation.visible_labels.get("仓库") != session.warehouse:
            self._session = None
            self._session_generation = None
            self._blocked = True
            raise SubmissionBlocked("visible warehouse does not match verified session")

    def _validate_session_token(
        self, session: VerifiedSession, *, allow_blocked: bool = False
    ) -> None:
        if self._blocked and not allow_blocked:
            raise SubmissionBlocked("controller is blocked pending human reconciliation")
        if self._session is None or self._session.session_id != session.session_id:
            raise SubmissionBlocked("session token was not verified by this controller")
        if self._session != session:
            raise SubmissionBlocked("session token context has changed")
        device_info = self._device_info()
        if device_info.serial != session.serial:
            self._session = None
            self._session_generation = None
            self._blocked = True
            raise SubmissionBlocked("connected device does not match verified session")
        generation = getattr(self.adapter, "session_generation", None)
        if self._session_generation is not None and generation != self._session_generation:
            self._session = None
            self._session_generation = None
            self._blocked = True
            raise SubmissionBlocked("device/app session changed and must be reverified")

    @staticmethod
    def _require_same_field(previous, current) -> None:
        if previous.resource_id:
            matches = previous.resource_id == current.resource_id
        else:
            matches = previous.bounds == current.bounds
        if not matches:
            raise UnknownPageError("focused input changed before text entry")

    @staticmethod
    def _unique_empty_field(observation: PageObservation):
        if observation.state is not PageState.SINGLE_EMPTY:
            raise UnknownPageError("expected the empty single-scan page")
        return WmsController._require_single_focused_field(observation, allow_text=False)

    @staticmethod
    def _require_single_focused_field(observation: PageObservation, *, allow_text: bool):
        fields = observation.focused_inputs
        if len(fields) != 1:
            raise UnknownPageError("exactly one focused enabled input is required")
        field = fields[0]
        if not field.editable or (not allow_text and field.text.strip()):
            raise UnknownPageError("focused input must be editable and empty")
        return field

    def _observe_until_known(self) -> PageObservation:
        for attempt in range(self.poll_attempts):
            observation = self.observe()
            if observation.state is not PageState.SINGLE_EMPTY:
                return observation
            if attempt + 1 < self.poll_attempts and self.poll_interval_seconds:
                sleep(self.poll_interval_seconds)
        return observation

    @staticmethod
    def _result_signature(observation: PageObservation) -> tuple:
        return (
            observation.state,
            tuple(observation.latest_tracking_records),
            observation.logid,
            observation.dialog_text,
            observation.visible_labels.get("扫描状态"),
        )

    def _observe_until_result(self, before: PageObservation) -> PageObservation:
        before_signature = self._result_signature(before)
        for attempt in range(self.poll_attempts):
            observation = self.observe()
            if (
                observation.state is not PageState.BAG_READY
                and self._result_signature(observation) != before_signature
            ):
                return observation
            if attempt + 1 < self.poll_attempts and self.poll_interval_seconds:
                sleep(self.poll_interval_seconds)
        return observation

    def _observe_after_recovery(self, session: VerifiedSession) -> PageObservation:
        observation = self.observe()
        for attempt in range(self.poll_attempts):
            if observation.state in {PageState.BAG_READY, PageState.SCAN_RESULT}:
                if (
                    observation.visible_labels.get("大包号") != session.container_no
                    or observation.visible_labels.get("排序码") != session.sort_code
                    or observation.visible_labels.get("仓库") != session.warehouse
                ):
                    return observation
                if (
                    len(observation.focused_inputs) == 1
                    and not observation.focused_inputs[0].text.strip()
                ):
                    return observation
            elif observation.state is not PageState.DUPLICATE_DIALOG:
                return observation
            if attempt + 1 < self.poll_attempts and self.poll_interval_seconds:
                sleep(self.poll_interval_seconds)
            observation = self.observe()
        return observation

    def _evidence(
        self,
        observation: PageObservation | None,
        session: VerifiedSession,
        *,
        error: str,
    ) -> ScanEvidence:
        return ScanEvidence(
            observed_at_utc=datetime.now(UTC),
            container_no=(
                observation.visible_labels.get("大包号")
                if observation is not None
                else session.container_no
            ),
            sort_code=(
                observation.visible_labels.get("排序码")
                if observation is not None
                else session.sort_code
            ),
            error_text=error,
            redacted_xml_ref=observation.redacted_xml_ref if observation is not None else "",
        )

    def _device_info(self) -> DeviceInfo:
        try:
            return self.adapter.metadata()
        except Exception:
            if getattr(self.adapter, "is_simulated", False):
                return DeviceInfo(serial="simulator", state="device", authorized=True)
            raise DeviceActionError("connected device metadata is required")

    def _ensure_real_authorized(self) -> None:
        if getattr(self.adapter, "is_simulated", False):
            return
        if not self.real_submission_authorized:
            raise SubmissionBlocked("real WMS actions require explicit site authorization")
        self._ensure_real_device_connected()

    def _ensure_real_device_connected(self) -> None:
        if getattr(self.adapter, "is_simulated", False):
            return
        try:
            info = self.adapter.metadata()
        except Exception as exc:
            raise SubmissionBlocked("real device must be connected and authorized") from exc
        if info.state.lower() != "device" or not info.authorized:
            raise SubmissionBlocked("real device must be connected and authorized")

    def _require_warehouse(self, observation: PageObservation) -> None:
        if observation.visible_labels.get("仓库") != self.warehouse:
            raise SubmissionBlocked("visible warehouse does not match configuration")

    def _tap_navigation_target(
        self,
        *,
        expected_state: PageState,
        exact_texts: tuple[str, ...],
        next_state: PageState,
    ) -> PageObservation:
        try:
            snapshot = self.adapter.snapshot_xml()
            current = self.detector.detect(snapshot)
            self._require_warehouse(current)
            target = self.detector.find_unique_target(
                snapshot,
                expected_state=expected_state,
                exact_texts=exact_texts,
            )
        except Exception as exc:
            if isinstance(exc, SubmissionBlocked):
                raise
            raise UnknownPageError("navigation target is missing or ambiguous") from exc
        try:
            self.adapter.tap(*target.center)
            after = self.observe()
        except Exception as exc:
            self._blocked = True
            raise UnknownPageError("navigation result could not be verified") from exc
        if after.state is not next_state:
            self._blocked = True
            raise UnknownPageError("WMS did not reach the expected navigation page")
        try:
            self._require_warehouse(after)
        except SubmissionBlocked:
            self._blocked = True
            raise
        return after

    def _issue_verified_session(self, container_no: str, sort_code: str) -> VerifiedSession:
        device_info = self._device_info()
        self._session = VerifiedSession(
            serial=device_info.serial,
            warehouse=self.warehouse,
            container_no=container_no,
            sort_code=sort_code,
            session_id=str(uuid4()),
            verified_at_utc=datetime.now(UTC),
        )
        self._session_generation = getattr(self.adapter, "session_generation", None)
        return self._session

