CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at_utc TEXT NOT NULL
);

CREATE TABLE imports (
    id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    imported_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0)
);

CREATE TABLE import_rows (
    id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    row_no INTEGER NOT NULL CHECK (row_no > 0),
    raw_json TEXT NOT NULL,
    tracking TEXT,
    carrier TEXT,
    status_raw TEXT,
    outbound_utc TEXT,
    exclude_reason TEXT,
    UNIQUE (import_id, row_no)
);

CREATE TABLE carrier_configs (
    id TEXT PRIMARY KEY,
    carrier_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    sort_code TEXT NOT NULL,
    bag_no TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    version INTEGER NOT NULL CHECK (version > 0),
    updated_at_utc TEXT NOT NULL,
    UNIQUE (carrier_key, sort_code)
);

CREATE UNIQUE INDEX idx_carrier_configs_normalized
    ON carrier_configs(lower(carrier_key), lower(sort_code));

CREATE TABLE carrier_config_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id TEXT NOT NULL,
    carrier_key TEXT NOT NULL,
    sort_code TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT NOT NULL,
    at_utc TEXT NOT NULL
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    import_id TEXT REFERENCES imports(id),
    name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('draft','ready','running','pausing','paused','blocked','completed','stopped')),
    device_mode TEXT NOT NULL DEFAULT 'simulated' CHECK (device_mode IN ('simulated','real')),
    real_mode_authorized INTEGER NOT NULL DEFAULT 0 CHECK (real_mode_authorized IN (0,1)),
    source_hash TEXT NOT NULL DEFAULT '',
    config_snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE task_items (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tracking TEXT NOT NULL,
    carrier_key TEXT NOT NULL,
    sort_code TEXT NOT NULL,
    container_no TEXT NOT NULL,
    outbound_at_utc TEXT NOT NULL,
    source_row_numbers_json TEXT NOT NULL,
    queue_index INTEGER NOT NULL CHECK (queue_index >= 0),
    status TEXT NOT NULL CHECK (status IN ('wait_physical','ready','in_flight','success','already_in_bag','unknown','rejected','cancelled')),
    physical_check_id TEXT,
    source_hash TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE (task_id, tracking),
    UNIQUE (task_id, queue_index)
);

CREATE TABLE physical_checks (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES task_items(id),
    tracking TEXT NOT NULL,
    container_no TEXT NOT NULL,
    operator TEXT NOT NULL,
    checked_at_utc TEXT NOT NULL,
    method TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    revoked_at_utc TEXT
);

CREATE INDEX idx_physical_checks_current ON physical_checks(item_id, revoked_at_utc);

CREATE TABLE item_sources (
    item_id TEXT NOT NULL REFERENCES task_items(id) ON DELETE CASCADE,
    import_id TEXT NOT NULL REFERENCES imports(id),
    row_no INTEGER NOT NULL,
    PRIMARY KEY (item_id, import_id, row_no),
    FOREIGN KEY (import_id, row_no) REFERENCES import_rows(import_id, row_no)
);

CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES task_items(id),
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    status TEXT NOT NULL CHECK (status IN ('in_flight','success','already_in_bag','unknown','rejected')),
    session_id TEXT NOT NULL,
    device_serial TEXT NOT NULL,
    warehouse TEXT NOT NULL,
    container_no TEXT NOT NULL,
    sort_code TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    outcome TEXT,
    before_json TEXT NOT NULL,
    after_json TEXT,
    logid TEXT,
    error_code TEXT,
    recovery_status TEXT NOT NULL DEFAULT 'not_required',
    UNIQUE (item_id, attempt_no)
);

CREATE INDEX idx_attempts_status ON attempts(status);

CREATE TABLE duplicate_recoveries (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),
    status TEXT NOT NULL CHECK (status IN ('pending','in_flight','recovered','failed','unknown')),
    tracking TEXT NOT NULL,
    container_no TEXT NOT NULL,
    logid TEXT,
    evidence_ref TEXT NOT NULL DEFAULT '',
    observed_at_utc TEXT,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    item_id TEXT REFERENCES task_items(id),
    attempt_id TEXT REFERENCES attempts(id),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    at_utc TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE INDEX idx_events_task_time ON events(task_id, at_utc, id);

CREATE TABLE logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(id),
    item_id TEXT REFERENCES task_items(id),
    attempt_id TEXT REFERENCES attempts(id),
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    at_utc TEXT NOT NULL
);

CREATE TRIGGER one_in_flight_item_per_task
BEFORE UPDATE OF status ON task_items
WHEN NEW.status = 'in_flight'
 AND EXISTS (
     SELECT 1 FROM task_items current
     WHERE current.task_id = NEW.task_id
       AND current.status = 'in_flight'
       AND current.id <> NEW.id
 )
BEGIN
    SELECT RAISE(ABORT, 'task already has an in-flight item');
END;

INSERT INTO schema_migrations(version, applied_at_utc)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
