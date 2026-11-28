# Project Implementation Rules

- Treat `TT_Auto_Bag_Scanner_Development.md` as the feature and acceptance specification.
- Owner decision (2026-11-27): the app has no simulator mode; it runs real-device mode only. `FakeDeviceAdapter` is a test double only, injected via `AppController(adapter_factory=...)`; `tests/conftest.py` keeps every test off real ADB. Do not send commands to a real PDA or WMS during automated tests.
- Never close containers, dispatch parcels, mutate inventory, save WMS credentials, or retry an ambiguous submission.
- A real submission requires a valid per-item physical check, a verified device/session/container/sort, and an authorized explicit start.
- Owner-approved exception (2026-11-27): when the 设置 option "跳过实物核验" is on (default off), the saved setting is the authorization: on start, each waiting item gets an `authorized_skip` record under the configured operator, which stands in for the physical check. Keep it opt-in and auditable; never enable it by default, skip the record, or allow it without an operator name.
- Persist `IN_FLIGHT` and its attempt before sending one Enter. Convert uncertain outcomes to `UNKNOWN` for human reconciliation.
- Owner decision (2026-11-27): an `UNKNOWN` item does not pause the task. The task continues with the next item and may switch bags (Back navigation only, never closing); `UNKNOWN` items stay listed for later reconciliation and are never retried automatically. `IN_FLIGHT` and pending duplicate recoveries still block, except a recovery refused before anything was typed: it is recorded with a `recovery not attempted:` reason, the item stays `ALREADY_IN_BAG`, and the next item re-verifies the bag. Every submission still re-verifies the bag page first.
- Owner decision (2026-11-27): 扫描工作区「排序模式」 selects 单排序 (default) or 混合排序 (`MixedSortController`). In 混合排序 the bag is typed only after the WMS-shown sort code equals the item's configured sort code; a mismatch is REJECTED and blocks the task. Success requires the 扫描的包裹 count to rise by exactly one.
- Do not hardcode sample shipment numbers, device serials, container numbers, or carrier mappings.
- Write and run a focused failing test before production code. Keep test fixtures synthetic or redacted.
- Keep UI pages concise with functional labels and status only. Do not add explanatory paragraphs.
- Keep app data under `%LOCALAPPDATA%/TT-Auto-Bag-Scanner/`, outside the repository and synchronized folders.
- Do not batch-delete project files. Preserve existing files and unrelated changes.

