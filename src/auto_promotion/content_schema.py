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
