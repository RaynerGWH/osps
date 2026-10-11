'use strict';
const $ = (q, root=document) => root.querySelector(q);
const $$ = (q, root=document) => [...root.querySelectorAll(q)];
const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = cents => '$'+(cents/100).toLocaleString('en-SG',{minimumFractionDigits:2,maximumFractionDigits:2});
const signed = cents => (cents>0?'+':cents<0?'-':'')+money(Math.abs(cents));
const decimal = cents => (cents/100).toFixed(2);
const date = iso => new Date(iso).toLocaleDateString('en-SG',{day:'numeric',month:'short',year:'numeric',timeZone:'Asia/Singapore'});
const time = iso => new Date(iso).toLocaleTimeString('en-SG',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Singapore'});
const state={session:null,sessions:[],roster:[],view:'session',leaderboard:null,leaderboardError:'',leaderboardRequest:0,leaderboardQuery:'',busy:false,polling:false,writeVersion:0,animated:new Set(),paymentSaves:new Map()};
let toastTimer;
const pendingRequests=new Map();
function requestId(){const bytes=new Uint8Array(20);crypto.getRandomValues(bytes);return [...bytes].map(b=>b.toString(16).padStart(2,'0')).join('');}
async function api(path,data){
  const options={credentials:'same-origin',cache:'no-store'};
  let pendingKey,storageKey;
  if(data!==undefined){
    state.writeVersion++;
    pendingKey=JSON.stringify([path,data]);
    if(crypto.subtle){
      const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(pendingKey));
      storageKey='poker-pending:'+Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');
    }
    let id=pendingRequests.get(pendingKey);
    try{if(!id&&storageKey)id=sessionStorage.getItem(storageKey);}catch{}
    id=id||requestId();pendingRequests.set(pendingKey,id);
    try{if(storageKey)sessionStorage.setItem(storageKey,id);}catch{}
    Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json','X-Request-Id':id},body:JSON.stringify(data)});
  }
  let response;
  for(let attempt=0;attempt<2;attempt++){
    try{response=await fetch(path,options);if(response.status>=500&&attempt===0)continue;break;}
    catch{if(attempt===1)throw new Error('Connection lost. Your change is not confirmed. Retry to check the saved result.');}
  }
  const result=await response.json();
  // Keep uncertain requests' IDs across retries and refreshes. A later success
  // returns the stored result instead of recording a second identical buy-in.
  if(pendingKey&&(response.ok||(response.status>=400&&response.status<500&&response.status!==429))){
    pendingRequests.delete(pendingKey);try{if(storageKey)sessionStorage.removeItem(storageKey);}catch{}
  }
  if(!response.ok){const error=new Error(result.error||'Could not save. Please try again.');error.status=response.status;throw error;}
  return result;
}
function toast(text){$('#toast').textContent=text;$('#toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.remove('show'),2800);}
function editable(p){return state.session.role==='host'||p.id===state.session.my_player_id;}
function ordered(){return [...state.session.players].sort((a,b)=>state.session.status==='settled'?(b.cashout-b.buyin)-(a.cashout-a.buyin)||a.id-b.id:a.id-b.id);}
function currentSessionCode(){
  return state.sessions.find(s=>s.status==='active')?.code||state.sessions[0]?.code||state.session?.code;
}
function renderNavigation(){
  const board=state.view==='leaderboard',current=!board&&Boolean(state.session)&&state.session.code===currentSessionCode();
  $('#site-nav').innerHTML=`<nav class="topbar" aria-label="Main navigation"><div class="nav-inner">
    <span class="wordmark"><img src="/favicon.svg?v=67" alt="" width="48" height="44">o$p$</span>
    <div class="nav-links"><button class="nav-link ${current?'active':''}" data-action="current" ${current?'aria-current="page"':''}>Current session</button><button class="nav-link ${!board&&!current?'active':''}" data-action="history" ${!board&&!current?'aria-current="page"':''}>History</button><button class="nav-link ${board?'active':''}" data-action="leaderboard" ${board?'aria-current="page"':''}>Leaderboard</button></div>
    <button class="button new-session" data-action="new"><span aria-hidden="true">+</span> New session</button>
  </div></nav>`;
}
function render(){
  if(state.view==='leaderboard')return renderLeaderboard();
  const s=state.session;if(!s){renderNavigation();$('#app').innerHTML=`<header class="session-bar"><h1>Sessions</h1></header><div class="table-frame"><p class="empty">${state.sessions.length?'Choose a session from History.':'No sessions yet.'}</p></div>`;return;}
  const closed=s.status==='settled',host=s.role==='host',guest=s.role==='guest';
  const animate=!state.animated.has(s.code);state.animated.add(s.code);
  renderNavigation();
  $('#app').innerHTML=`<header class="session-bar ${animate?'enter':''}">
    <div class="session-title"><div class="date"><span>${date(s.created)}</span><span class="meta-separator" aria-hidden="true">·</span><span>SGD</span>${s.demo?'<span class="badge">Sample</span>':''}<span class="status-pill ${closed?'settled':'live'}">${closed?'Settled':'In progress'}</span></div><h1>${esc(s.name)}</h1></div>
    <div class="actions">${closed?'<button class="button primary" data-action="copy">Copy summary</button><button class="button" data-action="reopen-session">Reopen session</button>':guest?'<button class="button primary" data-action="join">Join session</button>':`<button class="button primary" data-action="buyin"><span aria-hidden="true">+</span> Add buy-in</button><button class="button" data-action="cashouts" ${s.players.length&&s.total_buyin?'':'disabled'}>${host?'Settle up':'Final amount'}</button>`}${host?'<details class="session-menu"><summary aria-label="Session options" title="Session options"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg></summary><div class="session-menu-items"><button data-action="delete-session">Delete session</button></div></details>':''}</div>
  </header><div id="connection"></div>
  <section class="table-frame ${animate?'enter enter-delay':''}"><div class="table-scroll"><table aria-label="Players and amounts"><thead><tr><th class="rank">#</th><th>Player</th><th class="numeric">Buy-ins</th>${closed?'<th class="numeric final-column">Cash out</th><th class="numeric">Net result</th>':''}</tr></thead><tbody>${ordered().map((p,i)=>`<tr class="${i<3?'top':''}"><td class="rank">${String(i+1).padStart(2,'0')}</td><td class="player-cell"><span class="player"><span class="player-dot" aria-hidden="true"></span><button data-action="player" data-id="${p.id}">${esc(p.name)}</button></span></td><td class="numeric">${money(p.buyin)}</td>${closed?`<td class="numeric final-column">${money(p.cashout)}</td><td class="numeric ${p.cashout>=p.buyin?'green':'red'}">${signed(p.cashout-p.buyin)}</td>`:''}</tr>`).join('')}</tbody></table></div>${!s.players.length?'<div class="empty">No buy-ins yet.</div>':''}<div class="total-row"><span>${s.players.length} player${s.players.length===1?'':'s'}</span><span>${closed?'Buy-ins / cash-outs':'Total buy-ins'}<strong>${money(s.total_buyin)}</strong></span></div></section>
  ${closed?settlementHTML():''}${buyinHistoryHTML()}`;
  revealHistory();
}
function resultClass(net){return net>0?'green':net<0?'red':'muted';}
function leaderboardRows(){
  const players=state.leaderboard.players.filter(p=>nameKey(p.name).includes(nameKey(state.leaderboardQuery)));
  return players.map(p=>`<tr class="${p.rank<=3?'top':''}"><td class="rank">${String(p.rank).padStart(2,'0')}</td><td class="player-cell"><span class="player"><span class="player-dot" aria-hidden="true"></span><button data-action="player-stats" data-key="${esc(p.key)}">${esc(p.name)}</button></span></td><td class="numeric">${p.sessions}</td><td class="numeric stats-extra">${p.wins}</td><td class="numeric stats-extra">${Math.round(p.wins/p.sessions*100)}%</td><td class="numeric stats-extra">${money(p.buyin)}</td><td class="numeric net-column ${resultClass(p.net)}">${signed(p.net)}</td></tr>`).join('')||'<tr><td colspan="7" class="empty">No matching players.</td></tr>';
}
function renderLeaderboard(){
  renderNavigation();
  const data=state.leaderboard,animate=!state.animated.has('leaderboard');
  if(data)state.animated.add('leaderboard');
  $('#app').innerHTML=`<header class="session-bar ${animate?'enter':''}"><div class="session-title"><div class="date">All time <span class="meta-separator" aria-hidden="true">·</span> SGD</div><h1>Leaderboard</h1></div>${data?.players.length?`<input type="search" class="leaderboard-search" id="leaderboard-search" aria-label="Find player" placeholder="Find player" value="${esc(state.leaderboardQuery)}">`:''}</header>
    ${state.leaderboardError?`<div class="leaderboard-message" role="alert"><p>${esc(state.leaderboardError)}</p><button class="button" data-action="leaderboard">Try again</button></div>`:!data?'<p class="loading" role="status">Loading leaderboard...</p>':!data.players.length?'<div class="table-frame"><p class="empty">No settled sessions yet.</p></div>':`
    <section class="table-frame leaderboard-table ${animate?'enter enter-delay':''}"><div class="table-scroll" tabindex="0" role="region" aria-label="Player rankings"><table aria-label="All-time player rankings"><thead><tr><th class="rank">#</th><th>Player</th><th class="numeric">Sessions</th><th class="numeric stats-extra" title="Sessions with a profit">Wins</th><th class="numeric stats-extra">Win rate</th><th class="numeric stats-extra">Buy-ins</th><th class="numeric">Net result</th></tr></thead><tbody id="leaderboard-rows">${leaderboardRows()}</tbody></table></div><div class="total-row"><span>${data.session_count} settled session${data.session_count===1?'':'s'} · ${data.players.length} player${data.players.length===1?'':'s'}</span><span class="leaderboard-total">Total buy-ins<strong>${money(data.total_buyin)}</strong></span></div></section>`}`;
}
async function openLeaderboard(){
  const request=++state.leaderboardRequest;
  state.view='leaderboard';state.leaderboard=null;state.leaderboardError='';
  const url=new URL(location.href);url.searchParams.set('view','leaderboard');history.replaceState({},'',url);
  render();window.scrollTo(0,0);
  try{
    const data=await api('/api/leaderboard');
    if(state.view!=='leaderboard'||request!==state.leaderboardRequest)return;
    state.leaderboard=data;
  }catch(err){
    if(state.view!=='leaderboard'||request!==state.leaderboardRequest)return;
    state.leaderboardError=err.message;
  }
  if(state.view==='leaderboard'&&request===state.leaderboardRequest)render();
}
function playerStatsModal(key){
  const p=state.leaderboard?.players.find(p=>p.key===key);if(!p)return;
  modal(esc(p.name),`<div class="stats-result"><span>Net result</span><strong class="${resultClass(p.net)}">${signed(p.net)}</strong></div><dl class="player-stats"><div><dt>Sessions</dt><dd>${p.sessions}</dd></div><div><dt>Profitable sessions</dt><dd>${p.wins} <small>(${Math.round(p.wins/p.sessions*100)}%)</small></dd></div><div><dt>Buy-ins</dt><dd>${money(p.buyin)}</dd></div><div><dt>Cash outs</dt><dd>${money(p.cashout)}</dd></div><div><dt>Average result</dt><dd class="${resultClass(p.net)}">${signed(Math.round(p.net/p.sessions))}</dd></div><div><dt>Best result</dt><dd class="${resultClass(p.best)}">${signed(p.best)}</dd></div><div><dt>Worst result</dt><dd class="${resultClass(p.worst)}">${signed(p.worst)}</dd></div><div><dt>Losses / break-even</dt><dd>${p.losses} / ${p.draws}</dd></div></dl><div class="stats-history"><h3>Sessions</h3><div class="archive-list">${p.history.map(s=>`<button class="archive-session" data-action="open-session" data-code="${esc(s.code)}"><span class="archive-name">${esc(s.name)}<small>${date(s.created)} · Buy-in ${money(s.buyin)}</small></span><span class="archive-total ${resultClass(s.net)}">${signed(s.net)}</span><span class="archive-arrow" aria-hidden="true">→</span></button>`).join('')}</div></div>`,true);
}
function buyinHistoryHTML(){
  const entries=state.session.buyins;
  if(!entries.length)return '';
  const days=new Map();
  for(const b of entries){const day=date(b.created);if(!days.has(day))days.set(day,[]);days.get(day).push(b);}
  return `<section class="history-section" aria-labelledby="buyin-history-title" data-reveal="${esc(state.session.code)}"><div class="section-heading"><h2 id="buyin-history-title">Buy-in history</h2></div><div class="history-log">${[...days].map(([day,rows])=>`<div class="history-day"><h3>${day}</h3><ul>${rows.map(b=>`<li class="history-entry ${b.voided?'removed':''}"><span class="history-player">${esc(b.name)}${b.voided?'<small>Removed</small>':''}</span><span class="history-amount">${money(b.amount)}</span><time datetime="${esc(b.created)}" title="${esc(day+' '+time(b.created))}">${time(b.created)}</time></li>`).join('')}</ul></div>`).join('')}</div></section>`;
}
let historyObserver;
const revealedHistory=new Set();
function revealHistory(){
  historyObserver?.disconnect();
  const section=$('[data-reveal]');
  if(!section||revealedHistory.has(section.dataset.reveal)||!('IntersectionObserver' in window)||matchMedia('(prefers-reduced-motion: reduce)').matches)return;
  historyObserver=new IntersectionObserver(entries=>{
    for(const entry of entries)if(entry.isIntersecting){entry.target.classList.add('enter');revealedHistory.add(entry.target.dataset.reveal);historyObserver.unobserve(entry.target);}
  },{threshold:.05});
  historyObserver.observe(section);
}
function sessionHistory(){
  modal('Session history',`<div class="archive-head"><span>Session</span><span>Buy-ins</span></div><div id="session-history-list" class="archive-list" aria-live="polite"></div><div class="archive-more"><button class="text-button" data-action="older-sessions" data-offset="0">Load sessions</button></div>`,true);
  loadSessionHistory(0);
}
async function loadSessionHistory(offset){
  const list=$('#session-history-list'),button=$('[data-action="older-sessions"]');
  if(!list||!button||button.disabled)return;
  button.disabled=true;button.textContent='Loading...';list.setAttribute('aria-busy','true');
  $('#modal-error').classList.add('hidden');
  try{
    const page=await api('/api/sessions?offset='+offset);
    if(!list.isConnected)return;
    const displayed=new Set($$('[data-code]',list).map(el=>el.dataset.code));
    const rows=page.sessions.filter(s=>!displayed.has(s.code));
    list.insertAdjacentHTML('beforeend',rows.map(s=>`<button class="archive-session" data-action="open-session" data-code="${esc(s.code)}"><span class="archive-name">${esc(s.name)}<small>${date(s.created)} · ${s.status==='settled'?(s.unpaid_count?`${s.unpaid_count} unpaid`:'All paid'):'In progress'}</small></span><span class="archive-total">${money(s.total_buyin)}</span><span class="archive-arrow" aria-hidden="true">→</span></button>`).join(''));
    if(!list.children.length)list.innerHTML='<p class="inline-note">No sessions yet.</p>';
    button.hidden=page.next_offset===null;button.dataset.offset=page.next_offset??0;
  }catch(err){if(list.isConnected)showError(err.message);}
  finally{if(list.isConnected){button.disabled=false;button.textContent=offset===0&&!list.children.length?'Try again':'Older sessions';list.setAttribute('aria-busy','false');}}
}
function paymentCheckbox(t,names){
  const canEdit=state.session.role==='host'||state.session.my_player_id===t.to_id;
  const pending=state.paymentSaves.has(state.session.code+':'+t.id);
  return `<label class="payment-check ${t.paid?'is-paid':''}"><input type="checkbox" data-transfer-id="${t.id}" ${t.paid?'checked':''} ${canEdit&&!pending?'':'disabled'} aria-label="Paid: ${esc(names[t.from_id])} to ${esc(names[t.to_id])}, ${money(t.amount)}"><span>${t.paid?'Paid':'Unpaid'}</span></label>`;
}
function settlementHTML(){
  const s=state.session,names=Object.fromEntries(s.players.map(p=>[p.id,p.name]));
  return `<section class="settlement-section"><div class="section-heading"><h2>Payments</h2><span>${s.transfers.length} transfer${s.transfers.length===1?'':'s'}</span></div><div class="transfers">${s.transfers.length?s.transfers.map(t=>`<div class="transfer"><span class="name">${esc(names[t.from_id])}</span><span class="arrow">→</span><span class="name">${esc(names[t.to_id])}</span><span class="transfer-value"><span class="amount">${money(t.amount)}</span>${paymentCheckbox(t,names)}</span></div>`).join(''):'<div class="empty">No payments due.</div>'}</div></section>`;
}
function modal(title,body,wide=false,titleAction=''){
  const el=$('#modal');el.className=wide?'wide':'';
  el.innerHTML=`<div class="dialog-head"><div class="dialog-title"><h2 id="modal-title">${title}</h2>${titleAction}</div><button class="close" data-action="close" aria-label="Close">×</button></div><div class="dialog-body"><div id="modal-error" class="error hidden" role="alert"></div>${body}</div>`;
  if(!el.open)el.showModal();
  requestAnimationFrame(()=>{$('input:not([type="hidden"]):not([readonly]),select',el)?.focus();});
}
function showError(message){if($('#modal').open){const el=$('#modal-error');el.textContent=message;el.classList.remove('hidden');}else toast(message);}
function amountField(id,value=''){return `<div class="money-input"><span>S$</span><input id="${id}" name="amount" inputmode="decimal" autocomplete="off" required value="${value}" placeholder="0.00" pattern="[0-9]{1,6}([.][0-9]{1,2})?" aria-label="Buy-in amount"></div>`;}
function newSession(){modal('New session',`<form id="new-session"><div class="field"><label for="session-name">Name</label><input id="session-name" name="name" required maxlength="70" value="${esc(date(Date.now()))}"></div><div class="dialog-actions"><button class="button primary" type="submit">Start session</button></div></form>`);}
function deleteSessionModal(){
  const s=state.session;if(s?.role!=='host')return;
  $('.session-menu')?.removeAttribute('open');
  modal('Delete session?',`<p class="delete-session-name">${esc(s.name)}</p><p class="inline-note">This permanently removes its buy-ins, payments and results, including from the leaderboard.</p><div class="dialog-actions"><button class="button" data-action="close" autofocus>Cancel</button><button class="button danger" data-action="confirm-delete-session" data-code="${esc(s.code)}">Delete session</button></div>`);
  $('[data-action="close"]',$('.dialog-actions'))?.focus();
}
function reopenSessionModal(){
  const s=state.session;if(s?.status!=='settled')return;
  modal('Reopen session?',`<p class="delete-session-name">${esc(s.name)}</p><p class="inline-note">Keep the buy-ins and final amounts for editing. The payment plan and paid marks will be reset.</p><div class="dialog-actions"><button class="button" data-action="close" autofocus>Cancel</button><button class="button primary" data-action="confirm-reopen-session" data-code="${esc(s.code)}" data-settled-at="${esc(s.settled_at)}">Reopen session</button></div>`);
  $('[data-action="close"]',$('.dialog-actions'))?.focus();
}
async function reopenSession(code,settledAt){
  if(state.session?.code!==code||state.session.status!=='settled')return;
  const saved=await api(`/api/sessions/${encodeURIComponent(code)}/reopen`,{settled_at:settledAt});
  state.leaderboard=null;state.leaderboardRequest++;
  if(state.session?.code!==code)return;
  state.session=saved;
  const listing=state.sessions.find(s=>s.code===code);if(listing){listing.status=saved.status;listing.unpaid_count=saved.transfers.filter(t=>!t.paid).length;}
  $('#modal').close();render();toast(saved.status==='active'?'Session reopened.':'This session has already been settled again.');
  try{await refreshSessions();}catch{/* The reopened session is already saved. */}
  if(state.session?.code===code)render();
}
function forgetSession(code){
  state.sessions=state.sessions.filter(s=>s.code!==code);
  state.roster=[];state.leaderboard=null;state.leaderboardRequest++;
  if(state.currentCode===code)state.currentCode=null;
  if(state.session?.code===code){state.session=null;state.view='session';}
  const url=new URL(location.href);
  if(url.searchParams.get('session')===code){url.searchParams.delete('session');url.searchParams.delete('view');history.replaceState({},'',url);}
}
async function deleteSession(code){
  if(state.session?.code!==code||state.session.role!=='host')return;
  await api(`/api/sessions/${encodeURIComponent(code)}/delete`,{});
  forgetSession(code);$('#modal').close();render();toast('Session deleted.');
  try{await refreshSessions();}catch{/* The deletion succeeded even if history cannot refresh yet. */}
  render();
}
const tidyName=value=>String(value??'').trim().replace(/\s+/g,' ');
const nameKey=value=>tidyName(value).toLocaleLowerCase('en-SG');
function playerNames(mode){
  const s=state.session,names=new Map();
  const canAdd=mode==='join'||s.role==='host';
  if(canAdd)for(const name of state.roster)names.set(nameKey(name),name);
  if(mode==='buyin')for(const p of s.players.filter(editable)){
    if(!names.has(nameKey(p.name)))names.set(nameKey(p.name),p.name);
  }
  if(mode==='join')for(const p of s.players)names.delete(nameKey(p.name));
  return [...names.values()].sort((a,b)=>a.localeCompare(b,'en-SG',{sensitivity:'base'}));
}
function playerOptionsHTML(mode,query=''){
  const names=playerNames(mode),key=nameKey(query),typed=tidyName(query);
  const matches=names.filter(name=>nameKey(name).includes(key));
  const canAdd=(mode==='join'||state.session.role==='host')&&typed&&!names.some(name=>nameKey(name)===key)&&!state.session.players.some(p=>nameKey(p.name)===key);
  return matches.map(name=>`<button type="button" class="player-option" data-action="pick-player" data-name="${esc(name)}">${esc(name)}</button>`).join('')+
    (canAdd?`<button type="button" class="player-option new-player-option" data-action="pick-player" data-name="${esc(typed)}">+ Add ${esc(typed)}</button>`:'');
}
function playerPickerHTML(mode,value=''){
  return `<div class="field"><label for="player-name">${mode==='join'?'Your name':'Player'}</label><input id="player-name" name="name" data-player-mode="${mode}" required maxlength="40" autocomplete="off" spellcheck="false" value="${esc(value)}" placeholder="Search or add a player"><div id="player-options" class="player-options" role="group" aria-label="Players">${playerOptionsHTML(mode,value)}</div></div>`;
}
function updatePlayerOptions(){
  const input=$('#player-name'),options=$('#player-options');
  if(!input||!options)return;
  options.innerHTML=playerOptionsHTML(input.dataset.playerMode,input.value);
}
async function loadPlayerOptions(){
  const input=$('#player-name');if(!input)return;
  if(input.dataset.playerMode==='buyin'&&state.session.role!=='host')return;
  try{
    const result=await api('/api/players');state.roster=result.players;
    if($('#player-name')===input&&!$('#player-options').hidden)updatePlayerOptions();
  }catch{
    if($('#player-name')===input){
      showError('Could not load saved players. You can still enter a name.');
      $('#player-options').insertAdjacentHTML('beforeend','<button type="button" class="player-option" data-action="retry-players">Retry player list</button>');
    }
  }
}
function buyinChoice(name,value){
  const key=nameKey(name),player=state.session.players.find(p=>nameKey(p.name)===key);
  if(player){
    if(!editable(player))throw new Error('You can only add your own buy-ins.');
    return {action:'buyin',body:{player_id:player.id,amount:value}};
  }
  if(state.session.role!=='host')throw new Error('Choose your name from this session.');
  const canonical=state.roster.find(name=>nameKey(name)===key)||tidyName(name);
  return {action:'players',body:{name:canonical,amount:value}};
}
function buyinModal(){
  const mine=state.session.role==='player'?state.session.players.find(p=>p.id===state.session.my_player_id)?.name:'';
  modal('Add buy-in',`<form id="buyin-form">${playerPickerHTML('buyin',mine)}<div class="field"><label for="buyin-amount">Amount</label>${amountField('buyin-amount')}</div><div class="dialog-actions"><button class="button primary" type="submit">Add buy-in</button></div></form>`);
  loadPlayerOptions();
}
function joinModal(){
  modal('Join session',`<form id="join-form">${playerPickerHTML('join')}<div class="dialog-actions"><button type="submit" class="button primary">Join</button></div></form>`);
  loadPlayerOptions();
}
function playerModal(id){
  const s=state.session,p=s.players.find(p=>p.id===Number(id));if(!p)return;
  const entries=s.buyins.filter(b=>b.player===p.id),edit=s.status==='active'&&editable(p);
  const renameButton=editable(p)?`<button type="button" class="edit-name-button" data-action="rename-player" data-id="${p.id}" aria-label="Edit name" title="Edit name"><svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 5 4 4M4 20l4-1L20 7a2.12 2.12 0 0 0-3-3L5 16l-1 4Z"/></svg></button>`:'';
  modal(esc(p.name),`<div class="buyin-list">${entries.length?entries.map(b=>`<div class="buyin-entry ${b.voided?'removed':''}"><span>${money(b.amount)}<small>${date(b.created)} · ${time(b.created)}${b.voided?' · Removed':''}</small></span>${edit&&!b.voided?`<button class="remove" data-action="remove" data-id="${b.id}" aria-label="Remove ${money(b.amount)} buy-in">×</button>`:''}</div>`).join(''):'<p class="inline-note">No buy-ins.</p>'}</div>${edit?`<form id="player-buyin"><input type="hidden" name="player_id" value="${p.id}"><label for="extra-amount">Add buy-in</label>${amountField('extra-amount')}<div class="dialog-actions"><button class="button primary" type="submit">Add buy-in</button></div></form>`:`<div class="cash-balance"><span>Buy-in <strong>${money(p.buyin)}</strong></span>${p.cashout!==null?`<span>Final <strong>${money(p.cashout)}</strong></span>`:''}</div>`}`,false,renameButton);
}
function renamePlayerModal(id){
  const p=state.session.players.find(p=>p.id===Number(id));if(!p||!editable(p))return;
  modal('Edit name',`<form id="rename-player-form"><input type="hidden" name="player_id" value="${p.id}"><div class="field"><label for="edit-player-name">Name</label><input id="edit-player-name" name="name" required maxlength="40" autocomplete="off" value="${esc(p.name)}"></div><div class="dialog-actions"><button type="button" class="button" data-action="player" data-id="${p.id}">Cancel</button><button type="submit" class="button primary">Save name</button></div></form>`);
}
async function renamePlayer(id,name){
  const code=state.session.code;
  const saved=await api(`/api/sessions/${code}/rename`,{player_id:id,name});
  const updated=saved.players.find(p=>p.id===id);
  state.roster=[];
  if(state.session.code!==code)return;
  // Merge the name only: a payment may have finished saving in the meantime.
  const player=state.session.players.find(p=>p.id===id);
  if(player&&updated){
    player.name=updated.name;
    for(const entry of state.session.buyins)if(entry.player===id)entry.name=updated.name;
  }
  $('#modal').close();render();toast('Name updated.');
}
function cents(value){if(!/^\d{1,6}([.]\d{1,2})?$/.test(value))return null;const [whole,fraction='']=value.split('.');return Number(whole)*100+Number(fraction.padEnd(2,'0'));}
function cashoutModal(){
  const s=state.session,host=s.role==='host';
  modal('Final amounts',`<form id="cashout-form"><div class="cash-head"><span>Player</span><span style="text-align:right">Buy-ins</span><span style="text-align:right">Cash out</span></div>${s.players.map(p=>`<div class="cash-row"><label for="final-${p.id}">${esc(p.name)}</label><span class="bought">${money(p.buyin)}</span><div class="money-input"><span>$</span><input id="final-${p.id}" data-player-id="${p.id}" name="final-${p.id}" inputmode="decimal" placeholder="0.00" value="${p.cashout===null?'':decimal(p.cashout)}" pattern="[0-9]{1,6}([.][0-9]{1,2})?" ${editable(p)?'required':'readonly'} aria-label="Final amount for ${esc(p.name)}"></div></div>`).join('')}<div class="cash-balance"><span>Total buy-ins <strong>${money(s.total_buyin)}</strong></span><span id="balance-check"></span></div><div class="dialog-actions"><button class="button primary" type="submit">${host?'Settle up':'Save'}</button></div></form>`,true);
  balanceCheck();
}
function balanceCheck(){
  const inputs=$$('#cashout-form [data-player-id]');let total=0,missing=0;
  for(const input of inputs){const value=cents(input.value);if(value===null)missing++;else total+=value;}
  const gap=state.session.total_buyin-total,el=$('#balance-check');if(!el)return;
  el.textContent=missing?`${missing} to enter`:gap?`${money(Math.abs(gap))} ${gap>0?'short':'over'}`:'Balanced ✓';
  el.className=missing?'muted':gap?'red':'green';
}
function summary(){
  const s=state.session,names=Object.fromEntries(s.players.map(p=>[p.id,p.name]));
  return [`🃏 ${s.name}`,'','RESULTS',...ordered().map(p=>`${p.name}: ${signed(p.cashout-p.buyin)}`),'','💸 PAYMENT SUMMARY',...s.transfers.map(t=>`• ${names[t.from_id]} → ${names[t.to_id]}: ${money(t.amount)} · ${t.paid?'PAID':'UNPAID'}`),...(s.transfers.length?[]:['No transfers needed.']),'',`Total buy-ins / cash-outs: ${money(s.total_buyin)}`].join('\n');
}
async function refreshSessions(){state.sessions=(await api('/api/sessions')).sessions;}
function pendingPaymentView(session){
  for(const pending of state.paymentSaves.values())if(pending.code===session.code&&pending.settledAt===session.settled_at){
    const transfer=session.transfers.find(t=>t.id===pending.id);if(transfer)transfer.paid=pending.paid;
  }
  return session;
}
async function openSession(code){state.session=pendingPaymentView(await api('/api/sessions/'+encodeURIComponent(code)));state.view='session';state.leaderboardRequest++;if(state.session.status==='active')state.currentCode=code;const url=new URL(location.href);url.searchParams.set('session',code);url.searchParams.delete('view');history.replaceState({},'',url);render();window.scrollTo(0,0);}
async function save(action,body){state.session=await api(`/api/sessions/${state.session.code}/${action}`,body);$('#modal').close();render();}
async function setPaymentPaid(id,paid){
  const code=state.session.code,settledAt=state.session.settled_at,key=code+':'+id,transfer=state.session.transfers.find(t=>t.id===id);
  if(!transfer||state.paymentSaves.has(key))return;
  const previous=Boolean(transfer.paid);
  state.paymentSaves.set(key,{code,id,paid,settledAt});
  transfer.paid=paid;
  render();
  try{
    const saved=await api(`/api/sessions/${code}/paid`,{transfer_id:id,paid,settled_at:settledAt});
    if(state.session?.code===code&&state.session.settled_at===settledAt){
      if(saved.status&&saved.settled_at!==settledAt){state.session=pendingPaymentView(saved);render();return;}
      // Merge only this payment. Another payment may already have changed
      // while this response was in flight.
      const current=state.session.transfers.find(t=>t.id===id);
      if(current)current.paid=Boolean(saved.transfers.find(t=>t.id===id).paid);
    }
  }catch(err){
    if(state.session?.code===code&&state.session.settled_at===settledAt){const current=state.session.transfers.find(t=>t.id===id);if(current)current.paid=previous;}
    showError(err.message);
  }finally{
    state.paymentSaves.delete(key);
    if(state.session?.code===code&&state.session.settled_at===settledAt){
      const focusedId=document.activeElement?.dataset?.transferId;
      render();
      if(focusedId)$(`input[data-transfer-id="${focusedId}"]`)?.focus({preventScroll:true});
    }
  }
}
document.addEventListener('click',async event=>{
  if(!event.target.closest('.session-menu'))$('.session-menu')?.removeAttribute('open');
  const el=event.target.closest('[data-action]');if(!el||el.disabled||state.busy)return;
  const action=el.dataset.action;
  try{
    if(action==='close')return $('#modal').close();
    if(action==='pick-player'){
      $('#player-name').value=el.dataset.name;$('#player-options').hidden=true;
      ($('#buyin-amount')||$('#join-form button[type="submit"]')).focus();return;
    }
    if(action==='retry-players'){$('#modal-error').classList.add('hidden');el.remove();return loadPlayerOptions();}
    if(action==='buyin')return buyinModal();
    if(action==='player')return playerModal(el.dataset.id);
    if(action==='rename-player')return renamePlayerModal(el.dataset.id);
    if(action==='cashouts')return cashoutModal();
    if(action==='join')return joinModal();
    if(action==='new')return newSession();
    if(action==='reopen-session')return reopenSessionModal();
    if(action==='confirm-reopen-session'){state.busy=true;el.disabled=true;await reopenSession(el.dataset.code,el.dataset.settledAt);return;}
    if(action==='delete-session')return deleteSessionModal();
    if(action==='confirm-delete-session'){state.busy=true;el.disabled=true;await deleteSession(el.dataset.code);return;}
    if(action==='history')return sessionHistory();
    if(action==='leaderboard')return openLeaderboard();
    if(action==='player-stats')return playerStatsModal(el.dataset.key);
    if(action==='current'){
      state.busy=true;await refreshSessions();
      const code=currentSessionCode();
      if(!code)return newSession();
      if(state.view==='session'&&code===state.session?.code){window.scrollTo(0,0);return;}
      await openSession(code);return;
    }
    if(action==='older-sessions')return await loadSessionHistory(Number(el.dataset.offset));
    if(action==='open-session'){state.busy=true;await openSession(el.dataset.code);$('#modal').close();return;}
    if(action==='copy'){
      try{await navigator.clipboard.writeText(summary());toast('Summary copied.');}
      catch{modal('Summary',`<textarea readonly aria-label="Settlement summary" style="width:100%;height:330px;background:var(--bg);color:var(--text);padding:13px;font:14px var(--body-font)">${esc(summary())}</textarea>`);}
      return;
    }
    if(action==='remove'){
      const entry=state.session.buyins.find(b=>b.id===Number(el.dataset.id));
      modal('Remove buy-in?',`<p class="inline-note">${esc(entry.name)}: ${money(entry.amount)}</p><div class="dialog-actions"><button class="button" data-action="close">Cancel</button><button class="button primary" data-action="confirm-remove" data-id="${entry.id}">Remove</button></div>`);return;
    }
    if(action==='confirm-remove'){state.busy=true;el.disabled=true;await save('undo',{entry_id:Number(el.dataset.id)});toast('Buy-in removed.');}
  }catch(err){showError(err.message);}finally{state.busy=false;if(el.isConnected)el.disabled=false;}
});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&$('.session-menu[open]')){const menu=$('.session-menu');menu.removeAttribute('open');$('summary',menu).focus();}
});
document.addEventListener('change',async event=>{
  if(event.target.matches('input[data-transfer-id]')){
    const input=event.target,transfer=state.session.transfers.find(t=>t.id===Number(input.dataset.transferId));
    if(!transfer)return;
    if(state.busy){input.checked=Boolean(transfer.paid);return;}
    await setPaymentPaid(transfer.id,input.checked);
    return;
  }
});
document.addEventListener('focusin',event=>{
  if(event.target.id==='player-name'){$('#player-options').hidden=false;updatePlayerOptions();}
});
document.addEventListener('input',event=>{
  if(event.target.id==='leaderboard-search'){state.leaderboardQuery=event.target.value;$('#leaderboard-rows').innerHTML=leaderboardRows();}
  if(event.target.closest('#cashout-form'))balanceCheck();
  if(event.target.id==='player-name'){$('#player-options').hidden=false;updatePlayerOptions();}
});
document.addEventListener('submit',async event=>{
  event.preventDefault();const form=event.target;if(state.busy||!form.reportValidity())return;
  state.busy=true;const button=$('button[type="submit"]',form);if(button)button.disabled=true;
  const data=Object.fromEntries(new FormData(form));
  try{
    if(form.id==='new-session'){const result=await api('/api/sessions',data);await refreshSessions();$('#modal').close();await openSession(result.code);}
    if(form.id==='rename-player-form')await renamePlayer(Number(data.player_id),data.name);
    if(form.id==='buyin-form'||form.id==='player-buyin'){
      if(cents(data.amount)===null||cents(data.amount)===0)throw new Error('Enter a buy-in greater than $0.00.');
      const choice=form.id==='buyin-form'?buyinChoice(data.name,data.amount):{action:'buyin',body:{player_id:Number(data.player_id),amount:data.amount}};
      await save(choice.action,choice.body);
      toast('Buy-in added.');
    }
    if(form.id==='cashout-form'){
      const inputs=$$('[data-player-id]',form);
      const values=inputs.filter(i=>!i.readOnly).map(i=>({player_id:Number(i.dataset.playerId),amount:i.value}));
      if(state.session.role==='host'){
        let total=0;
        for(const v of values){const n=cents(v.amount);if(n===null)throw new Error('Enter a final amount for everyone, including $0.');total+=n;}
        const gap=state.session.total_buyin-total;
        if(gap)throw new Error(`Final amounts are ${money(Math.abs(gap))} ${gap>0?'short':'over'}.`);
        await save('settle',{values});await refreshSessions();render();
      }else await save('cashouts',{values});
    }
    if(form.id==='join-form'){
      const name=state.roster.find(name=>nameKey(name)===nameKey(data.name))||tidyName(data.name);
      await save('join',{name});await refreshSessions();
    }
  }catch(err){showError(err.message);}finally{state.busy=false;if(button?.isConnected)button.disabled=false;}
});
$('#modal').addEventListener('click',event=>{
  if(event.target!==$('#modal')||state.busy)return;const r=$('#modal').getBoundingClientRect();
  if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)$('#modal').close();
});
$('#modal').addEventListener('cancel',event=>{if(state.busy)event.preventDefault();});
async function poll(){
  if(state.busy||state.paymentSaves.size||state.polling||$('#modal').open||document.hidden||document.activeElement?.tagName==='SELECT')return;
  state.polling=true;const code=state.session?.code,writeVersion=state.writeVersion,view=state.view,request=state.leaderboardRequest;
  const stillHere=()=>state.view===view&&state.session?.code===code&&state.leaderboardRequest===request&&!state.busy&&state.writeVersion===writeVersion&&!$('#modal').open;
  try{
    if(view==='leaderboard'){
      const fresh=await api('/api/leaderboard');if(!stillHere())return;
      if(JSON.stringify(fresh)!==JSON.stringify(state.leaderboard)||state.leaderboardError){
        const search=document.activeElement?.id==='leaderboard-search';
        const selection=search?[document.activeElement.selectionStart,document.activeElement.selectionEnd]:null;
        state.leaderboard=fresh;state.leaderboardError='';render();
        if(search){const input=$('#leaderboard-search');input?.focus({preventScroll:true});if(input&&selection)input.setSelectionRange(...selection);}
      }
    }else if(!code){
      const page=await api('/api/sessions');if(!stillHere())return;
      state.sessions=page.sessions;
      const current=currentSessionCode();
      if(current){const fresh=await api('/api/sessions/'+encodeURIComponent(current));if(!stillHere())return;state.session=pendingPaymentView(fresh);const url=new URL(location.href);url.searchParams.set('session',current);history.replaceState({},'',url);render();}
    }else{
      const fresh=await api('/api/sessions/'+code);if(!stillHere())return;
      if(JSON.stringify(fresh)!==JSON.stringify(state.session)){state.session=fresh;render();}
      else if($('#connection'))$('#connection').textContent='';
    }
  }
  catch(err){
    if(!stillHere())return;
    if(view==='leaderboard'){state.leaderboardError=err.message;render();}
    else if(err.status===404&&code){forgetSession(code);render();toast('This session is no longer available.');}
    else if($('#connection')){$('#connection').className='connection';$('#connection').textContent='Offline. Reconnecting...';}
  }
  finally{state.polling=false;}
}
async function start(){
  try{await refreshSessions();const params=new URL(location.href).searchParams,code=params.get('session');if(params.get('view')==='leaderboard'){if(code){try{state.session=pendingPaymentView(await api('/api/sessions/'+encodeURIComponent(code)));}catch(err){if(err.status!==404)throw err;forgetSession(code);}}return await openLeaderboard();}if(code)return await openSession(code);const current=currentSessionCode();if(current)return await openSession(current);render();}
  catch(err){renderNavigation();$('#app').innerHTML=`<p class="error">${esc(err.message)}</p><button class="button primary" data-action="new">New session</button>`;}
}
start();setInterval(poll,3000);
