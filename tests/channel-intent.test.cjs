const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../src/auto_promotion/web/app.js'), 'utf8');

function form(desktop, channel) {
  const nodes = new Map();
  const submitted = [], notices = [];
  const values = {execution_channel: channel, accounts:['a'], name:'fixture', starts:'2026-10-28T09:00',
    ends:'2026-10-29T09:00', languages:'en', candidates:'1', confidence:'0.8', daily:'1', interval:'120',
    ordering_mode:'independent_accounts', min_members:'0', min_activity:'0', max_joins:'0'};
  const node = key => {
    if (!nodes.has(key)) nodes.set(key, {value:channel, open:true, insertAdjacentHTML(){}, focus(){}});
    return nodes.get(key);
  };
  const tasks = [];
  const context = {$: node, state:{accounts:[{id:'a',platform:'x',label:'fixture',mode:'simulation',execution_backend:desktop?'desktop':'browser'}]},
    Date, names:{x:'X'}, isoLocal:()=>'', esc:String, openPanel:()=>1, panelGeneration:1,
    location:{hash:''}, toast: t=>notices.push(t), closePanel(){}, refresh:async()=>{},
    busy:(_, cb)=>tasks.push(cb()), api:async(_,__,payload)=>{submitted.push(payload);return{id:'new'};},
    FormData:class {get(k){return values[k]??'';}getAll(k){return values[k]??[];}set(k,v){values[k]=v;}}};
  vm.createContext(context);
  const a=source.indexOf('function taskForm()'), b=source.indexOf('function taskReadinessPanel(',a);
  vm.runInContext(source.slice(a,b)+';taskForm();',context);
  return {context,nodes,node,values,submitted,notices,tasks,submit:()=>node('#task-form').onsubmit({preventDefault(){},target:{},submitter:{}})};
}

test('unsupported desktop/online choice is explained and preserved without creating a task',()=>{
  const f=form(true,'cloud'); f.submit();
  assert.equal(f.values.execution_channel,'cloud');
  assert.equal(f.node('#execution-channel').value,'cloud');
  assert.equal(f.submitted.length,0);
  assert.match(f.notices[0],/本机通道/);
});

test('supported explicit choice and its source reach the task unchanged',async()=>{
  const f=form(false,'cloud'); f.submit(); await Promise.all(f.tasks);
  assert.equal(f.submitted.length,1);
  assert.equal(f.submitted[0].policy.execution_channel,'cloud');
  assert.equal(f.submitted[0].policy.execution_channel_source,'workbench_task_form');
});

test('changing original-post options cannot silently rewrite channel selection',()=>{
  const f=form(false,'local');
  f.node('#task-form [name=allow_owned_x_posts]').onchange({target:{checked:true}});
  assert.equal(f.node('#execution-channel').value,'local');
});
