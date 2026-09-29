// Credential UI contract without network or real user credentials.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('monitor-login.html','utf8');
function fixture(hash){
 const ids=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],{value:'',disabled:false,hidden:true,checked:true,type:'password',setAttribute(){}}]));
 const requests=[],visits=[],cleaned=[];
 const context=vm.createContext({document:{getElementById:id=>ids[id]},URLSearchParams,JSON,Error,TypeError,
  location:{hash,replace:path=>visits.push(path)},history:{replaceState:(_a,_b,path)=>cleaned.push(path)},
  fetch:async(path,options)=>{requests.push({path,body:JSON.parse(options.body)});return {ok:true,json:async()=>({ok:true})};}});
 vm.runInContext(fs.readFileSync('monitor-login.js','utf8'),context);
 return {ids,requests,visits,cleaned};
}
(async()=>{
 const normal=fixture('#old-permanent-token');
 assert.deepEqual(normal.cleaned,['/monitor/login']);
 normal.ids.username.value='salvador';normal.ids.password.value='test-only long password';
 await normal.ids.loginform.onsubmit({preventDefault(){}});
 assert.equal(normal.requests[0].path,'/monitor/login');
 assert.equal(normal.requests[0].body.token,undefined);
 assert.deepEqual(normal.visits,['/monitor']);assert.equal(normal.ids.password.value,'');
 const setup=fixture('#activate=one-use-test-token&user=maxi');
 assert.equal(setup.ids.username.value,'maxi');assert.equal(setup.ids.username.readOnly,true);
 assert.equal(setup.ids.confirmgroup.hidden,false);assert.equal(setup.ids.confirm.required,true);
 assert.equal(setup.ids.password.autocomplete,'new-password');
 setup.ids.password.value='test-only long password';setup.ids.confirm.value='different';
 await setup.ids.loginform.onsubmit({preventDefault(){}});
 assert.equal(setup.requests.length,0);assert.match(setup.ids.message.textContent,/no coinciden/);
 setup.ids.confirm.value=setup.ids.password.value;
 await setup.ids.loginform.onsubmit({preventDefault(){}});
 assert.equal(setup.requests[0].path,'/monitor/activate');assert.equal(setup.requests[0].body.token,'one-use-test-token');
 assert.equal(setup.ids.password.value,'');assert.equal(setup.ids.confirm.value,'');
 console.log('Login UI passed: removed fragments, separate flows, confirmation, and password clearing.');
})().catch(e=>{console.error(e);process.exitCode=1;});
