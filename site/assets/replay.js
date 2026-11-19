"use strict";
(async()=>{
const camera=document.getElementById('camera-video');if(!camera)return;
const play=document.getElementById('replay-play'),seek=document.getElementById('replay-seek'),speed=document.getElementById('replay-speed');
const colors=['#2563eb','#d97706','#087e75','#7c3aed','#b33e62','#526373'];
let rows,ready=false;
try{const r=await fetch('assets/demo/recorded-joints.json');if(!r.ok)throw Error('Joint data unavailable');rows=(await r.json()).samples;if(!rows?.length)throw Error('Empty recording');}
catch(e){document.getElementById('replay-time').textContent=e.message;return;}
const duration=rows.length/30;seek.max=String(duration-1/30);
const readouts=document.getElementById('joint-readouts');
for(let i=1;i<=7;i++){const e=document.createElement('div');e.innerHTML=`<span>${i<7?'J'+i:'G'}</span><strong id="value-${i}">—</strong><small>${i<7?'deg':'mm'}</small>`;e.style.borderTopColor=colors[i-1]||'#172331';readouts.append(e);}
function chart(id,columns,time,unit){
 const canvas=document.getElementById(id),rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;
 const width=Math.max(rect.width,200),height=160;canvas.width=width*dpr;canvas.height=height*dpr;const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
 const left=42,right=width-12,top=23,bottom=135;let min=Infinity,max=-Infinity;
 for(const row of rows)for(const col of columns){min=Math.min(min,row[col]);max=Math.max(max,row[col]);}
 min=Math.floor(min/10)*10;max=Math.ceil(max/10)*10;if(max<=min)max=min+1;
 ctx.font='11px system-ui';ctx.fillStyle='#627183';ctx.fillText(unit,5,12);
 for(let k=0;k<=3;k++){const value=min+(max-min)*k/3,y=bottom-k*(bottom-top)/3;ctx.strokeStyle='#e3e8ef';ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(right,y);ctx.stroke();ctx.fillText(value.toFixed(0),3,y+4);}
 for(const col of columns){ctx.strokeStyle=colors[col-1]||'#172331';ctx.lineWidth=1.4;ctx.beginPath();rows.forEach((r,i)=>{const x=left+(i/30)/duration*(right-left),y=bottom-(r[col]-min)/(max-min)*(bottom-top);if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);});ctx.stroke();}
 const x=left+time/duration*(right-left);ctx.strokeStyle='#172331';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,bottom);ctx.stroke();ctx.fillStyle='#627183';ctx.fillText('0 s',left,155);ctx.fillText(duration.toFixed(1)+' s',right-35,155);
}
function draw(){const time=Math.min(camera.currentTime,duration-1/30),row=rows[Math.min(rows.length-1,Math.floor(time*30))];seek.value=String(time);document.getElementById('replay-time').textContent=time.toFixed(2)+' s';for(let i=1;i<=7;i++)document.getElementById('value-'+i).textContent=row[i].toFixed(2);chart('joint-chart',[1,2,3,4,5,6],time,'J1–J6 / deg');chart('gripper-chart',[7],time,'G / mm');}
async function start(){if(!ready)return;try{if(camera.ended)camera.currentTime=0;await camera.play();play.dataset.playing='true';}catch(e){camera.pause();document.getElementById('replay-time').textContent='Playback unavailable: '+e.message;}}
function pause(){camera.pause();delete play.dataset.playing;}
play.addEventListener('click',()=>camera.paused?start():pause());
seek.addEventListener('input',()=>{camera.currentTime=Number(seek.value);draw();});
speed.addEventListener('change',()=>{camera.playbackRate=Number(speed.value);});
camera.addEventListener('ended',()=>{pause();draw();});camera.addEventListener('seeked',draw);camera.addEventListener('timeupdate',draw);
const loaded=v=>v.readyState>=1?Promise.resolve():new Promise((resolve,reject)=>{v.addEventListener('loadedmetadata',resolve,{once:true});v.addEventListener('error',reject,{once:true});});
try{await loaded(camera);ready=true;play.disabled=false;draw();}catch{document.getElementById('replay-time').textContent='Video could not load';return;}
function frame(){if(!camera.paused){draw();}requestAnimationFrame(frame);}requestAnimationFrame(frame);window.addEventListener('resize',draw);
})();
