"""Credential-free desktop connections and immutable migration provenance."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_browser_accounts(
 account_id TEXT PRIMARY KEY REFERENCES accounts(id),external_id TEXT NOT NULL UNIQUE,
 identity TEXT NOT NULL,profile_url TEXT NOT NULL,connector TEXT NOT NULL,browser TEXT NOT NULL,
 details TEXT NOT NULL DEFAULT '{}',observation TEXT NOT NULL DEFAULT '{}',updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta_browser_sources(
 id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),path TEXT NOT NULL,
 task_id TEXT NOT NULL REFERENCES tasks(id),fingerprint TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'unknown',details TEXT NOT NULL DEFAULT '{}',updated_at REAL NOT NULL,
 UNIQUE(account_id,path)
);
CREATE TABLE IF NOT EXISTS meta_browser_history(
 source_id TEXT NOT NULL REFERENCES meta_browser_sources(id),external_id TEXT NOT NULL,
 item_id TEXT NOT NULL UNIQUE REFERENCES items(id),details TEXT NOT NULL,
 PRIMARY KEY(source_id,external_id)
);
CREATE TABLE IF NOT EXISTS meta_browser_contacts(
 platform TEXT NOT NULL,event_key TEXT NOT NULL,item_id TEXT NOT NULL REFERENCES items(id),
 account_id TEXT NOT NULL REFERENCES accounts(id),state TEXT NOT NULL,
 PRIMARY KEY(platform,event_key)
);
CREATE TABLE IF NOT EXISTS meta_browser_claims(
 item_id TEXT PRIMARY KEY REFERENCES items(id),owner TEXT NOT NULL,job TEXT NOT NULL,
 context TEXT NOT NULL,claim TEXT NOT NULL,proof TEXT NOT NULL,
 expires_at REAL NOT NULL,submission_started INTEGER NOT NULL DEFAULT 0,
 result TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta_browser_checks(
 item_id TEXT PRIMARY KEY REFERENCES items(id),checked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tiktok_dm_collections(
 account_id TEXT PRIMARY KEY REFERENCES accounts(id),connected_at REAL NOT NULL,
 collected_at REAL NOT NULL,coverage TEXT NOT NULL,source_url TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tiktok_dm_conversations(
 account_id TEXT NOT NULL REFERENCES accounts(id),conversation_id TEXT NOT NULL,
 peer_username TEXT NOT NULL,source_url TEXT NOT NULL,coverage TEXT NOT NULL,
 first_seen REAL NOT NULL,last_seen REAL NOT NULL,
 PRIMARY KEY(account_id,conversation_id)
);
CREATE TABLE IF NOT EXISTS tiktok_dm_messages(
 account_id TEXT NOT NULL,conversation_id TEXT NOT NULL,message_key TEXT NOT NULL,
 message_id TEXT NOT NULL,direction TEXT NOT NULL,sender_username TEXT NOT NULL,
 text TEXT NOT NULL,occurred_at REAL,timestamp_label TEXT NOT NULL,id_verified INTEGER NOT NULL,
 first_seen REAL NOT NULL,last_seen REAL NOT NULL,
 PRIMARY KEY(account_id,conversation_id,message_key),
 FOREIGN KEY(account_id,conversation_id) REFERENCES tiktok_dm_conversations(account_id,conversation_id)
);
CREATE TABLE IF NOT EXISTS tiktok_dm_sent_receipts(
 item_id TEXT PRIMARY KEY REFERENCES items(id),account_id TEXT NOT NULL REFERENCES accounts(id),
 conversation_id TEXT NOT NULL,message_id TEXT NOT NULL,verified_at REAL NOT NULL,
 UNIQUE(account_id,conversation_id,message_id)
);
"""
