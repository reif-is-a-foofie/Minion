-- Anonymous aggregate rows only (no queries, paths, or raw install_id).

CREATE TABLE telemetry_row (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at INTEGER NOT NULL,
  event TEXT NOT NULL,
  install_bucket TEXT,
  app_version TEXT,
  os TEXT,
  arch TEXT,
  python TEXT,
  mode TEXT,
  rerank TEXT,
  returned INTEGER,
  top_k INTEGER,
  has_kind_filter INTEGER,
  has_path_glob INTEGER,
  has_role_filter INTEGER,
  has_query INTEGER,
  hit_kinds TEXT,
  file_kind TEXT,
  parser TEXT,
  chunks INTEGER,
  skipped INTEGER,
  result TEXT,
  reason_class TEXT
);

CREATE INDEX idx_telemetry_received ON telemetry_row (received_at);
CREATE INDEX idx_telemetry_event ON telemetry_row (event);
