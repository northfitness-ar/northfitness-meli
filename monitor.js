'use strict';
const $=id=>document.getElementById(id);
const currency=new Intl.NumberFormat('es-AR',{style:'currency',currency:'ARS',maximumFractionDigits:2});
const fmt=value=>value===null||value===undefined?'Pendiente':currency.format(Number(value));
const stamp=value=>{try{return new Intl.DateTimeFormat('es-AR',{timeZone:'America/Argentina/Buenos_Aires',dateStyle:'short',timeStyle:'short'}).format(new Date(value));}catch{return value;}};
$('day').value=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Argentina/Buenos_Aires',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
let busy=false, allOrders=[], pageIndex=0, lastSuccess=0, timer=null;
function cell(row,value,tag='td'){const el=document.createElement(tag);el.textContent=value;row.append(el);}
const percent=value=>value===null||value===undefined?'Pendiente':Number(value).toLocaleString('es-AR',{maximumFractionDigits:2})+'%';
function profitCells(row,data){cell(row,percent(data.margin_percent));cell(row,fmt(data.unit_profit));cell(row,fmt(data.profit));}
function drawProducts(d){
 $('rows').replaceChildren();
 for(const product of d.sold_products||[]){
  for(const variant of product.variants){const row=document.createElement('tr');cell(row,product.product);cell(row,variant.variant);
   if(variant.components?.length){const detail=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Componentes';detail.append(summary);for(const part of variant.components){const line=document.createElement('div');line.textContent=part.quantity+' × '+part.sku+' · '+fmt(part.unit_cost);detail.append(line);}row.children[0].append(detail);}cell(row,String(variant.units));cell(row,fmt(variant.unit_cost));cell(row,fmt(variant.total_cost));profitCells(row,variant);$('rows').append(row);}
  const total=document.createElement('tr');total.className='product-total';cell(total,'TOTAL '+product.product,'th');cell(total,'','td');cell(total,String(product.units));cell(total,'');cell(total,fmt(product.total_cost));profitCells(total,product);$('rows').append(total);
 }
 if(!(d.sold_products||[]).length){const row=document.createElement('tr');const empty=document.createElement('td');empty.colSpan=8;empty.textContent='Sin ventas pagadas para esta fecha.';row.append(empty);$('rows').append(row);}
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
  $('comparison').textContent='Comparación detallada disponible en la sección inferior.';
  $('state').className=d.stale?'error':'';$('state').textContent=d.stale?'No se pudo actualizar · '+stamp(d.fetched_at):'Última actualización: '+stamp(d.fetched_at);
  $('gross').textContent=fmt(d.gross);$('cancelled').textContent=fmt(d.cancelled);
  const traffic=d.traffic||{};
  $('visits').textContent=number(traffic.visits);$('paidsales').textContent=number(traffic.paid_orders);$('conversion').textContent=percent(traffic.conversion_percent);
  $('trafficstate').textContent=(traffic.reason||'Fuente: API de visitas de Mercado Libre.')+(traffic.fetched_at?' Consulta: '+stamp(traffic.fetched_at)+'.':'');
  const estimate=d.management_estimate;$('net').textContent=fmt(estimate?estimate.result:d.net_estimate);
  $('checktax').textContent=fmt(estimate?.check_tax);$('iibb').textContent=fmt(estimate?.iibb);$('fixed').textContent=fmt(d.fixed_costs);$('merchandise').textContent=fmt(d.merchandise_cost);
  $('ads').textContent=d.ads_status==='conciliado'?fmt(d.ads):(d.ads===null?'Pendiente':fmt(d.ads))+' · '+d.ads_missing_days+' día(s) pendiente(s)';
  $('fees').textContent=fmt(d.fees);$('logistics').textContent=fmt(d.logistics_known);$('logisticsstate').textContent=d.logistics_missing_orders?d.logistics_missing_orders+' órdenes pendientes':'';
  allOrders=d.orders||[];drawOrders();
  drawProducts(d);
 }catch(e){$('state').className='error';$('state').textContent=e.message;$('live').textContent='SIN ACTUALIZAR';$('live').className='error';}
 finally{busy=false;$('refresh').disabled=false;timer=setTimeout(update,(selectedDay!==$('day').value||selectedPeriod!==$('period').value)?0:Math.max(0,30000-(Date.now()-startedAt)));}
}

const iso=d=>d.toISOString().slice(0,10);
const dayDate=s=>new Date(s+'T12:00:00Z');
const today=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'America/Argentina/Buenos_Aires',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
function shiftDay(s,n){const d=dayDate(s);d.setUTCDate(d.getUTCDate()+n);return iso(d);}
function dateLabel(s){return new Intl.DateTimeFormat('es-AR',{timeZone:'UTC',weekday:'long',day:'numeric',month:'long'}).format(dayDate(s));}
function weekLabel(s){const d=dayDate(s),a=shiftDay(s,-((d.getUTCDay()+6)%7));return dateLabel(a)+' — '+dateLabel(shiftDay(a,6));}
function navigation(){
 const mode=$('period').value,day=$('day').value;
 $('day').max=today();
 $('periodlabel').textContent=mode==='week'?weekLabel(day):mode==='month'?day.slice(0,7):dateLabel(day);
 $('periodback').textContent=mode==='week'?'← Semana anterior':'← Anterior';
 $('periodforward').textContent=mode==='week'?'Semana siguiente →':'Siguiente →';
 const next=mode==='month'?day.slice(0,7)>=today().slice(0,7):shiftDay(mode==='week'?shiftDay(day,-((dayDate(day).getUTCDay()+6)%7)):day,mode==='week'?7:1)>today();
 $('periodforward').disabled=next;
 $('reference').replaceChildren();$('compareresults').hidden=true;$('compareranges').textContent='';
 const candidates=[];
 for(let n=-28;n<=28;n+=7){const candidate=shiftDay(day,n);if(n&&candidate.slice(0,7)===day.slice(0,7)&&candidate<=today())candidates.push(candidate);}
 for(const value of candidates){const option=document.createElement('option');option.value=value;option.textContent=mode==='week'?weekLabel(value):dateLabel(value);$('reference').append(option);}
 const previous=candidates.filter(x=>x<day).at(-1);if(previous)$('reference').value=previous;
 $('comparebutton').disabled=mode==='month'||!candidates.length;
 $('comparestate').textContent=mode==='month'?'Elegí Diario o Semanal para comparar días equivalentes dentro del mes.':candidates.length?'Elegí la referencia y pulsá Comparar.':'Todavía no hay otro día equivalente disponible en este mes.';
}
function movePeriod(direction){
 const mode=$('period').value,d=dayDate($('day').value);
 if(mode==='month'){d.setUTCDate(1);d.setUTCMonth(d.getUTCMonth()+direction);}else d.setUTCDate(d.getUTCDate()+direction*(mode==='week'?7:1));
 $('day').value=iso(d)>today()?today():iso(d);navigation();update();
}
$('periodback').onclick=()=>movePeriod(-1);$('periodforward').onclick=()=>movePeriod(1);
$('periodtoday').onclick=()=>{$('day').value=today();navigation();update();};
const number=v=>v===null||v===undefined?'Pendiente':Number(v).toLocaleString('es-AR',{maximumFractionDigits:2});
function change(a,b,points=false){
 if(a==null||b==null)return 'Pendiente';
 if(points)return (Number(a)-Number(b)).toLocaleString('es-AR',{signDisplay:'exceptZero',maximumFractionDigits:2})+' pp';
 if(Number(b)<=0)return Number(a)===0&&Number(b)===0?'Sin cambio':'Sin base porcentual';
 return ((Number(a)-Number(b))*100/Number(b)).toLocaleString('es-AR',{signDisplay:'exceptZero',maximumFractionDigits:1})+'%';
}
function metricRows(target,a,b,spec){
 target.replaceChildren();
 for(const [label,key,format,points] of spec){const row=document.createElement('tr');cell(row,label);cell(row,format(a[key]));cell(row,format(b[key]));cell(row,points?change(a[key],b[key])+' · '+change(a[key],b[key],true):change(a[key],b[key]));target.append(row);}
}
function metrics(d){
 const e=d.management_estimate||{};
 return {...d,visits:d.traffic?.visits,paid_orders:d.traffic?.paid_orders,conversion_percent:d.traffic?.conversion_percent,sales:Number(d.gross)-Number(d.cancelled),result:e.result,check_tax:e.check_tax,iibb:e.iibb,tax_base:e.tax_base,
 ads:d.ads_missing_days?null:d.ads,logistics_known:d.logistics_missing_orders?null:d.logistics_known,
 resultComparable:d.ads_missing_days||d.logistics_missing_orders?null:e.result};
}
function productKey(p){return JSON.stringify([p.product,...p.variants.flatMap(v=>v.components?.length?v.listing_keys:[]).sort()]);}
function productComparison(a,b){
 const root=$('compareproducts');root.replaceChildren();
 const am=new Map(a.map(p=>[productKey(p),p])),bm=new Map(b.map(p=>[productKey(p),p]));
 const fields=[['Unidades','units',number],['Ventas','sales',fmt],['Mercadería','total_cost',fmt],['Costo/u','unit_cost',fmt],['Ganancia/u','unit_profit',fmt],['Ganancia total','profit',fmt],['Margen','margin_percent',percent]];
 function line(table,label,x,y){
  const tr=document.createElement('tr');cell(tr,label,'th');
  for(const [title,k,f] of fields){const td=document.createElement('td');
   const vx=x?x[k]:(['units','sales','total_cost','profit'].includes(k)?0:null),vy=y?y[k]:(['units','sales','total_cost','profit'].includes(k)?0:null);
   td.textContent=f(vx)+' / '+f(vy);const small=document.createElement('small');small.textContent=change(vx,vy,k==='margin_percent');td.append(small);tr.append(td);}
  table.append(tr);
 }
 for(const key of new Set([...am.keys(),...bm.keys()])){
  const x=am.get(key),y=bm.get(key),details=document.createElement('details'),summary=document.createElement('summary');
  summary.textContent=(x||y).product+' · '+number(x?.units||0)+' / '+number(y?.units||0)+' unidades · '+change(x?.units||0,y?.units||0);details.append(summary);
  const wrap=document.createElement('div');wrap.className='tablewrap';const table=document.createElement('table'),thead=document.createElement('thead'),head=document.createElement('tr');
  for(const label of ['Variante',...fields.map(f=>f[0])])cell(head,label,'th');thead.append(head);table.append(thead);
  const body=document.createElement('tbody');
  const ax=new Map((x?.variants||[]).map(v=>[v.variant,v])),by=new Map((y?.variants||[]).map(v=>[v.variant,v]));
  for(const variant of new Set([...ax.keys(),...by.keys()]))line(body,variant,ax.get(variant),by.get(variant));
  const total=v=>v?{...v,unit_cost:v.total_cost==null?null:Number(v.total_cost)/v.units}:null;
  line(body,'TOTAL PRODUCTO',total(x),total(y));table.append(body);wrap.append(table);details.append(wrap);root.append(details);
 }
}
let comparisonRequest=0;
$('reference').onchange=()=>{$('compareresults').hidden=true;$('compareranges').textContent='';};
$('comparebutton').onclick=async()=>{
 const id=++comparisonRequest,day=$('day').value,mode=$('period').value,reference=$('reference').value;
 $('comparebutton').disabled=true;$('compareresults').hidden=true;$('comparestate').textContent='Consultando ambos períodos…';
 try{
  const r=await fetch('/monitor/compare?'+new URLSearchParams({date:day,period:mode,reference}),{cache:'no-store'}),d=await r.json();
  if(day!==$('day').value||mode!==$('period').value||reference!==$('reference').value)return;
  if(!r.ok)throw new Error(d.error||'No se pudo comparar.');
  const a=d.current,b=d.reference;
  $('compareranges').textContent='Seleccionado: '+stamp(a.period_start)+' — '+stamp(a.period_end)+' | Referencia: '+stamp(b.period_start)+' — '+stamp(b.period_end);
  metricRows($('comparemetrics'),metrics(a),metrics(b),[['Ventas brutas','gross',fmt],['Cancelaciones','cancelled',fmt],['Ventas netas de cancelaciones','sales',fmt],['Órdenes','orders_count',number],['Unidades','sold_units',number],['Mercadería','merchandise_cost',fmt],['Comisiones','fees',fmt],['Logística completa','logistics_known',fmt],['Órdenes con logística pendiente','logistics_missing_orders',number],['Ads cerrado','ads',fmt],['Días de Ads pendientes','ads_missing_days',number],['Gastos fijos','fixed_costs',fmt],['Impuesto al cheque','check_tax',fmt],['IIBB','iibb',fmt],['Base impositiva','tax_base',fmt],['Resultado con Ads y logística completos','resultComparable',fmt]]);
  productComparison(a.sold_products,b.sold_products);$('compareresults').hidden=false;
  const trafficRows=document.createElement('tbody');
  metricRows(trafficRows,metrics(a),metrics(b),[['Visitas a publicaciones','visits',number],['Ventas · órdenes pagadas','paid_orders',number],['Conversión estimada','conversion_percent',percent,true]]);
  $('comparemetrics').prepend(...trafficRows.children);
  $('comparestate').textContent='Consultado: '+stamp(d.fetched_at)+'. Ads y logística incompletos quedan pendientes; no se comparan como cero.';
  $('comparestate').textContent+=' Visitas: '+(a.traffic?.reason||'seleccionado disponible')+' / '+(b.traffic?.reason||'referencia disponible')+'. Conversión: variación relativa y diferencia en puntos porcentuales; fórmula estimada, no conciliada con el panel ML.';
 }catch(e){$('comparestate').textContent=e.message;}
 finally{if(id===comparisonRequest)$('comparebutton').disabled=$('period').value==='month'||!$('reference').options.length;}
};
async function init(){
 const token=location.hash.slice(1);
 if(token){try{const r=await fetch('/monitor/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});const d=await r.json();if(!r.ok)throw new Error(d.error);}catch(e){$('state').className='error';$('state').textContent=e.message;return;}}
 clock();setInterval(clock,1000);await update();
}
$('period').addEventListener('change',()=>{pageIndex=0;navigation();update();});$('search').addEventListener('input',()=>{pageIndex=0;drawOrders();});$('prev').addEventListener('click',()=>{pageIndex--;drawOrders();});$('next').addEventListener('click',()=>{pageIndex++;drawOrders();});
$('refresh').addEventListener('click',update);$('day').addEventListener('change',()=>{navigation();update();});navigation();init();
