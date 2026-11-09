// Offline browser contract: all navigation and submission requests are intercepted.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const expect = (locator) => ({
  toHaveCount: async n => {await locator.first().waitFor({state:'attached'}); assert.equal(await locator.count(), n);},
  toBeVisible: async () => {await locator.waitFor({state:'visible'});},
  toBeEnabled: async () => {assert.equal(await locator.isEnabled(),true);},
  toHaveText: async text => {assert.equal(await locator.innerText(),text);}
});
const {execFileSync} = require('node:child_process');
const programs = JSON.parse(execFileSync(process.env.AUTO_PROMOTION_PYTHON || '.venv/Scripts/python.exe', ['-c',
  'import json; from auto_promotion.backends.tabbit import IDENTITY_JS,PREPARE_JS,SUBMIT_JS,VERIFY_JS; print(json.dumps([IDENTITY_JS,PREPARE_JS,SUBMIT_JS,VERIFY_JS]))'
], {encoding:'utf8'}));
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.AP_BROWSER_EXECUTABLE});
  try {
    for (const mode of ['modal','modal_with_background','inline']) {
      const modal=mode==='modal_with_background';
      const inlineMode=mode.includes('inline');
      const context = await browser.newContext();
      let posts=0, parent='456';
      await context.route('**/*', async route => {
        const url = new URL(route.request().url());
        if(url.pathname === '/CreateTweet') {
          posts++;
          return route.fulfill({contentType:'application/json', body:JSON.stringify({data:{create_tweet:{tweet_results:{result:{rest_id:'789',legacy:{in_reply_to_status_id_str:parent}}}}}})});
        }
        assert.equal(url.hostname,'x.com');
        const own=url.pathname.endsWith('/789'), id=own?'789':'456', author=own?'brand':'target';
        const editor=`<div contenteditable="true" data-testid="tweetTextarea_0"></div><button data-testid="tweetButton" onclick="fetch('/CreateTweet',{method:'POST'})">Reply</button>`;
        const body=`<button data-testid="SideNav_AccountSwitcher_Button" aria-label="Account menu">Brand @brand</button><a aria-label="Profile" href="/brand">Profile</a>
          <div><article data-testid="tweet"><span data-testid="User-Name">@${author}</span><span data-testid="tweetText">${own?'exact 世界':'source'}</span><a href="/${author}/status/${id}"><time datetime="2026-10-15T06:00:00Z"></time></a><button data-testid="reply" aria-label="1 Reply. Reply" onclick="${modal?"document.querySelector('#backgroundComposer').hidden=false;":''}setTimeout(()=>document.querySelector('${inlineMode?'#inlineComposer':'#composer'}').hidden=false,300)">Reply</button></article>
          ${inlineMode?`<div id="inlineComposer"><div contenteditable="true" data-testid="tweetTextarea_0"></div><button data-testid="tweetButtonInline" onclick="fetch('/CreateTweet',{method:'POST'})">Reply</button></div>`:''}
          ${modal?'<div id="backgroundComposer" hidden><div contenteditable="true" data-testid="tweetTextarea_0">background draft</div><button data-testid="tweetButtonInline">Reply</button></div>':''}</div>
          ${inlineMode?'':`<div id="composer" role="dialog" hidden>${editor}</div>`}`;
        return route.fulfill({contentType:'text/html; charset=utf-8', body});
      });
      const page=await context.newPage();
      const item={action:'reply',target:'https://x.com/target/status/456',text:'exact 世界',media:[]};
      async function run(code, expected='brand', receipt={}) {
        return new AsyncFunction('page','expect','assert','item','expected','receipt', programs[0]+code)(page,expect,assert,item,expected,receipt);
      }
      assert.deepEqual(await run(programs[1]),{prepared:true});
      assert.equal(posts,0);
      if (modal) {
        assert.equal(await page.locator('#backgroundComposer [contenteditable]').innerText(),'background draft');
        assert.equal(await page.locator('#composer [contenteditable]').innerText(),item.text);
      }
      await assert.rejects(run(programs[2], 'other'), /XIdentityMismatch/);
      assert.equal(posts,0);
      const receipt=await run(programs[2]);
      assert.equal(posts,1);
      assert.equal(receipt.target_verified,true);
      assert.equal((await run(programs[3],'brand',receipt)).verified,true);
      assert.equal((await run(programs[3],'brand',{...receipt,target_verified:false})).verified,false);
      assert.equal(posts,1);
      await context.close();
    }
    console.log('PASS: inline reply and delayed modal with and without background editor, exact Unicode, identity mismatch, parent match and read-only verification');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
