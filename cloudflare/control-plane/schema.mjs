// Execution evidence lives on the queue row; operator audit and historical
// quotas are preserved separately. Only due-time and attempt-time are indexed.
export const JOB_COLUMNS = "id,kind,reference_id,task_id,account_id,pool,version,payload,state,available_at,expires_at,owner,lease_token,lease_until,attempt_id,result";
const JOBS = `
 CREATE TABLE jobs(
   id TEXT PRIMARY KEY,kind TEXT NOT NULL,reference_id TEXT NOT NULL,
   task_id TEXT NOT NULL,account_id TEXT NOT NULL,pool TEXT NOT NULL,
   version TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,
   available_at REAL NOT NULL,expires_at REAL NOT NULL,owner TEXT,lease_token TEXT,
   lease_until REAL NOT NULL DEFAULT 0,attempt_id TEXT,result TEXT NOT NULL DEFAULT '{}',
   claimed_at REAL,attempt_at REAL,completed_at REAL,
   quota_scopes TEXT NOT NULL DEFAULT '[]',
   account_pacing_until REAL NOT NULL DEFAULT 0,task_pacing_until REAL NOT NULL DEFAULT 0
 ) WITHOUT ROWID;
 CREATE INDEX jobs_due ON jobs(pool,available_at,id) WHERE state='pending';
 CREATE INDEX jobs_attempt_time ON jobs(attempt_at) WHERE attempt_at IS NOT NULL;
`;
const LEDGER_VIEW = `
 CREATE VIEW quota_events AS
 SELECT attempt_id,scope,scope_key,MAX(at) AS at FROM (
   SELECT attempt_id,scope,scope_key,at FROM quota_history
   UNION ALL
   SELECT j.attempt_id,json_extract(s.value,'$.scope'),json_extract(s.value,'$.key'),j.attempt_at
   FROM jobs j,json_each(j.quota_scopes) s WHERE j.attempt_at IS NOT NULL
 ) GROUP BY attempt_id,scope,scope_key;
`;
export const SCHEMA = `
 CREATE TABLE config(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
 INSERT INTO config VALUES(1,'{"global_stop":true,"live_enabled":false,"max_workers":6,"global_limit":{"daily":100,"hourly":20},"platform_workers":{},"pool_workers":{}}');
 CREATE TABLE accounts(id TEXT PRIMARY KEY,identity TEXT NOT NULL,platform TEXT NOT NULL,payload TEXT NOT NULL,next_at REAL NOT NULL DEFAULT 0,cooldown_until REAL NOT NULL DEFAULT 0,disabled INTEGER NOT NULL DEFAULT 0);
 CREATE INDEX account_identity ON accounts(identity);
 CREATE TABLE tasks(id TEXT PRIMARY KEY,state TEXT NOT NULL,phase TEXT NOT NULL,version TEXT NOT NULL,payload TEXT NOT NULL);
 ${JOBS}
 CREATE TABLE task_pacing(task_id TEXT PRIMARY KEY,next_at REAL NOT NULL);
 CREATE TABLE snapshot_cursor(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL);
 CREATE TABLE nodes(id TEXT PRIMARY KEY,token_hash TEXT NOT NULL,pools TEXT NOT NULL,accounts TEXT NOT NULL,enabled INTEGER NOT NULL,last_seen REAL NOT NULL DEFAULT 0);
 CREATE TABLE quota_history(attempt_id TEXT NOT NULL,scope TEXT NOT NULL,scope_key TEXT NOT NULL,at REAL NOT NULL,PRIMARY KEY(attempt_id,scope,scope_key));
 CREATE INDEX quota_window ON quota_history(scope,scope_key,at);
 CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,kind TEXT NOT NULL,job_id TEXT,details TEXT NOT NULL);
 ${LEDGER_VIEW}
`;

export function migrate(storage) {
  // Atomic DDL/copy: exhaustion rolls everything back and execution stays closed.
  // Retain the original job rows as read-only migration evidence, including
  // owners/tokens, unknown attempts and native schedules. Never replay the archive.
  storage.transactionSync(() => {
    storage.sql.exec(`
      ALTER TABLE jobs RENAME TO jobs_v1_archive;
      DROP INDEX jobs_due;
      DROP INDEX jobs_owner;
      DROP INDEX jobs_task;
      ${JOBS}
      INSERT INTO jobs(${JOB_COLUMNS}) SELECT ${JOB_COLUMNS} FROM jobs_v1_archive;
      ALTER TABLE quota_events RENAME TO quota_history;
      ${LEDGER_VIEW}
    `);
  });
}
