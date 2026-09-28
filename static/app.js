const $ = id => document.getElementById(id);
const state = { auth:null, pid:null, setup:null, dashboard:null, tx:[], docs:[], recon:[], prices:[], fx:[], page:'overview', manage:'trades', range:'30', editing:{}, selectedHolding:null };
const types = ['buy','sell','deposit','withdrawal','dividend','interest','fee','transfer','fx','split','adjustment_in','adjustment_out'];
const esc = s => String(s ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const money = (n,c='AUD') => n == null || !Number.isFinite(Number(n)) ? '—' : new Intl.NumberFormat('en-AU',{style:'currency',currency:c,maximumFractionDigits:2}).format(n);
const pct = n => n == null ? '—' : `${n>=0?'+':''}${n.toFixed(2)}%`;
const number = n => Number(n||0).toLocaleString('en-AU',{maximumFractionDigits:6});
const signClass = n => n == null ? '' : n > 0 ? 'up' : n < 0 ? 'down' : '';
function cashMovement(t){
  const gross=Number(t.quantity||0)*Number(t.price||0),fee=Number(t.fee||0),amount=Number(t.amount||0);
  if(t.type==='buy')return -gross-fee;
  if(t.type==='sell')return gross-fee;
  if(t.type==='split')return null;
  if(['withdrawal','fee','transfer','fx','adjustment_out'].includes(t.type))return -amount-(['transfer','fx'].includes(t.type)?fee:0);
  if(t.type==='dividend')return amount-Number(t.tax||0)-fee;
  return amount;
}
const empty = message => `<div class="empty">${esc(message)}</div>`;
const melbourneDay = () => {const parts=Object.fromEntries(new Intl.DateTimeFormat('en-AU',{timeZone:'Australia/Melbourne',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(x=>[x.type,x.value]));return `${parts.year}-${parts.month}-${parts.day}`};
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
function showManage(panel='trades'){
  state.manage=panel;if(state.page!=='admin')show('admin');
  document.querySelectorAll('[data-manage]').forEach(b=>b.classList.toggle('active',b.dataset.manage===panel));
  document.querySelectorAll('[data-panel]').forEach(el=>el.hidden=el.dataset.panel!==panel);
}
async function load(){
  const [setup,dashboard,tx,docs,recon,prices,fx]=await Promise.all([
    api(query('/setup')),api(query('/dashboard')),api(query('/transactions')),api(query('/documents')),api(query('/reconciliations')),
    state.auth.role==='admin'?api('/prices'):Promise.resolve([]),state.auth.role==='admin'?api('/fx-rates'):Promise.resolve([])]);
  Object.assign(state,{setup,dashboard,tx,docs,recon,prices,fx});
  $('portfolioName').textContent=setup.portfolios.find(p=>p.id===state.pid)?.name+'’s portfolio';
  const latest=dashboard.holdings.map(h=>h.price_day).filter(Boolean).sort().at(-1);
  $('dataStamp').textContent=latest?`Latest holding price: ${latest}`:'No holding prices yet';
  renderOptions();updateTypeFields();renderOverview();renderTransactions();renderCash();renderDocs();renderRecon();renderManage();
  if(state.page==='holdingDetail')renderDetail();
}
function renderOptions(){
  const {accounts,instruments}=state.setup;
  for(const id of ['txAccount','docAccount'])options($(id),accounts,a=>`${a.name} · ${a.currency}`,'All accounts');
  options($('docHolding'),instruments,i=>`${i.symbol} · ${i.name}`,'All holdings');
  if(state.auth.role==='admin'){
    for(const form of [$('txForm'),$('cashForm'),$('documentForm'),$('docMetaForm'),$('reconForm')])
      options(field(form,'account_id'),accounts,a=>`${a.name} · ${a.currency}`,
              form===$('documentForm')||form===$('docMetaForm')?'None':'Choose account');
    options(field($('cashForm'),'target_account_id'),accounts,a=>`${a.name} · ${a.currency}`,'Choose destination');
    for(const form of [$('txForm'),$('cashForm'),$('documentForm'),$('docMetaForm'),$('priceForm')])
      options(field(form,'instrument_id'),instruments,i=>`${i.symbol} · ${i.name}`,
              form===$('documentForm')||form===$('docMetaForm')?'None':'Choose holding');
    field($('portfolioForm'),'name').value=state.setup.portfolios.find(p=>p.id===state.pid)?.name||'';
  }
  const years=[...new Set(state.docs.map(d=>d.tax_year).filter(Boolean))].sort().reverse();
  const selectedYear=$('docYear').value;
  $('docYear').innerHTML='<option value="">All tax years</option>'+years.map(y=>`<option>${esc(y)}</option>`).join('');
  if([...$('docYear').options].some(x=>x.value===selectedYear))$('docYear').value=selectedYear;
}
function renderOverview(){
  const d=state.dashboard;
  $('totalValue').textContent=money(d.value);
  $('totalValue').className=`big-number ${signClass(d.value)}`;
  $('totalContext').textContent=d.value==null?'Add prices and a USD/AUD rate to complete the valuation':`AUD · investments and cash${d.fx_rate?` · USD/AUD ${number(d.fx_rate)} (${d.fx_day})`:''}`;
  $('profit').textContent=money(d.profit);$('profit').className=signClass(d.profit);
  $('dayChange').textContent=money(d.day_change);$('dayChange').className=signClass(d.day_change);
  $('holdings').innerHTML=d.holdings.length?d.holdings.map(h=>`<div class="row clickable" data-holding="${h.id}" tabindex="0" role="button"><div class="row-main"><b>${esc(h.symbol)} <span class="pill">${esc(h.exchange)}</span></b><small>${esc(h.name)} · ${number(h.quantity)} shares · ${esc(h.brokers.map(b=>b.name).join(', '))}</small></div><div class="row-side"><b class="${signClass(h.value)}">${money(h.value,h.currency)}</b><small>${money(h.price,h.currency)} / share · <span class="${signClass(h.gain_pct)}">${pct(h.gain_pct)}</span></small></div></div>`).join(''):empty('No holdings yet. Add an account, a holding and a buy transaction in Manage.');
  const valued=d.holdings.filter(h=>h.value!=null).map(h=>({...h,aud:h.value*(h.currency==='USD'?(d.fx_rate||0):1)}));
  const total=valued.reduce((s,h)=>s+h.aud,0);
  $('allocation').innerHTML=valued.length&&total>0?valued.map(h=>`<div class="row"><div class="row-main"><b>${esc(h.symbol)}</b><small>${(h.aud/total*100).toFixed(1)}% of invested holdings</small><div class="bar"><span style="width:${Math.max(1,h.aud/total*100)}%"></span></div></div><div class="row-side ${signClass(h.aud)}">${money(h.aud)}</div></div>`).join(''):empty('Add prices to see allocation.');
  $('cashSummary').innerHTML=d.cash.length?d.cash.map(a=>`<div class="row"><div class="row-main"><b>${esc(a.name)}</b><small>Cash available · ${esc(a.currency)}</small></div><div class="row-side"><b class="${signClass(a.balance)}">${money(a.balance,a.currency)}</b></div></div>`).join(''):empty('No cash accounts yet.');
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
  $('txList').innerHTML=items.length?items.map(t=>`<div class="row"><div class="row-main"><b>${esc(t.type.replaceAll('_',' ').toUpperCase())} ${esc(t.symbol||'')}</b><small>${esc(t.occurred_at)} · ${esc(t.account_name)}${t.target_account_name?' → '+esc(t.target_account_name):''}${t.note?' · '+esc(t.note):''}</small><small>${t.quantity?number(t.quantity)+' × '+money(t.price,t.currency):''}${t.tax?' · tax '+money(t.tax,t.currency):''}${t.fee?' · fee '+money(t.fee,t.currency):''}</small></div><div class="row-side"><b class="${signClass(cashMovement(t))}">${money(cashMovement(t),t.currency)}</b>${state.auth.role==='admin'?state.recon.some(r=>r.adjustment_transaction_id===t.id)?'<small>Managed by its balance check</small>':`<small><button data-edit="${t.id}">Edit</button> <button data-delete="${t.id}">Delete</button></small>`:''}</div></div>`).join(''):empty('No matching transactions.');
}
function renderCash(){
  const d=state.dashboard;
  $('cashAccounts').innerHTML=d.cash.length?d.cash.map(a=>`<div class="row"><div class="row-main"><b>${esc(a.name)}</b><small>${esc(a.broker||'Bank')} · ${esc(a.currency)} · cash only</small></div><div class="row-side"><small>Cash balance</small><b class="${signClass(a.balance)}">${money(a.balance,a.currency)}</b></div></div>`).join(''):empty('Add your bank and broker cash accounts in Manage.');
}
function renderDocs(){
  const hid=$('docHolding').value,aid=$('docAccount').value,year=$('docYear').value;
  const data=state.docs.filter(d=>(!hid||String(d.instrument_id)===hid)&&(!aid||String(d.account_id)===aid)&&(!year||d.tax_year===year));
  $('docList').innerHTML=data.length?data.map(d=>`<div class="row"><div class="row-main"><b>${esc(d.title)}</b><small>${esc([d.symbol,d.account_name,d.tax_year,d.original_name].filter(Boolean).join(' · '))}</small></div><div class="row-side"><a href="/api/documents/${d.id}/file" target="_blank" rel="noopener">Open</a>${state.auth.role==='admin'?` <button data-edit-doc="${d.id}">Edit</button> <button data-delete-doc="${d.id}">Delete</button>`:''}</div></div>`).join(''):empty('No documents match these filters.');
}
function renderRecon(){
  const byAccount=new Map(state.dashboard.cash.map(a=>[a.id,a]));
  $('reconList').innerHTML=state.recon.length?state.recon.map(r=>{
    const account=byAccount.get(r.account_id),isToday=r.day===melbourneDay();
    const observed=account?.balance;
    const difference=observed==null?null:r.reported_value-observed;
    const match=state.auth.role==='admin'&&isToday&&difference!=null&&Math.abs(difference)>=.005?` <button data-match-recon="${r.id}" class="primary">Set cash to reported</button>`:'';
    return `<div class="row"><div class="row-main"><b>${esc(r.account_name)} · ${esc(r.day)} · Cash</b><small>Reported <span class="${signClass(r.reported_value)}">${money(r.reported_value,r.currency)}</span>${r.note?' · '+esc(r.note):''}</small><small>${isToday?`Current cash balance <span class="${signClass(observed)}">${money(observed,r.currency)}</span>`:'Historical check · current comparison unavailable'}</small></div><div class="row-side">${isToday&&difference!=null?`<span class="${signClass(difference)}">Difference ${money(difference,r.currency)}</span>`:''}${match}${state.auth.role==='admin'?` <button data-edit-recon="${r.id}">Edit</button> <button data-delete-recon="${r.id}">Delete</button>`:''}</div></div>`
  }).join(''):empty('No balance checks yet. Add a current broker balance in Manage.');
}
function renderManage(){
  if(state.auth.role!=='admin')return;
  const record=(title,sub,buttons,value=null)=>`<div class="row"><div class="row-main"><b class="${signClass(value)}">${esc(title)}</b><small>${esc(sub)}</small></div><div class="row-side">${buttons}</div></div>`;
  const editDelete=(kind,id)=>`<button data-edit-${kind}="${id}">Edit</button><button class="danger" data-delete-${kind}="${id}">Delete</button>`;
  const trade=state.tx.filter(t=>['buy','sell','split'].includes(t.type)).slice(0,6);
  const cash=state.tx.filter(t=>!['buy','sell','split'].includes(t.type)).slice(0,6);
  $('recentTrades').innerHTML=trade.length?trade.map(t=>record(`${t.type.toUpperCase()} ${t.symbol||''}`,`${t.occurred_at} · ${t.account_name} · ${number(t.quantity)} shares`,editDelete('tx',t.id))).join(''):empty('No trades yet.');
  $('recentCash').innerHTML=cash.length?cash.map(t=>record(`${t.type.replaceAll('_',' ').toUpperCase()} · ${money(cashMovement(t),t.currency)}`,`${t.occurred_at} · ${t.account_name}`,state.recon.some(r=>r.adjustment_transaction_id===t.id)?'<small>Managed in Balance checks</small>':editDelete('tx',t.id),cashMovement(t))).join(''):empty('No cash activity yet.');
  $('accountManageList').innerHTML=state.setup.accounts.length?state.setup.accounts.map(a=>{const cash=state.dashboard.cash.find(x=>x.id===a.id)?.balance;return record(`${a.name} · ${money(cash,a.currency)}`,`${a.broker||a.kind} · ${a.currency} · cash available`,editDelete('account',a.id),cash)}).join(''):empty('No accounts in this portfolio.');
  $('instrumentManageList').innerHTML=state.setup.instruments.length?state.setup.instruments.map(i=>record(i.symbol,`${i.name} · ${i.exchange}`,editDelete('instrument',i.id))).join(''):empty('No holdings added.');
  $('priceManageList').innerHTML=state.prices.length?state.prices.map(p=>record(`${p.symbol} · ${money(p.close,p.currency)}`,`${p.day} · ${p.source}`,`<button data-edit-price="${p.instrument_id}|${p.day}">Edit</button><button class="danger" data-delete-price="${p.instrument_id}|${p.day}">Delete</button>`,p.close)).join(''):empty('No prices recorded.');
  $('fxManageList').innerHTML=state.fx.length?state.fx.map(x=>record(`${number(x.usd_aud)} AUD / USD`,`${x.day} · ${x.source}`,`<button data-edit-fx="${x.day}">Edit</button><button class="danger" data-delete-fx="${x.day}">Delete</button>`)).join(''):empty('No exchange rates recorded.');
  $('manageDocs').innerHTML=state.docs.length?state.docs.map(d=>record(d.title,[d.symbol,d.account_name,d.tax_year].filter(Boolean).join(' · '),`<a href="/api/documents/${d.id}/file" target="_blank" rel="noopener">Open</a> ${editDelete('doc',d.id)}`)).join(''):empty('No documents yet.');
  $('manageChecks').innerHTML=state.recon.length?state.recon.map(r=>record(r.account_name,`${r.day} · cash ${money(r.reported_value,r.currency)}`,editDelete('recon',r.id),r.reported_value)).join(''):empty('No balance checks yet.');
}
function renderDetail(){
  const h=state.dashboard.holdings.find(x=>x.id===state.selectedHolding);
  if(!h){show('overview');return}
  const tx=state.tx.filter(t=>t.instrument_id===h.id),docs=state.docs.filter(d=>d.instrument_id===h.id);
  const priceRows=h.prices.slice(-60).reverse();
  $('detailContent').innerHTML=`<div class="card"><div class="detail-head"><div><span class="pill">${esc(h.exchange)} · ${esc(h.currency)}</span><h2>${esc(h.symbol)}</h2><span class="muted">${esc(h.name)}</span></div><div class="row-side"><strong class="${signClass(h.price)}">${money(h.price,h.currency)}</strong><small>As of ${esc(h.price_day||'no price')} · ${esc(h.price_source||'')}</small></div></div><div class="detail-grid"><div class="metric"><small>Current value</small><strong class="${signClass(h.value)}">${money(h.value,h.currency)}</strong></div><div class="metric"><small>Shares</small><strong>${number(h.quantity)}</strong></div><div class="metric"><small>Average cost</small><strong>${money(h.avg_cost,h.currency)}</strong></div><div class="metric"><small>Unrealised gain</small><strong class="${signClass(h.gain)}">${money(h.gain,h.currency)} · ${pct(h.gain_pct)}</strong></div></div><p class="muted fine">Average cost uses a pooled cost basis. Realised gain on sales: <span class="${signClass(h.realized)}">${money(h.realized,h.currency)}</span>. Tax reporting may use different rules.</p></div>
  <div class="card"><h2>Held through broker</h2>${h.brokers.map(b=>`<div class="row"><b>${esc(b.name)}</b><span>${number(b.quantity)} shares</span></div>`).join('')}</div>
  <div class="card chart-card"><h2>Price history</h2><canvas id="detailChart" height="200" aria-label="Holding price history chart"></canvas><p class="muted fine">Daily closing prices in ${esc(h.currency)}. Missing dates have no price.</p></div>
  <div class="two-col"><div class="card"><h2>Transactions</h2>${tx.length?tx.map(t=>`<div class="row"><div class="row-main"><b>${esc(t.type)} · ${esc(t.occurred_at)}</b><small>${esc(t.account_name)} · ${number(t.quantity)} shares</small></div><div class="row-side ${signClass(cashMovement(t))}">${money(cashMovement(t),t.currency)}</div></div>`).join(''):empty('No transactions.')}</div>
  <div class="card"><h2>Documents</h2>${docs.length?docs.map(d=>`<div class="row"><div class="row-main"><b>${esc(d.title)}</b><small>${esc(d.tax_year||d.original_name)}</small></div><a href="/api/documents/${d.id}/file" target="_blank" rel="noopener">Open</a></div>`).join(''):empty('No documents attached to this holding.')}</div></div>
  <div class="card"><h2>Recent price history</h2>${priceRows.length?priceRows.map(p=>`<div class="row"><span>${esc(p.day)} <small class="muted">${esc(p.source)}</small></span><b class="${signClass(p.close)}">${money(p.close,h.currency)}</b></div>`).join(''):empty('Add prices manually or connect a delayed price feed.')}</div>`;
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
  const isTrade=['buy','sell','split'].includes(t.type),form=$(isTrade?'txForm':'cashForm');
  showManage(isTrade?'trades':'cash');state.editing[form.id]=id;
  $(isTrade?'txFormTitle':'cashFormTitle').textContent='Edit '+(isTrade?'trade':'cash activity');
  $(isTrade?'cancelEdit':'cancelCashEdit').hidden=false;
  populate(form,t);updateTypeFields();
  if(t.note)form.querySelector('details')?.setAttribute('open','');
  form.scrollIntoView({behavior:'smooth',block:'start'});
}
async function action(fn){try{await fn()}catch(e){toast(e.message,true)}}
function populate(form,data){for(const [key,value] of Object.entries(data)){const el=field(form,key);if(el)el.value=value??''}}
function resetForm(form){form.reset();form.querySelectorAll('input[type="date"]').forEach(el=>el.value=melbourneDay());form.querySelector('details')?.removeAttribute('open')}
function updateReconFields(){
  const today=field($('reconForm'),'day').value===melbourneDay(),checkbox=field($('reconForm'),'apply_now');
  checkbox.disabled=!today;if(!today)checkbox.checked=false;
  $('reconHelp').textContent=today?'This sets only cash in the chosen account. Shares remain in Holdings. A matched correction appears in Transactions and affects profit.':'Historical checks are saved for reference; only a check dated today can change the current cash balance.';
}
function cancelEditor(id){
  const form=$(id);delete state.editing[id];resetForm(form);
  const title={txForm:'Record a trade',cashForm:'Record cash activity',accountForm:'Add account',instrumentForm:'Add holding',reconForm:'Add balance check'};
  if(title[id])$(id==='txForm'?'txFormTitle':id==='cashForm'?'cashFormTitle':id==='accountForm'?'accountFormTitle':id==='instrumentForm'?'instrumentFormTitle':'reconFormTitle').textContent=title[id];
  const cancel=form.querySelector('[data-cancel],#cancelEdit,#cancelCashEdit');if(cancel)cancel.hidden=true;
  if(id==='docMetaForm')form.hidden=true;
  updateTypeFields();
  if(id==='reconForm')updateReconFields();
}
function updateTypeFields(){
  const trade=$('editType').value,kind=$('cashType').value;
  document.querySelectorAll('[data-trade-price]').forEach(el=>el.hidden=trade==='split');
  document.querySelectorAll('[data-cash]').forEach(el=>el.hidden=!el.dataset.cash.split(' ').includes(kind));
  const account=state.setup?.accounts.find(a=>String(a.id)===field($('cashForm'),'account_id').value);
  document.querySelector('[data-fx-contribution]').hidden=!['deposit','withdrawal'].includes(kind)||account?.currency!=='USD';
}
async function saveForm(form,path,method='POST'){
  const d=formData(form);d.portfolio_id=state.pid;
  await api(path,method,d);resetForm(form);await load();toast('Saved');
}
function startEdit(formId, record, titleId, label, panel){
  showManage(panel);const form=$(formId);state.editing[formId]=record.id;
  populate(form,record);$(titleId).textContent=label;
  const cancel=form.querySelector('[data-cancel],#cancelEdit,#cancelCashEdit');if(cancel)cancel.hidden=false;
  form.scrollIntoView({behavior:'smooth',block:'start'});
}
function editDocument(id){
  const d=state.docs.find(x=>x.id===id);if(!d)return;
  showManage('files');const form=$('docMetaForm');form.hidden=false;state.editing.docMetaForm=id;
  populate(form,d);form.scrollIntoView({behavior:'smooth',block:'start'});
}
function editRecon(id){const r=state.recon.find(x=>x.id===id);if(r){startEdit('reconForm',r,'reconFormTitle','Edit balance check','checks');field($('reconForm'),'apply_now').checked=r.day===melbourneDay();updateReconFields()}}
async function deleteRecord(kind,id){
  const urls={tx:`/transactions/${id}`,account:`/accounts/${id}`,instrument:`/instruments/${id}`,doc:`/documents/${id}`,recon:`/reconciliations/${id}`};
  if(!confirm(`Delete this ${({tx:'transaction',instrument:'holding',doc:'document',recon:'balance check'})[kind]||kind}?${kind==='instrument'?' Its price history will also be removed.':''}${kind==='recon'?' Any cash correction made by this check will be removed.':''}`))return;
  await api(kind==='tx'||kind==='account'||kind==='doc'||kind==='recon'?query(urls[kind]):urls[kind],'DELETE');
  await load();toast('Deleted');
}
function bind(){
  $('loginForm').addEventListener('submit',e=>{e.preventDefault();(async()=>{try{const d=formData(e.target);state.auth=await api('/login','POST',d);$('loginError').textContent='';await enter()}catch(err){$('loginError').textContent=err.message}})()});
  $('logout').onclick=()=>action(async()=>{await api('/logout','POST');location.reload()});
  $('homeBrand').onclick=()=>show('overview');
  $('portfolioSelect').onchange=()=>action(async()=>{state.pid=Number($('portfolioSelect').value);Object.keys(state.editing).forEach(cancelEditor);show('overview');await load()});
  document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>b.dataset.page==='admin'?showManage(state.manage):show(b.dataset.page));
  $('backToOverview').onclick=()=>show('overview');
  $('holdings').addEventListener('click',e=>{const row=e.target.closest('[data-holding]');if(row){state.selectedHolding=Number(row.dataset.holding);show('holdingDetail');renderDetail()}});
  $('holdings').addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.dataset.holding){e.preventDefault();e.target.click()}});
  $('ranges').onclick=e=>{const b=e.target.closest('[data-range]');if(!b)return;state.range=b.dataset.range;document.querySelectorAll('#ranges button').forEach(x=>x.classList.toggle('active',x===b));drawChart()};
  window.addEventListener('resize',()=>{if(state.dashboard&&state.page==='overview')drawChart();if(state.page==='holdingDetail')renderDetail()});
  for(const id of ['txSearch','txType','txAccount','txFrom','txTo'])$(id).addEventListener(id==='txSearch'?'input':'change',renderTransactions);
  for(const id of ['docHolding','docAccount','docYear'])$(id).addEventListener('change',renderDocs);
  $('txList').onclick=e=>action(async()=>{const edit=e.target.closest('[data-edit]'),del=e.target.closest('[data-delete]');if(edit)editTransaction(Number(edit.dataset.edit));if(del)await deleteRecord('tx',del.dataset.delete)});
  $('docList').onclick=e=>action(async()=>{const edit=e.target.closest('[data-edit-doc]'),del=e.target.closest('[data-delete-doc]');if(edit)editDocument(Number(edit.dataset.editDoc));if(del)await deleteRecord('doc',del.dataset.deleteDoc)});
  $('reconList').onclick=e=>action(async()=>{
    const edit=e.target.closest('[data-edit-recon]'),del=e.target.closest('[data-delete-recon]'),match=e.target.closest('[data-match-recon]');
    if(edit)editRecon(Number(edit.dataset.editRecon));if(del)await deleteRecord('recon',del.dataset.deleteRecon);
    if(match){const r=state.recon.find(x=>x.id===Number(match.dataset.matchRecon)),a=state.dashboard.cash.find(x=>x.id===r?.account_id);
      if(!r||!a)return;
      const observed=a.balance;if(observed==null)return;
      const delta=r.reported_value-observed;
      if(confirm(`Set ${r.account_name} cash to ${money(r.reported_value,r.currency)}?\n\nCurrent cash: ${money(observed,r.currency)}. This will ${delta>=0?'add':'remove'} ${money(Math.abs(delta),r.currency)} of cash. Shares stay separate. The correction appears in Transactions and changes reported profit.`)){
        const result=await api(query(`/reconciliations/${r.id}/match`),'POST',{portfolio_id:state.pid,expected_cash_balance:observed});await load();toast(result.amount?'Cash now matches the check':'Cash already matches');
      }
    }
  });
  document.querySelectorAll('[data-manage]').forEach(b=>b.onclick=()=>showManage(b.dataset.manage));
  document.querySelectorAll('[data-goto]').forEach(b=>b.onclick=()=>show(b.dataset.goto));
  document.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=()=>cancelEditor(b.dataset.cancel));
  $('cancelEdit').onclick=()=>cancelEditor('txForm');$('cancelCashEdit').onclick=()=>cancelEditor('cashForm');
  $('editType').onchange=updateTypeFields;$('cashType').onchange=updateTypeFields;field($('cashForm'),'account_id').onchange=updateTypeFields;
  field($('reconForm'),'day').onchange=updateReconFields;
  $('admin').onclick=e=>action(async()=>{
    const b=e.target.closest('button[data-edit-tx],button[data-delete-tx],button[data-edit-account],button[data-delete-account],button[data-edit-instrument],button[data-delete-instrument],button[data-edit-price],button[data-delete-price],button[data-edit-fx],button[data-delete-fx],button[data-edit-doc],button[data-delete-doc],button[data-edit-recon],button[data-delete-recon]');
    if(!b)return;
    const attr=b.getAttributeNames().find(x=>x.startsWith('data-edit-')||x.startsWith('data-delete-'));
    const [op,kind]=attr.slice(5).split('-'),id=b.getAttribute(attr);
    if(op==='delete'){
      if(kind==='price'){const [iid,day]=id.split('|');if(confirm('Delete this price?')){await api(`/prices/${iid}/${day}`,'DELETE');await load();toast('Deleted')}return}
      if(kind==='fx'){if(confirm('Delete this exchange rate?')){await api(`/fx-rate/${id}`,'DELETE');await load();toast('Deleted')}return}
      await deleteRecord(kind,id);return;
    }
    if(kind==='tx'){editTransaction(Number(id));return}
    if(kind==='doc'){editDocument(Number(id));return}
    if(kind==='recon'){editRecon(Number(id));return}
    if(kind==='account'){const x=state.setup.accounts.find(a=>a.id===Number(id));if(x)startEdit('accountForm',x,'accountFormTitle','Edit account','accounts');return}
    if(kind==='instrument'){const x=state.setup.instruments.find(a=>a.id===Number(id));if(x)startEdit('instrumentForm',x,'instrumentFormTitle','Edit holding','holdings');return}
    if(kind==='price'){const [iid,day]=id.split('|');const p=state.prices.find(x=>String(x.instrument_id)===iid&&x.day===day);if(p){populate($('priceForm'),p);$('priceForm').scrollIntoView({behavior:'smooth',block:'start'});toast('Change the price and save to update it')}return}
    if(kind==='fx'){const f=state.fx.find(x=>x.day===id);if(f){populate($('fxForm'),f);$('fxForm').scrollIntoView({behavior:'smooth',block:'start'});toast('Change the rate and save to update it')}}
  });
  $('txForm').onsubmit=e=>{e.preventDefault();action(async()=>{const form=e.target,d=formData(form),id=state.editing.txForm;d.portfolio_id=state.pid;d.amount=0;d.target_amount=0;d.tax=0;d.target_account_id='';d.fx_rate=0;if(d.type==='split'){d.price=0;d.fee=0}await api(id?`/transactions/${id}`:'/transactions',id?'PUT':'POST',d);cancelEditor('txForm');await load();toast(id?'Trade updated':'Trade saved')})};
  $('cashForm').onsubmit=e=>{e.preventDefault();action(async()=>{const form=e.target,d=formData(form),id=state.editing.cashForm;d.portfolio_id=state.pid;d.quantity=0;d.price=0;if(d.type!=='dividend'){d.instrument_id='';d.tax=0}if(!['transfer','fx'].includes(d.type)){d.target_account_id='';d.target_amount=0;d.fee=0}if(d.type!=='fx')d.target_amount=0;if(!['deposit','withdrawal'].includes(d.type))d.fx_rate=0;await api(id?`/transactions/${id}`:'/transactions',id?'PUT':'POST',d);cancelEditor('cashForm');await load();toast(id?'Activity updated':'Activity saved')})};
  for(const [id,path] of [['accountForm','/accounts'],['instrumentForm','/instruments']])$(id).onsubmit=e=>{e.preventDefault();action(async()=>{const rid=state.editing[id];await api(rid?`${path}/${rid}`:path,rid?'PUT':'POST',{...formData(e.target),portfolio_id:state.pid});cancelEditor(id);await load();toast(rid?'Updated':'Saved')})};
  $('reconForm').onsubmit=e=>{e.preventDefault();action(async()=>{const id=state.editing.reconForm,d=formData(e.target);d.portfolio_id=state.pid;d.apply_now=field(e.target,'apply_now').checked&&!field(e.target,'apply_now').disabled;const result=await api(id?`/reconciliations/${id}`:'/reconciliations',id?'PUT':'POST',d);cancelEditor('reconForm');await load();show('reconcile');toast(result.applied?'Cash set to the reported value':'Balance check saved')})};
  $('portfolioForm').onsubmit=e=>{e.preventDefault();action(async()=>{const name=field(e.target,'name').value;await api(`/portfolios/${state.pid}`,'PUT',{name});const opt=$('portfolioSelect').querySelector(`option[value="${state.pid}"]`);if(opt)opt.textContent=name;await load();toast('Portfolio renamed')})};
  for(const [id,path] of [['priceForm','/prices'],['fxForm','/fx-rate']])$(id).onsubmit=e=>{e.preventDefault();action(()=>saveForm(e.target,path))};
  $('documentForm').onsubmit=e=>{e.preventDefault();action(async()=>{const data=new FormData(e.target);data.set('portfolio_id',state.pid);await api('/documents','POST',data);e.target.reset();await load();toast('Document uploaded')})};
  $('docMetaForm').onsubmit=e=>{e.preventDefault();action(async()=>{await api(`/documents/${state.editing.docMetaForm}`,'PUT',{...formData(e.target),portfolio_id:state.pid});cancelEditor('docMetaForm');await load();toast('Document updated')})};
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
  $('txType').innerHTML='<option value="">All types</option>'+types.map(t=>`<option value="${t}">${t.toUpperCase()}</option>`).join('');
  const day=melbourneDay();
  document.querySelectorAll('form input[type="date"]').forEach(el=>el.value=day);
  updateTypeFields();updateReconFields();await load();show('overview');
}
bind();api('/auth').then(async a=>{if(a.authenticated){state.auth=a;await enter()}else if(!a.configured)$('loginError').textContent='Set account passwords in .env before signing in.'}).catch(e=>toast(e.message,true));
