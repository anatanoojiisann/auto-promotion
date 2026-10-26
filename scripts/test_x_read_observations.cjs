// Repository Tabbit JavaScript only: disposable contexts and intercepted requests.
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const program = execFileSync(process.env.AP_PYTHON || 'python3', ['-c',
  'from auto_promotion.backends.tabbit import READ_ONLY_SCAN_JS; print(READ_ONLY_SCAN_JS)'],
  {cwd:root,env:{...process.env,PYTHONPATH:path.join(root,'src')},encoding:'utf8'});
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;

(async () => {
  const browser = await chromium.launch({headless:true,executablePath:process.env.AP_BROWSER_EXECUTABLE});
  try {
    for (const mode of ['normal','primary429','media429','mediaabort','challenge','iframe429','unrelated429','budget','deadline']) {
      const context = await browser.newContext();
      const seen = [];
      try {
        await context.route('**/*', async route => {
          const request = route.request(), url = new URL(request.url());
          seen.push(url.pathname);
          assert(['x.com','api.x.com','video.twimg.com'].includes(url.hostname), 'unexpected fixture host');
          if (url.hostname === 'api.x.com') {
            const rejected = mode === 'primary429' || url.pathname.endsWith('/UserTweets');
            return route.fulfill({status:rejected?429:200,headers:{'Access-Control-Allow-Origin':'*','Retry-After':'120'},contentType:'application/json',body:'{}'});
          }
          if (url.hostname === 'video.twimg.com') {
            return mode==='mediaabort' ? route.abort('aborted') : route.fulfill({status:429,headers:{'Access-Control-Allow-Origin':'*'},body:''});
          }
          const switcher = '<button data-testid="SideNav_AccountSwitcher_Button">Brand @brand</button><a aria-label="Profile" href="/brand">Profile</a>';
          if (url.searchParams.get('frame')) return route.fulfill({status:429,body:'frame limit'});
          if (url.pathname !== '/search') {
            return route.fulfill({contentType:'text/html',body:switcher + (mode==='iframe429'?'<iframe src="https://x.com/home?frame=1"></iframe>':'')});
          }
          if (mode === 'challenge') return route.fulfill({contentType:'text/html',body:switcher+'<div data-testid="arkoseChallenge">Check</div>'});
          const article = '<article data-testid="tweet"><a href="/someone/status/123"><time datetime="2026-10-25T00:00:00Z">now</time></a><div data-testid="tweetText">video help</div></article>';
          const extra = mode==='media429' || mode==='mediaabort' ? "await fetch('https://video.twimg.com/video.mp4').catch(()=>{});" :
            mode==='unrelated429' ? "await fetch('https://api.x.com/i/api/graphql/hash/UserTweets');" : '';
          return route.fulfill({contentType:'text/html',body:switcher+'<div data-testid="primaryColumn"></div><script>(async()=>{'+
            "await fetch('https://api.x.com/i/api/graphql/hash/SearchTimeline').catch(()=>{});"+extra+
            (mode==='deadline'?'':`document.querySelector('[data-testid=primaryColumn]').innerHTML=${JSON.stringify(article)};`)+'})();</script>'});
        });
        const page = await context.newPage();
        const started = Date.now();
        const value = await new AsyncFunction('page','assert','expected','query','scratch','scanTimeoutMs','requestLimit',program)(
          page,assert,'brand','video',false,mode==='deadline'?1000:5000,mode==='budget'?2:null);
        if (mode === 'primary429') {
          assert.equal(value.rate_limited,true);assert.equal(value.retry_after_seconds,120);
          assert.deepEqual(value.posts,[]);assert.equal(value.read_page_complete,false);
        } else if (mode === 'challenge') {
          assert.equal(value.account_block_reason,'x_challenge');assert.deepEqual(value.posts,[]);
        } else if (mode === 'budget') {
          assert.equal(value.read_state,'budget_exhausted');assert(!seen.includes('/i/api/graphql/hash/SearchTimeline'));
        } else if (mode === 'deadline') {
          assert.equal(value.read_state,'timeout');assert.equal(value.read_page_complete,false);
          assert(Date.now()-started<3000,'deadline must bound repeated selector waits');
        } else {
          assert.equal(value.rate_limited,undefined);assert.equal(value.account_block_reason,undefined);
          assert.equal(value.posts.length,1);assert.equal(value.read_state,'partial');
          if (mode!=='normal') assert(Object.values(value.read_resource_failures).reduce((a,b)=>a+b,0)>=1);
        }
      } finally {await context.close();}
    }
    console.log('PASS: 9 repository Tabbit read-only JavaScript cases; all requests intercepted');
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
