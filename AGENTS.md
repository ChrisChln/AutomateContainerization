# Project Implementation Rules

- Treat `TT_Auto_Bag_Scanner_Development.md` as the feature and acceptance specification.
- Keep simulator mode isolated and default. Do not send commands to a real PDA or WMS during automated tests.
- Never close containers, dispatch parcels, mutate inventory, save WMS credentials, or retry an ambiguous submission.
- A real submission requires a valid per-item physical check, a verified device/session/container/sort, and an authorized explicit start.
- Owner-approved exception (2026-11-27): when the 设置 option "跳过实物核验" is on (default off), the saved setting is the authorization: on start, each waiting item gets an `authorized_skip` record under the configured operator, which stands in for the physical check. Keep it opt-in and auditable; never enable it by default, skip the record, or allow it without an operator name.
- Persist `IN_FLIGHT` and its attempt before sending one Enter. Convert uncertain outcomes to `UNKNOWN` and pause for human reconciliation.
- Do not hardcode sample shipment numbers, device serials, container numbers, or carrier mappings.
- Write and run a focused failing test before production code. Keep test fixtures synthetic or redacted.
- Keep UI pages concise with functional labels and status only. Do not add explanatory paragraphs.
- Keep app data under `%LOCALAPPDATA%/TT-Auto-Bag-Scanner/`, outside the repository and synchronized folders.
- Do not batch-delete project files. Preserve existing files and unrelated changes.

