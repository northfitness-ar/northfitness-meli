// Read-only DOM harness for the shipped dashboard script. No external requests.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.value='';this.textContent='';this.hidden=false;}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=[...nodes];}
 prepend(...nodes){this.children.unshift(...nodes);}
 addEventListener(){}
 get options(){return this.children;}
}
const html=fs.readFileSync('monitor.html','utf8');
const ids=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element()]));
ids.period.value='day';
const sample={gross:'2472039.00',cancelled:'53779.00',paid_sales:123,sold_units:139,
 fetched_at:'2026-09-21T23:59:00-03:00',period_start:'2026-09-21T00:00:00-03:00',period_end:'2026-09-22T00:00:00-03:00',
 traffic:{visits:1000,conversion_percent:'12.3'},sold_products:[{product:'Straps',units:3,total_cost:'30',average_sale_price:'133.47',variants:[{variant:'Negro',units:3,sale_price:'200.20',average_sale_price:'133.47',unit_cost:'10',total_cost:'30'}]}],
 orders:[{id:'1',status:'paid',items:[{product:'Straps',variant:'Negro',units:3,sale_price:'100'}],average_sale_price:'100',revenue:'300'}]};
let response=sample;
const context=vm.createContext({document:{getElementById:id=>ids[id],createElement:tag=>new Element(tag)},Intl,Date,Number,Map,Set,JSON,URLSearchParams,
 setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1,location:{hash:''},fetch:async()=>({ok:true,json:async()=>response})});
vm.runInContext(fs.readFileSync('monitor.js','utf8').replace('navigation();init();','navigation();'),context);
(async()=>{
 ids.day.value='2026-09-28';ids.period.value='month';vm.runInContext('navigation()',context);
 assert.equal(ids.reference.options.length,8);assert.equal(ids.reference.options[0].value,'2026-01-28');assert.equal(ids.reference.value,'2026-08-28');assert.equal(ids.comparebutton.disabled,false);
 ids.day.value='2026-08-31';vm.runInContext('navigation()',context);assert.equal(ids.reference.options[1].value,'2026-02-28');
 await vm.runInContext('update()',context);
 assert.equal(ids.paidsales.textContent,'123');assert.equal(ids.units.textContent,'139');
 assert.match(ids.netsales.textContent,/2\.418\.260,00/);
 assert.equal(ids.rows.children[0].children.length,10);assert.equal(ids.orders.children[0].children.length,11);
 response={current:sample,reference:{...sample,traffic:{visits:null,conversion_percent:null,reason:'Sin corte histórico'}},fetched_at:sample.fetched_at};
 await ids.comparebutton.onclick();
 assert.equal(ids.comparemetrics.children[0].children[0].textContent,'Facturación bruta');
 const labels=ids.comparemetrics.children.map(r=>r.children[0].textContent).join(' ');
 assert.doesNotMatch(labels,/Ads|Logística|Órdenes/);
 const visits=ids.comparemetrics.children.find(r=>r.children[0].textContent==='Visitas a publicaciones');
 assert.equal(visits.children[2].textContent,'No disponible');assert.equal(visits.children[3].textContent,'—');
 assert.doesNotMatch(html,/trafficstate|Ganancia después de comisión|Conversión = órdenes/);
 console.log('UI checks passed: monthly references, prices, totals, comparison labels and missing data.');
})().catch(e=>{console.error(e);process.exitCode=1;});
