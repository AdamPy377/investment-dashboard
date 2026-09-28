const $ = id => document.getElementById(id);
const state = { auth:null, pid:null, setup:null, dashboard:null, tx:[], docs:[], recon:[], page:'overview', range:'30', editId:null, selectedHolding:null };
const types = ['buy','sell','deposit','withdrawal','dividend','interest','fee','transfer','fx','split'];
const esc = s => String(s ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const money = (n,c='AUD') => n == null || !Number.isFinite(Number(n)) ? '—' : new Intl.NumberFormat('en-AU',{style:'currency',currency:c,maximumFractionDigits:2}).format(n);
const pct = n => n == null ? '—' : `${n>=0?'+':''}${n.toFixed(2)}%`;
const number = n => Number(n||0).toLocaleString('en-AU',{maximumFractionDigits:6});
const signClass = n => n == null ? '' : n >= 0 ? 'up' : 'down';
const empty = message => `<div class="empty">${esc(message)}</div>`;
let toastTimer;
function toast(message,error=false){const el=$('toast');el.textContent=message;el.className=error?'error-toast':'';el.style.display='block';clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.style.display='none',4500)}
async function api(path,method='GET',body){
  const options={method,credentials:'same-origin',headers:{}};
  if(state.auth?.csrf && method!=='GET') options.headers['X-CSRF-Token']=state.auth.csrf;
  if(body instanceof FormData) options.body=body;
  else if(body!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body)}
  const response=await fetch('/api'+path,options);
  const data=await response.json().catch(()=>({error:'Server returned an unexpected response'}));
  if(!response.ok) throw Error(data.error||'Request failed');
  return data;
}
function query(path){return `${path}${path.includes('?')?'&':'?'}portfolio_id=${state.pid}`}
function field(form,name){return form.elements.namedItem(name)}
function formData(form){return Object.fromEntries(new FormData(form).entries())}
function options(select, items, label, blank=''){const current=select.value;select.innerHTML=(blank!==null?`<option value="">${esc(blank)}</option>`:'')+items.map(x=>`<option value="${x.id}">${esc(label(x))}</option>`).join('');if([...select.options].some(o=>o.value===current))select.value=current}
function show(page){
  if(page==='admin'&&state.auth.role!=='admin')return;
  state.page=page;
  document.querySelectorAll('.page').forEach(el=>el.hidden=el.id!==page);
  document.querySelectorAll('nav button').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
  $('pageTitle').textContent=({overview:'Overview',transactions:'Transactions',cash:'Cash & accounts',documents:'Documents',reconcile:'Reconcile',admin:'Manage',holdingDetail:'Holding details'})[page];
  if(page==='overview'&&state.dashboard)requestAnimationFrame(drawChart);
  window.scrollTo({top:0,behavior:'smooth'});
}
async function load(){
  const [setup,dashboard,tx,docs,recon]=await Promise.all([
    api(query('/setup')),api(query('/dashboard')),api(query('/transactions')),api(query('/documents')),api(query('/reconciliations'))]);
  Object.assign(state,{setup,dashboard,tx,docs,recon});
  $('portfolioName').textContent=setup.portfolios.find(p=>p.id===state.pid)?.name+'’s portfolio';
  const latest=dashboard.holdings.map(h=>h.price_day).filter(Boolean).sort().at(-1);
  $('dataStamp').textContent=latest?`Latest holding price: ${latest}`:'No holding prices yet';
  renderOptions();renderOverview();renderTransactions();renderCash();renderDocs();renderRecon();
  if(state.page==='holdingDetail')renderDetail();
}
function renderOptions(){
  const {accounts,instruments}=state.setup;
  for(const id of ['txAccount','docAccount'])options($(id),accounts,a=>`${a.name} · ${a.currency}`,'All accounts');
  options($('docHolding'),instruments,i=>`${i.symbol} · ${i.name}`,'All holdings');
  for(const name of ['account_id','target_account_id'])options(field($('txForm'),name),accounts,a=>`${a.name} · ${a.currency}`,name==='account_id'?'Choose account':'None');
  for(const name of ['instrument_id'])for(const form of [$('txForm'),$('documentForm'),$('priceForm')])options(field(form,name),instruments,i=>`${i.symbol} · ${i.name}`,form===$('priceForm')?'Choose holding':'None');
  for(const form of [$('documentForm'),$('reconForm')])options(field(form,'account_id'),accounts,a=>`${a.name} · ${a.currency}`,form===$('reconForm')?'Choose account':'None');
  const years=[...new Set(state.docs.map(d=>d.tax_year).filter(Boolean))].sort().reverse();
  $('docYear').innerHTML='<option value="">All tax years</option>'+years.map(y=>`<option>${esc(y)}</option>`).join('');
  $('txAccount').value='';$('docAccount').value='';$('docHolding').value='';
}
function renderOverview(){
  const d=state.dashboard;
  $('totalValue').textContent=money(d.value);
  $('totalContext').textContent=d.value==null?'Add prices and a USD/AUD rate to complete the valuation':'AUD · investments and cash';
  $('profit').textContent=money(d.profit);$('profit').className=signClass(d.profit);
  $('dayChange').textContent=money(d.day_change);$('dayChange').className=signClass(d.day_change);
  $('holdings').innerHTML=d.holdings.length?d.holdings.map(h=>`<div class="row clickable" data-holding="${h.id}" tabindex="0" role="button"><div class="row-main"><b>${esc(h.symbol)} <span class="pill">${esc(h.exchange)}</span></b><small>${esc(h.name)} · ${number(h.quantity)} shares</small></div><div class="row-side"><b>${money(h.value,h.currency)}</b><small>${money(h.price,h.currency)} / share · <span class="${signClass(h.gain_pct)}">${pct(h.gain_pct)}</span></small></div></div>`).join(''):empty('No holdings yet. Add an account, a holding and a buy transaction in Manage.');
  const valued=d.holdings.filter(h=>h.value!=null).map(h=>({...h,aud:h.value*(h.currency==='USD'?(d.fx_rate||0):1)}));
  const total=valued.reduce((s,h)=>s+h.aud,0);
  $('allocation').innerHTML=valued.length&&total>0?valued.map(h=>`<div class="row"><div class="row-main"><b>${esc(h.symbol)}</b><small>${(h.aud/total*100).toFixed(1)}% of invested holdings</small><div class="bar"><span style="width:${Math.max(1,h.aud/total*100)}%"></span></div></div><div class="row-side">${money(h.aud)}</div></div>`).join(''):empty('Add prices to see allocation.');
  $('cashSummary').innerHTML=d.cash.length?d.cash.map(a=>`<div class="row"><div class="row-main"><b>${esc(a.name)}</b><small>${esc(a.broker||a.kind)}</small></div><div class="row-side"><b>${money(a.balance,a.currency)}</b></div></div>`).join(''):empty('No cash accounts yet.');
  drawChart();
}
function drawChart(){
  const canvas=$('chart'),rect=canvas.getBoundingClientRect(),scale=window.devicePixelRatio||1;
  if(!rect.width)return;
  canvas.width=Math.round(rect.width*scale);canvas.height=Math.round(rect.height*scale);
  const ctx=canvas.getContext('2d');ctx.scale(scale,scale);
  const w=rect.width,h=rect.height, series=state.dashboard.series;
  const start=state.range==='all'?0:Math.max(0,series.length-Number(state.range));
  const points=series.slice(start), valid=points.flatMap(x=>[x.value,x.invested]).filter(x=>x!=null&&isFinite(x));
  if(!valid.length){ctx.fillStyle='#99aabf';ctx.font='14px sans-serif';ctx.fillText('Add dated prices and exchange rates to draw the chart',15,h/2);return}
  let min=Math.min(...valid),max=Math.max(...valid);if(min===max){min-=1;max+=1}
  const left=55,right=12,top=14,bottom=25,x=i=>left+(points.length<=1?0:i/(points.length-1))*(w-left-right),y=v=>top+(max-v)/(max-min)*(h-top-bottom);
  ctx.strokeStyle='#31425d';ctx.fillStyle='#95a7c0';ctx.lineWidth=1;ctx.font='11px sans-serif';
  for(let k=0;k<4;k++){const py=top+k*(h-top-bottom)/3;ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(w-right,py);ctx.stroke();const val=max-k*(max-min)/3;ctx.fillText(val>=1000?`${(val/1000).toFixed(1)}k`:val.toFixed(0),3,py+4)}
  for(const [key,color] of [['invested','#b6a5eb'],['value','#81e3d7']]){
    ctx.strokeStyle=color;ctx.lineWidth=2.5;ctx.beginPath();let drawing=false;
    points.forEach((p,i)=>{if(p[key]==null){drawing=false;return}if(!drawing)ctx.moveTo(x(i),y(p[key]));else ctx.lineTo(x(i),y(p[key]));drawing=true});ctx.stroke();
  }
  ctx.fillStyle='#95a7c0';ctx.fillText(points[0].day,left,h-5);if(points.length>1)ctx.fillText(points.at(-1).day,w-80,h-5);
}
function renderTransactions(){
  const term=$('txSearch').value.toLowerCase(),type=$('txType').value,account=$('txAccount').value,from=$('txFrom').value,to=$('txTo').value;
  const items=state.tx.filter(t=>(!type||t.type===type)&&(!account||String(t.account_id)===account)&&(!from||t.occurred_at>=from)&&(!to||t.occurred_at<=to)&&(!term||`${t.symbol||''} ${t.account_name} ${t.note} ${t.type}`.toLowerCase().includes(term)));
  $('txList').innerHTML=items.length?items.map(t=>`<div class="row"><div class="row-main"><b>${esc(t.type.toUpperCase())} ${esc(t.symbol||'')}</b><small>${esc(t.occurred_at)} · ${esc(t.account_name)}${t.target_account_name?' → '+esc(t.target_account_name):''}${t.note?' · '+esc(t.note):''}</small><small>${t.quantity?number(t.quantity)+' × '+money(t.price,t.currency):''}${t.tax?' · tax '+money(t.tax,t.currency):''}${t.fee?' · fee '+money(t.fee,t.currency):''}</small></div><div class="row-side"><b>${money(t.amount||((t.quantity||0)*(t.price||0)),t.currency)}</b>${state.auth.role==='admin'?`<small><button data-edit="${t.id}">Edit</button> <button data-delete="${t.id}">Delete</button></small>`:''}</div></div>`).join(''):empty('No matching transactions.');
}
function renderCash(){
  const d=state.dashboard;
  $('cashAccounts').innerHTML=d.cash.length?d.cash.map(a=>`<div class="row"><div class="row-main"><b>${esc(a.name)}</b><small>${esc(a.broker||'Bank')} · ${esc(a.kind)} · ${esc(a.currency)}</small></div><div class="row-side"><b class="${signClass(a.balance)}">${money(a.balance,a.currency)}</b></div></div>`).join(''):empty('Add your bank and broker cash accounts in Manage.');
}
function renderDocs(){
  const hid=$('docHolding').value,aid=$('docAccount').value,year=$('docYear').value;
  const data=state.docs.filter(d=>(!hid||String(d.instrument_id)===hid)&&(!aid||String(d.account_id)===aid)&&(!year||d.tax_year===year));
  $('docList').innerHTML=data.length?data.map(d=>`<div class="row"><div class="row-main"><b>${esc(d.title)}</b><small>${esc([d.symbol,d.account_name,d.tax_year,d.original_name].filter(Boolean).join(' · '))}</small></div><div class="row-side"><a href="/api/documents/${d.id}/file" target="_blank" rel="noopener">Open</a>${state.auth.role==='admin'?` <button data-delete-doc="${d.id}">Delete</button>`:''}</div></div>`).join(''):empty('No documents match these filters.');
}
function renderRecon(){
  const byAccount=new Map(state.dashboard.cash.map(a=>[a.id,a]));
  $('reconList').innerHTML=state.recon.length?state.recon.map(r=>{const account=byAccount.get(r.account_id),isToday=r.day===new Date().toLocaleDateString('en-CA'),difference=account?.account_value==null?null:r.reported_value-account.account_value;return `<div class="row"><div class="row-main"><b>${esc(r.account_name)} · ${esc(r.day)}</b><small>Broker reported ${money(r.reported_value,r.currency)}${r.note?' · '+esc(r.note):''}</small><small>${isToday?'Dashboard account estimate '+money(account?.account_value,r.currency):'Historical comparison needs a dated account snapshot'}</small></div><div class="row-side">${isToday&&difference!=null?`Difference ${money(difference,r.currency)}`:''}</div></div>`}).join(''):empty('No balance checks yet. Add a current broker balance in Manage.');
}
function renderDetail(){
  const h=state.dashboard.holdings.find(x=>x.id===state.selectedHolding);
  if(!h){show('overview');return}
  const tx=state.tx.filter(t=>t.instrument_id===h.id),docs=state.docs.filter(d=>d.instrument_id===h.id);
  const priceRows=h.prices.slice(-60).reverse();
  $('detailContent').innerHTML=`<div class="card"><div class="detail-head"><div><span class="pill">${esc(h.exchange)} · ${esc(h.currency)}</span><h2>${esc(h.symbol)}</h2><span class="muted">${esc(h.name)}</span></div><div class="row-side"><strong>${money(h.price,h.currency)}</strong><small>As of ${esc(h.price_day||'no price')} · ${esc(h.price_source||'')}</small></div></div><div class="detail-grid"><div class="metric"><small>Current value</small><strong>${money(h.value,h.currency)}</strong></div><div class="metric"><small>Shares</small><strong>${number(h.quantity)}</strong></div><div class="metric"><small>Average cost</small><strong>${money(h.avg_cost,h.currency)}</strong></div><div class="metric"><small>Unrealised gain</small><strong class="${signClass(h.gain)}">${money(h.gain,h.currency)} · ${pct(h.gain_pct)}</strong></div></div><p class="muted fine">Average cost uses a pooled cost basis. Realised gain on sales: ${money(h.realized,h.currency)}. Tax reporting may use different rules.</p></div>
  <div class="card chart-card"><h2>Price history</h2><canvas id="detailChart" height="200" aria-label="Holding price history chart"></canvas><p class="muted fine">Daily closing prices in ${esc(h.currency)}. Missing dates have no price.</p></div>
  <div class="two-col"><div class="card"><h2>Transactions</h2>${tx.length?tx.map(t=>`<div class="row"><div class="row-main"><b>${esc(t.type)} · ${esc(t.occurred_at)}</b><small>${esc(t.account_name)} · ${number(t.quantity)} shares</small></div><div class="row-side">${money(t.amount||t.quantity*t.price,t.currency)}</div></div>`).join(''):empty('No transactions.')}</div>
  <div class="card"><h2>Documents</h2>${docs.length?docs.map(d=>`<div class="row"><div class="row-main"><b>${esc(d.title)}</b><small>${esc(d.tax_year||d.original_name)}</small></div><a href="/api/documents/${d.id}/file" target="_blank" rel="noopener">Open</a></div>`).join(''):empty('No documents attached to this holding.')}</div></div>
  <div class="card"><h2>Recent price history</h2>${priceRows.length?priceRows.map(p=>`<div class="row"><span>${esc(p.day)} <small class="muted">${esc(p.source)}</small></span><b>${money(p.close,h.currency)}</b></div>`).join(''):empty('Add prices manually or connect a delayed price feed.')}</div>`;
  requestAnimationFrame(()=>drawDetailChart(h));
}
function drawDetailChart(h){
  const c=$('detailChart');if(!c)return;const rect=c.getBoundingClientRect();if(!rect.width)return;
  const scale=window.devicePixelRatio||1;c.width=Math.round(rect.width*scale);c.height=Math.round(rect.height*scale);
  const ctx=c.getContext('2d');ctx.scale(scale,scale);
  const points=h.prices.slice(-365),w=rect.width,height=rect.height;
  if(!points.length){ctx.fillStyle='#99aabf';ctx.font='14px sans-serif';ctx.fillText('Add historical prices to see this holding’s chart',12,height/2);return}
  let min=Math.min(...points.map(p=>p.close)),max=Math.max(...points.map(p=>p.close));if(min===max){min*=.95;max*=1.05}
  const left=55,right=12,top=14,bottom=25;
  ctx.fillStyle='#9aaec7';ctx.strokeStyle='#30445d';ctx.font='11px sans-serif';
  for(let i=0;i<4;i++){let yy=top+(height-top-bottom)*i/3;ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(w-right,yy);ctx.stroke();ctx.fillText((max-(max-min)*i/3).toFixed(2),3,yy+3)}
  ctx.strokeStyle='#82e3d7';ctx.lineWidth=2.5;ctx.beginPath();points.forEach((p,i)=>{const x=left+(points.length===1?0:i/(points.length-1))*(w-left-right),y=top+(max-p.close)/(max-min)*(height-top-bottom);if(i)ctx.lineTo(x,y);else ctx.moveTo(x,y)});ctx.stroke();
  ctx.fillText(points[0].day,left,height-5);if(points.length>1)ctx.fillText(points.at(-1).day,w-80,height-5);
}
function editTransaction(id){
  const t=state.tx.find(t=>t.id===id);if(!t)return;
  show('admin');state.editId=id;$('txFormTitle').textContent='Edit transaction';$('cancelEdit').hidden=false;
  for(const [key,value] of Object.entries(t)){const el=field($('txForm'),key);if(el)el.value=value??''}
  $('txForm').scrollIntoView({behavior:'smooth'});
}
async function action(fn){try{await fn()}catch(e){toast(e.message,true)}}
async function saveForm(form,path,method='POST'){
  const d=formData(form);d.portfolio_id=state.pid;
  await api(path,method,d);form.reset();await load();toast('Saved');
}
function bind(){
  $('loginForm').addEventListener('submit',e=>{e.preventDefault();(async()=>{try{const d=formData(e.target);state.auth=await api('/login','POST',d);$('loginError').textContent='';await enter()}catch(err){$('loginError').textContent=err.message}})()});
  $('logout').onclick=()=>action(async()=>{await api('/logout','POST');location.reload()});
  $('portfolioSelect').onchange=()=>action(async()=>{state.pid=Number($('portfolioSelect').value);show('overview');await load()});
  document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>show(b.dataset.page));
  $('backToOverview').onclick=()=>show('overview');
  $('holdings').addEventListener('click',e=>{const row=e.target.closest('[data-holding]');if(row){state.selectedHolding=Number(row.dataset.holding);show('holdingDetail');renderDetail()}});
  $('holdings').addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.dataset.holding){e.preventDefault();e.target.click()}});
  $('ranges').onclick=e=>{const b=e.target.closest('[data-range]');if(!b)return;state.range=b.dataset.range;document.querySelectorAll('#ranges button').forEach(x=>x.classList.toggle('active',x===b));drawChart()};
  window.addEventListener('resize',()=>{if(state.dashboard&&state.page==='overview')drawChart();if(state.page==='holdingDetail')renderDetail()});
  for(const id of ['txSearch','txType','txAccount','txFrom','txTo'])$(id).addEventListener(id==='txSearch'?'input':'change',renderTransactions);
  for(const id of ['docHolding','docAccount','docYear'])$(id).addEventListener('change',renderDocs);
  $('txList').onclick=e=>action(async()=>{const edit=e.target.closest('[data-edit]'),del=e.target.closest('[data-delete]');if(edit)editTransaction(Number(edit.dataset.edit));if(del&&confirm('Delete this transaction?')){await api(query(`/transactions/${del.dataset.delete}`),'DELETE');await load();toast('Deleted')}});
  $('docList').onclick=e=>action(async()=>{const b=e.target.closest('[data-delete-doc]');if(b&&confirm('Delete this document?')){await api(query(`/documents/${b.dataset.deleteDoc}`),'DELETE');await load();toast('Deleted')}});
  $('txForm').onsubmit=e=>{e.preventDefault();action(async()=>{const path=state.editId?`/transactions/${state.editId}`:'/transactions';await saveForm(e.target,path,state.editId?'PUT':'POST');state.editId=null;$('txFormTitle').textContent='Add transaction';$('cancelEdit').hidden=true})};
  $('cancelEdit').onclick=()=>{state.editId=null;$('txFormTitle').textContent='Add transaction';$('txForm').reset();$('cancelEdit').hidden=true};
  for(const [id,path] of [['accountForm','/accounts'],['instrumentForm','/instruments'],['priceForm','/prices'],['fxForm','/fx-rate'],['reconForm','/reconciliations']])$(id).onsubmit=e=>{e.preventDefault();action(()=>saveForm(e.target,path))};
  $('documentForm').onsubmit=e=>{e.preventDefault();action(async()=>{const data=new FormData(e.target);data.set('portfolio_id',state.pid);await api('/documents','POST',data);e.target.reset();await load();toast('Document uploaded')})};
  $('passwordForm').onsubmit=e=>{e.preventDefault();action(async()=>{await api('/change-password','POST',formData(e.target));e.target.reset();toast('Password changed')})};
  $('refreshPrices').onclick=()=>action(async()=>{const result=await api('/refresh','POST');await load();toast(`Updated ${result.updated.length} holdings${result.errors.length?'; '+result.errors.join('; '):''}`,!!result.errors.length)});
  $('refreshFx').onclick=()=>action(async()=>{const r=await api('/fx-refresh','POST');await load();toast(`USD/AUD ${r.rate} on ${r.day}`)});
  $('exportLink').onclick=e=>{e.preventDefault();window.location.href='/api'+query('/export/transactions.csv')};
}
async function enter(){
  $('login').hidden=true;$('app').hidden=false;
  state.pid=state.auth.portfolio_id;
  $('signedIn').textContent=state.auth.username;
  document.querySelectorAll('.admin-only').forEach(el=>el.hidden=state.auth.role!=='admin');
  if(state.auth.role==='viewer')$('portfolioSelect').hidden=true;
  const setup=await api(query('/setup'));
  $('portfolioSelect').innerHTML=setup.portfolios.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('');
  $('portfolioSelect').value=state.pid;
  $('editType').innerHTML=types.map(t=>`<option value="${t}">${t.toUpperCase()}</option>`).join('');
  $('txType').innerHTML='<option value="">All types</option>'+types.map(t=>`<option value="${t}">${t.toUpperCase()}</option>`).join('');
  const day=new Date().toLocaleDateString('en-CA');
  document.querySelectorAll('form input[type="date"]').forEach(el=>el.value=day);
  await load();show('overview');
}
bind();api('/auth').then(async a=>{if(a.authenticated){state.auth=a;await enter()}else if(!a.configured)$('loginError').textContent='Set account passwords in .env before signing in.'}).catch(e=>toast(e.message,true));
