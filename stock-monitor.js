'use strict';
(()=>{
 const el=id=>document.getElementById(id); let data=null, busy=false, timer=null, pendingWrite=null;
 const text=(parent,tag,value,cls)=>{const n=document.createElement(tag);n.textContent=value;if(cls)n.className=cls;parent.append(n);return n;};
 const show=n=>n==null?'Sin dato':Number(n).toLocaleString('es-AR',{maximumFractionDigits:1});
 const date=s=>s?new Date(s).toLocaleString('es-AR',{timeZone:'America/Argentina/Buenos_Aires'}):'Pendiente';
 const kinds={receive:'Ingreso',sale:'Salida',transfer:'Despacho a Full',count:'Conteo físico',collection_dispatch:'Despacho de reserva Full'};
 function bar(td,days,threshold){
  text(td,'strong',days==null?'Sin estimación':days===0?'Agotado · 0 días':show(days)+(days>=730?'+':'')+' días');
  if(days==null)return;
  const meter=document.createElement('meter');meter.min=0;meter.max=90;meter.value=Math.min(days,90);meter.setAttribute('aria-label',show(days)+' días de stock');
  meter.className='stock-meter '+(days<threshold?'stock-low':'stock-ok');td.append(meter);
  text(td,'small','Reponer antes de '+threshold+' días');
 }
 function render(d){
  data=d;el('stockrows').replaceChildren();el('stockpending').replaceChildren();el('stockhistory').replaceChildren();el('stockassumptions').replaceChildren();el('stockcollections').replaceChildren();el('stockcollection').replaceChildren(new Option('Elegir colecta',''));
  el('stockstatus').textContent=d.error||'Stock actual · consultado '+date(d.as_of);el('stockstatus').className=d.error?'error':'';
  el('stockemail').textContent=d.email_ready?'Alertas por correo habilitadas · '+d.email_state:'Alertas por correo pendientes: falta configurar el envío de email.';
  const previous=el('stocksku').value;el('stocksku').replaceChildren();el('stockpurchase').replaceChildren(new Option('Sin pedido asociado',''));
  const names=new Map();
  for(const r of d.rows||[]){
   const label=r.name+' · '+r.variant;names.set(r.sku,label);el('stocksku').append(new Option(label,r.sku));
   const tr=document.createElement('tr');text(tr,'th',label);const f=text(tr,'td',show(r.full));
   const unavailable=(r.full_details||[]).filter(x=>x.not_available_quantity!=null);
   if(unavailable.length)text(f,'small',unavailable.reduce((n,x)=>n+x.not_available_quantity,0)+' no disponibles en Full');
   if(r.full==null)text(f,'small','Última captura: '+r.full_snapshot+' · '+date(r.full_snapshot_at));
   const w=text(tr,'td',show(r.warehouse_physical??r.warehouse)+' físicos');text(w,'small',show(r.warehouse)+' libres · '+show(r.warehouse_reserved||0)+' reservados para Full');if(r.estimated)text(w,'small','Estimado · revisar conteo','stock-warning');
   bar(text(tr,'td',''),r.full_days,r.full_target_days);bar(text(tr,'td',''),r.total_days,r.lead_days+r.safety_days);
   const action=text(tr,'td',r.action);if(r.full_action)text(action,'strong',r.full_action);text(action,'small',show(r.rate)+' unidades/día · '+r.pending+' pendientes');
   if(r.suggested>0)text(action,'small','Compra sugerida: '+r.suggested);el('stockrows').append(tr);
  }
  if([...el('stocksku').options].some(o=>o.value===previous))el('stocksku').value=previous;
  for(const p of d.pending||[]){
   el('stockpurchase').append(new Option(p.name||p.id,p.id));
   for(const i of p.items){if(!i.remaining)continue;const tr=document.createElement('tr');tr.className='stock-inbound';
    for(const v of [p.name||p.id,p.status,names.get(i.sku)||i.sku,show(i.remaining),p.eta||'Sin fecha confirmada'])text(tr,'td',v);el('stockpending').append(tr);
   }
  }
  el('stockfullaccess').textContent=d.full_access_limitations||'';
  for(const c of d.full_collections||[]){
   if(c.status==='reserved'&&c.items.some(i=>i.reserved_remaining>0))el('stockcollection').append(new Option(c.name||c.id,c.id));
   for(const i of c.items){const tr=document.createElement('tr');
    const status=c.status==='awaiting_stock'?'Planificada · espera ingreso de mercadería':c.status==='unverified'?'Pendiente de confirmar retiro':i.reserved_remaining?'Reservada · todavía en depósito':'Despachada · conciliar ingreso con MELI';
    for(const v of [(c.name||c.id)+' · '+(c.source||'Declarada'),c.date||'Sin fecha confirmada',names.get(i.sku)||i.sku,show(i.reserved_remaining),show(i.dispatched),status+' · '+i.quantity+' unidades previstas'])text(tr,'td',v);
    el('stockcollections').append(tr);
   }
  }
  const receipts=d.full_receptions;
  el('stockreceptions').textContent=!receipts?'Ingresos Full: sin consulta.':receipts.error||('Ingresos registrados por MELI: '+(receipts.results||[]).length+' movimientos en la página consultada · '+(receipts.complete?'consulta completa':'hay más páginas')+'. Se consultan para conciliar; no confirman automáticamente una colecta.');
  for(const e of d.history||[]){const tr=document.createElement('tr');for(const v of [date(e.created),e.actor,names.get(e.sku)||e.sku,kinds[e.kind],e.quantity,e.note])text(tr,'td',v);el('stockhistory').append(tr);}
  el('stockmethod').textContent=d.method||'';
  for(const a of [...(d.assumptions||[]),...(d.missing_listings?.length?['Hay ventas sin equivalencia de inventario: '+d.missing_listings.join(', ')+'. Las recomendaciones requieren revisión.']:[])])text(el('stockassumptions'),'li',a);
 }
 async function refresh(force=false){
  if(busy||document.querySelector('main').hidden)return;busy=true;clearTimeout(timer);el('stockrefresh').disabled=true;
  try{const r=await fetch('/monitor/stock'+(force?'?refresh=1':''),{cache:'no-store'});if(r.status===401){location.replace('/monitor/login');return;}const d=await r.json();if(!r.ok)throw Error(d.error||'No se pudo consultar stock.');render(d);}
  catch(e){el('stockstatus').textContent='SIN ACTUALIZAR · '+e.message;el('stockstatus').className='error';}
  finally{busy=false;el('stockrefresh').disabled=false;timer=setTimeout(refresh,300000);}
 }
 function selectSection(section,focus=false){
  const stock=section==='stock';
  el('profitability-view').hidden=stock;el('stocksection').hidden=!stock;
  document.querySelector('header .controls').hidden=stock;
  document.querySelector('h1').textContent=stock?'Stock y reposición':'Monitor de rentabilidad';
  for(const [id,active] of [['tab-profit',!stock],['tab-stock',stock]]){
   el(id).setAttribute('aria-selected',String(active));el(id).tabIndex=active?0:-1;
   if(active&&focus)el(id).focus();
  }
  try{localStorage.setItem('nf-monitor-section',stock?'stock':'profit');}catch(_){}
 }
 for(const [id,section] of [['tab-profit','profit'],['tab-stock','stock']]){
  el(id).onclick=()=>selectSection(section);
  el(id).onkeydown=event=>{
   if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){
    event.preventDefault();selectSection(event.key==='Home'?'profit':event.key==='End'?'stock':section==='stock'?'profit':'stock',true);
   }
  };
 }
 let initialSection='profit';try{initialSection=localStorage.getItem('nf-monitor-section')||'profit';}catch(_){}
 selectSection(initialSection);
 el('stockrefresh').onclick=()=>refresh(true);
 el('stockkind').onchange=()=>{el('stockpurchase').disabled=el('stockkind').value!=='receive';if(el('stockpurchase').disabled)el('stockpurchase').value='';};
 el('stockform').onsubmit=async event=>{
  event.preventDefault();if(!data?.revision)return;el('stocksave').disabled=true;
  const payload=pendingWrite||{id:crypto.randomUUID(),revision:data.revision,sku:el('stocksku').value,kind:el('stockkind').value,quantity:Number(el('stockqty').value),purchase_id:el('stockpurchase').value,collection_id:el('stockcollection').value,note:el('stocknote').value}; pendingWrite=payload;
  try{const r=await fetch('/monitor/stock/movement',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(r.status===401){location.replace('/monitor/login');return;}const d=await r.json();if(!r.ok){pendingWrite=null;throw Error(d.error);}pendingWrite=null;el('stockmovestatus').textContent='Movimiento guardado.';el('stockqty').value='';el('stocknote').value='';await refresh(true);}
  catch(e){el('stockmovestatus').textContent=e.message||'No se pudo guardar. Actualizá antes de reintentar.';}
  finally{el('stocksave').disabled=false;}
 };
 refresh();
})();
