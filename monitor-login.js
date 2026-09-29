'use strict';
const $=id=>document.getElementById(id);
const fragment=new URLSearchParams(location.hash.slice(1));
let activation=fragment.get('activate');
const activating=!!activation;
// Secrets stay in memory only and are removed from browser history immediately.
history.replaceState(null,'','/monitor/login');
if(activating){
 $('title').textContent='Creá tu contraseña';
 $('intro').textContent='Elegí una contraseña de entre 15 y 128 caracteres. Podés usar una frase larga.';
 $('username').value=fragment.get('user')||'';$('username').readOnly=true;
 $('passwordlabel').textContent='Nueva contraseña';$('password').autocomplete='new-password';$('password').minLength=15;
 $('confirmgroup').hidden=false;$('confirm').required=true;
 $('submit').textContent='Activar mi cuenta';
 $('help').textContent='Este enlace es privado, se usa una sola vez y vence a las 24 horas.';
 $('backlogin').hidden=false;
}
$('showpassword').onclick=()=>{
 const show=$('password').type==='password';$('password').type=show?'text':'password';
 $('showpassword').textContent=show?'Ocultar':'Mostrar';$('showpassword').setAttribute('aria-pressed',String(show));
 $('showpassword').setAttribute('aria-label',show?'Ocultar contraseña':'Mostrar contraseña');
};
$('loginform').onsubmit=async event=>{
 event.preventDefault();if($('submit').disabled)return;
 $('message').textContent='';
 if(activating&&$('password').value!==$('confirm').value){$('message').textContent='Las contraseñas no coinciden.';return;}
 $('submit').disabled=true;
 try{
  const response=await fetch(activating?'/monitor/activate':'/monitor/login',{
   method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',
   body:JSON.stringify({username:$('username').value,password:$('password').value,remember:$('remember').checked,...(activating?{token:activation}:{})})
  });
  const result=await response.json();if(!response.ok)throw new Error(result.error||'No se pudo ingresar.');
  activation=null;$('password').value='';$('confirm').value='';location.replace('/monitor');
 }catch(error){$('message').textContent=error instanceof TypeError?'No se pudo conectar. Revisá tu conexión y volvé a intentar.':error.message;}
 finally{$('submit').disabled=false;}
};
