"""Additive migration; no existing task, manifest, receipt or credential is rewritten."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS hf_policies(
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL,platform TEXT NOT NULL,policy_key TEXT NOT NULL,
 version INTEGER NOT NULL,count_epoch INTEGER NOT NULL DEFAULT 1,config TEXT NOT NULL,
 empty_streak INTEGER NOT NULL DEFAULT 0,last_settled_seq INTEGER NOT NULL DEFAULT 0,
 next_scan_at REAL NOT NULL DEFAULT 0,last_complete_at REAL,last_p3_at REAL,
 active_run_id TEXT,pending_trigger INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,
 UNIQUE(project_id,platform,policy_key)
);
CREATE TABLE IF NOT EXISTS hf_config_versions(
 policy_id TEXT NOT NULL,version INTEGER NOT NULL,config TEXT NOT NULL,created_at REAL NOT NULL,
 PRIMARY KEY(policy_id,version)
);
CREATE TABLE IF NOT EXISTS hf_scan_runs(
 id TEXT PRIMARY KEY,policy_id TEXT NOT NULL,seq INTEGER NOT NULL,config_version INTEGER NOT NULL,
 count_epoch INTEGER NOT NULL,config TEXT NOT NULL,upper_at REAL NOT NULL,
 started_at REAL NOT NULL,finished_at REAL,deadline_at REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'collecting',coverage_incomplete INTEGER NOT NULL DEFAULT 0,
 error TEXT,settled_at REAL,new_high INTEGER NOT NULL DEFAULT 0,
 fetched INTEGER NOT NULL DEFAULT 0,duplicates INTEGER NOT NULL DEFAULT 0,
 UNIQUE(policy_id,seq)
);
CREATE TABLE IF NOT EXISTS hf_query_progress(
 run_id TEXT NOT NULL,query TEXT NOT NULL,lower_at REAL NOT NULL,upper_at REAL NOT NULL,
 cursor TEXT,pages INTEGER NOT NULL DEFAULT 0,complete INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(run_id,query)
);
CREATE TABLE IF NOT EXISTS hf_watermarks(
 policy_id TEXT NOT NULL,query TEXT NOT NULL,watermark REAL NOT NULL,PRIMARY KEY(policy_id,query)
);
CREATE TABLE IF NOT EXISTS hf_candidates(
 id TEXT PRIMARY KEY,policy_id TEXT NOT NULL,project_id TEXT NOT NULL,platform TEXT NOT NULL,
 source_event_id TEXT NOT NULL,action_kind TEXT NOT NULL DEFAULT 'reply',source TEXT NOT NULL,
 source_hash TEXT NOT NULL,source_event_at REAL NOT NULL,first_seen_at REAL NOT NULL,
 expires_at REAL,priority TEXT,state TEXT NOT NULL DEFAULT 'DISCOVERED',version INTEGER NOT NULL DEFAULT 1,
 classification TEXT NOT NULL DEFAULT '{}',config_version INTEGER NOT NULL DEFAULT 0,
 owner_account_id TEXT,channel_eligibility TEXT NOT NULL DEFAULT 'unknown',block_reason TEXT,
 draft TEXT NOT NULL DEFAULT '',draft_origin TEXT,media_plan TEXT NOT NULL DEFAULT '{}',
 selected_version INTEGER,selected_at REAL,selected_by TEXT,defer_until REAL,
 batch_id TEXT,p3_assessment_id TEXT,task_id TEXT,item_id TEXT,
 external_reference TEXT,published_at REAL,verification_started_at REAL,
 verification_polls INTEGER NOT NULL DEFAULT 0,attention_at REAL,
 closed_reason TEXT,updated_at REAL NOT NULL,
 UNIQUE(project_id,platform,source_event_id,action_kind)
);
CREATE INDEX IF NOT EXISTS hf_candidates_policy ON hf_candidates(policy_id,state,priority);
CREATE TABLE IF NOT EXISTS hf_scan_candidates(
 run_id TEXT NOT NULL,candidate_id TEXT NOT NULL,source_hash TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',priority TEXT,error TEXT,
 PRIMARY KEY(run_id,candidate_id)
);
CREATE TABLE IF NOT EXISTS hf_admissions(
 candidate_id TEXT NOT NULL,source_hash TEXT NOT NULL,created_at REAL NOT NULL,
 PRIMARY KEY(candidate_id,source_hash)
);
CREATE TABLE IF NOT EXISTS hf_batches(
 id TEXT PRIMARY KEY,policy_id TEXT NOT NULL,opened_at REAL NOT NULL,due_at REAL NOT NULL,
 config_version INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'open',sealed_at REAL,delivered_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS hf_one_open_batch ON hf_batches(policy_id) WHERE state='open';
CREATE TABLE IF NOT EXISTS hf_batch_members(
 batch_id TEXT NOT NULL,candidate_id TEXT NOT NULL,PRIMARY KEY(batch_id,candidate_id)
);
CREATE TABLE IF NOT EXISTS hf_assessments(
 id TEXT PRIMARY KEY,policy_id TEXT NOT NULL,created_at REAL NOT NULL,count_epoch INTEGER NOT NULL,
 candidates TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'assessed'
);
CREATE TABLE IF NOT EXISTS hf_notices(
 id TEXT PRIMARY KEY,policy_id TEXT NOT NULL,dedupe_key TEXT NOT NULL,channel TEXT NOT NULL,
 notice_type TEXT NOT NULL,batch_id TEXT,state TEXT NOT NULL DEFAULT 'pending',revision INTEGER NOT NULL DEFAULT 1,
 snapshot TEXT NOT NULL,body TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,
 message_id TEXT,created_at REAL NOT NULL,delivered_at REAL,error TEXT,
 UNIQUE(dedupe_key,channel)
);
CREATE TABLE IF NOT EXISTS hf_notice_attempts(
 notice_id TEXT NOT NULL,revision INTEGER NOT NULL,attempt INTEGER NOT NULL,
 started_at REAL NOT NULL,finished_at REAL,outcome TEXT NOT NULL,body TEXT NOT NULL,
 message_id TEXT,PRIMARY KEY(notice_id,revision,attempt)
);
CREATE TABLE IF NOT EXISTS hf_decisions(
 id INTEGER PRIMARY KEY AUTOINCREMENT,candidate_id TEXT NOT NULL,version INTEGER NOT NULL,
 action TEXT NOT NULL,actor TEXT NOT NULL,at REAL NOT NULL,details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hf_generation(
 id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,version INTEGER NOT NULL,kind TEXT NOT NULL,
 state TEXT NOT NULL,provider TEXT NOT NULL,provider_job_id TEXT,source TEXT,
 real_product_output INTEGER NOT NULL DEFAULT 0,parameters TEXT NOT NULL,
 started_at REAL NOT NULL,finished_at REAL,estimated_cost REAL,actual_cost REAL,asset TEXT,
 UNIQUE(candidate_id,version,kind)
);
CREATE TABLE IF NOT EXISTS hf_usage(
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL,kind TEXT NOT NULL,at REAL NOT NULL,
 duration_ms INTEGER,estimated_cost REAL,actual_cost REAL,details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hf_usage_budget ON hf_usage(project_id,kind,at);
CREATE TABLE IF NOT EXISTS hf_observations(
 candidate_id TEXT NOT NULL,offset_seconds INTEGER NOT NULL,due_at REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',observed_at REAL,metrics TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(candidate_id,offset_seconds)
);
CREATE TABLE IF NOT EXISTS hf_publish_retries(
 candidate_id TEXT NOT NULL,sequence INTEGER NOT NULL,item_id TEXT NOT NULL,created_at REAL NOT NULL,
 PRIMARY KEY(candidate_id,sequence),UNIQUE(item_id)
);
CREATE TABLE IF NOT EXISTS interaction_intents(
 project_id TEXT NOT NULL,platform TEXT NOT NULL,source_event_id TEXT NOT NULL,action_kind TEXT NOT NULL,
 candidate_id TEXT,item_id TEXT,state TEXT NOT NULL,created_at REAL NOT NULL,
 PRIMARY KEY(project_id,platform,source_event_id,action_kind)
);
INSERT OR IGNORE INTO meta VALUES('hf_schema_version','1');
"""
