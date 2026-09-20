'use strict';
const $=id=>document.getElementById(id);
const currency=new Intl.NumberFormat('es-AR',{style:'currency',currency:'ARS',maximumFractionDigits:2});
const fmt=value=>value===null||value===undefined?'Pendiente':currency.format(Number(value));
const stamp=value=>{try{return new Intl.DateTimeFormat('es-AR',{timeZone:'America/Argentina/Buenos_Aires',dateStyle:'short',timeStyle:'short'}).format(new Date(value));}catch{return value;}};
$('day').value=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Argentina/Buenos_Aires',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
let busy=false, allOrders=[], pageIndex=0, lastSuccess=0, timer=null;
function cell(row,value,tag='td'){const el=document.createElement(tag);el.textContent=value;row.append(el);}
function drawProducts(d){
 $('rows').replaceChildren();
 for(const product of d.sold_products||[]){
  for(const variant of product.variants){const row=document.createElement('tr');cell(row,product.product);cell(row,variant.variant);
   if(variant.components?.length){const detail=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Componentes';detail.append(summary);for(const part of variant.components){const line=document.createElement('div');line.textContent=part.quantity+' × '+part.sku+' · '+fmt(part.unit_cost);detail.append(line);}row.children[0].append(detail);}cell(row,String(variant.units));cell(row,fmt(variant.unit_cost));cell(row,fmt(variant.total_cost));$('rows').append(row);}
  const total=document.createElement('tr');total.className='product-total';cell(total,'TOTAL '+product.product,'th');cell(total,'','td');cell(total,String(product.units));cell(total,'');cell(total,fmt(product.total_cost));$('rows').append(total);
 }
 if(!(d.sold_products||[]).length){const row=document.createElement('tr');const empty=document.createElement('td');empty.colSpan=5;empty.textContent='Sin ventas pagadas para esta fecha.';row.append(empty);$('rows').append(row);}
 $('totalunits').textContent=String(d.sold_units??0);$('totalmerchandise').textContent=fmt(d.merchandise_cost);
}
function drawOrders(){
 const q=$('search').value.trim().toLocaleLowerCase('es');
 const rows=allOrders.filter(o=>JSON.stringify([o.id,o.items]).toLocaleLowerCase('es').includes(q));
 const pages=Math.max(1,Math.ceil(rows.length/50));pageIndex=Math.min(pageIndex,pages-1);$('orders').replaceChildren();
 for(const o of rows.slice(pageIndex*50,(pageIndex+1)*50)){
  const row=document.createElement('tr');cell(row,o.date_created?stamp(o.date_created):'—');cell(row,o.id);
  cell(row,({paid:'Pagada',cancelled:'Cancelada'})[o.status]||o.status);
  cell(row,(o.items||[]).map(i=>i.product+' · '+i.variant).join('; '));cell(row,String((o.items||[]).reduce((n,i)=>n+i.units,0)));
  for(const k of ['revenue','fee','cogs','margin'])cell(row,fmt(o[k]));$('orders').append(row);
 }
 $('page').textContent=(pageIndex+1)+' / '+pages;$('prev').disabled=pageIndex===0;$('next').disabled=pageIndex+1>=pages;$('ordercount').textContent='('+rows.length+')';
}
function clock(){
 $('clock').textContent=new Intl.DateTimeFormat('es-AR',{timeZone:'America/Argentina/Buenos_Aires',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date());
 if(lastSuccess && Date.now()-lastSuccess>90000){$('live').textContent='SIN ACTUALIZAR';$('live').className='error';}
}
async function update(){
 if(busy)return;clearTimeout(timer);busy=true;const startedAt=Date.now(),selectedDay=$('day').value,selectedPeriod=$('period').value;$('refresh').disabled=true;
 try{
  const response=await fetch('/monitor/data?date='+encodeURIComponent(selectedDay)+'&period='+encodeURIComponent(selectedPeriod),{cache:'no-store'});
  const d=await response.json();if(!response.ok)throw new Error(d.error||'No se pudo actualizar.');
  if(selectedDay!==$('day').value||selectedPeriod!==$('period').value)return;
  if(!d.stale)lastSuccess=Date.parse(d.fetched_at);$('live').textContent=d.stale?'SIN ACTUALIZAR':'LIVE';$('live').className=d.stale?'error':'';
  $('range').textContent=stamp(d.period_start)+' — '+stamp(d.period_end);
  const comparison=d.comparison;
  $('comparison').textContent=comparison?(comparison.percent===null?'Sin base de comparación':Number(comparison.percent).toLocaleString('es-AR',{maximumFractionDigits:1})+'% en ventas netas')+' · vs. '+stamp(comparison.start)+' — '+stamp(comparison.end):'Comparación pendiente';
  $('state').className=d.stale?'error':'';$('state').textContent=d.stale?'No se pudo actualizar · '+stamp(d.fetched_at):'Última actualización: '+stamp(d.fetched_at);
  $('gross').textContent=fmt(d.gross);$('cancelled').textContent=fmt(d.cancelled);
  const estimate=d.management_estimate;$('net').textContent=fmt(estimate?estimate.result:d.net_estimate);
  $('checktax').textContent=fmt(estimate?.check_tax);$('iibb').textContent=fmt(estimate?.iibb);$('fixed').textContent=fmt(d.fixed_costs);$('merchandise').textContent=fmt(d.merchandise_cost);
  $('ads').textContent=d.ads_status==='conciliado'?fmt(d.ads):(d.ads===null?'Pendiente':fmt(d.ads))+' · '+d.ads_missing_days+' día(s) pendiente(s)';
  $('fees').textContent=fmt(d.fees);$('logistics').textContent=fmt(d.logistics_known);$('logisticsstate').textContent=d.logistics_missing_orders?d.logistics_missing_orders+' órdenes pendientes':'';
  allOrders=d.orders||[];drawOrders();
  drawProducts(d);
 }catch(e){$('state').className='error';$('state').textContent=e.message;$('live').textContent='SIN ACTUALIZAR';$('live').className='error';}
 finally{busy=false;$('refresh').disabled=false;timer=setTimeout(update,(selectedDay!==$('day').value||selectedPeriod!==$('period').value)?0:Math.max(0,30000-(Date.now()-startedAt)));}
}
async function init(){
 const token=location.hash.slice(1);
 if(token){try{const r=await fetch('/monitor/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});const d=await r.json();if(!r.ok)throw new Error(d.error);}catch(e){$('state').className='error';$('state').textContent=e.message;return;}}
 clock();setInterval(clock,1000);await update();
}
$('period').addEventListener('change',()=>{pageIndex=0;update();});$('search').addEventListener('input',()=>{pageIndex=0;drawOrders();});$('prev').addEventListener('click',()=>{pageIndex--;drawOrders();});$('next').addEventListener('click',()=>{pageIndex++;drawOrders();});
$('refresh').addEventListener('click',update);$('day').addEventListener('change',update);init();
