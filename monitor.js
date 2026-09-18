'use strict';
const $=id=>document.getElementById(id);
const currency=new Intl.NumberFormat('es-AR',{style:'currency',currency:'ARS',maximumFractionDigits:2});
const fmt=value=>value===null||value===undefined?'Pendiente':currency.format(Number(value));
const stamp=value=>{try{return new Intl.DateTimeFormat('es-AR',{timeZone:'America/Argentina/Buenos_Aires',dateStyle:'short',timeStyle:'short'}).format(new Date(value));}catch{return value;}};
$('day').value=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Argentina/Buenos_Aires',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
let busy=false;
function cell(row,value,tag='td'){const el=document.createElement(tag);el.textContent=value;row.append(el);}
function drawProducts(d){
 $('rows').replaceChildren();
 for(const product of d.sold_products||[]){
  for(const variant of product.variants){const row=document.createElement('tr');cell(row,product.product);cell(row,variant.variant);cell(row,String(variant.units));cell(row,fmt(variant.unit_cost));cell(row,fmt(variant.total_cost));$('rows').append(row);}
  const total=document.createElement('tr');total.className='product-total';cell(total,'TOTAL '+product.product,'th');cell(total,'','td');cell(total,String(product.units));cell(total,'');cell(total,fmt(product.total_cost));$('rows').append(total);
 }
 if(!(d.sold_products||[]).length){const row=document.createElement('tr');const empty=document.createElement('td');empty.colSpan=5;empty.textContent='Sin ventas pagadas para esta fecha.';row.append(empty);$('rows').append(row);}
 $('totalunits').textContent=String(d.sold_units??0);$('totalmerchandise').textContent=fmt(d.merchandise_cost);
}
async function update(){
 if(busy)return;busy=true;$('refresh').disabled=true;
 try{
  const response=await fetch('/monitor/data?date='+encodeURIComponent($('day').value),{cache:'no-store'});
  const d=await response.json();if(!response.ok)throw new Error(d.error||'No se pudo actualizar.');
  $('state').className=d.stale?'error':'';$('state').textContent=d.stale?'No se pudo actualizar · '+stamp(d.fetched_at):'Última actualización: '+stamp(d.fetched_at);
  $('gross').textContent=fmt(d.gross);$('cancelled').textContent=fmt(d.cancelled);
  const estimate=d.management_estimate;$('net').textContent=fmt(estimate?estimate.result:d.net_estimate);
  $('checktax').textContent=fmt(estimate?.check_tax);$('iibb').textContent=fmt(estimate?.iibb);$('fixed').textContent=fmt(d.fixed_costs);$('merchandise').textContent=fmt(d.merchandise_cost);
  $('ads').textContent=d.ads_status==='conciliado'?fmt(d.ads):'Pendiente';
  drawProducts(d);
 }catch(e){$('state').className='error';$('state').textContent=e.message;}
 finally{busy=false;$('refresh').disabled=false;}
}
async function init(){
 const token=location.hash.slice(1);history.replaceState(null,'',location.pathname);
 if(token){try{const r=await fetch('/monitor/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});const d=await r.json();if(!r.ok)throw new Error(d.error);}catch(e){$('state').className='error';$('state').textContent=e.message;return;}}
 await update();setInterval(update,300000);
}
$('refresh').addEventListener('click',update);$('day').addEventListener('change',update);init();
