'use strict';
const $=id=>document.getElementById(id);
const currency=new Intl.NumberFormat('es-AR',{style:'currency',currency:'ARS',maximumFractionDigits:2});
const fmt=value=>value===null||value===undefined?'Pendiente':currency.format(Number(value));
const labels={comision:'Comisión',costo:'Costo',logistics:'Logística',tax_adjustment:'Impuestos',devoluciones_sin_conciliar:'Devoluciones',estado_no_liquidado:'Pago pendiente'};
$('day').value=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Argentina/Buenos_Aires',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
let busy=false;
async function update(){
 if(busy)return;busy=true;$('refresh').disabled=true;
 try{
  const response=await fetch('/monitor/data?date='+encodeURIComponent($('day').value),{cache:'no-store'});
  const d=await response.json();if(!response.ok)throw new Error(d.error||'No se pudo actualizar.');
  $('state').className=d.stale?'error':'';
  $('state').textContent=(d.stale?'LECTURA ANTERIOR · '+d.error+' ':'Última lectura: ')+d.fetched_at+(d.excluded_out_of_range?' · '+d.excluded_out_of_range+' pedidos fuera del período excluidos.':'');
  $('gross').textContent=fmt(d.gross);$('cancelled').textContent=fmt(d.cancelled);$('sales').textContent=fmt(d.sales_after_known_refunds);
  $('net').textContent=d.net_estimate===null?'Sin conciliar':fmt(d.net_estimate);
  $('netnote').textContent=d.net_estimate===null?'Faltan datos: no hay un neto confiable.':'Provisorio; sujeto a devoluciones y cargos posteriores.';
  $('coverage').textContent=d.complete_orders+' de '+d.orders_count+' pedidos completos ('+d.coverage_percent+'%)';
  $('ads').textContent=fmt(d.ads)+' · '+d.ads_status.replaceAll('_',' ');$('fixed').textContent=fmt(d.fixed_costs);
  $('rows').replaceChildren();
  for(const order of d.orders){const tr=document.createElement('tr');for(const value of [order.id,order.status,fmt(order.revenue),fmt(order.fee),fmt(order.cogs),fmt(order.margin),order.missing.map(k=>labels[k]||k).join(', ')||'Completo']){const td=document.createElement('td');td.textContent=value;tr.append(td);}$('rows').append(tr);}
  if(!d.orders.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=7;td.textContent='Sin operaciones en este período.';tr.append(td);$('rows').append(tr);}
 }catch(e){$('state').className='error';$('state').textContent=e.message+' Los valores visibles corresponden a la última lectura exitosa.';}
 finally{busy=false;$('refresh').disabled=false;}
}
async function init(){
 const token=location.hash.slice(1);history.replaceState(null,'',location.pathname);
 if(token){try{const r=await fetch('/monitor/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});const d=await r.json();if(!r.ok)throw new Error(d.error);}catch(e){$('state').className='error';$('state').textContent=e.message;return;}}
 await update();setInterval(update,300000);
}
$('refresh').addEventListener('click',update);$('day').addEventListener('change',update);init();
