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
 average_ticket:'19660.65',units_per_sale:'1.13',product_profit:'500000',days_count:1,management_estimate:{ads_included_days:0,result:'400000'},
 comparison:{current_sales:110,sales:100,current_paid_sales:12,paid_sales:10,current_units:15,units:10,current_start:'2026-09-21T00:00:00-03:00',current_end:'2026-09-21T15:00:00-03:00',start:'2026-09-14T00:00:00-03:00',end:'2026-09-14T15:00:00-03:00'},
 fetched_at:'2026-09-21T23:59:00-03:00',period_start:'2026-09-21T00:00:00-03:00',period_end:'2026-09-22T00:00:00-03:00',
 traffic:{visits:1000,conversion_percent:'12.3'},sold_products:[{product:'Straps',units:3,total_cost:'30',average_sale_price:'133.47',variants:[{variant:'Negro',units:3,sale_price:'200.20',average_sale_price:'133.47',unit_cost:'10',total_cost:'30'}]}],
 orders:[{id:'1',status:'paid',items:[{product:'Straps',variant:'Negro',units:3,sale_price:'100'}],average_sale_price:'100',revenue:'300'}]};
let response=sample;
const preferences=new Map();const localStorage={getItem:k=>preferences.get(k),setItem:(k,v)=>preferences.set(k,v)};
const context=vm.createContext({document:{getElementById:id=>ids[id],createElement:tag=>new Element(tag)},Intl,Date,Number,Map,Set,JSON,URLSearchParams,
 setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1,localStorage,location:{hash:''},fetch:async()=>({ok:true,json:async()=>response})});
vm.runInContext(fs.readFileSync('monitor.js','utf8').replace('saveSettings();init();','saveSettings();'),context);
(async()=>{
 ids.day.value='2026-09-28';ids.period.value='month';vm.runInContext('navigation()',context);
 assert.equal(ids.reference.options.length,8);assert.equal(ids.reference.options[0].value,'2026-01-28');assert.equal(ids.reference.value,'2026-08-28');assert.equal(ids.comparebutton.disabled,false);
 ids.day.value='2026-08-31';vm.runInContext('navigation()',context);assert.equal(ids.reference.options[1].value,'2026-02-28');
 await vm.runInContext('update()',context);
 assert.equal(ids.paidsales.textContent,'123');assert.equal(ids.units.textContent,'139');
 assert.equal(ids.quickrevenue.textContent,'+10%');assert.equal(ids.quicksales.textContent,'+20%');assert.equal(ids.quickunits.textContent,'+50%');
 assert.equal(ids.unitsperorder.textContent,'1,13');assert.match(ids.resultscope.textContent,/Antes de Ads/);
 ids.productsort.value='profit';assert.equal(vm.runInContext("ranked([{profit:null,units:10},{profit:20,units:2},{profit:100,units:1}])[0].profit",context),100);
 ids.productsort.value='units';assert.equal(vm.runInContext("ranked([{profit:null,units:10},{profit:20,units:2},{profit:100,units:1}])[0].units",context),10);
 ids.reference.value='2026-02-28';vm.runInContext('saveSettings()',context);
 assert.equal(JSON.parse(preferences.get('nf-monitor-view')).sort,'units');assert.equal(JSON.parse(preferences.get('nf-monitor-view')).reference,'2026-02-28');
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
