const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {pending:'待复核', success:'成功', failure:'失败', partial:'部分成功', uncertain:'待确认', excluded:'不计入'};
const kinds = {deployment:'正式部署（APPLY）', deployment_attempt:'部署尝试 · 执行未确认', no_motion:'无运动 / 影子验收', recording:'未归属的录制记录', teleop:'遥操作采集示范'};
const fields = {'test-type':'test_type','function':'function','expected':'expected','comments':'comments','failure-reason':'failure_reason','tags':'tags','reviewer':'reviewer'};
let catalog = [], sources = {}, settings = {}, csrf = '', mode = 'deployment', selected = null, copy = null, filtered = [], verdict = 'pending', markers = [], revision = 0, dirty = false, videos = [], scanTimer = null, saving = false;
let editingMarker = null;
let instanceLabel = '';
const draftKey = id => 'agilex-human-review-draft-v1:' + id;
const time = seconds => { const n = Math.max(0, Number(seconds) || 0); return String(Math.floor(n / 60)).padStart(2,'0') + ':' + String(Math.floor(n % 60)).padStart(2,'0') + (n % 1 ? '.' + String(Math.floor(n % 1 * 10)) : ''); };
const date = value => { const d = new Date(value); return Number.isFinite(+d) ? d.toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) : String(value || '未知'); };
const size = n => n >= 1073741824 ? (n/1073741824).toFixed(2)+' GiB' : n >= 1048576 ? (n/1048576).toFixed(1)+' MiB' : (n/1024).toFixed(1)+' KiB';

function message(id, value) { $(id).textContent = value || ''; $(id).hidden = !value; }
function toast(value) { $('toast').textContent = value; $('toast').hidden = false; setTimeout(() => {$('toast').hidden = true;}, 3000); }
async function api(path, body) {
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 95000);
  try {
    const r = await fetch(path, {cache:'no-store', signal:controller.signal, ...(body ? {method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':csrf},body:JSON.stringify(body)} : {})});
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || '请求失败：' + r.status);
    return data;
  } finally {clearTimeout(timeout);}
}
function modeRecords() {
  return catalog.filter(r => mode === 'teleop' ? r.kind === 'teleop' : !['recording','teleop'].includes(r.kind));
}
function applyFilters() {
  const search = $('search').value.trim().toLowerCase(), step = $('step').value, kind = $('kind-filter').value, outcome = $('verdict-filter').value, source = $('source-filter').value;
  filtered = modeRecords().filter(r => (mode === 'teleop' || step === 'all' || String(r.step) === step)
    && (mode === 'teleop' || kind === 'all' || (kind === 'physical' ? ['deployment','deployment_attempt'].includes(r.kind) : r.kind === kind))
    && (outcome === 'all' || (r.review.outcome || 'pending') === outcome)
    && (source === 'all' || r.copies.some(c => c.source_id === source))
    && (!$('only-video').checked || r.videos.length > 0)
    && (!search || [r.name,r.started_at,r.task,r.review.comments,r.review.function,r.review.test_type,r.review.tags,r.trial,r.error].join(' ').toLowerCase().includes(search)));
  renderList(); renderSummary();
  if (!selected || !filtered.some(r => r.id === selected.id)) selectRecord(filtered[0]?.id);
}
function renderList() {
  $('filtered-count').textContent = filtered.length + ' 条';
  $('record-list').innerHTML = filtered.length ? filtered.map(r => `<button class="record-card${r.id === selected?.id ? ' selected' : ''}" data-record="${esc(r.id)}" aria-pressed="${r.id === selected?.id}"><div class="card-top"><span class="card-time">${esc(date(r.started_at).replace(/^2026\//,''))}</span><span class="card-review ${r.review.outcome && r.review.outcome !== 'pending' ? 'reviewed' : ''}">${esc(labels[r.review.outcome || 'pending'])}</span></div><div class="card-id">${r.trial != null ? 'TRIAL ' + esc(r.trial) + ' · ' : ''}${esc(r.id.slice(0,8))}${r.review_source_changed ? ' · 源记录有变化' : ''}</div><div>${esc(r.review.function || r.task?.split(' [')[0] || kinds[r.kind])}</div><div class="card-bottom"><span>${r.step ? 'STEP '+esc(r.step) : esc(r.operation)}</span><span>${r.videos.length ? '▣ ' + r.videos.length + ' 路录像' : '无MP4'} · ${r.duration_s != null ? Number(r.duration_s).toFixed(1)+'s' : '—'}</span></div></button>`).join('') : '<div class="empty-state">没有匹配记录。<br>可以更换筛选或更新目录。</div>';
}
function renderSummary() {
  const n = filtered.length, counts = Object.fromEntries(Object.keys(labels).map(k => [k,filtered.filter(r => (r.review.outcome || 'pending') === k).length]));
  const eligible = filtered.filter(r=>mode==='teleop' || r.kind==='deployment');
  const successes = eligible.filter(r=>r.review.outcome==='success').length, failures = eligible.filter(r=>r.review.outcome==='failure').length;
  const assessed = n - counts.pending, denominator = successes + failures;
  const rate = denominator ? (100 * successes / denominator).toFixed(1) + '%' : '—';
  $('summary').innerHTML = `<div class="summary-item"><strong>${n}</strong><span>当前队列</span></div><div class="summary-item"><strong>${assessed}<small> / ${n}</small></strong><span>已评价</span></div><div class="summary-item"><strong>${counts.pending}</strong><span>待复核</span></div><div class="summary-item"><strong>${rate}</strong><span>${mode==='teleop'?'示范合格率':'人工成功率'}</span></div><div class="summary-item">成功 ${successes} / 失败 ${failures} / 部分 ${counts.partial} / 待确认 ${counts.uncertain} / 不计入 ${counts.excluded}<br>${mode==='teleop'?'合格 ÷（合格＋拒绝）':'成功 ÷（成功＋失败），只计正式部署'}；待复核与部分成功不计分母。</div>`;
}
function fillTaxonomy() {
  for (const [id,key] of [['test-types','test_types'],['functions','functions'],['failure-reasons','failure_reasons']]) $(id).innerHTML = settings[key].map(s => '<option value="'+esc(s)+'"></option>').join('');
}
function renderCoverage() {
  $('coverage').innerHTML = Object.entries(sources).map(([id,s]) => `<section><strong>${esc(id === 'robot-usb' ? 'AgileX 录制U盘' : 'PC 历史归档U盘')} · ${s.available ? '上次读取成功' : '当前不可读'}</strong><br>UUID ${esc(s.status?.uuid || '未核到')}<br>${esc(s.status?.mountpoint || '')}<br>扫描：${esc(date(s.scanned_at))}<br>索引 ${s.indexed_count || 0} 条；${s.status ? '剩余 '+size(s.status.free_bytes) : ''}${s.last_error ? '<p class="coverage-error">'+esc(s.last_error)+'</p>' : ''}${s.errors?.length ? '<p>未纳入项 '+s.errors.length+' 条：</p>'+s.errors.map(e=>'<p class="coverage-error">'+esc(e.path)+'<br>'+esc(e.error)+'</p>').join('') : '<p>本轮支持范围内无读取错误。</p>'}</section>`).join('') + '<p>范围：两颗已登记U盘中的控制库部署、影子、验收、采集及原生episode；另含早期部署回执。未扫描其他主机、其他盘或其他格式，不能据此宣称历史数据绝对齐全。</p>';
  const available = Object.values(sources).filter(s=>s.available).length;
  $('source-status').textContent = (instanceLabel ? instanceLabel+' · ' : '')+'最近扫描 '+available+' / '+Object.keys(sources).length+' 处可读';
}
async function load({initial=false}={}) {
  try {
    const data = await api('/api/catalog');
    catalog = data.records; sources = data.sources; settings = data.settings; csrf = data.csrf;
    instanceLabel = data.instance_label || '';
    document.title = 'Emerge Robotics · 数据复核'+(instanceLabel?' · '+instanceLabel:'');
    $('db-location').textContent = '评价库：' + data.db_path+(data.instance_note?' · '+data.instance_note:'');
    $('deployment-count').textContent = catalog.filter(r=>['deployment','deployment_attempt'].includes(r.kind) && String(r.step)==='5000').length;
    $('teleop-count').textContent = catalog.filter(r=>r.kind==='teleop').length;
    fillTaxonomy(); renderCoverage();
    $('rescan').textContent = data.scanning ? '正在更新…' : '↻ 更新目录'; $('rescan').disabled = data.scanning;
    message('global-error',data.qa?'软件验收副本 · 此页的测试评价不会写入正式人工评价库。':'');
    if (data.scanning && !scanTimer) scanTimer = setTimeout(async()=>{scanTimer=null; await load();},2500);
    if (initial) {
      const hash = location.hash.slice(1);
      applyFilters();
      if (hash && filtered.some(r=>r.id===hash)) selectRecord(hash);
    } else {
      const old = selected;
      if (old) selected = catalog.find(r=>r.id===old.id) || old;
      applyFilters();
      if (selected && old?.id === selected.id && !dirty) selectRecord(selected.id);
    }
  } catch(e) {message('global-error','读取复核目录失败：'+e.message);}
}
function formData() {
  const data = {id:selected.id,revision,outcome:verdict,markers,source_id:copy.source_id,evidence_sha256:copy.evidence_sha256};
  for (const [id,key] of Object.entries(fields)) data[key] = $(id).value;
  return data;
}
function stash() {if (dirty && selected) {try {localStorage.setItem(draftKey(selected.id),JSON.stringify(formData()));} catch(e) {message('save-error','浏览器草稿暂存失败，请点击保存评价：'+e.message);}}}
function markDirty() {
  if (!selected) return;
  dirty = true; $('save-state').textContent = '未保存 · 本机草稿'; $('save-state').classList.add('dirty'); stash();
}
function renderVerdicts() {
  const choices = mode === 'teleop' ? {...labels,success:'示范合格',failure:'拒绝 / 不合格'} : labels;
  $('verdict-buttons').innerHTML = Object.entries(choices).map(([k,v])=>`<button type="button" class="verdict-button${k===verdict?' selected':''}" role="radio" aria-checked="${k===verdict}" data-verdict="${k}">${esc(v)}</button>`).join('');
  $('record-verdict').textContent = choices[verdict] || '待复核';
}
function selectRecord(id) {
  stash(); videos.forEach(v=>v.pause()); videos=[];
  selected = catalog.find(r=>r.id===id) || null; copy = selected;
  dirty = false; message('save-error',''); message('media-error','');
  $('record-workspace').hidden = !selected; $('empty-workspace').hidden = !!selected;
  renderList();
  if (!selected) return;
  history.replaceState(null,'','#'+selected.id);
  const reviewed = selected.review;
  let draft = null;
  try {draft = JSON.parse(localStorage.getItem(draftKey(id)) || 'null');} catch {}
  const useDraft = draft && draft.revision === reviewed.revision && draft.evidence_sha256 === copy.evidence_sha256;
  const current = useDraft ? draft : reviewed;
  revision = reviewed.revision || 0; verdict = current.outcome || 'pending'; markers = JSON.parse(JSON.stringify(current.markers || []));
  for (const [field,key] of Object.entries(fields)) $(field).value = current[key] || (key==='reviewer' ? localStorage.getItem('agilex-reviewer-v1') || 'Tiger' : '');
  $('save-state').textContent = useDraft ? '未保存 · 已恢复本机草稿' : revision ? '已保存 · '+date(reviewed.saved_at)+' · r'+revision : '尚无评价';
  $('save-state').classList.toggle('dirty',!!useDraft); dirty = !!useDraft;
  if (draft && !useDraft) message('save-error','有一份较旧的本机草稿未自动覆盖当前评价。如需找回，请先保留当前JSON导出后再处理。');
  renderRecord(); renderVerdicts(); renderMarkers();
}
function renderRecord() {
  $('record-eyebrow').textContent = mode === 'teleop' ? 'TELEOPERATION / DATA QUALITY' : 'PI0.5 / FULL FINETUNING / STEP ' + (copy.step || 'UNKNOWN');
  $('record-title').textContent = date(copy.started_at);
  $('record-subtitle').textContent = [copy.trial!=null?'TRIAL '+copy.trial:copy.operation,kinds[copy.kind],copy.source_label].join(' · ');
  $('task').textContent = copy.task || '源记录没有填写任务文本；可在评价中补充测试目的。';
  $('source-copy-wrap').hidden = selected.copies.length < 2;
  $('source-copy').innerHTML = selected.copies.map(c=>`<option value="${esc(c.source_id)}">${esc(c.source_label)} · UUID ${esc(c.uuid)}${c.present?'':' · 原路径未发现'}</option>`).join('');
  $('source-copy').value = copy.source_id;
  const latency = copy.latency || {}, profile = copy.runtime_profile || {};
  $('facts').innerHTML = [['录制时长',copy.duration_s != null ? Number(copy.duration_s).toFixed(2)+' s' : '未记录'],['状态样本',copy.samples ?? '未记录'],['模型检查点',copy.step ?? '未记录'],['文件保存核验',copy.artifact_status || '未记录 / 未核验'],['已记录提交耗时 P50',latency.p50_ms!=null ? latency.p50_ms.toFixed(1)+' ms' : '未记录'],['已记录提交耗时 P95',latency.p95_ms!=null ? latency.p95_ms.toFixed(1)+' ms' : '未记录'],['有效耗时样本',latency.n || '—'],['执行配置',profile.action_hz ? profile.precision+' / '+profile.action_hz+'Hz / H'+profile.horizon : copy.capture_category || '未记录']].map(([k,v])=>`<div><label>${esc(k)}</label><strong>${esc(v)}</strong></div>`).join('');
  const warnings = [...copy.warnings];
  if (!copy.source_available || !copy.present) warnings.push('当前来源不可用或原路径未发现：正在展示上次目录快照，播放仍须通过实时来源检查。');
  if (selected.review_source_changed) warnings.push('该记录在上次评价后发生来源变化，请重新核对；原评价与历史仍保留。');
  if (copy.kind === 'no_motion') warnings.push('这是一条无运动/影子记录，不纳入部署真机成功率。');
  message('source-warning',warnings.join('\n'));
  const raw = copy.raw_files.map(f=>f.name+' ('+size(f.size)+')');
  $('program-detail').textContent = JSON.stringify({record_id:copy.id,source_uuid:copy.uuid,metadata:copy.metadata,program_status:copy.status,recording_status:copy.recording_status,artifact_status:copy.artifact_status,error:copy.error,source_sha256:copy.source_sha256,runtime_profile:copy.runtime_profile,contract_sha256:copy.contract_sha256,latency:copy.latency,raw_files:raw,batch_id:copy.batch_id,capture_notes:copy.capture_notes,legacy_human_review:copy.legacy_human_review || null},null,2);
  $('attachments').innerHTML = copy.attachments.map((f,i)=>`<a href="${artifactUrl('attachment',i)}" download>${esc(f.name)} ↗</a>`).join('');
  $('metadata').hidden = true; $('metadata').textContent = '';
  $('history-content').textContent = '点击查看当前记录的评价修改历史。';
  const groups = [...new Set(copy.videos.map(v=>v.group))];
  $('video-group').innerHTML = groups.map(g=>`<option value="${esc(g)}">${esc(g)}</option>`).join('');
  $('video-group').disabled = groups.length < 2;
  loadVideos();
}
function artifactUrl(kind,index) {return '/api/'+kind+'?id='+encodeURIComponent(copy.id)+'&source='+encodeURIComponent(copy.source_id)+'&index='+index;}
function loadVideos() {
  videos.forEach(v=>v.pause()); videos=[]; message('media-error','');
  $('clock').textContent='00:00 / —'; $('seek').value=0; $('play').textContent='▶ 播放';
  const group = $('video-group').value;
  const chosen = copy.videos.map((v,i)=>({...v,index:i})).filter(v=>v.group===group).sort((a,b)=>(a.role==='front'?0:a.role==='right'?1:2)-(b.role==='front'?0:b.role==='right'?1:2));
  $('transport').hidden = !chosen.length;
  if (!chosen.length) {
    const total = copy.raw_files.reduce((n,f)=>n+f.size,0);
    $('video-grid').innerHTML='<div class="no-video"><strong>此记录暂无可播放的录像</strong>已发现原始流 '+size(total)+'。'+(copy.kind==='deployment_attempt'?'该部署尝试可能在预检时结束。':'可保留评价；不要据缺失画面直接认定任务成功。')+'</div>';
    return;
  }
  $('video-grid').innerHTML = chosen.map(v=>`<div class="video-card"><div class="video-label"><span>${esc(v.role==='front'?'前方相机 / FRONT':v.role==='right'?'右腕相机 / RIGHT WRIST':v.role)}</span><span>${esc(size(v.size))}</span></div><video controls playsinline preload="metadata" aria-label="${esc(v.role)} 历史录像" src="${artifactUrl('video',v.index)}"></video><span class="video-state">载入历史录像…</span></div>`).join('');
  videos = [...$('video-grid').querySelectorAll('video')];
  videos.forEach((v,i)=>{
    v.playbackRate = Number($('speed').value);
    v.addEventListener('loadeddata',()=>{v.parentElement.querySelector('.video-state').textContent='';});
    v.addEventListener('loadedmetadata',()=>{
      if (i===0) { $('seek').max = v.duration || 1; $('clock').textContent=time(0)+' / '+time(v.duration); }
    });
    v.addEventListener('error',()=>{v.parentElement.querySelector('.video-state').textContent='录像不可读'; message('media-error','录像加载失败：请核对USB/SSH、文件是否仍在保存或浏览器解码支持；可更新目录后重试。原评价仍可保存。');});
    v.addEventListener('play',()=>{ $('play').textContent='Ⅱ 暂停'; videos.forEach(o=>{if(o!==v && o.paused) {o.currentTime=Math.min(v.currentTime,o.duration||v.currentTime);o.play().catch(e=>message('media-error','另一视角未能播放：'+e.message));}}); });
    v.addEventListener('pause',()=>{if(!v.ended){$('play').textContent='▶ 播放';videos.forEach(o=>{if(o!==v && !o.paused)o.pause();});}});
    v.addEventListener('seeking',()=>{videos.forEach(o=>{if(o!==v && Number.isFinite(o.duration) && Math.abs(o.currentTime-v.currentTime)>.35)o.currentTime=Math.min(v.currentTime,o.duration);});});
    if(i===0) v.addEventListener('timeupdate',()=>{ $('seek').value=v.currentTime; $('clock').textContent=time(v.currentTime)+' / '+time(v.duration); });
  });
}
function seekTo(seconds) {videos.forEach(v=>{if(Number.isFinite(v.duration))v.currentTime=Math.min(Math.max(0,seconds),v.duration);});}
function renderMarkers() {
  $('markers').innerHTML = markers.map((m,i)=>`<div class="marker-item"><button data-marker-seek="${i}" title="跳转到该时间">↗ ${esc(time(m.seconds))}</button><span class="marker-text">${esc(m.text)}</span><button data-marker-edit="${i}" aria-label="编辑标记${i+1}">✎</button><button data-marker-delete="${i}" aria-label="移除标记${i+1}">×</button></div>`).join('');
}
async function save(next=false) {
  if(!selected || saving)return;
  message('save-error',''); saving=true; $('save').disabled=$('save-next').disabled=true;
  try {
    const payload = formData();
    const result = await api('/api/review',payload);
    selected.review=result; revision=result.revision; dirty=false; selected.review_source_changed=false;
    localStorage.removeItem(draftKey(selected.id)); localStorage.setItem('agilex-reviewer-v1',$('reviewer').value);
    $('save-state').textContent='已保存 · '+date(result.saved_at)+' · r'+revision; $('save-state').classList.remove('dirty');
    const index = filtered.findIndex(r=>r.id===selected.id), nextId=filtered[index+1]?.id;
    applyFilters(); toast('评价已保存到本机评价库');
    if(next && nextId) selectRecord(nextId); else if(next) toast('当前队列已到最后一条，评价已保存');
  } catch(e) {message('save-error','保存失败：'+e.message+'。当前草稿仍保留；不要重复覆盖其他页面的新评价。');}
  finally {saving=false;$('save').disabled=$('save-next').disabled=false;}
}
for(const id of Object.keys(fields))$(id).addEventListener('input',markDirty);
for(const id of ['search','step','kind-filter','verdict-filter','source-filter','only-video'])$(id).addEventListener(id==='search'?'input':'change',()=>{stash();applyFilters();});
$('record-list').addEventListener('click',e=>{const b=e.target.closest('[data-record]');if(b)selectRecord(b.dataset.record);});
$('verdict-buttons').addEventListener('click',e=>{const b=e.target.closest('[data-verdict]');if(b){verdict=b.dataset.verdict;renderVerdicts();markDirty();}});
document.querySelectorAll('[data-mode]').forEach(b=>b.addEventListener('click',()=>{
  stash(); mode=b.dataset.mode;
  document.querySelectorAll('[data-mode]').forEach(o=>{o.classList.toggle('active',o===b);o.setAttribute('aria-selected',o===b?'true':'false');});
  $('step').disabled=$('kind-filter').disabled=mode==='teleop';
  $('scope-chip').textContent=mode==='teleop'?'遥操作 · 示范质量复核':'π0.5 · 全量微调 · STEP '+($('step').value==='all'?'全部':$('step').value);
  $('verdict-note').textContent=mode==='teleop'?'独立评价示范质量。原采集批次的合格/拒绝记录保持原样，单独显示。':'尚未评价的记录保持“待复核”，不计入成功率。';
  selected=null; applyFilters();
}));
$('source-copy').addEventListener('change',()=>{stash();copy=selected.copies.find(c=>c.source_id===$('source-copy').value);renderRecord();});
$('video-group').addEventListener('change',loadVideos);
$('play').addEventListener('click',async()=>{if(!videos.length)return;if(!videos[0].paused)videos.forEach(v=>v.pause());else{try{await Promise.all(videos.map(v=>v.play()));}catch(e){message('media-error',e.message);}}});
$('back-five').addEventListener('click',()=>seekTo((videos[0]?.currentTime||0)-5));
$('seek').addEventListener('input',()=>seekTo(Number($('seek').value)));
$('speed').addEventListener('change',()=>videos.forEach(v=>{v.playbackRate=Number($('speed').value);}));
function openMarker(index=null) {editingMarker=index;const value=index==null?{seconds:Number((videos[0]?.currentTime||0).toFixed(2)),text:''}:markers[index];$('marker-seconds').value=value.seconds;$('marker-observation').value=value.text;$('marker-error').textContent='';$('marker-dialog').showModal();$('marker-observation').focus();}
$('mark').addEventListener('click',()=>openMarker());
$('marker-cancel').addEventListener('click',()=>$('marker-dialog').close());
$('marker-save').addEventListener('click',()=>{const seconds=Number($('marker-seconds').value),text=$('marker-observation').value.trim();if(!Number.isFinite(seconds)||seconds<0||seconds>86400||!text){$('marker-error').textContent='请填写有效录像时间和观察文字。';return;}if(editingMarker==null)markers.push({seconds,text});else markers[editingMarker]={seconds,text};markers.sort((a,b)=>a.seconds-b.seconds);renderMarkers();markDirty();$('marker-dialog').close();});
$('markers').addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.markerSeek!=null)seekTo(markers[Number(b.dataset.markerSeek)].seconds);
  if(b.dataset.markerDelete!=null){markers.splice(Number(b.dataset.markerDelete),1);renderMarkers();markDirty();}
  if(b.dataset.markerEdit!=null)openMarker(Number(b.dataset.markerEdit));
});
$('save').addEventListener('click',()=>save());$('save-next').addEventListener('click',()=>save(true));
$('previous').addEventListener('click',()=>{const i=filtered.findIndex(r=>r.id===selected?.id);if(i>0)selectRecord(filtered[i-1].id);});
$('show-metadata').addEventListener('click',async()=>{try{$('metadata').hidden=false;$('metadata').textContent='正在读取原始记录摘要…';const r=await api('/api/metadata?id='+selected.id+'&source='+copy.source_id);$('metadata').textContent=JSON.stringify(r,null,2);}catch(e){$('metadata').textContent=e.message;}});
document.querySelector('.history').addEventListener('toggle',async e=>{if(!e.target.open || !selected)return;try{const rows=await api('/api/history?id='+selected.id);$('history-content').innerHTML=rows.length?rows.map(r=>`<div class="history-entry">r${r.revision} · ${esc(date(r.saved_at))} · ${esc(r.reviewer)} · ${esc(labels[r.outcome])}<br>${esc(r.test_type)} / ${esc(r.function)}<p>${esc(r.comments)}</p></div>`).join(''):'暂无已保存评价。';}catch(e){$('history-content').textContent=e.message;}});
$('rescan').addEventListener('click',async()=>{try{stash();$('rescan').disabled=true;$('rescan').textContent='正在更新…';await api('/api/rescan',{});scanTimer=setTimeout(async()=>{scanTimer=null;await load();},2000);}catch(e){message('global-error',e.message);$('rescan').disabled=false;}});
$('settings-open').addEventListener('click',()=>{$('taxonomy-types').value=settings.test_types.join('\n');$('taxonomy-functions').value=settings.functions.join('\n');$('taxonomy-failures').value=settings.failure_reasons.join('\n');$('settings-error').textContent='';$('settings-dialog').showModal();});
$('settings-cancel').addEventListener('click',()=>$('settings-dialog').close());
$('settings-save').addEventListener('click',async()=>{const lines=id=>[...new Set($(id).value.split('\n').map(s=>s.trim()).filter(Boolean))];try{settings=await api('/api/settings',{test_types:lines('taxonomy-types'),functions:lines('taxonomy-functions'),failure_reasons:lines('taxonomy-failures')});fillTaxonomy();$('settings-dialog').close();toast('分类已保存');}catch(e){$('settings-error').textContent=e.message;}});
$('export-open').addEventListener('click',()=>{if(dirty){toast('当前有未保存草稿，请先保存评价再导出');return;}if(!filtered.length){toast('当前队列为空，请调整筛选');return;}const query='?ids='+encodeURIComponent(filtered.map(r=>r.id).join(','));$('export-json').href='/api/export.json'+query;$('export-csv').href='/api/export.csv'+query;$('export-scope').textContent='本次导出：'+(mode==='teleop'?'遥操作数采':'部署真机')+' / '+($('step').value==='5000'&&mode!=='teleop'?'STEP 5000 / ':'')+filtered.length+' 条记录。';$('export-dialog').showModal();});
$('export-close').addEventListener('click',()=>$('export-dialog').close());
window.addEventListener('beforeunload',e=>{stash();if(dirty){e.preventDefault();e.returnValue='';}});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();save(true);}});
load({initial:true});
