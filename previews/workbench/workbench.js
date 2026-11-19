'use strict';
const $ = id => document.getElementById(id);
const mediaRoot = '../../site/assets/demo/';
const source = $('source');
const pages = {
  connect:['连接与校准','Connection','M8 3v5m8-5v5M6 8h12v2a6 6 0 0 1-6 6v5'],
  teleop:['遥操作与采集','Teleoperation','M3 7h12v11H3z M15 10l6-3v12l-6-3'],
  motion:['运动测试','Motion tests','M2 13h5l3-9 4 16 3-8h5'],
  deploy:['模型部署','Policy deployment','M3 12l9-8 9 8-9 8z M3 16l9 5 9-5'],
  dagger:['DAgger 介入','DAgger intervention','M3 8h15m-5-5 5 5-5 5 M21 16H6m5-5-5 5 5 5'],
  records:['试验记录','Recordings','M5 3h12l3 3v15H5z M8 10h9m-9 4h9m-9 4h6']
};
const paths = {play:'M8 4l12 8-12 8z',pause:'M7 5v14m9-14v14'};
function icon(path) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="'+path+'"/></svg>'; }
function dual(en,zh){return '<span class="dual"><b>'+en+'</b><small lang="zh-CN">'+zh+'</small></span>';}
function group(label,keys){return '<div class="nav-group">'+keys.map(k=>'<a href="#'+k+'" data-page="'+k+'">'+icon(pages[k][2])+dual(k==='deploy'?'Deployment':k==='dagger'?'DAgger':pages[k][1],pages[k][0])+'</a>').join('')+'</div>';}
$('navigation').innerHTML=group('', ['connect','teleop','motion','deploy','dagger'])+group('', ['records']);
const draft={route:'FP32 + RTC',step:'10000',duration:'40',task:'右臂将红色方块放入绿色桶'};
const routes=[['FP32 + RTC','FP32 + RTC'],['BF16 + 轨迹优化','BF16 + trajectory optimization']];
const colors=['#298b83','#2563eb','#c07616','#925578','#7954bd','#496c83'];
const visible=new Set([1,2,4]);
let current='deploy',samples=[],provenance=null,unavailable='',poster=null,dataReady=false;
let timerStart=null,waitFinished=false,initialSeek=true,clock=12,length=0,lastFrame=-1;
const isSample=()=>samples.length>0 && provenance;
function fmt(t){return Number.isFinite(t)?Math.floor((t+.001)/60)+':'+String(Math.floor((t+.001)%60)).padStart(2,'0'):'—';}
function facts(rows){return '<dl class="statuslist">'+rows.map(([en,zh,value])=>'<div><dt>'+dual(en,zh)+'</dt><dd>'+value+'</dd></div>').join('')+'</dl>';}
function section(en,zh,body,extra=''){return '<section class="module"><div class="section-head"><h2>'+dual(en,zh)+'</h2>'+extra+'</div>'+body+'</section>';}
function metric(en,zh,value,big=false){return '<div><label>'+dual(en,zh)+'</label><strong'+(big?' class="big mono"':'')+'>'+value+'</strong></div>';}
function button(en,zh,action,cls='button'){return '<button type="button" class="'+cls+'" data-do="'+action+'">'+dual(en,zh)+'</button>';}
function label(en,zh,field){return '<label class="field">'+dual(en,zh)+field+'</label>';}
function table(headers,rows){return '<div class="table-scroll"><table><thead><tr>'+headers.map(([en,zh])=>'<th>'+dual(en,zh)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(cell=>'<td>'+cell+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';}
function camera(){
 const views=[['front','Front','前视'],['right','Right wrist','右腕']];
 return section('Camera replay','相机回放','<div class="feeds">'+views.map(([id,en,zh])=>'<figure class="feed"><canvas id="cam-'+id+'" width="480" height="360" aria-label="'+en+' / '+zh+'"></canvas><figcaption>'+dual(en,zh)+'</figcaption></figure>').join('')+'</div><div class="playbar"><button id="play" data-do="play" aria-label="Play / 播放">'+icon(paths.play)+'</button><span class="clock" id="timecode">—</span><input id="scrub" type="range" step="0.033333" min="0" value="12" max="47" aria-label="Timeline / 回放进度"><span id="endtime" class="clock">—</span><select id="rate" aria-label="Speed / 倍速"><option value=".5">0.5×</option><option value="1">1×</option><option value="2">2×</option></select></div><p id="media-origin" class="source-line"></p>');
}
function plot(){return '<section class="module"><div class="chart-head"><h2>'+dual('Joint angles','关节角度')+'</h2><div class="legend">'+colors.map((c,i)=>'<button data-joint="'+i+'" style="--swatch:'+c+'" aria-pressed="'+visible.has(i)+'" aria-label="Toggle J'+(i+1)+'"><i></i>J'+(i+1)+'</button>').join('')+'</div></div><div class="chart-frame"><canvas id="trace" aria-label="Recorded joint angles / 实测关节角度"></canvas></div></section>';}
function parameters(){return '<section class="params"><div class="params-head"><h2>'+dual('Model & task','模型与任务')+'</h2></div><div class="params-body">'+label('Execution route','执行路线','<select id="route">'+routes.map(([r,en])=>'<option value="'+r+'">'+en+'</option>').join('')+'</select>')+'<div class="pair">'+label('Checkpoint','检查点','<select id="step">'+[5000,10000,15000,20000].map(s=>'<option>'+s+'</option>').join('')+'</select>')+label('Duration (s)','时长（秒）','<input id="duration" type="number" min="1" max="1800" step="1" required>')+'</div>'+label('Task prompt','任务指令','<textarea id="task" maxlength="256"></textarea>')+button('Review draft','查看部署草稿','draft','primary block')+'</div></section>';}
function admission(){return section('Readiness','执行条件',facts([['Robot','机械臂',dual('Offline','未连接')],['USB storage','录制 U 盘',dual('Unverified','未核验')],['Model','模型实例',dual('Offline','未接入')]]));}
function deployment(){return '<div class="split"><div>'+camera()+plot()+'</div><div class="sidebar-panel">'+parameters()+'<div class="module" style="margin-top:18px">'+admission()+'</div></div></div>';}
function sampleFacts(){return isSample()?facts([['State samples','状态记录',samples.length.toLocaleString('en-US')],['Front frames','前视原始帧',provenance.native_frames.front],['Wrist frames','右腕原始帧',provenance.native_frames.right],['Outcome','任务结果',dual('Unscored','未评分')]]):'—';}
function teleop(){return '<div class="split"><div>'+camera()+plot()+'</div><div class="sidebar-panel">'+section('Capture session','采集会话',facts([['Mode','模式',dual('Replay','回放')],['Recorder','录制器',dual('Offline','未连接')]])+button('Inspect recording','查看录制信息','source'))+section('Samples','采样',sampleFacts())+'</div></div>';}
function jointTable(){return table([['Joint','关节'],['Measured (°)','实测角度']],colors.map((_,i)=>['J'+(i+1),'<span id="measure-'+i+'">—</span>']));}
function motion(){return '<div class="split"><div>'+camera()+plot()+'</div><div class="sidebar-panel">'+section('Joint readout','回放帧读数',jointTable())+section('Motion review','运动复核',button('Go to record','打开试验记录','record'))+'</div></div>';}
function alignmentTable(){return table([['Channel','通道'],['Joint error','关节偏差'],['Gripper error','夹爪偏差'],['Feedback','反馈']],[['Left · can_left','左侧'],['Right · can_right','右侧']].map(([en,zh])=>[dual(en,zh),'—','—',dual('Offline','无实时反馈')]));}
function connect(){return '<div class="split"><div>'+section('Devices','设备连接',table([['Device','设备'],['Function','用途'],['Status','状态']],[[dual('GPU workstation','4090 工作站'),dual('Inference','模型推理'),dual('Offline','未接入')],[dual('Robot computer','AgileX 工控机'),dual('Control & capture','执行与采集'),dual('Offline','未接入')],[dual('USB storage','录制 U 盘'),dual('Episode storage','数据保存'),dual('Unverified','待核验')]]))+section('Arm channels','机械臂通道',alignmentTable())+'</div><div class="sidebar-panel">'+section('Calibration','校准状态',facts([['Left zero','左侧零点','—'],['Right zero','右侧零点','—']])+button('Check inputs','核对预览输入','check'))+'</div></div>';}
function dagger(){return '<div class="dg"><div class="stat-grid">'+metric('Wait time','等待时间','<span id="wait">—</span>',true)+metric('Policy','模型执行',dual('Offline','未接入'))+metric('Recorder','纠正录制',dual('Idle','未录制'))+'</div>'+section('Arm handover','双臂交接',alignmentTable()+'<div class="actions">'+button('Intervene','失败介入','intervene','primary')+button('Finish & save','结束并保存','finish')+'</div><p id="dg-msg" role="status" class="inline-state"></p>')+'</div>';}
function records(){return '<div class="split"><div>'+camera()+plot()+'</div><div class="sidebar-panel">'+section('Episode','试验记录',isSample()?facts([['Date','采集日期',provenance.date],['Route','执行路线',provenance.model.route],['Checkpoint','检查点',provenance.model.checkpoint],['Playback','视频时长',provenance.video_duration_s.toFixed(2)+' s'],['Capture','采集时长',provenance.native_episode_duration_s.toFixed(2)+' s']]):'—')+section('Samples','采样',sampleFacts())+'</div></div>';}
const templates={deploy:deployment,teleop,motion,dagger,connect,records};
const notes={
  deploy:['Draft settings stay in this browser. Playback uses an archived episode.','参数只保存在当前页面，回放使用已归档的试验。'],
  teleop:['Archived camera frames and joint samples share the recorded timeline.','相机画面与关节采样按原始回放时间轴显示。'],
  motion:['Readouts are measured right-arm angles from the selected frame.','读数来自所选回放帧的右臂实测角度。'],
  records:['The source run stopped when its evidence queue filled. Task outcome was not scored.','原试验因证据队列已满而中止，任务结果未评分。'],
  connect:['Device checks require a live connection. This preview opens local media only.','设备核验需要实机连接。此预览只读取本地媒体。'],
  dagger:['Preview timer only. Shared CAN targets cannot independently verify leader alignment.','预演只运行本地计时。共享 CAN 目标不能独立证明主臂对齐。']
};
function footer(){
 const [en,zh]=notes[current];
 $('foot').innerHTML='<div class="foot-note">'+dual(en,zh)+'</div>'+(['deploy','teleop','motion','records'].includes(current)?button('Source & license','数据来源与许可','source','text-button'):'');
}
function alignNavigation(){
  const nav=$('navigation'),active=nav.querySelector('[aria-current=page]');
  if(active && nav.scrollWidth>nav.clientWidth){
    const shift=active.getBoundingClientRect().left-nav.getBoundingClientRect().left;
    nav.scrollLeft+=shift-(nav.clientWidth-active.offsetWidth)/2;
  }
}
function render(){
  current=Object.hasOwn(pages,location.hash.slice(1))?location.hash.slice(1):'deploy';
  $('heading').textContent=pages[current][1];$('kind').textContent=pages[current][0];footer();
  for(const a of $('navigation').querySelectorAll('a')){if(a.dataset.page===current)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');}
  $('stop-preview').hidden=['dagger','connect'].includes(current);
  alignNavigation();
  $('layout').innerHTML=templates[current]();
  for(const key of Object.keys(draft))if($(key))$(key).value=draft[key];
  if($('rate'))$('rate').value=String(source.playbackRate);
  if(!$( 'play') && !source.paused)source.pause();
  refresh(true);window.scrollTo(0,0);
}
function drawFeeds(pic) {
  if(!pic||!$('cam-front'))return;
  const w=pic.videoWidth||pic.naturalWidth,h=pic.videoHeight||pic.naturalHeight;
  if(!w||!h)return;
  for(const [id,x] of [['front',80],['right',720]]){
    const c=$('cam-'+id);c.getContext('2d').drawImage(pic,x*w/1280,66*h/480,480*w/1280,360*h/480,0,0,c.width,c.height);
  }
}
function sampleAt(t){let lo=0,hi=samples.length-1;while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(samples[mid][0]<=t)lo=mid;else hi=mid-1;}return samples[lo];}
function drawTrace(){
  const c=$('trace');if(!c||!samples.length)return;
  const w=c.clientWidth,h=c.clientHeight,dpr=window.devicePixelRatio||1;
  if(c.width!==Math.round(w*dpr)||c.height!==Math.round(h*dpr)){c.width=Math.round(w*dpr);c.height=Math.round(h*dpr);}
  const ctx=c.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
  const left=36,right=12,top=13,bottom=h-23,span=w-left-right;
  const x=t=>left+t/(length||1)*span,y=v=>top+(150-v)/270*(bottom-top);
  ctx.font='10px "Segoe UI",sans-serif';ctx.fillStyle='#627183';ctx.textAlign='right';
  for(const v of [120,60,0,-60,-120]){ctx.strokeStyle=v===0?'#b6c2d1':'#e6ebf1';ctx.beginPath();ctx.moveTo(left,y(v));ctx.lineTo(w-right,y(v));ctx.stroke();ctx.fillText(v+'°',left-5,y(v)+3);}
  ctx.textAlign='center';for(const t of [0,10,20,30,40])ctx.fillText(t+'s',x(t),h-5);
  for(const i of visible){ctx.beginPath();ctx.strokeStyle=colors[i];ctx.lineWidth=1.8;samples.forEach((r,k)=>k?ctx.lineTo(x(r[0]),y(r[i+1])):ctx.moveTo(x(r[0]),y(r[i+1])));ctx.stroke();}
  ctx.lineWidth=1;ctx.strokeStyle='#667788';ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(x(clock),top);ctx.lineTo(x(clock),bottom);ctx.stroke();ctx.setLineDash([]);
  const row=sampleAt(clock);for(const i of visible){ctx.fillStyle=colors[i];ctx.beginPath();ctx.arc(x(clock),y(row[i+1]),3,0,Math.PI*2);ctx.fill();}
}
function reflectWait(){if(!$('wait'))return;
  $('wait').textContent=timerStart===null?'—':((performance.now()-timerStart)/1000).toFixed(1)+' s';
  $('dg-msg').innerHTML=timerStart!==null?dual('Waiting for handover','等待交接 · 预演'):waitFinished?dual('Ended · no recording','预演已结束 · 未生成录制文件'):'';
  document.querySelector('[data-do="intervene"]').disabled=timerStart!==null;
  document.querySelector('[data-do="finish"]').disabled=timerStart===null;
}
function refresh(force=false){
  reflectWait();
  if(!$('play'))return;
  const playable=dataReady && Number.isFinite(source.duration) && !source.error;
  if(!initialSeek&&Number.isFinite(source.currentTime))clock=source.currentTime;
  if($('scrub')){$('scrub').max=length||47;$('scrub').value=clock;$('scrub').disabled=!playable;}
  $('play').disabled=!playable;$('timecode').textContent=fmt(clock);$('endtime').textContent=length?fmt(length):'—';
  $('play').innerHTML=icon(source.paused?paths.play:paths.pause);$('play').setAttribute('aria-label',source.paused?'Play / 播放':'Pause / 暂停');
  $('media-origin').textContent=unavailable||(!provenance?'Loading / 读取中…':provenance.date.replaceAll('-','.')+'  /  '+provenance.model.precision+'  /  Checkpoint '+provenance.model.checkpoint);
  const n=Math.floor(clock*30);if(force||n!==lastFrame){lastFrame=n;
    if(!initialSeek&&!source.seeking&&source.readyState>=2)drawFeeds(source);else if(poster && Math.abs(clock-12)<.05)drawFeeds(poster);
    drawTrace();if(samples.length){const r=sampleAt(clock);for(let i=0;i<6;i++)if($('measure-'+i))$('measure-'+i).textContent=r[i+1].toFixed(2);}}
}
function modal(title,rows,extra){
  $('dialog-title').innerHTML=dual(...title);const parent=$('dialog-body');parent.replaceChildren();
  const dl=document.createElement('dl');dl.className='statuslist';
  for(const [en,zh,b] of rows){const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.innerHTML=dual(en,zh);dd.textContent=b;div.append(dt,dd);dl.append(div);}
  parent.append(dl);if(extra){const p=document.createElement('p');p.className='caption';p.innerHTML=dual(...extra);parent.append(p);}
  $('dialog').showModal();
}
function saveDraftKey(e){if(Object.hasOwn(draft,e.target.id))draft[e.target.id]=e.target.value;}
$('layout').addEventListener('input',e=>{saveDraftKey(e);if(e.target.id==='scrub'&&Number.isFinite(source.duration)){source.currentTime=Number(e.target.value);clock=source.currentTime;drawTrace();}});
$('layout').addEventListener('change',e=>{saveDraftKey(e);if(e.target.id==='rate')source.playbackRate=Number(e.target.value);});
document.body.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b||b.disabled)return;
  if(b.dataset.joint!==undefined){const i=Number(b.dataset.joint);if(visible.has(i))visible.delete(i);else visible.add(i);b.setAttribute('aria-pressed',String(visible.has(i)));drawTrace();return;}
  switch(b.dataset.do){
    case 'play':if(source.paused){try{if(source.ended)source.currentTime=0;await source.play();}catch{unavailable='Playback unavailable / 无法播放';refresh();}}else source.pause();break;
    case 'intervene':timerStart=performance.now();waitFinished=false;reflectWait();break;
    case 'finish':timerStart=null;waitFinished=true;reflectWait();break;
    case 'record':location.hash='records';break;
    case 'check':modal(['Preview inputs','预览输入'],[['Media','历史媒体',source.readyState>=2?'Loaded / 已载入':'Unavailable / 不可用'],['Joint samples','关节记录',String(samples.length)],['Robot link','实机通路','Offline / 未接入']]);break;
    case 'draft':if(!$('duration').checkValidity()||!draft.task.trim()){if(!$('duration').reportValidity())return;$('task').focus();return;}modal(['Deployment draft','部署草稿'],[['Route','执行路线',routes.find(r=>r[0]===draft.route)?.[1]||draft.route],['Checkpoint','检查点',draft.step],['Duration','时长',draft.duration+' s'],['Task prompt','任务指令',draft.task]]);break;
    case 'source':modal(['Source & license','数据来源与许可'],[['Episode','样例',provenance?.episode_id||'—'],['Joint samples','右臂关节采样',String(samples.length)],['License','许可','CC BY 4.0'],['Credit','署名','Yang Taige, Ye Hanzhi / Emerge Robotics']],['The two camera views are cropped from the published recording. Joint angles use its time base.','两路画面从公开录像裁切，实测关节角使用同一回放时间轴。']);break;
  }
});
$('stop-preview').addEventListener('click',()=>{
  source.pause();
  if(timerStart!==null){timerStart=null;waitFinished=true;reflectWait();}
  const n=$('notice');n.innerHTML=dual('Preview stopped','预演已停止');n.hidden=false;
  clearTimeout(n.hideTimer);n.hideTimer=setTimeout(()=>{n.hidden=true;},1700);
});
window.addEventListener('hashchange',render);window.addEventListener('resize',()=>{alignNavigation();if($('trace'))drawTrace();});
source.addEventListener('loadedmetadata',()=>{length=source.duration;if(initialSeek){source.currentTime=Math.min(12,length);initialSeek=false;}refresh(true);});
for(const event of ['loadeddata','seeked','timeupdate','play','pause','ended'])source.addEventListener(event,()=>refresh(true));
source.addEventListener('error',()=>{unavailable='Video unavailable / 视频载入失败';refresh(true);});
setInterval(()=>{if(!document.hidden && timerStart!==null)reflectWait();},100);
async function init(){
  render();
  try{
    const offline=window.WORKBENCH_PREVIEW;
    const get=async path=>{const r=await fetch(mediaRoot+path);if(!r.ok)throw Error('asset');return r.json();};
    const [p,j]=offline?[offline.provenance,offline.joints]:await Promise.all([get('media-provenance.json'),get('recorded-joints.json')]);
    provenance=p;samples=j.samples;length=p.video_duration_s;dataReady=true;
    poster=new Image();poster.onload=()=>refresh(true);poster.src=offline?.poster||mediaRoot+'preview-0360.jpg';
    source.src=offline?.video||mediaRoot+p.video;source.load();render();
  }catch{unavailable='Recording unavailable / 无法读取归档录像';refresh(true);}
}
init();
