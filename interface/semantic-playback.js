// Frozen offline inference series. Playback never submits an inference or robot job.
import {replayView, reasonLabel} from './presence-replay-model.mjs';
import {phaseNames as phases, reviewedJoinForEpisode, stageDisplay} from './presence-stage-join.mjs';
const panels = [];
function node(tag, text, cls) { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(cls)e.className=cls; return e; }

function makePlayback(host, data, presenceData, joinData) {
  const panel=node('article',undefined,'panel semantic-playback');
  const heading=node('div',undefined,'panel-title');
  heading.append(node('h2','任务阶段＋人员识别 · 同源回放'),node('span','历史录像 / 仅显示','pill neutral')); panel.append(heading);
  panel.append(node('p',data.model.name+' · '+data.model.precision+' · '+data.model.backend+' · '+data.model.hardware,'panel-copy'));
  const videoNote=node('p','完整双视角；按采样时刻加实测耗时模拟结果到达，属于离线复盘。','footnote');panel.append(videoNote);
  const controls=node('div',undefined,'semantic-controls');
  const epSelect=node('select'); epSelect.setAttribute('aria-label','播放录像');
  const profileSelect=node('select'); profileSelect.setAttribute('aria-label','查看识别方案');
  for (const ep of data.episodes) {const option=node('option','第 '+ep.ordinal+' 条 · '+ep.duration_s.toFixed(2)+' 秒');option.value=ep.episode_id;epSelect.append(option);}
  for (const profile of data.profiles) {const option=node('option',profile.label);option.value=profile.id;profileSelect.append(option);}
  profileSelect.value=data.default_profile;
  controls.append(epSelect,profileSelect); panel.append(controls);
  const video=node('video'); video.controls=true;video.preload='metadata';video.playsInline=true;video.setAttribute('aria-label','真实历史录像双视角播放'); panel.append(video);
  const cards=node('div',undefined,'semantic-findings'), visible={};
  for (const [key,label] of Object.entries({stage:'任务阶段候选',person:'人员／手识别',observation:'观察流程 · 仅显示'})) {
    const card=node('div'), value=node('strong','等待同源录像');value.dataset.paired=key;value.setAttribute('aria-live','polite');
    card.append(node('label',label),value);cards.append(card);visible[key]=value;
  }
  const pairSource=node('p','等待来源和时间基准核对','footnote');pairSource.dataset.paired='source';
  panel.append(cards,pairSource,node('p','识别到人／手→暂停显示；确认离开→新图重新观察→可恢复候选显示。不发送停止、恢复或其他机械臂指令。','footnote'));
  const status=node('p','等待录像元数据','semantic-playback-status');
  const evidence=node('details');evidence.append(node('summary','阶段判断依据 · 全部概率、采样记录与原始结果'));
  evidence.append(status);
  const method=node('p',undefined,'footnote');evidence.append(method);
  const distribution=node('div',undefined,'semantic-distribution'); const bars={};
  for (const [key,label] of Object.entries(phases)) {const row=node('div'); const bar=node('progress');bar.max=1;bar.value=0;bar.setAttribute('aria-label',label+'模型概率'); const value=node('span','—');row.append(node('label',label),bar,value);distribution.append(row);bars[key]={bar,value};}
  evidence.append(distribution,node('p','六项是同一个阶段问题内的条件概率；拒答概率另列。夹爪开合不是持物证明，单问方案没有独立的持物或入桶预测。','footnote'));
  const extra=node('pre',undefined,'semantic-playback-extra');evidence.append(extra);
  const table=node('table',undefined,'semantic-timeline');
  const head=node('thead');const tr=node('tr');for(const label of ['采样点 / 源时间','模型原始阶段','运动证据复核','耗时','拒答告警','所有选项概率'])tr.append(node('th',label));head.append(tr);table.append(head);
  const body=node('tbody');table.append(body);const scroll=node('div',undefined,'semantic-timeline-scroll');scroll.append(table);evidence.append(scroll);panel.append(evidence);
  evidence.addEventListener('toggle',()=>{if(!evidence.open)video.pause();});
  let currentEpisode=null, rows=[],pendingSeek=null, waiting=true, expectedSource='';
  function renderTime() {
    const time=video.currentTime || 0;
    const media={error:video.error,seeking:video.seeking,waiting,readyState:video.readyState,ended:video.ended};
    const stage=stageDisplay(currentEpisode,rows,time,media,presenceData?.gate_config?.max_age_s);
    const row=stage.row;
    visible.stage.textContent=stage.label;
    const pair=reviewedJoinForEpisode(currentEpisode.episode_id,presenceData,joinData);
    if (!pair) {
      visible.person.textContent='无法判断 · 无已核对的同源识别';visible.observation.textContent='等待同源资料';
      pairSource.textContent='当前阶段录像没有可核对的人员识别源，不能拼接其他桌面视频的判断。';
    } else {
      const personTime=time+pair.join.stage_to_presence_offset_s;
      const observation=replayView(pair.clip,personTime,media,presenceData.gate_config), accepted=observation.row;
      const presence=accepted?.semantic_presence || accepted?.gate?.semantic_presence;
      visible.person.textContent=observation.blocked || accepted?.valid===false || presence==='uncertain'
        ? '无法判断' : presence==='present' ? '检测到人员／人手' : presence==='absent' ? '当前未见人员／人手' : '无法判断';
      const flow={PAUSED:'暂停状态 · 仅显示',CLEAR_CONFIRMING:'正在确认连续空场',REOBSERVE_REQUIRED:'确认空场后，等待新图重新观察',RESUME_ELIGIBLE:'可恢复候选 · 仅显示'};
      visible.observation.textContent=observation.blocked ? '等待新观察 · '+observation.blocked : flow[observation.state] || '无法判断';
      const positiveBefore=(pair.clip.records || []).some(item=>item.evaluated_at_pts_s<=personTime && item.valid!==false && item.semantic_presence==='present');
      pairSource.textContent='同一录像 '+currentEpisode.episode_id+'；已核对原始头部流 SHA 与两个软件时间原点，人员时钟偏移 '+(pair.join.stage_to_presence_offset_s*1000).toFixed(3)+' ms。'+
        (positiveBefore?'':'本片此前未检出人／手，不能称为实际离开事件。')+
        (observation.blocked?'':' 当前流程依据：'+reasonLabel(accepted?.gate?.reason))+(stage.blocked?' 阶段：'+stage.blocked+'。':'');
    }
    for(const [key,item] of Object.entries(bars)) {const p=row?.probabilities?.[key];item.bar.value=typeof p==='number'?p:0;item.value.textContent=typeof p==='number'?(p*100).toFixed(1)+'%':'—';}
    if(!row) {status.textContent='源时间 '+time.toFixed(2)+' s · 尚无此前模型采样结果';extra.textContent='';return;}
    const assessment=row.phase_assessment;
    status.textContent='播放 '+time.toFixed(2)+' s · 输入 '+row.offset_s.toFixed(3)+' s · 原始候选 '+(phases[row.choice] || '拒答')+
      ' · 复核 '+(phases[assessment?.choice || row.choice] || '拒答')+' · 推理 '+row.end_to_end_ms.toFixed(0)+' ms · 无合适选项 '+(100*row.none_probability).toFixed(1)+'%';
    extra.textContent=JSON.stringify({raw_stage:row.choice,motion_evidence_review:assessment || '本方案未做时序复核',
      other_requested_answers:row.other_answers || {},physical_task_result:'unverified'},null,2);
    for(const tr of body.children)tr.classList.toggle('active',tr.dataset.index===String(row.index));
  }
  function renderSeries(reset=false) {
    const profile=data.profiles.find(x=>x.id===profileSelect.value);
    rows=currentEpisode.series[profile.id] || [];method.textContent=profile.method;
    const asset=currentEpisode.videos?.[profile.id] || currentEpisode.video;
    if(video.getAttribute('src')!==asset.url) {video.pause();waiting=true;pendingSeek=reset?0:(video.currentTime || 0);expectedSource=new URL(asset.url,location.href).href;video.src=asset.url;}
    videoNote.textContent='当前视频烧录：'+(asset.label || asset.overlay_profile || '已核验方案')+'。完整原始双视角；按采样时刻加实测耗时模拟结果到达，属于离线复盘。';
    body.replaceChildren();
    for(const row of rows) {const tr=node('tr');tr.dataset.index=String(row.index);
      const cell=node('td');const button=node('button',row.index+' / '+row.offset_s.toFixed(3)+' s','button quiet');button.onclick=()=>{const arrival=row.offset_s+row.end_to_end_ms/1000+.001;video.currentTime=Math.min(arrival,video.duration || arrival);renderTime();};cell.append(button);tr.append(cell);
      tr.append(node('td',phases[row.choice] || '拒答'),node('td',row.phase_assessment ? (phases[row.phase_assessment.choice] || '拒答')+(row.phase_assessment.gate_applied?' · 已降级':'') : '未使用时序复核'),node('td',row.end_to_end_ms.toFixed(0)+' ms'),node('td',(row.none_probability*100).toFixed(1)+'%'));
      const detailCell=node('td'),detail=node('details');detail.append(node('summary','展开完整结果'),node('pre',JSON.stringify({phase_probabilities:row.probabilities,none_probability:row.none_probability,review:row.phase_assessment || null,other_answers:row.other_answers},null,2)));detailCell.append(detail);tr.append(detailCell);body.append(tr);}
    renderTime();
  }
  function selectEpisode() {video.pause();currentEpisode=data.episodes.find(x=>x.episode_id===epSelect.value);renderSeries(true);}
  epSelect.onchange=selectEpisode;profileSelect.onchange=()=>renderSeries(false);
  video.ontimeupdate=renderTime;video.onloadedmetadata=()=>{if(video.currentSrc!==expectedSource)return;if(pendingSeek!==null){video.currentTime=Math.min(pendingSeek,video.duration);pendingSeek=null;}renderTime();};
  const available=()=>{if(video.currentSrc!==expectedSource)return;waiting=false;renderTime();};
  video.onloadeddata=available;video.oncanplay=available;video.onplaying=available;video.onseeked=available;
  video.onseeking=renderTime;video.onwaiting=()=>{waiting=true;renderTime();};video.onstalled=()=>{waiting=true;renderTime();};video.onended=renderTime;
  video.onerror=()=>{waiting=true;renderTime();status.textContent='录像加载失败，不能把未播放当验收通过。请查看网页或USB状态。';};
  video.onplay=()=>{for(const other of document.querySelectorAll('.semantic-playback video, .presence-replay video'))if(other!==video)other.pause();};
  const anchor=host.querySelector('.semantic-workspace'); if(anchor)anchor.after(panel);else host.append(panel);
  selectEpisode();panels.push(panel);
}
async function load() {
  try {const response=await fetch('/api/semantic/review',{cache:'no-store'});if(!response.ok)return;const data=await response.json();
    const readOptional=async path=>{try{const response=await fetch(path,{cache:'no-store'});return response.ok?await response.json():null;}catch{return null;}};
    const [presenceData,joinData]=await Promise.all([readOptional('/api/presence'),readOptional('/2026-10-18-13-20-reviewed-presence-stage-join.json')]);
    for(const page of ['teleop','deploy']){const host=document.querySelector('#page-'+page);if(host)makePlayback(host,data,presenceData,joinData);}}
  catch(error){console.error('Offline semantic playback unavailable',error);}
}
load();
