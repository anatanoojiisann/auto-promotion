SCHEMA = """
CREATE TABLE IF NOT EXISTS content_queue_entries(
 id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,
 platform TEXT NOT NULL,payload TEXT NOT NULL,context TEXT NOT NULL,
 content_hash TEXT NOT NULL,created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS content_queue_batches(
 id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,request TEXT NOT NULL,
 task_id TEXT UNIQUE,created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS content_queue_members(
 content_id TEXT PRIMARY KEY REFERENCES content_queue_entries(id),
 batch_id TEXT NOT NULL REFERENCES content_queue_batches(id),position INTEGER NOT NULL,
 UNIQUE(batch_id,position)
);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS content_producer_links(
 source_kind TEXT NOT NULL,source_id TEXT NOT NULL,source_revision TEXT NOT NULL,
 content_id TEXT NOT NULL REFERENCES content_queue_entries(id),
 PRIMARY KEY(source_kind,source_id,source_revision)
);
CREATE TABLE IF NOT EXISTS content_production_runs(
 id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,request TEXT NOT NULL,
 profile_snapshot TEXT NOT NULL,created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS content_production_items(
 run_id TEXT NOT NULL REFERENCES content_production_runs(id),position INTEGER NOT NULL,
 source_id TEXT NOT NULL,source_snapshot TEXT NOT NULL,state TEXT NOT NULL,
 owner TEXT,lease_until REAL NOT NULL DEFAULT 0,content_id TEXT REFERENCES content_queue_entries(id),reason TEXT,
 PRIMARY KEY(run_id,position)
);
"""
