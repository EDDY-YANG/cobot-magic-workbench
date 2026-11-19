"use strict";
(async()=>{let token;const form=document.getElementById('connect-form'),result=document.getElementById('connection-result');
try{const r=await fetch('sdk/config');if(!r.ok)throw Error();const s=await r.json();token=s.csrf;for(const [k,v] of Object.entries(s.config||{}))if(form.elements[k])form.elements[k].value=v;}
catch{result.textContent='Connect is available in the downloaded Windows SDK / 连接功能仅在下载的 Windows SDK 中提供';form.querySelector('button').disabled=true;}
form.addEventListener('submit',async e=>{e.preventDefault();const button=form.querySelector('button');button.disabled=true;result.textContent='Connecting / 连接中…';
try{const body=Object.fromEntries(new FormData(form));body.remote_port=Number(body.remote_port);body.local_port=Number(body.local_port);const r=await fetch('sdk/connect',{method:'POST',headers:{'Content-Type':'application/json','X-SDK-Token':token},body:JSON.stringify(body)});const s=await r.json();if(!r.ok)throw Error(s.error);result.textContent='Client ready / 客户端已启动：';const a=document.createElement('a');a.href=s.url;a.textContent='Open workbench / 打开工作台';result.append(a);window.location.assign(s.url);}
catch(error){result.textContent=error.message;button.disabled=false;}});})();
