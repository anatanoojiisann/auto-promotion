import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from "miniflare";

test("production kernel costs six SQLite writes per ordinary completed job", {timeout:60000}, async () => {
  const schema = await readFile(new URL("../schema.mjs",import.meta.url),"utf8");
  const source = await readFile(new URL("../kernel.mjs",import.meta.url),"utf8");
  const dependency = 'import { SCHEMA, migrate } from "./schema.mjs";';
  assert.ok(source.includes(dependency));
  const script = `import { DurableObject } from 'cloudflare:workers';
${schema.replace(/^export /gm, "")}
${source.replace(dependency, "").replace(/^export /gm, "")}
export class Meter extends DurableObject {
  fetch(request) {
    let written=0,read=0,now=1800000000;
    const storage={sql:{exec:(q,...args)=>{
      const c=this.ctx.storage.sql.exec(q,...args),rows=c.toArray();
      written+=c.rowsWritten;read+=c.rowsRead;return {toArray:()=>rows};
    }},transactionSync:fn=>this.ctx.storage.transactionSync(fn)};
    if(new URL(request.url).pathname==='/migration') {
      storage.sql.exec(LEGACY_SCHEMA);
      for(const state of ['pending','unknown','scheduled','leased']) {
        storage.sql.exec('INSERT INTO jobs(id,kind,reference_id,task_id,account_id,pool,version,payload,state,available_at,expires_at,owner,lease_token,lease_until,attempt_id,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          state,'send',state,'t','a','x.edge','frozen','{"text":"immutable synthetic"}',state,now,now+900,'n','synthetic-token',now+180,state==='pending'?null:'attempt-'+state,'{}');
      }
      storage.sql.exec('INSERT INTO quota_events VALUES(?,?,?,?)','attempt-unknown','cloud_global','live',now);
      const upgraded=new ControlKernel(storage,{clock:()=>now});
      const previous=written;
      new ControlKernel(storage,{clock:()=>now});
      return Response.json({schema:upgraded.status().schema,rows:upgraded.rows('SELECT id,state,attempt_id,payload,lease_token FROM jobs ORDER BY id'),archive:upgraded.one('SELECT COUNT(*) n FROM jobs_v1_archive').n,quota:upgraded.one('SELECT COUNT(*) n FROM quota_events').n,stopped:upgraded.settings().global_stop,reopen_writes:written-previous});
    }
    const k=new ControlKernel(storage,{clock:()=>now});
    k.configure({global_stop:false,live_enabled:true,max_workers:6,global_limit:{daily:100000,hourly:100000}});
    const account={id:'a',identity:'x:a',platform:'x',mode:'live',limits:{daily:100000,hourly:100000},min_interval:0,allowed_actions:['reply']};
    const task={id:'t',name:'synthetic',account_ids:['a'],version:'policy',state:'active',phase:'ready',manifest_hash:'frozen',policy:{starts_at:new Date((now-1)*1000).toISOString(),ends_at:new Date((now+7200)*1000).toISOString(),daily_limit:100000,min_interval:0,pacing_scope:'account',ordering_mode:'strict'}};
    k.sync({revision:1,accounts:[account],tasks:[task],jobs:[]});
    k.registerNode({id:'n',token_hash:'a'.repeat(64),pools:['x.edge'],account_ids:['a']});
    const node=k.node('n','a'.repeat(64));
    const scopes=['global','account','task','target','user','platform'].map(scope=>({scope,key:canonical(['live',scope==='platform'?'x':scope]),limit:{daily:100000,hourly:100000}}));
    const stages=[];
    const stage=(name,fn)=>{const w=written,r=read;const result=fn();stages.push({name,writes:written-w,reads:read-r});return result;};
    const job={id:'send:j',kind:'send',reference_id:'j',task_id:'t',account_id:'a',pool:'x.edge',action:'reply',position:0,version:'body',payload:{action:'reply',execution_mode:'automatic',target:'https://example.invalid/1',text:'synthetic'},state:'not_attempted',available_at:now+60,expires_at:now+900,scopes};
    stage('enqueue',()=>k.sync({revision:1,accounts:[],tasks:[],jobs:[job]}));
    stage('wait',()=>k.claim(node,'x.edge',job.id));now+=60;
    const lease=stage('claim',()=>k.claim(node,'x.edge',job.id));
    stage('authorize',()=>k.authorize(node,job.id,lease.lease_token,'attempt'));
    stage('repeat_authorize',()=>k.authorize(node,job.id,lease.lease_token,'attempt'));
    now+=30;stage('early_renew',()=>k.renew(node,job.id,lease.lease_token));
    const outcome={state:'confirmed',attempt_id:'attempt',result:{verified:true,external_reference:'https://example.invalid/receipt'}};
    stage('receipt',()=>k.complete(node,job.id,lease.lease_token,outcome));
    stage('repeat_receipt',()=>k.complete(node,job.id,lease.lease_token,outcome));
    const events=k.rows('SELECT * FROM quota_events').map(e=>({attempt_id:e.attempt_id,scope:e.scope,key:e.scope_key,at:e.at}));
    stage('echo_snapshot_and_quotas',()=>k.sync({revision:1,accounts:[account],tasks:[task],jobs:[{...job,...outcome}],quota_events:events}));
    const before={written,read};
    for(let i=1;i<=500;i++) {
      const j={...job,id:'send:bulk-'+i,reference_id:'bulk-'+i,position:i,available_at:now};
      k.sync({revision:1,accounts:[],tasks:[],jobs:[j]});
      const l=k.claim(node,'x.edge',j.id);if(!l)throw new Error('bulk claim failed '+i);
      k.authorize(node,j.id,l.lease_token,'bulk-'+i);
      k.complete(node,j.id,l.lease_token,{...outcome,attempt_id:'bulk-'+i});
    }
    return Response.json({stages,quota_dimensions:events.length,bulk_500:{writes:written-before.written,reads:read-before.read},excludes:'Shared setup, sync revisions, node heartbeat, alarms, exceptional outcomes and migration; no external platform calls.'});
  }
}
export default {fetch(){return new Response('Isolated test')}};`;
  const mf=new Miniflare(convertV4MiniflareOptions({name:"six-write-test",modules:true,script,compatibilityDate:"2025-09-01",durableObjects:{METER:{className:"Meter",useSQLite:true}},cf:false,log:new Log(LogLevel.ERROR)}));
  try {
    const ns=await mf.getDurableObjectNamespace("METER");
    const response=await ns.get(ns.idFromName("meter")).fetch("https://local/measure");
    assert.equal(response.status,200,await response.clone().text());
    const data=await response.json();
    assert.deepEqual(data.stages.map(s=>[s.name,s.writes]),[
      ["enqueue",2],["wait",0],["claim",1],["authorize",2],["repeat_authorize",0],
      ["early_renew",0],["receipt",1],["repeat_receipt",0],["echo_snapshot_and_quotas",0],
    ]);
    assert.equal(data.quota_dimensions,9);
    assert.equal(data.bulk_500.writes,3000);
    assert.ok(data.bulk_500.reads<5000000,"Read amplification must also fit the free daily allowance in this fixture");
    const migrationResponse=await ns.get(ns.idFromName("migration")).fetch("https://local/migration");
    assert.equal(migrationResponse.status,200,await migrationResponse.clone().text());
    const migration=await migrationResponse.json();
    assert.equal(migration.schema,2);
    assert.equal(migration.archive,4);
    assert.equal(migration.quota,1);
    assert.equal(migration.stopped,true);
    assert.equal(migration.reopen_writes,0);
    assert.deepEqual(migration.rows.map(r=>r.state),["leased","pending","scheduled","unknown"]);
    assert.ok(migration.rows.every(r=>r.lease_token==="synthetic-token"&&r.payload==='{"text":"immutable synthetic"}'));
    console.log(JSON.stringify({sqlite_write_budget:data,migration:{rows_preserved:4,quota_preserved:true,reopen_writes:0}}));
  } finally {await mf.dispose();}
});
