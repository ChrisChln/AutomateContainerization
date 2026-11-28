"""Guarded bag setup, one-shot scan submission, and duplicate recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic, sleep
from uuid import uuid4

from tt_auto_bag_scanner.domain.device import DeviceAdapter
from tt_auto_bag_scanner.domain.models import (
    ALREADY_GROUPED_REASON,
    CarrierConfig,
    DeviceInfo,
    DeviceMode,
    InputField,
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
    ScreenOffError,
    UnknownPageError,
)
from tt_auto_bag_scanner.infrastructure.state_detector import StateDetector

_UNVERIFIED = object()
_RETURN_HOME_STATES = frozenset(
    {
        PageState.BAG_READY,
        PageState.SCAN_RESULT,
        PageState.SINGLE_EMPTY,
        PageState.SINGLE_OPEN_FAILED,
        PageState.CONTAINER_MENU,
    }
)
_MAX_HOME_SWIPES = 5
_MAX_RETURN_HOME_BACKS = 4


class SubmissionBlocked(RuntimeError):
    """A precondition failed before the one-shot scan could be sent."""


class ScanNotSent(SubmissionBlocked):
    """Scan input verification failed before text entry or Enter."""


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
        timeout_seconds: float | None = None,
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
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self.timeout_seconds = timeout_seconds
        self.detector = StateDetector(target_package=target_package)
        self._session: VerifiedSession | None = None
        self._session_generation: int | None = None
        self._blocked = False
        self._pending_duplicate: tuple[str, str, str] | None = None
        # The open-bag pages do not show the warehouse, so it is verified on HOME
        # and trusted only while the device/app session generation is unchanged.
        self._warehouse_verified_for: object = _UNVERIFIED
        self._duplicate_recovery_attempted = False

    @property
    def device_mode(self) -> DeviceMode | None:
        is_simulated = getattr(self.adapter, "is_simulated", None)
        if is_simulated is True:
            return DeviceMode.SIMULATED
        if is_simulated is False:
            return DeviceMode.REAL
        return None

    def observe(self, *, reading_scan_result: bool = False) -> PageObservation:
        current_generation = getattr(self.adapter, "session_generation", None)
        if (
            self._session is not None
            and self._session_generation is not None
            and current_generation != self._session_generation
        ):
            self._warehouse_verified_for = _UNVERIFIED
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
            if isinstance(exc, ScreenOffError):
                raise
            raise DeviceActionError("unable to read current WMS page") from exc
        # A keyboard-obscured result can be re-read via the detector's safe tap.
        # Keep the token only during this result read and only on a matching bag.
        obscured_result = (
            reading_scan_result
            and observation.state is PageState.UNKNOWN
            and observation.keyboard_visible
            and observation.safe_tap_point is not None
            and self._session is not None
            and observation.visible_labels.get("大包号") == self._session.container_no
            and observation.visible_labels.get("排序码") == self._session.sort_code
            and self._warehouse_ok(observation)
        )
        if observation.state in {PageState.UNKNOWN, PageState.LOGIN} and not obscured_result:
            self._warehouse_verified_for = _UNVERIFIED
            if self._session is not None:
                # An unreadable result drops the token. Allow a fresh preflight
                # unless the result page shows a context change.
                session = self._session
                labels = observation.visible_labels
                if (
                    not reading_scan_result
                    or observation.state is PageState.LOGIN
                    or labels.get("大包号") not in (None, "", session.container_no)
                    or labels.get("排序码") not in (None, "", session.sort_code)
                    or labels.get("仓库") not in (None, "", session.warehouse)
                ):
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
            self._reveal_home_target(before, ("组大包", "大包管理"))
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
        if focused.state is not PageState.SINGLE_EMPTY:
            raise UnknownPageError("expected the empty single-scan page")
        self._require_focused_same_field(field, focused.input_fields)
        try:
            self.adapter.enter_text(config.container_no)
            self.adapter.press_key("ENTER")
            after = self._observe_until_state(PageState.BAG_READY)
        except Exception as exc:
            self._session = None
            self._blocked = True
            raise AmbiguousSubmission("bag-open result could not be verified") from exc
        if after.state is not PageState.BAG_READY:
            self._session = None
            self._blocked = True
            raise UnknownPageError("PDA 未确认开包，请检查 PDA 当前页面后重试")
        actual_bag = after.visible_labels.get("大包号", "")
        actual_sort = after.visible_labels.get("排序码", "")
        if actual_bag != config.container_no or actual_sort != config.sort_code:
            self._session = None
            self._blocked = True
            raise SubmissionBlocked("WMS container or sort code does not match configuration")
        self._require_single_empty_input(after)
        if not self._warehouse_ok(after):
            self._session = None
            self._blocked = True
            raise SubmissionBlocked("WMS warehouse does not match configuration")
        return self._issue_verified_session(actual_bag, actual_sort)

    def verify_current_or_open_bag(
        self,
        config: CarrierConfig,
        *,
        unresolved_submissions: int | None = None,
    ) -> VerifiedSession:
        """Read-only verify an existing bag, otherwise use the guarded open flow.

        With an explicit zero unresolved count, a different visible bag is left
        through verified Back steps (never closed) before opening the configured one.
        """
        if self._blocked:
            raise SubmissionBlocked("controller is blocked pending human reconciliation")
        if not config.enabled:
            raise SubmissionBlocked("carrier configuration is disabled")
        if not config.container_no.strip() or not config.sort_code.strip():
            raise SubmissionBlocked("container and sort code are required")
        observation = self.observe()
        if (
            self._session is None
            and not self._blocked
            and unresolved_submissions == 0
            and observation.state is PageState.UNKNOWN
        ):
            labels = observation.visible_labels
            inputs = observation.input_fields
            if (
                observation.keyboard_visible
                and observation.single_sort_without_dialog
                and not observation.dialog_text
                and not observation.logid
                and labels.get("排序码") == config.sort_code
                and labels.get("大包号", "") in ("", config.container_no)
                and labels.get("仓库", "") in ("", self.warehouse)
                and len(inputs) == 1
                and inputs[0].focused
                and inputs[0].editable
                and not inputs[0].text.strip()
            ):
                self._ensure_real_device_connected()
                self.adapter.press_key("BACK")
                observation = self.observe()
                if observation.keyboard_visible:
                    raise SubmissionBlocked("on-screen keyboard is still visible; close it on the PDA")
        if (
            self._session is None
            and not self._blocked
            and unresolved_submissions == 0
            and (observation.duplicate_in_bag or observation.grouping_dialog_tracking)
            and observation.visible_labels.get("排序码") == config.sort_code
            and observation.visible_labels.get("大包号", "") in ("", config.container_no)
            and observation.visible_labels.get("仓库", "") in ("", self.warehouse)
        ):
            # A leftover 已在当前大包中 / 组包中 dialog: one Back only closes it.
            self._ensure_real_device_connected()
            self.adapter.press_key("BACK")
            observation = self.observe()
        if (
            self._session is None
            and not self._blocked
            and unresolved_submissions == 0
            and observation.state is PageState.UNKNOWN
            and observation.grouping_inline_tracking
            and observation.visible_labels.get("排序码") == config.sort_code
            and observation.visible_labels.get("大包号", "") in ("", config.container_no)
            and observation.visible_labels.get("仓库", "") in ("", self.warehouse)
        ):
            # WMS already rejected this tracking as 组包中 (already grouped). Only the
            # leftover text is cleared; nothing is submitted.
            self._ensure_real_device_connected()
            field = observation.input_fields[0]
            if not field.focused:
                self.adapter.tap_field(field)
            self.adapter.clear_text()
            observation = self.observe()
            if observation.keyboard_visible and observation.single_sort_without_dialog:
                self.adapter.press_key("BACK")
                observation = self.observe()
        if (
            self._session is None
            and not self._blocked
            and unresolved_submissions == 0
            and observation.state is PageState.UNKNOWN
        ):
            observation = self._observe_until_changed(PageState.UNKNOWN)
        if (
            not self._blocked
            and unresolved_submissions == 0
            and observation.state is PageState.UNKNOWN
        ):
            observation = self._back_out_of_unknown_page()
        if (
            unresolved_submissions
            and observation.state in {PageState.BAG_READY, PageState.SCAN_RESULT}
            and self._shows_other_bag(observation, config)
        ):
            raise SubmissionBlocked("visible container does not match configuration")
        if (
            self._session is None
            and not unresolved_submissions
            and observation.state in _RETURN_HOME_STATES
            and not self._warehouse_ok(observation)
        ):
            observation = self._return_home(observation)
        if (
            self._session is None
            and unresolved_submissions == 0
            and observation.state in {PageState.BAG_READY, PageState.SCAN_RESULT}
            and self._shows_other_bag(observation, config)
        ):
            observation = self._return_home(observation)
        if self._session is None and observation.state is PageState.SINGLE_OPEN_FAILED:
            # No bag is open on this page; Back only leaves the failed entry.
            observation = self._return_home(observation)
        if observation.state in {PageState.BAG_READY, PageState.SCAN_RESULT}:
            self._require_warehouse(observation)
            visible_bag = observation.visible_labels.get("大包号", "")
            if visible_bag and visible_bag != config.container_no:
                raise SubmissionBlocked("visible container does not match configuration")
            if not visible_bag and (
                self._session is None or self._session.container_no != config.container_no
            ):
                raise SubmissionBlocked("visible container is required without a verified session")
            if observation.visible_labels.get("排序码") != config.sort_code:
                raise SubmissionBlocked("visible sort code does not match configuration")
            self._require_single_empty_input(observation)
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
        raise UnknownPageError(
            "PDA 当前页面无法识别，请回到首页、大包管理或单排序页后重试"
        )

    @staticmethod
    def _shows_other_bag(observation: PageObservation, config: CarrierConfig) -> bool:
        labels = observation.visible_labels
        visible_bag = labels.get("大包号", "")
        visible_sort = labels.get("排序码", "")
        return bool(
            (visible_bag and visible_bag != config.container_no)
            or (visible_sort and visible_sort != config.sort_code)
        )

    def _return_home(self, observation: PageObservation) -> PageObservation:
        """Verified Back steps to HOME so the warehouse can be read; never closes a bag."""
        if observation.state in {PageState.BAG_READY, PageState.SCAN_RESULT}:
            self._require_single_empty_input(observation)
        for _ in range(_MAX_RETURN_HOME_BACKS):
            self.adapter.press_key("BACK")
            observation = self._observe_until_changed(observation.state)
            if observation.state is PageState.HOME:
                return observation
            if observation.state not in _RETURN_HOME_STATES:
                self._blocked = True
                raise UnknownPageError("PDA 返回首页时遇到未知页面，请手动回到首页后重试")
        self._blocked = True
        raise UnknownPageError("PDA 未能返回首页，请手动回到首页后重试")

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
        self._require_single_empty_input(current)

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
                not self._bag_matches(after_back, session.container_no)
                or after_back.visible_labels.get("排序码") != session.sort_code
                or not self._warehouse_ok(after_back)
            ):
                self._session = None
                self._session_generation = None
                self._blocked = True
                raise SubmissionBlocked("bag context changed while leaving the current bag")
        self._session = None
        self._session_generation = None
        self._blocked = True
        raise UnknownPageError("Back navigation did not reach a stable empty single-scan page")

    def _focus_scan_input(
        self, field: InputField, session: VerifiedSession
    ) -> PageObservation:
        self.adapter.tap_field(field)
        focused = self.observe()
        if focused.keyboard_visible:
            focused = self._dismiss_keyboard(focused, session)
        self._require_session_page(focused, session)
        current = self._require_single_empty_input(focused)
        if not current.focused:
            raise UnknownPageError("exactly one focused input is required after tapping")
        return focused

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
        field = self._require_single_empty_input(before)
        try:
            focused = self._focus_scan_input(field, session)
        except (UnknownPageError, SubmissionBlocked) as exc:
            raise ScanNotSent(str(exc)) from exc
        if item.tracking in focused.latest_tracking_records:
            raise SubmissionBlocked("tracking already appears in the current latest record")
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
            after = self._observe_until_result(focused)
        except Exception:
            evidence = self._evidence(None, session, error="submission result could not be read")
            return ScanResult(
                attempt_id=attempt_id,
                item_id=item.id,
                tracking=item.tracking,
                outcome=ScanOutcome.UNKNOWN,
                evidence=evidence,
            )
        after = self._settle_grouping_dialog(after, focused, item.tracking)
        grouping = self._grouping_skip(item, session, attempt_id, after, focused)
        if grouping is not None:
            return grouping
        result = self._classify_scan_result(item, session, attempt_id, after, focused)
        if (
            result.outcome is ScanOutcome.UNKNOWN
            and self._session is session
            and after.keyboard_visible
            and after.safe_tap_point is not None
        ):
            # The keyboard can hide the result or dialog. Tapping a plain title hides it
            # without closing any WMS dialog; the page is then read again. Never a second Enter.
            try:
                self.adapter.tap(*after.safe_tap_point)
                reread = self._observe_until_result(focused)
                reread = self._settle_grouping_dialog(reread, focused, item.tracking)
            except Exception:
                pass
            else:
                grouping = self._grouping_skip(item, session, attempt_id, reread, focused)
                if grouping is not None:
                    return grouping
                result = self._classify_scan_result(item, session, attempt_id, reread, focused)
        if result.outcome is ScanOutcome.UNKNOWN:
            self._session = None
            self._session_generation = None
        return result

    def _grouping_skip(
        self,
        item: QueueItem,
        session: VerifiedSession,
        attempt_id: str,
        after: PageObservation,
        focused: PageObservation,
    ) -> ScanResult | None:
        """A fresh 组包中 dialog for this tracking: one Back, then skip the item."""
        if not (
            after.grouping_dialog_tracking == item.tracking
            and after.logid != focused.logid
            and after.visible_labels.get("排序码") == session.sort_code
            and after.visible_labels.get("大包号", session.container_no) == session.container_no
            and after.visible_labels.get("仓库", session.warehouse) == session.warehouse
        ):
            return None
        self._session = None
        self._session_generation = None
        try:
            self.adapter.press_key("BACK")
        except Exception:
            self._blocked = True
        return ScanResult(
            attempt_id=attempt_id,
            item_id=item.id,
            tracking=item.tracking,
            outcome=ScanOutcome.REJECTED,
            evidence=self._evidence(after, session, error="状态异常，状态为组包中"),
            skip_rejected_item=True,
        )

    def _settle_grouping_dialog(
        self, after: PageObservation, focused: PageObservation, tracking: str
    ) -> PageObservation:
        """Keep reading (never another Enter) while a fresh WMS error dialog is still rendering."""
        deadline = self._deadline()
        for attempt in range(self.poll_attempts):
            if (
                after.grouping_dialog_tracking == tracking
                or after.dialog_tracking == tracking
                or after.state is not PageState.UNKNOWN
                or not after.logid
                or after.logid == focused.logid
            ):
                return after
            if self._past(deadline):
                break
            if self.poll_interval_seconds:
                sleep(self.poll_interval_seconds)
            try:
                after = self.observe(reading_scan_result=True)
            except Exception:
                return after
        return after

    def _dismiss_keyboard(
        self, observation: PageObservation, session: VerifiedSession
    ) -> PageObservation:
        """Tap a verified bag page label outside the keyboard before text entry."""
        self._require_session_page(observation, session)
        if observation.safe_tap_point is None:
            raise SubmissionBlocked("on-screen keyboard has no safe outside tap target")
        self.adapter.tap(*observation.safe_tap_point)
        after = self.observe()
        if after.keyboard_visible:
            raise SubmissionBlocked("on-screen keyboard is still visible; close it on the PDA")
        self._require_session_page(after, session)
        field = self._require_single_empty_input(after)
        if not field.focused:
            self.adapter.tap_field(field)
            after = self.observe()
            if after.keyboard_visible:
                raise SubmissionBlocked(
                    "on-screen keyboard reopens on focus; disable it on the PDA"
                )
        return after

    def abandon_duplicate_recovery(self) -> None:
        """Give up on a duplicate dialog without typing; the bag must be re-verified."""
        self._pending_duplicate = None
        self._duplicate_recovery_attempted = False
        self._session = None
        self._session_generation = None
        self._blocked = False

    @property
    def active_session(self) -> VerifiedSession | None:
        return self._session

    def invalidate_session(self) -> None:
        """Invalidate after manual takeover, app restart, or an external disconnect."""
        self._session = None
        self._session_generation = None
        self._blocked = True
        self._warehouse_verified_for = _UNVERIFIED

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
        if (
            not current.duplicate_in_bag
            or current.logid != result.evidence.logid
            or current.dialog_tracking != result.tracking
        ):
            raise SubmissionBlocked("duplicate evidence does not match the current dialog")
        self._duplicate_recovery_attempted = True
        try:
            self.adapter.press_key("BACK")
            if current.keyboard_visible:
                after_back = self.observe()
                if (
                    after_back.state is PageState.DUPLICATE_DIALOG
                    and after_back.duplicate_in_bag
                    and after_back.logid == result.evidence.logid
                    and after_back.dialog_tracking == result.tracking
                    and not after_back.keyboard_visible
                ):
                    self.adapter.press_key("BACK")
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
            and self._bag_matches(after, session.container_no)
            and after.visible_labels.get("排序码") == session.sort_code
            and self._warehouse_ok(after)
            and not after.dialog_text
            and len(after.input_fields) == 1
            and not after.input_fields[0].text.strip()
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
        before: PageObservation,
    ) -> ScanResult:
        if (
            observation.state is PageState.DUPLICATE_DIALOG
            and observation.duplicate_in_bag
            and observation.logid
            and observation.dialog_tracking == item.tracking
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
        status_label = observation.visible_labels.get("扫描状态")
        no_failure_status = status_label in {None, "扫描成功", "Scan successful"}
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
            and before.latest_tracking_records[:1] != (item.tracking,)
            and self._session is session
            and self._warehouse_ok(observation)
            and self._bag_matches(observation, session.container_no)
            and actual_sort == session.sort_code
            and no_failure_status
            and not observation.dialog_text
            and not observation.logid
        ):
            return ScanResult(
                attempt_id=attempt_id,
                item_id=item.id,
                tracking=item.tracking,
                outcome=ScanOutcome.SUCCESS,
                evidence=ScanEvidence(
                    observed_at_utc=datetime.now(UTC),
                    latest_tracking=observation.latest_tracking_records[0],
                    # The result page may scroll the bag card away; it then comes
                    # from this controller's still-valid verified session.
                    container_no=actual_container or session.container_no,
                    sort_code=actual_sort,
                    redacted_xml_ref=observation.redacted_xml_ref,
                ),
            )
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
        if not self._bag_matches(observation, session.container_no):
            self._session = None
            self._session_generation = None
            self._blocked = True
            raise SubmissionBlocked("visible container does not match verified session")
        if observation.visible_labels.get("排序码") != session.sort_code:
            self._session = None
            self._session_generation = None
            self._blocked = True
            raise SubmissionBlocked("visible sort code does not match verified session")
        if not self._warehouse_ok(observation):
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
    def _bag_matches(observation: PageObservation, container_no: str) -> bool:
        """A visible bag must match; only a result page may omit it (session-backed)."""
        visible = observation.visible_labels.get("大包号", "")
        if visible:
            return visible == container_no
        return observation.state is PageState.SCAN_RESULT

    @staticmethod
    def _unique_empty_field(observation: PageObservation):
        if observation.state is not PageState.SINGLE_EMPTY:
            raise UnknownPageError("expected the empty single-scan page")
        return WmsController._require_single_empty_input(observation)

    @staticmethod
    def _require_single_empty_input(observation: PageObservation, *, dialog_only: bool = False):
        fields = observation.dialog_inputs if dialog_only else observation.input_fields
        if len(fields) != 1:
            raise UnknownPageError("exactly one enabled input is required")
        field = fields[0]
        if not field.editable or field.text.strip():
            raise UnknownPageError("input must be editable and empty")
        return field

    @staticmethod
    def _require_focused_same_field(previous, fields) -> None:
        focused = [field for field in fields if field.focused]
        if len(focused) != 1:
            raise UnknownPageError("exactly one focused input is required after tapping")
        WmsController._require_same_field(previous, focused[0])
        if not focused[0].editable or focused[0].text.strip():
            raise UnknownPageError("focused input must be editable and empty")

    def _deadline(self) -> float | None:
        if self.timeout_seconds is None:
            return None
        return monotonic() + self.timeout_seconds

    def _past(self, deadline: float | None) -> bool:
        return deadline is not None and monotonic() >= deadline

    def _back_out_of_unknown_page(self) -> PageObservation:
        """An unreadable leftover page (e.g. a failed scan) is left by Back only.

        Only called with zero unresolved submissions. The first Back may just hide
        the keyboard, so at most two are sent; nothing is typed or closed.
        """
        self._ensure_real_device_connected()
        self._session = None
        self._session_generation = None
        observation = self.observe()
        for _ in range(2):
            self.adapter.press_key("BACK")
            observation = self._observe_until_changed(PageState.UNKNOWN)
            if observation.state is not PageState.UNKNOWN:
                break
        return observation

    def _observe_until_changed(self, previous: PageState) -> PageObservation:
        """Wait for an identified page using read-only polling."""
        deadline = self._deadline()
        for attempt in range(self.poll_attempts):
            state = self.detector.detect(self.adapter.snapshot_xml()).state
            if state is not previous and state is not PageState.UNKNOWN:
                return self.observe()
            if self._past(deadline):
                break
            if attempt + 1 < self.poll_attempts and self.poll_interval_seconds:
                sleep(self.poll_interval_seconds)
        return self.observe()

    def _observe_until_state(self, target: PageState) -> PageObservation:
        """Read-only polling after one navigation tap; never taps again.

        Transient loading pages are read without side effects; the target page
        is then confirmed through the full observe() checks.
        """
        deadline = self._deadline()
        for attempt in range(self.poll_attempts):
            if self.detector.detect(self.adapter.snapshot_xml()).state is target:
                return self.observe()
            if self._past(deadline):
                break
            if attempt + 1 < self.poll_attempts and self.poll_interval_seconds:
                sleep(self.poll_interval_seconds)
        return self.observe()

    @staticmethod
    def _result_signature(observation: PageObservation) -> tuple:
        return (
            observation.state,
            tuple(observation.latest_tracking_records),
            observation.logid,
            observation.dialog_text,
            observation.dialog_tracking,
            observation.visible_labels.get("扫描状态"),
        )

    def _observe_until_result(self, before: PageObservation) -> PageObservation:
        before_signature = self._result_signature(before)
        deadline = self._deadline()
        for attempt in range(self.poll_attempts):
            observation = self.observe(reading_scan_result=True)
            if (
                observation.state is not PageState.BAG_READY
                and self._result_signature(observation) != before_signature
            ):
                return observation
            if self._past(deadline):
                break
            if attempt + 1 < self.poll_attempts and self.poll_interval_seconds:
                sleep(self.poll_interval_seconds)
        return observation

    def _observe_after_recovery(self, session: VerifiedSession) -> PageObservation:
        deadline = self._deadline()
        observation = self.observe()
        for attempt in range(self.poll_attempts):
            if observation.state in {PageState.BAG_READY, PageState.SCAN_RESULT}:
                if (
                    not self._bag_matches(observation, session.container_no)
                    or observation.visible_labels.get("排序码") != session.sort_code
                    or not self._warehouse_ok(observation)
                ):
                    return observation
                if (
                    len(observation.input_fields) == 1
                    and not observation.input_fields[0].text.strip()
                ):
                    return observation
            elif observation.state is not PageState.DUPLICATE_DIALOG:
                return observation
            if self._past(deadline):
                break
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

    def _warehouse_ok(self, observation: PageObservation) -> bool:
        visible = observation.visible_labels.get("仓库", "")
        if visible:
            return visible == self.warehouse
        return (
            self._warehouse_verified_for is not _UNVERIFIED
            and self._warehouse_verified_for == getattr(self.adapter, "session_generation", None)
        )

    def _require_warehouse(self, observation: PageObservation) -> None:
        if observation.state is PageState.HOME:
            if observation.visible_labels.get("仓库", "") != self.warehouse:
                self._warehouse_verified_for = _UNVERIFIED
                raise SubmissionBlocked("home warehouse does not match configuration")
            self._warehouse_verified_for = getattr(self.adapter, "session_generation", None)
            return
        if not self._warehouse_ok(observation):
            raise SubmissionBlocked(
                "warehouse must match configuration or be verified on the home page first"
            )

    def _reveal_home_target(
        self, observation: PageObservation, exact_texts: tuple[str, ...]
    ) -> None:
        """Swipe the home list up (navigation only) until one entry is visible."""
        for _ in range(_MAX_HOME_SWIPES + 1):
            snapshot = self.adapter.snapshot_xml()
            try:
                self.detector.find_unique_target(
                    snapshot, expected_state=PageState.HOME, exact_texts=exact_texts
                )
                return
            except Exception:
                pass
            observation = self.detector.detect(snapshot)
            area = observation.scroll_area
            if observation.state is not PageState.HOME or area is None:
                return
            left, top, right, bottom = area
            x = (left + right) // 2
            height = bottom - top
            self.adapter.swipe(x, bottom - height // 5, x, top + height // 5)

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
            after = self._observe_until_state(next_state)
        except ScreenOffError:
            self._blocked = True
            raise
        except Exception as exc:
            self._blocked = True
            raise UnknownPageError("navigation result could not be verified") from exc
        if after.state is not next_state:
            self._blocked = True
            raise UnknownPageError("PDA 页面未加载到预期页面，请检查 PDA 当前页面后重试")
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


_MIXED_NAVIGATION_STATES = frozenset(
    {PageState.MIXED_EMPTY, PageState.CONTAINER_MENU, PageState.HOME}
)


class MixedSortController(WmsController):
    """混合排序: each item is its tracking, then (once the WMS sort code matches) its bag.

    No bag is opened or closed. The verified session binds the device, warehouse and the
    item's configured bag/sort; every submission re-verifies the empty 混合排序 page.
    """

    sort_mode = "mixed"

    def observe(self, *, reading_scan_result: bool = False) -> PageObservation:
        verified_for = self._warehouse_verified_for
        session, generation, blocked = self._session, self._session_generation, self._blocked
        observation = super().observe(reading_scan_result=reading_scan_result)
        if reading_scan_result and session is not None:
            # A frame still loading mid-submission is not a context change (a changed
            # device/app generation raises above); the steps re-check the page anyway.
            self._session, self._session_generation, self._blocked = (
                session,
                generation,
                blocked,
            )
        if (
            reading_scan_result
            or observation.mixed_inline_grouping_tracking
            or observation.mixed_failed_input_text
            or observation.mixed_grouping_tracking
        ):
            # Same device session (a changed generation raises above): a result frame
            # still rendering or a known 组包中 message keeps the HOME warehouse check.
            self._warehouse_verified_for = verified_for
        return observation

    def open_bag(self, config: CarrierConfig) -> VerifiedSession:
        return self.verify_current_or_open_bag(config, unresolved_submissions=0)

    def switch_bag(
        self, config: CarrierConfig, *, unresolved_submissions: int
    ) -> VerifiedSession:
        """Nothing is open in 混合排序; only the page is re-verified for the new config."""
        if unresolved_submissions != 0:
            raise SubmissionBlocked("cannot switch bags with unresolved submissions")
        self._drop_session()
        return self.verify_current_or_open_bag(config, unresolved_submissions=0)

    def verify_current_or_open_bag(
        self,
        config: CarrierConfig,
        *,
        unresolved_submissions: int | None = None,
    ) -> VerifiedSession:
        if self._blocked:
            raise SubmissionBlocked("controller is blocked pending human reconciliation")
        if not config.enabled:
            raise SubmissionBlocked("carrier configuration is disabled")
        if not config.container_no.strip() or not config.sort_code.strip():
            raise SubmissionBlocked("container and sort code are required")
        self._ensure_real_authorized()
        observation = self.observe()
        if (
            self._session is None
            and unresolved_submissions == 0
            and (
                observation.mixed_inline_grouping_tracking
                or observation.mixed_failed_input_text
            )
        ):
            # WMS already rejected this tracking (组包中 or a scan failure); only the
            # leftover text is cleared. Nothing is submitted.
            self._ensure_real_device_connected()
            field = observation.input_fields[0]
            if not field.focused:
                self.adapter.tap_field(field)
            self.adapter.clear_text()
            observation = self.observe()
        if observation.state is PageState.UNKNOWN and unresolved_submissions == 0:
            observation = self._observe_until_changed(PageState.UNKNOWN)
        if observation.state is PageState.UNKNOWN and unresolved_submissions == 0:
            observation = self._back_out_of_unknown_page()
        if (
            self._session is None
            and unresolved_submissions == 0
            and observation.state is PageState.MIXED_AWAIT_BAG
        ):
            # A previous item stopped before its bag (it stays UNKNOWN, never retried).
            # Back only leaves the page (the first Back may just hide the keyboard);
            # no bag is typed.
            self._ensure_real_device_connected()
            for _ in range(3):
                self.adapter.press_key("BACK")
                observation = self._observe_until_changed(PageState.MIXED_AWAIT_BAG)
                if observation.state is not PageState.MIXED_AWAIT_BAG:
                    break
            if observation.state not in _MIXED_NAVIGATION_STATES:
                self._blocked = True
                raise UnknownPageError("PDA 未能离开等待大包号页面，请手动回到混合排序空页后重试")
        if observation.state in {
            PageState.MIXED_EMPTY,
            PageState.CONTAINER_MENU,
        } and not self._warehouse_ok(observation):
            observation = self._back_to_home(observation)
        if observation.state is PageState.HOME:
            self._require_warehouse(observation)
            self._reveal_home_target(observation, ("组大包", "大包管理"))
            observation = self._tap_navigation_target(
                expected_state=PageState.HOME,
                exact_texts=("组大包", "大包管理"),
                next_state=PageState.CONTAINER_MENU,
            )
        if observation.state is PageState.CONTAINER_MENU:
            observation = self._tap_navigation_target(
                expected_state=PageState.CONTAINER_MENU,
                exact_texts=("混合排序",),
                next_state=PageState.MIXED_EMPTY,
            )
        if observation.state is not PageState.MIXED_EMPTY:
            self._drop_session()
            raise UnknownPageError(
                "PDA 当前页面无法识别，请回到首页、大包管理或混合排序页后重试"
            )
        self._require_warehouse(observation)
        self._require_single_empty_input(observation)
        self._ensure_real_device_connected()
        session = self._session
        if (
            session is not None
            and session.container_no == config.container_no
            and session.sort_code == config.sort_code
        ):
            self._validate_session_token(session)
            return session
        return self._issue_verified_session(config.container_no, config.sort_code)

    def _back_to_home(self, observation: PageObservation) -> PageObservation:
        """Verified Back steps (never closing anything) so HOME can verify the warehouse."""
        for _ in range(_MAX_RETURN_HOME_BACKS):
            self.adapter.press_key("BACK")
            observation = self._observe_until_changed(observation.state)
            if observation.state is PageState.HOME:
                return observation
            if observation.state not in _MIXED_NAVIGATION_STATES:
                self._blocked = True
                raise UnknownPageError("PDA 返回首页时遇到未知页面，请手动回到首页后重试")
        self._blocked = True
        raise UnknownPageError("PDA 未能返回首页，请手动回到首页后重试")

    def recover_duplicate(
        self, result: ScanResult, session: VerifiedSession
    ) -> RecoveryResult:
        raise SubmissionBlocked("混合排序不产生待恢复的重复提交")

    def _require_mixed_page(
        self,
        observation: PageObservation,
        expected: PageState,
        session: VerifiedSession,
    ) -> None:
        if observation.state is not expected:
            raise UnknownPageError(f"expected the {expected.value} page")
        if not self._warehouse_ok(observation):
            raise SubmissionBlocked("warehouse is not verified for this device session")
        self._validate_session_token(session)

    def _focus_mixed_input(
        self,
        observation: PageObservation,
        expected: PageState,
        session: VerifiedSession,
    ) -> PageObservation:
        """Tap the unique empty input and verify it remains safe to type into."""
        field = self._require_single_empty_input(observation)
        keyboard_was_visible = observation.keyboard_visible
        if not field.focused:
            self.adapter.tap_field(field)
            observation = self.observe(reading_scan_result=True)
        # The keyboard may stay up: 混合排序 evidence is the sort code before the bag
        # and the scanned count after it, and text goes into the focused input.
        self._require_mixed_page(observation, expected, session)
        current = self._require_single_empty_input(observation)
        if (
            not keyboard_was_visible
            and observation.keyboard_visible
            and not field.resource_id
            and not current.resource_id
            and field.class_name == current.class_name
            and field.bounds[::2] == current.bounds[::2]
            and current.bounds[1] < field.bounds[1]
            and field.bounds[1] - current.bounds[1] == field.bounds[3] - current.bounds[3]
        ):
            # Android moved the anonymous input vertically to make room for the keyboard.
            field = current
        self._require_focused_same_field(field, observation.input_fields)
        return observation

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

        try:
            before = self.observe(reading_scan_result=True)
            self._require_mixed_page(before, PageState.MIXED_EMPTY, session)
            if before.mixed_scanned_count is None:
                raise UnknownPageError("混合排序 scanned count is not visible")
            focused = self._focus_mixed_input(before, PageState.MIXED_EMPTY, session)
        except (UnknownPageError, SubmissionBlocked) as exc:
            self._drop_session()
            raise ScanNotSent(str(exc)) from exc
        try:
            self.adapter.enter_text(item.tracking)
        except Exception as exc:
            self._blocked = True
            self._drop_session()
            raise SubmissionBlocked("tracking entry failed before submission") from exc

        # From this Enter onward any error is ambiguous; nothing is ever re-sent.
        try:
            self.adapter.press_key("ENTER")
            await_bag = self._poll_mixed(
                lambda page: page.state is PageState.MIXED_AWAIT_BAG
                or bool(page.mixed_grouping_tracking)
                or self._inline_tracking_error(page) == item.tracking
            )
        except Exception:
            return self._mixed_unknown(item, session, attempt_id, None, "运单扫描结果无法读取")

        if self._inline_tracking_error(await_bag) == item.tracking:
            return self._skip_after_inline_error(item, attempt_id, await_bag)

        if (
            await_bag.mixed_grouping_tracking == item.tracking
            and await_bag.logid
            and await_bag.logid != focused.logid
        ):
            self._settle_after_grouping(item.tracking)
            return ScanResult(
                attempt_id=attempt_id,
                item_id=item.id,
                tracking=item.tracking,
                outcome=ScanOutcome.REJECTED,
                evidence=ScanEvidence(
                    observed_at_utc=datetime.now(UTC),
                    latest_tracking=item.tracking,
                    error_text=(
                        ALREADY_GROUPED_REASON
                        if "状态为已组包" in await_bag.dialog_text
                        else "WMS 包裹组包状态异常"
                    ),
                    logid=await_bag.logid,
                    redacted_xml_ref=await_bag.redacted_xml_ref,
                ),
                skip_rejected_item=True,
            )
        if (
            await_bag.state is not PageState.MIXED_AWAIT_BAG
            or await_bag.mixed_tracking not in (item.tracking, "")
        ):
            return self._mixed_unknown(
                item, session, attempt_id, await_bag, "WMS 未显示该运单的排序码"
            )
        wms_sort = await_bag.visible_labels.get("排序码", "")
        if wms_sort != item.sort_code:
            self._drop_session()
            return ScanResult(
                attempt_id=attempt_id,
                item_id=item.id,
                tracking=item.tracking,
                outcome=ScanOutcome.REJECTED,
                evidence=ScanEvidence(
                    observed_at_utc=datetime.now(UTC),
                    latest_tracking=item.tracking,
                    sort_code=wms_sort,
                    error_text=(
                        f"WMS 排序码 {wms_sort} 与配置 {item.sort_code} 不符，未输入大包号"
                    ),
                    redacted_xml_ref=await_bag.redacted_xml_ref,
                ),
            )

        try:
            ready = self._focus_mixed_input(await_bag, PageState.MIXED_AWAIT_BAG, session)
            if (
                ready.mixed_tracking not in (item.tracking, "")
                or ready.visible_labels.get("排序码") != item.sort_code
            ):
                raise UnknownPageError("tracking or sort code changed before the bag entry")
            self.adapter.enter_text(session.container_no)
        except Exception as exc:
            return self._mixed_unknown(
                item, session, attempt_id, await_bag, f"大包号未输入：{exc}"
            )
        try:
            self.adapter.press_key("ENTER")
            after = self._poll_mixed(
                lambda page: page.state is PageState.MIXED_EMPTY
                or page.mixed_grouping_tracking == item.tracking
                or bool(page.dialog_text and page.logid)
                or page.closed_container_cancel_point is not None
            )
        except Exception:
            return self._mixed_unknown(item, session, attempt_id, None, "大包扫描结果无法读取")

        if after.closed_container_cancel_point is not None:
            return self._cancel_closed_container(item, session, attempt_id, after)

        if (
            after.mixed_grouping_tracking == item.tracking
            and "状态为已组包" in after.dialog_text
            and after.logid
            and after.logid != ready.logid
        ):
            self._settle_after_grouping(item.tracking)
            return ScanResult(
                attempt_id=attempt_id,
                item_id=item.id,
                tracking=item.tracking,
                outcome=ScanOutcome.REJECTED,
                evidence=ScanEvidence(
                    observed_at_utc=datetime.now(UTC),
                    latest_tracking=item.tracking,
                    error_text=ALREADY_GROUPED_REASON,
                    logid=after.logid,
                    redacted_xml_ref=after.redacted_xml_ref,
                ),
                skip_rejected_item=True,
            )

        if (
            after.state is PageState.MIXED_EMPTY
            and after.mixed_scanned_count == before.mixed_scanned_count + 1
            and after.logid in (None, before.logid)
            and self._warehouse_ok(after)
        ):
            return ScanResult(
                attempt_id=attempt_id,
                item_id=item.id,
                tracking=item.tracking,
                outcome=ScanOutcome.SUCCESS,
                evidence=ScanEvidence(
                    observed_at_utc=datetime.now(UTC),
                    latest_tracking=item.tracking,
                    # 混合排序 never echoes the bag: it is the session-bound bag just typed.
                    container_no=session.container_no,
                    sort_code=wms_sort,
                    redacted_xml_ref=after.redacted_xml_ref,
                ),
            )
        return self._mixed_unknown(
            item, session, attempt_id, after, after.dialog_text or "扫描的包裹数量未增加"
        )

    def _cancel_closed_container(
        self,
        item: QueueItem,
        session: VerifiedSession,
        attempt_id: str,
        page: PageObservation,
    ) -> ScanResult:
        """The typed bag is closed: tap 取消 (never reopen) and block for a new bag."""
        self._drop_session()
        try:
            self.adapter.tap(*page.closed_container_cancel_point)
        except Exception:
            self._blocked = True
        return ScanResult(
            attempt_id=attempt_id,
            item_id=item.id,
            tracking=item.tracking,
            outcome=ScanOutcome.REJECTED,
            evidence=ScanEvidence(
                observed_at_utc=datetime.now(UTC),
                latest_tracking=item.tracking,
                container_no=session.container_no,
                error_text=f"大包 {session.container_no} 已关闭，已取消重新打开，请输入新的大包号",
                redacted_xml_ref=page.redacted_xml_ref,
            ),
        )

    @staticmethod
    def _inline_tracking_error(page: PageObservation) -> str:
        return page.mixed_inline_grouping_tracking or page.mixed_failed_input_text

    def _skip_after_inline_error(
        self, item: QueueItem, attempt_id: str, page: PageObservation
    ) -> ScanResult:
        """WMS refused the tracking before any bag was typed and left it in the input:
        clear only that text, keep the session on the confirmed empty page, skip the item."""
        error = (
            "状态异常，状态为组包中"
            if page.mixed_inline_grouping_tracking
            else "WMS 扫描失败，未输入大包号"
        )
        try:
            field = page.input_fields[0]
            if not field.focused:
                self.adapter.tap_field(field)
            self.adapter.clear_text()
            after = self._poll_mixed(
                lambda current: current.state is PageState.MIXED_EMPTY
                or current.mixed_grouping_tracking == item.tracking
            )
            if after.mixed_grouping_tracking == item.tracking:
                self._settle_after_grouping(item.tracking)
            elif after.state is not PageState.MIXED_EMPTY or not self._warehouse_ok(after):
                self._drop_session()
        except Exception:
            self._drop_session()
        return ScanResult(
            attempt_id=attempt_id,
            item_id=item.id,
            tracking=item.tracking,
            outcome=ScanOutcome.REJECTED,
            evidence=ScanEvidence(
                observed_at_utc=datetime.now(UTC),
                latest_tracking=item.tracking,
                error_text=error,
                logid=page.logid,
                redacted_xml_ref=page.redacted_xml_ref,
            ),
            skip_rejected_item=True,
        )

    def _settle_after_grouping(self, tracking: str) -> None:
        """Close the grouping status dialog with Back, clear only the leftover tracking text and
        keep the session when the empty 混合排序 page is confirmed. Nothing is submitted."""
        try:
            self.adapter.press_key("BACK")
            leftover = lambda page: (  # noqa: E731
                page.state is PageState.MIXED_EMPTY
                or self._inline_tracking_error(page) == tracking
                or (page.mixed_grouping_tracking == tracking and not page.keyboard_visible)
            )
            page = self._poll_mixed(leftover)
            if page.mixed_grouping_tracking == tracking and not page.keyboard_visible:
                # The first Back only hid the keyboard; one more closes the dialog.
                self.adapter.press_key("BACK")
                page = self._poll_mixed(leftover)
            if self._inline_tracking_error(page) == tracking:
                field = page.input_fields[0]
                if not field.focused:
                    self.adapter.tap_field(field)
                self.adapter.clear_text()
                page = self._poll_mixed(lambda current: current.state is PageState.MIXED_EMPTY)
            if page.state is not PageState.MIXED_EMPTY or not self._warehouse_ok(page):
                self._drop_session()
        except Exception:
            self._drop_session()

    def _poll_mixed(self, done) -> PageObservation:
        deadline = self._deadline()
        observation = self.observe(reading_scan_result=True)
        for attempt in range(self.poll_attempts):
            if done(observation):
                return observation
            if self._past(deadline) or attempt + 1 >= self.poll_attempts:
                break
            if self.poll_interval_seconds:
                sleep(self.poll_interval_seconds)
            observation = self.observe(reading_scan_result=True)
        return observation

    def _drop_session(self) -> None:
        self._session = None
        self._session_generation = None

    def _mixed_unknown(
        self,
        item: QueueItem,
        session: VerifiedSession,
        attempt_id: str,
        observation: PageObservation | None,
        error: str,
    ) -> ScanResult:
        self._drop_session()
        return ScanResult(
            attempt_id=attempt_id,
            item_id=item.id,
            tracking=item.tracking,
            outcome=ScanOutcome.UNKNOWN,
            evidence=ScanEvidence(
                observed_at_utc=datetime.now(UTC),
                latest_tracking=(observation.mixed_tracking or None) if observation else None,
                container_no=session.container_no,
                sort_code=observation.visible_labels.get("排序码") if observation else None,
                error_text=error,
                logid=observation.logid if observation else None,
                redacted_xml_ref=observation.redacted_xml_ref if observation else "",
            ),
        )

