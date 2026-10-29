const {test} = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../src/auto_promotion/web/tiktok-desktop.js");

function fixture(id = "item-1") {
  let clicks = 0, fills = 0;
  const snapshot = {identity:"web_test",site_verified:true, authenticated_navigation_verified:true,video_id:"123",
    editor_available:true, editor_text:"Helpful answer",post_enabled:true};
  const item = {item_id:id,payload_hash:"frozen-hash",payload:{action:"comment",text:"Helpful answer",
    target:"https://www.tiktok.com/@creator/video/123",tiktok_video_id:"123",media:[],title:""}};
  const connection = {identity:"web_test",browser:"tabbit",instance_id:"A1"};
  const permit = {allowed:true,submit_once:true,platform:"tiktok",item_id:id,payload_hash:item.payload_hash,
    target:item.payload.target,identity:connection.identity,browser:connection.browser,instance_id:"A1",
    expires_at:Date.now()/1000+60};
  const page = {evaluate:async()=>snapshot,locator:()=>({count:async()=>1,
    click:async()=>{clicks++;},fill:async text=>{fills++;snapshot.editor_text=text;}})};
  return {page,item,connection,permit,snapshot,counts:()=>({clicks,fills})};
}

test("no missing, expired or mismatched guard may click",async()=>{
  const f=fixture("invalid");
  for(const change of [{allowed:false},{expires_at:0},{item_id:"other"},{payload_hash:"changed"},
    {identity:"other"},{browser:"chrome"},{instance_id:"other"},{target:"different"}]) {
    await assert.rejects(ui.submitComment(f.page,f.item,f.connection,{...f.permit,...change}));
  }
  assert.equal(f.counts().clicks,0);
});

test("page identity, video, editor or parent changes block clicking",async()=>{
  for(const change of [{identity:"other"},{video_id:"999"},{editor_text:"changed"},
    {post_enabled:false},{authenticated_navigation_verified:false},{site_verified:false}]) {
    const f=fixture("context-"+JSON.stringify(change));Object.assign(f.snapshot,change);
    await assert.rejects(ui.submitComment(f.page,f.item,f.connection,f.permit));
    assert.equal(f.counts().clicks,0);
  }
  const f=fixture("reply");f.item.payload.action="reply";
  await assert.rejects(ui.submitComment(f.page,f.item,f.connection,f.permit));
  assert.equal(f.counts().clicks,0);
});

test("one click returns unknown until independent verification and cannot replay",async()=>{
  const f=fixture("once");
  assert.equal((await ui.submitComment(f.page,f.item,f.connection,f.permit)).status,"unknown");
  await assert.rejects(ui.submitComment(f.page,f.item,f.connection,f.permit));
  assert.equal(f.counts().clicks,1);
});

test("a click timeout cannot replay",async()=>{
  const f=fixture("timeout");let clicks=0;
  f.page.locator=()=>({count:async()=>1,click:async()=>{clicks++;throw new Error("Timeout");}});
  await assert.rejects(ui.submitComment(f.page,f.item,f.connection,f.permit));
  await assert.rejects(ui.submitComment(f.page,f.item,f.connection,f.permit));
  assert.equal(clicks,1);
});

test("parallel use of the same permit clicks at most once",async()=>{
  const f=fixture("parallel");
  const results=await Promise.allSettled([ui.submit(f.page,f.item,f.connection,f.permit),
    ui.submit(f.page,f.item,f.connection,f.permit)]);
  assert.equal(results.filter(r=>r.status==="fulfilled").length,1);
  assert.equal(f.counts().clicks,1);
});

test("drafting never submits and preserves unrelated existing text",async()=>{
  const f=fixture("draft");f.snapshot.editor_text="another draft";
  await assert.rejects(ui.prepareComment(f.page,f.item,f.connection));
  assert.deepEqual(f.counts(),{clicks:0,fills:0});
  f.snapshot.editor_text="";
  await ui.prepareComment(f.page,f.item,f.connection);
  assert.deepEqual(f.counts(),{clicks:0,fills:1});
});

test("reply needs independently verified parent and uses its inline Post button",async()=>{
  const f=fixture("verified-reply"); f.item.payload.action="reply";
  f.item.payload.tiktok_comment_id="444";
  const parent={comment_id:"444",target:f.item.payload.target,text:"Source question",
    author_profile:"/@question_author",id_verified:true,independent_read:true,
    evidence:"Verified comment permalink and author",observed_at:Date.now()/1000};
  const comment={text:parent.text,author_profile:parent.author_profile,reply_editor_available:false,
    reply_editor_text:"",reply_post_enabled:false};
  f.snapshot.comments=[comment];let posts=0,replyClicks=0;
  f.page.locator=selector=>{
    if(selector==='[data-e2e="comment-level-1"]') return {evaluateAll:async()=>[0],
      nth:()=>({locator:()=>({locator:()=>({click:async()=>{replyClicks++;comment.reply_editor_available=true;}})})})};
    if(selector.includes('comment-reply-input'))return {waitFor:async()=>{},count:async()=>1,
      fill:async text=>{comment.reply_editor_text=text;comment.reply_post_enabled=true;}};
    if(selector==='[data-e2e="comment-reply-post"]')return {count:async()=>1,click:async()=>{posts++;}};
    throw new Error("Unexpected selector: "+selector);
  };
  await assert.rejects(ui.prepareReply(f.page,f.item,f.connection,{...parent,id_verified:false}));
  assert.equal(replyClicks,0);
  await ui.prepareReply(f.page,f.item,f.connection,parent);
  assert.equal(posts,0);
  const result=await ui.submit(f.page,f.item,f.connection,f.permit);
  assert.equal(result.status,"unknown");assert.equal(posts,1);assert.equal(replyClicks,1);
});

test("reload of helper preserves consumed permits",()=>{
  const fs=require("node:fs"),vm=require("node:vm");
  const source=fs.readFileSync(require.resolve("../src/auto_promotion/web/tiktok-desktop.js"),"utf8");
  const context=vm.createContext({Set,Map,Date,URL});
  vm.runInContext(source,context);const first=context.tiktokDesktop;
  vm.runInContext(source,context);assert.equal(context.tiktokDesktop,first);
});
