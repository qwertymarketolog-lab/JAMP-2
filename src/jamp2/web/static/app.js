"use strict";
const form=document.getElementById("claim-form");
if(form){form.addEventListener("submit",async(event)=>{event.preventDefault();const error=document.getElementById("claim-error");const button=document.getElementById("submit-claim");error.hidden=true;
const kind=document.getElementById("subject-kind").value;const unit=document.getElementById("unit").value;
if(!((kind==="crypto_asset"&&unit==="USD")||(kind==="equity_security"&&unit==="RUB"))){error.textContent="Недопустимая пара типа объекта и валюты.";error.hidden=false;return;}
const body={text:document.getElementById("text").value,subject:{kind,identifier:document.getElementById("identifier").value},predicate:"price",expected_value:Number(document.getElementById("expected-value").value),unit};
button.disabled=true;try{const response=await fetch("/claims/verify",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-JAMP-Dashboard-Request":"1"},body:JSON.stringify(body)});const result=await response.json();
if(response.status===201&&result.redirect_url){window.location.assign(result.redirect_url);return;}error.textContent=String(response.status)+": "+(result.error||"REQUEST_FAILED");
}catch(_){error.textContent="Dashboard transport failed. This is not evidence of absence.";}finally{button.disabled=false;error.hidden=false;}});}
const replayButton=document.querySelector("[data-replay-id]");
if(replayButton){replayButton.addEventListener("click",async()=>{const id=replayButton.dataset.replayId;const output=document.getElementById("replay-result");const error=document.getElementById("replay-error");output.hidden=true;error.hidden=true;replayButton.disabled=true;
try{const response=await fetch("/verdicts/"+encodeURIComponent(id)+"/replay",{method:"POST",credentials:"same-origin",headers:{"X-JAMP-Dashboard-Request":"1"}});const result=await response.json();
if(!response.ok){error.textContent=String(response.status)+": "+(result.detail||result.error||"REPLAY_FAILED");error.hidden=false;}else{output.textContent=JSON.stringify(result,null,2);output.hidden=false;}}
catch(_){error.textContent="Replay transport failed. No successful replay is claimed.";error.hidden=false;}finally{replayButton.disabled=false;}});}
