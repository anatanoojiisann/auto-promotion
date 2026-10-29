const {test}=require("node:test"), assert=require("node:assert/strict");
const ui=require("../src/auto_promotion/web/tiktok-messages-desktop.js");
function fixture(id){
 let clicks=0,fills=0;
 const connection={identity:"web_test",browser:"tabbit",instance_id:"A1"};
 const identityProof={...connection,identity_verified:true,authenticated_menu_verified:true,observed_at:Date.now()/1000};
 const messages=[{message_id:"100",id_verified:true,direction:"inbound",text:"Question"},
   {message_id:"200",id_verified:true,direction:"outbound",text:"Old reply"}];
 const snapshot={source_url:"https://www.tiktok.com/messages?scene=business",inbox_verified:true,observed_at:Date.now()/1000,
   recipient_id:"peer_one",recipient_verified:true,conversation_id:"0:1:9:10",conversation_verified:true,
   conversations:[{messages}],editor_available:true,editor_text:"",can_message:true,send_control:{e2e:"dm-send"}};
 const item={item_id:id,payload_hash:"frozen",payload:{action:"send_direct_message",target:"https://www.tiktok.com/@peer_one",
   text:"Exact private draft",recipient_id:"peer_one",tiktok_conversation_id:"0:1:9:10",tiktok_message_id:"",media:[],title:""}};
 const frame={url:()=>snapshot.source_url,evaluate:async()=>snapshot,locator:()=>({count:async()=>1,
   fill:async t=>{fills++;snapshot.editor_text=t;},click:async()=>{clicks++;}})};
 const page={url:()=>"https://www.tiktok.com/business-suite/messages",frames:()=>[frame]};
 const permit={allowed:true,submit_once:true,platform:"tiktok",item_id:id,payload_hash:"frozen",target:item.payload.target,
   identity:connection.identity,browser:"tabbit",instance_id:"A1",expires_at:Date.now()/1000+60};
 return {page,frame,snapshot,item,connection,identityProof,permit,counts:()=>({clicks,fills})};
}
test("collecting or drafting never submits",async()=>{
 const f=fixture("draft");await ui.read(f.page,f.connection,f.identityProof);
 const ready=await ui.prepare(f.page,f.item,f.connection,f.identityProof);
 assert.equal(ui.proof(ready,f.item).recipient_id,"peer_one");assert.deepEqual(f.counts(),{clicks:0,fills:1});
});
test("unrelated private draft is preserved",async()=>{
 const f=fixture("existing");f.snapshot.editor_text="User private draft";
 await assert.rejects(ui.prepare(f.page,f.item,f.connection,f.identityProof));assert.deepEqual(f.counts(),{clicks:0,fills:0});
});
test("wrong identity, recipient, conversation and changed text never send",async()=>{
 for(const change of [{recipient_id:"other"},{conversation_id:"0:1:3:4"},{editor_text:"changed"},{can_message:false}]){
  const f=fixture("mismatch-"+JSON.stringify(change));await ui.prepare(f.page,f.item,f.connection,f.identityProof);
  Object.assign(f.snapshot,change);await assert.rejects(ui.submit(f.page,f.item,f.connection,f.identityProof,f.permit));assert.equal(f.counts().clicks,0);
 }
 const f=fixture("identity");await ui.prepare(f.page,f.item,f.connection,f.identityProof);
 await assert.rejects(ui.submit(f.page,f.item,f.connection,{...f.identityProof,identity:"other"},f.permit));assert.equal(f.counts().clicks,0);
});
test("missing, expired or changed frozen permit never sends",async()=>{
 const f=fixture("permit");await ui.prepare(f.page,f.item,f.connection,f.identityProof);
 for(const change of [{allowed:false},{expires_at:0},{payload_hash:"changed"},{target:"other"},{instance_id:"other"}])
  await assert.rejects(ui.submit(f.page,f.item,f.connection,f.identityProof,{...f.permit,...change}));
 assert.equal(f.counts().clicks,0);
});
test("parallel clicks use one permit once and click stays unknown",async()=>{
 const f=fixture("once");await ui.prepare(f.page,f.item,f.connection,f.identityProof);
 const results=await Promise.allSettled([ui.submit(f.page,f.item,f.connection,f.identityProof,f.permit),ui.submit(f.page,f.item,f.connection,f.identityProof,f.permit)]);
 assert.equal(results.filter(x=>x.status==="fulfilled").length,1);assert.equal(f.counts().clicks,1);
 assert.equal(results.find(x=>x.status==="fulfilled").value.status,"unknown");
});
test("send timeout cannot replay",async()=>{
 const f=fixture("timeout");await ui.prepare(f.page,f.item,f.connection,f.identityProof);let clicks=0;
 f.frame.locator=()=>({count:async()=>1,click:async()=>{clicks++;throw new Error("Timeout");}});
 await assert.rejects(ui.submit(f.page,f.item,f.connection,f.identityProof,f.permit));
 await assert.rejects(ui.submit(f.page,f.item,f.connection,f.identityProof,f.permit));assert.equal(clicks,1);
});
test("new messages before click invalidate the prepared context",async()=>{
 const f=fixture("changed");const prepared=await ui.prepare(f.page,f.item,f.connection,f.identityProof);
 f.snapshot.conversations[0].messages.push({message_id:"250",id_verified:true,direction:"inbound",text:"New question"});
 assert.throws(()=>ui.proof(prepared,f.item));await assert.rejects(ui.submit(f.page,f.item,f.connection,f.identityProof,f.permit));assert.equal(f.counts().clicks,0);
});
test("receipt independently matches only new native outgoing message",async()=>{
 const f=fixture("receipt");await ui.prepare(f.page,f.item,f.connection,f.identityProof);
 assert.equal((await ui.verify(f.page,f.item,f.connection,f.identityProof)).status,"unknown");
 f.snapshot.conversations[0].messages.push({message_id:"300",id_verified:true,direction:"inbound",text:f.item.payload.text});
 assert.equal((await ui.verify(f.page,f.item,f.connection,f.identityProof)).status,"unknown");
 f.snapshot.conversations[0].messages.push({message_id:"400",id_verified:true,direction:"outbound",text:f.item.payload.text,occurred_at:null});
 const result=await ui.verify(f.page,f.item,f.connection,f.identityProof);assert.equal(result.status,"confirmed");assert.equal(result.message_id,"400");
 f.snapshot.conversations[0].messages.push({message_id:"500",id_verified:true,direction:"outbound",text:f.item.payload.text});
 assert.equal((await ui.verify(f.page,f.item,f.connection,f.identityProof)).status,"unknown");
});
test("non-TikTok or ambiguous message frames are rejected",async()=>{
 const f=fixture("frames");f.page.frames=()=>[f.frame,f.frame];await assert.rejects(ui.read(f.page,f.connection,f.identityProof));
 f.page.frames=()=>[f.frame];f.page.url=()=>"https://evil.example/messages";await assert.rejects(ui.read(f.page,f.connection,f.identityProof));
});
