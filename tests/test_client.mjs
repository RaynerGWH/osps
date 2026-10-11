import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const apiSource=source.slice(0,source.indexOf('function toast('));
const reply=(status,data)=>({status,ok:status>=200&&status<300,json:async()=>data});
function client(fetch,storage=new Map()){
  const context=vm.createContext({crypto:webcrypto,TextEncoder,fetch,sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}});
  vm.runInContext(apiSource,context);
  return context.api;
}

test('reuses a request ID after an uncertain server response',async()=>{
  const ids=[];let calls=0;
  const api=client(async(_,options)=>{ids.push(options.headers['X-Request-Id']);calls++;return calls<=2?reply(503,{error:'Temporary failure'}):reply(200,{saved:true});});
  await assert.rejects(api('/api/sessions/example/buyin',{amount:'100'}));
  await api('/api/sessions/example/buyin',{amount:'100'});
  assert.equal(new Set(ids).size,1);
  await api('/api/sessions/example/buyin',{amount:'100'});
  assert.notEqual(ids[3],ids[0]);
});

test('an uncertain request keeps its ID through a page refresh',async()=>{
  const storage=new Map(),ids=[];
  const first=client(async(_,options)=>{ids.push(options.headers['X-Request-Id']);throw new Error('Network disconnected');},storage);
  await assert.rejects(first('/api/sessions/example/buyin',{amount:'50'}));
  const second=client(async(_,options)=>{ids.push(options.headers['X-Request-Id']);return reply(200,{saved:true});},storage);
  await second('/api/sessions/example/buyin',{amount:'50'});
  assert.equal(new Set(ids).size,1);
  assert.equal(storage.size,0);
});

test('a truncated success response retains its request ID',async()=>{
  const ids=[];let calls=0;
  const api=client(async(_,options)=>{ids.push(options.headers['X-Request-Id']);calls++;return calls===1?{status:200,ok:true,json:async()=>{throw new Error('Truncated response');}}:reply(200,{saved:true});});
  await assert.rejects(api('/api/sessions/example/buyin',{amount:'25'}));
  await api('/api/sessions/example/buyin',{amount:'25'});
  assert.equal(ids[0],ids[1]);
});

test('buy-in history shows Singapore dates across midnight and preserves removed entries safely',()=>{
  const context=vm.createContext({});
  vm.runInContext(source.slice(0,source.indexOf('let historyObserver;')),context);
  const session={code:'example',buyins:[
    {name:'A <img src=x>',amount:5025,created:'2026-10-10T16:05:00Z',voided:1},
    {name:'B',amount:10000,created:'2026-10-10T15:55:00Z',voided:0}
  ]};
  vm.runInContext('state.session='+JSON.stringify(session),context);
  const html=context.buyinHistoryHTML();
  assert.match(html,/11 Oct 2026/);
  assert.match(html,/10 Oct 2026/);
  assert.match(html,/12:05 am/i);
  assert.match(html,/11:55 pm/i);
  assert.match(html,/\$50\.25/);
  assert.match(html,/Removed/);
  assert.match(html,/A &lt;img src=x&gt;/);
  assert.doesNotMatch(html,/<img/);
  assert.ok(html.indexOf('11 Oct')<html.indexOf('10 Oct'));
});

test('payment controls and copied settlement reflect saved paid status',()=>{
  const context=vm.createContext({});
  vm.runInContext(source.slice(0,source.indexOf("document.addEventListener('click'")),context);
  const session={name:'Poker Night',role:'host',status:'settled',my_player_id:null,total_buyin:30000,
    players:[{id:1,name:'A',buyin:10000,cashout:0},{id:2,name:'B',buyin:10000,cashout:25000},{id:3,name:'C',buyin:10000,cashout:5000}],
    transfers:[{id:1,from_id:1,to_id:2,amount:10000,paid:true},{id:2,from_id:3,to_id:2,amount:5000,paid:false}]
  };
  vm.runInContext('state.session='+JSON.stringify(session),context);
  const html=context.settlementHTML();
  assert.match(html,/data-transfer-id="1" checked/);
  assert.match(html,/>Paid</);
  assert.match(html,/>Unpaid</);
  assert.doesNotMatch(html,/disabled/);
  assert.equal(context.summary(),[
    '🃏 Poker Night','','RESULTS','B: +$150.00','C: -$50.00','A: -$100.00','',
    '💸 PAYMENT SUMMARY','• A → B: $100.00 · PAID','• C → B: $50.00 · UNPAID','',
    'Total buy-ins / cash-outs: $300.00'
  ].join('\n'));
  vm.runInContext("state.session.role='guest'",context);
  assert.equal((context.settlementHTML().match(/disabled/g)||[]).length,2);
});

function paymentClient(){
  const requests=[],renders=[],errors=[];
  const context=vm.createContext({document:{activeElement:null}});
  vm.runInContext(source.slice(0,source.indexOf("document.addEventListener('click'")),context);
  const session={code:'example',name:'Poker Night',role:'host',status:'settled',my_player_id:null,total_buyin:30000,
    players:[{id:1,name:'A',buyin:10000,cashout:0},{id:2,name:'B',buyin:10000,cashout:25000},{id:3,name:'C',buyin:10000,cashout:5000}],
    transfers:[{id:1,from_id:1,to_id:2,amount:10000,paid:false},{id:2,from_id:3,to_id:2,amount:5000,paid:false}]
  };
  vm.runInContext('state.session='+JSON.stringify(session),context);
  context.mockApi=(_,body)=>new Promise((resolve,reject)=>requests.push({body,resolve,reject}));
  context.mockRender=()=>renders.push(context.settlementHTML());
  context.mockError=message=>errors.push(message);
  vm.runInContext('api=mockApi;render=mockRender;showError=mockError;',context);
  const current=()=>JSON.parse(vm.runInContext('JSON.stringify(state.session)',context));
  return {context,requests,renders,errors,current};
}

test('payment labels update before responses and out-of-order saves preserve other ticks',async()=>{
  const {context,requests,renders,current}=paymentClient();
  const first=context.setPaymentPaid(1,true);
  assert.match(renders[0],/data-transfer-id="1" checked disabled/);
  assert.match(renders[0],/>Paid</);
  const second=context.setPaymentPaid(2,true);
  assert.deepEqual(current().transfers.map(t=>t.paid),[true,true]);
  requests[1].resolve({transfers:[{id:1,paid:false},{id:2,paid:true}]});
  await second;
  requests[0].resolve({transfers:[{id:1,paid:true},{id:2,paid:false}]});
  await first;
  assert.deepEqual(current().transfers.map(t=>t.paid),[true,true]);
  assert.doesNotMatch(renders.at(-1),/disabled/);
});

test('a failed save reverts only that payment and reports the error',async()=>{
  const {context,requests,errors,current}=paymentClient();
  const first=context.setPaymentPaid(1,true),second=context.setPaymentPaid(2,true);
  requests[0].reject(new Error('Could not save payment.'));
  await first;
  assert.deepEqual(current().transfers.map(t=>t.paid),[false,true]);
  assert.deepEqual(errors,['Could not save payment.']);
  requests[1].resolve({transfers:[{id:2,paid:true}]});
  await second;
  assert.deepEqual(current().transfers.map(t=>t.paid),[false,true]);
});

test('reopening a session while saving preserves the immediate payment status',async()=>{
  const {context,requests,current}=paymentClient();
  const stale=current(),saving=context.setPaymentPaid(1,true);
  assert.equal(context.pendingPaymentView(stale).transfers[0].paid,true);
  vm.runInContext("state.session={code:'other',transfers:[]}",context);
  requests[0].resolve({transfers:[{id:1,paid:true}]});
  await saving;
  assert.equal(current().code,'other');
  assert.deepEqual(current().transfers,[]);
  assert.equal(vm.runInContext('state.paymentSaves.size',context),0);
});

test('a late payment response cannot restore a deleted session',async()=>{
  const {context,requests,current}=paymentClient();
  const saving=context.setPaymentPaid(1,true);
  vm.runInContext('state.session=null',context);
  requests[0].resolve({transfers:[{id:1,paid:true}]});
  await saving;
  assert.equal(current(),null);
  assert.equal(vm.runInContext('state.paymentSaves.size',context),0);
});

function deletionClient(){
  const requests=[],urls=[],messages=[];
  let closed=0;
  const context=vm.createContext({URL,location:{href:'https://example.com/?session=example'},
    history:{replaceState:(_,__,url)=>urls.push(String(url))},
    document:{querySelector:()=>({close:()=>closed++})}});
  vm.runInContext(source.slice(0,source.indexOf("document.addEventListener('click'")),context);
  context.mockApi=(path,body)=>new Promise((resolve,reject)=>requests.push({path,body,resolve,reject}));
  context.mockToast=message=>messages.push(message);
  vm.runInContext(`api=mockApi;render=()=>{};toast=mockToast;
    state.session={code:'example',role:'host'};state.currentCode='example';
    state.sessions=[{code:'example'}];state.roster=['Old name'];state.leaderboard={players:['Old result']};`,context);
  const current=()=>JSON.parse(vm.runInContext('JSON.stringify(state)',context));
  return {context,requests,urls,messages,current,closed:()=>closed};
}

test('deletion waits for success, clears stale data, and never auto-creates a replacement',async()=>{
  const {context,requests,urls,messages,current,closed}=deletionClient();
  const deleting=context.deleteSession('example');
  assert.equal(current().session.code,'example');
  assert.equal(closed(),0);
  assert.equal(requests[0].path,'/api/sessions/example/delete');
  requests[0].resolve({code:'example',deleted:true});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(current().session,null);
  assert.deepEqual(current().sessions,[]);
  assert.deepEqual(current().roster,[]);
  assert.equal(current().leaderboard,null);
  assert.equal(current().currentCode,null);
  assert.equal(closed(),1);
  assert.deepEqual(urls,['https://example.com/']);
  assert.deepEqual(messages,['Session deleted.']);
  assert.equal(requests[1].path,'/api/sessions');
  assert.equal(requests[1].body,undefined);
  requests[1].reject(new Error('History offline'));
  await deleting;
  assert.equal(requests.length,2);
  assert.equal(current().session,null);
});

test('failed deletion keeps the session and confirmation available for retry',async()=>{
  const {context,requests,urls,messages,current,closed}=deletionClient();
  const deleting=context.deleteSession('example');
  requests[0].reject(new Error('Could not delete session'));
  await assert.rejects(deleting,/Could not delete session/);
  assert.equal(current().session.code,'example');
  assert.equal(current().sessions.length,1);
  assert.equal(closed(),0);
  assert.deepEqual(urls,[]);
  assert.deepEqual(messages,[]);
});

test('deletion ignores a stale confirmation or a non-host',async()=>{
  const {context,requests}=deletionClient();
  await context.deleteSession('another-session');
  vm.runInContext("state.session.role='player'",context);
  await context.deleteSession('example');
  assert.deepEqual(requests,[]);
});

function rosterClient(role='host'){
  const context=vm.createContext({});
  vm.runInContext(source.slice(0,source.indexOf("document.addEventListener('click'")),context);
  vm.runInContext(`state.roster=['ZS','Rayner','Aloysius','Arden','Min'];state.session=${JSON.stringify({role,my_player_id:2,players:[{id:1,name:'rayner'},{id:2,name:'Min'}]})}`,context);
  return context;
}

test('player choices are alphabetical, searchable, deduplicated and safely escaped',()=>{
  const context=rosterClient();
  assert.deepEqual([...context.playerNames('buyin')],['Aloysius','Arden','Min','Rayner','ZS']);
  const html=context.playerOptionsHTML('buyin','RAYNER');
  assert.match(html,/>Rayner<\/button>/);
  assert.doesNotMatch(html,/Add RAYNER/);
  assert.doesNotMatch(html,/Aloysius/);
  const unsafe=context.playerOptionsHTML('buyin','<img src=x>');
  assert.match(unsafe,/&lt;img src=x&gt;/);
  assert.doesNotMatch(unsafe,/<img/);
  assert.deepEqual([...context.playerNames('join')],['Aloysius','Arden','ZS']);
});

test('saved names reuse current players and never carry player IDs into another session',()=>{
  const context=rosterClient(),plain=value=>JSON.parse(JSON.stringify(value));
  assert.deepEqual(plain(context.buyinChoice('  RAYNER  ','50')),{action:'buyin',body:{player_id:1,amount:'50'}});
  assert.deepEqual(plain(context.buyinChoice('zs','100')),{action:'players',body:{name:'ZS',amount:'100'}});
  vm.runInContext('state.session.players=[]',context);
  assert.deepEqual(plain(context.buyinChoice('rayner','25')),{action:'players',body:{name:'Rayner',amount:'25'}});
  assert.deepEqual(plain(context.buyinChoice('  New   Friend ','10')),{action:'players',body:{name:'New Friend',amount:'10'}});
  const member=rosterClient('player');
  assert.deepEqual([...member.playerNames('buyin')],['Min']);
  assert.throws(()=>member.buyinChoice('Rayner','50'),/only add your own/);
  assert.throws(()=>member.buyinChoice('Arden','50'),/Choose your name/);
});

test('renaming updates history and summary without undoing a concurrent payment',async()=>{
  const {context,requests,current}=paymentClient();
  context.document.querySelector=()=>({close(){}});
  context.toast=()=>{};
  vm.runInContext("state.session.buyins=[{player:1,name:'A',amount:10000}];state.roster=['A'];",context);
  const renaming=context.renamePlayer(1,'Arden');
  assert.equal(current().players[0].name,'A');
  const paying=context.setPaymentPaid(1,true);
  requests[1].resolve({transfers:[{id:1,paid:true}]});
  await paying;
  requests[0].resolve({players:[{id:1,name:'Arden',buyin:999}],transfers:[{id:1,paid:false}]});
  await renaming;
  assert.equal(current().players[0].name,'Arden');
  assert.equal(current().players[0].buyin,10000);
  assert.equal(current().transfers[0].paid,true);
  assert.equal(current().buyins[0].name,'Arden');
  assert.match(context.summary(),/Arden → B: \$100\.00 · PAID/);
  assert.equal(vm.runInContext('state.roster.length',context),0);
});

test('name editor is escaped and only available for an editable player',()=>{
  const context=rosterClient('player');let html='';
  context.modal=(_title,body)=>{html=body;};
  context.renamePlayerModal(1);
  assert.equal(html,'');
  vm.runInContext("state.session.players[1].name='<Min>';",context);
  context.renamePlayerModal(2);
  assert.match(html,/value="&lt;Min&gt;"/);
  assert.match(html,/>Save name</);
});

test('leaderboard keeps original ranks when filtering and escapes player names',()=>{
  const context=rosterClient();
  vm.runInContext(`state.leaderboard={players:[
    {key:'rayner',name:'Rayner',rank:1,sessions:4,wins:3,buyin:40000,net:17550},
    {key:'<min>',name:'<Min>',rank:2,sessions:2,wins:0,buyin:20000,net:-17550}
  ]};state.leaderboardQuery='MIN';`,context);
  const html=context.leaderboardRows();
  assert.match(html,/>02<\/td>/);
  assert.match(html,/&lt;Min&gt;/);
  assert.match(html,/-\$175\.50/);
  assert.doesNotMatch(html,/Rayner|<Min>/);
  vm.runInContext("state.leaderboardQuery='nobody'",context);
  assert.match(context.leaderboardRows(),/No matching players/);
});

test('leaderboard ignores late responses after leaving and recovers after an error',async()=>{
  const context=rosterClient(),requests=[];
  context.URL=URL;context.location={href:'http://localhost/?session=example'};
  context.history={replaceState(){}};context.window={scrollTo(){}};context.render=()=>{};
  context.api=()=>new Promise((resolve,reject)=>requests.push({resolve,reject}));
  const leaving=context.openLeaderboard();
  vm.runInContext("state.view='session'",context);
  requests[0].resolve({players:[{name:'Stale'}]});await leaving;
  assert.equal(vm.runInContext('state.leaderboard',context),null);
  const failed=context.openLeaderboard();requests[1].reject(new Error('Offline'));await failed;
  assert.equal(vm.runInContext('state.leaderboardError',context),'Offline');
  const retry=context.openLeaderboard();requests[2].resolve({players:[],session_count:0,total_buyin:0});await retry;
  assert.equal(vm.runInContext('state.leaderboardError',context),'');
  assert.equal(vm.runInContext('state.leaderboard.session_count',context),0);
});

function pollingClient(){
  const requests=[],renders=[],urls=[];
  const dialog={open:false},connection={textContent:''};
  const context=vm.createContext({URL,location:{href:'https://example.com/'},
    history:{replaceState:(_,__,url)=>urls.push(String(url))},
    document:{hidden:false,activeElement:null,querySelector:selector=>selector==='#modal'?dialog:selector==='#connection'?connection:null}});
  vm.runInContext(source.slice(0,source.indexOf("document.addEventListener('click'")),context);
  vm.runInContext(source.slice(source.indexOf('async function poll(){'),source.indexOf('start();setInterval')),context);
  context.api=(path,body)=>new Promise((resolve,reject)=>requests.push({path,body,resolve,reject}));
  context.render=()=>renders.push(JSON.parse(vm.runInContext('JSON.stringify(state)',context)));
  context.toast=()=>{};
  const current=()=>JSON.parse(vm.runInContext('JSON.stringify(state)',context));
  return {context,requests,renders,urls,dialog,current};
}

test('current session uses shared history rather than the browsers last opened game',()=>{
  const {context}=pollingClient();
  vm.runInContext(`state.currentCode='old';state.session={code:'old'};state.sessions=[
    {code:'settled',status:'settled'},{code:'new',status:'active'},{code:'old',status:'active'}];`,context);
  assert.equal(context.currentSessionCode(),'new');
  vm.runInContext('state.sessions.forEach(s=>s.status="settled")',context);
  assert.equal(context.currentSessionCode(),'settled');
});

test('an empty visitor page picks up another browsers new session without creating one',async()=>{
  const {context,requests,current,urls}=pollingClient();
  const polling=context.poll();
  assert.equal(requests[0].path,'/api/sessions');
  requests[0].resolve({sessions:[{code:'shared',status:'active'}]});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests[1].path,'/api/sessions/shared');
  requests[1].resolve({code:'shared',role:'guest',status:'active',players:[],transfers:[]});
  await polling;
  assert.equal(current().session.code,'shared');
  assert.equal(current().session.role,'guest');
  assert.ok(requests.every(request=>request.body===undefined));
  assert.deepEqual(urls,['https://example.com/?session=shared']);
});

test('leaderboard refreshes automatically and ignores results after leaving',async()=>{
  const {context,requests,current,renders}=pollingClient();
  vm.runInContext("state.view='leaderboard';state.leaderboardQuery='Ray';state.leaderboard={players:[],session_count:0};",context);
  const polling=context.poll();
  assert.equal(requests[0].path,'/api/leaderboard');
  requests[0].resolve({players:[{name:'Rayner',net:100}],session_count:1});
  await polling;
  assert.equal(current().leaderboard.session_count,1);
  assert.equal(current().leaderboardQuery,'Ray');
  assert.equal(renders.length,1);
  const leaving=context.poll();
  vm.runInContext("state.view='session';state.leaderboardRequest++;",context);
  requests[1].resolve({players:[],session_count:9});
  await leaving;
  assert.equal(current().leaderboard.session_count,1);
  assert.equal(renders.length,1);
});

test('live refresh does not interrupt an open form or replace a pending payment',async()=>{
  const {context,requests,dialog,current}=pollingClient();
  vm.runInContext("state.session={code:'shared',total_buyin:10000};",context);
  dialog.open=true;
  await context.poll();assert.equal(requests.length,0);
  dialog.open=false;
  const polling=context.poll();
  vm.runInContext('state.writeVersion++',context);
  requests[0].resolve({code:'shared',total_buyin:20000});
  await polling;
  assert.equal(current().session.total_buyin,10000);
});
