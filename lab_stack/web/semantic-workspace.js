// Optional, PC-only replay panel. Never submits robot jobs or changes mode.
import './semantic-playback.js';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
let token = null, workspace = null, pending = false;
const phaseNames = {approaching:'接近候选',grasping:'抓取候选',transporting:'搬运候选',placing:'放置候选',returning:'撤离候选',unknown:'无法判断'};
const holdingNames = {held:'可见持物候选',not_held:'可见未持物',unknown:'持物无法确认'};
const placementNames = {in_bucket:'可见入桶候选',not_in_bucket:'尚无入桶证据',unknown:'放置无法确认'};
const profileNames = {full_provenance_v1:'完整来源三问',compact_replay_v1:'紧凑三问，原图不变',phase_single_replay_v1:'当前双图，单问阶段',temporal_phase_current_v1:'当前双图 + 过去1秒实际反馈，单问阶段',temporal_phase_mosaic_v1:'过去/当前四视图拼图 + 实际反馈，单问阶段',temporal_phase_four_v1:'过去/当前四张原图 + 实际反馈，单问阶段'};
profileNames.temporal_phase_mosaic_fast_v1='768拼图 + 过去实际反馈，单问阶段';
profileNames.temporal_phase_mosaic_trim_v1='896拼图 + 过去实际反馈，精简重复说明，单问阶段';
profileNames.focused_dual_history_v2='事件 / 近期 / 当前图像与实际反馈，短尾窗运动复核';
const statusNames = {idle:'等待验证',running:'正在顺序验证',processing:'模型处理中',completed:'本次读取与推理已结束',failed:'验证失败',cancelled:'已取消',cancelling:'正在取消',model_candidate:'已收到模型候选',expired:'结果超过处理预算',unavailable:'模型服务不可用'};

// SEMANTIC_ANSWER_VIEW_BEGIN: conditional choice probability is not task success.
function semanticAnswerView(answer, names) {
  if (!answer) return {label:'尚无有效模型结果', detail:''};
  const p = answer.probabilities?.[answer.choice];
  const none = answer.none_probability;
  const details = [];
  if (typeof p === 'number' && Number.isFinite(p)) details.push('选项内模型概率 ' + (p*100).toFixed(1) + '%');
  if (typeof none === 'number' && Number.isFinite(none)) details.push('无合适选项告警 ' + (none*100).toFixed(1) + '%');
  details.push('需真实场景验证');
  return {label:answer.choice === null ? '模型拒答 / 无法判断' : names[answer.choice] || '无法判断', detail:details.join(' · ')};
}
// SEMANTIC_ANSWER_VIEW_END

function makePanel(page, prefix) {
  const host = $('#page-' + page);
  if (!host) return;
  const panel = document.createElement('article');
  panel.className = 'panel semantic-workspace';
  panel.dataset.semanticPanel = prefix;
  panel.innerHTML = `<div class="panel-title"><div><div class="eyebrow">TASK STAGE / READ ONLY</div><h2>任务阶段 · 只读观察</h2></div><span class="pill neutral" data-semantic="status">等待模型配置</span></div>
    <p class="panel-copy">来源：StartLux 对已审阅历史录像的阶段判断。π₀.₅ 输出动作，不输出语言阶段；此处不控制机械臂。</p>
    <div class="semantic-findings"><div><label>当前阶段候选 · 历史录像</label><strong data-semantic="phase">尚无模型结果</strong></div></div>
    <p class="footnote" data-semantic="source">等待已审阅的录像清单。</p>
    <p class="footnote" data-semantic="identity">模型、来源时刻与推理耗时尚未核对。</p>
    <p class="action-error" data-semantic="error" role="alert" hidden></p>
    <details data-semantic-details><summary>历史录像验证资料 · 原图、概率与完整回执</summary>
    <div class="semantic-controls"><label>验证录像<select data-semantic="episode" aria-label="选择已审阅录像"></select></label><label>采样点<input data-semantic="index" type="number" min="0" step="1" value="0" aria-label="录像采样点索引" /></label><button class="button" data-semantic="sample">判断这一采样点</button><button class="button primary" data-semantic="run">顺序验证整条</button><button class="button quiet" data-semantic="cancel">取消本次验证</button></div>
    <div class="semantic-images"><figure><figcaption>头部 · 历史原图</figcaption><img data-semantic-image="front" alt="所选历史录像的头部原始图像" hidden /></figure><figure><figcaption>右腕 · 历史原图</figcaption><img data-semantic-image="right" alt="所选历史录像的右腕原始图像" hidden /></figure></div>
    <p class="footnote" data-semantic="phase-prob"></p>
    <div class="semantic-findings"><div><label>持物候选</label><strong data-semantic="holding">尚无模型结果</strong><small data-semantic="holding-prob"></small></div><div><label>放置候选</label><strong data-semantic="placement">尚无模型结果</strong><small data-semantic="placement-prob"></small></div></div>
    <p class="footnote" data-semantic="temporal"></p><p class="footnote" data-semantic="consistency"></p><p class="footnote" data-semantic="save">本轮推理尚未保存回执。</p>
    <details><summary>查看本次实际模型输出与原始时间字段</summary><pre class="semantic-raw" data-semantic="raw">尚无记录</pre></details></details>`;
  const anchor = $('.mode-link-strip', host) || $('.page-readiness', host);
  if (anchor) anchor.after(panel); else host.append(panel);
  $('[data-semantic="episode"]', panel).addEventListener('change', () => {
    $('[data-semantic="index"]', panel).value = '0';
    preview(panel);
  });
  $('[data-semantic="index"]', panel).addEventListener('change', () => preview(panel));
  $('[data-semantic="sample"]', panel).addEventListener('click', () => run(panel, false));
  $('[data-semantic="run"]', panel).addEventListener('click', () => run(panel, true));
  $('[data-semantic="cancel"]', panel).addEventListener('click', () => post('/api/semantic/cancel', {}, panel));
}

function preview(panel, sample = null) {
  const episode = $('[data-semantic="episode"]', panel).value;
  const index = sample ? sample.index : Number($('[data-semantic="index"]', panel).value);
  const id = sample ? sample.episode_id : episode;
  if (!id || !Number.isInteger(index) || index < 0) return;
  const previewKey = id + ':' + index;
  if (panel.dataset.previewKey !== previewKey) {
    for (const key of ['phase','holding','placement']) {
      $('[data-semantic="'+key+'"]',panel).textContent = '尚无当前画面的模型结果';
      $('[data-semantic="'+key+'-prob"]',panel).textContent = '';
    }
    $('[data-semantic="identity"]',panel).textContent = '当前预览尚无匹配的模型结果；上次验证的回执保留在下方。';
    const warningEl = $('[data-semantic="consistency"]',panel);
    if (warningEl) warningEl.textContent = '';
  }
  panel.dataset.previewKey = previewKey;
  for (const role of ['front','right']) {
    const img = $('[data-semantic-image="' + role + '"]', panel);
    const path = '/api/semantic/image/' + encodeURIComponent(id) + '/' + index + '/' + role;
    if (img.dataset.sourcePath === path) continue;
    img.dataset.sourcePath = path;
    img.hidden = true;
    img.onload = () => {img.hidden = false;};
    img.onerror = () => {img.hidden = true;};
    img.src = path;
  }
  $('[data-semantic="source"]', panel).textContent = '历史录像 ' + id + ' · 采样点 ' + index + '；相机软件源时间保留在原始字段中。';
}

async function post(path, data, panel) {
  if (pending) return;
  pending = true;
  delete panel.dataset.postError;
  try {
    if (!token) throw new Error('网页会话尚未准备好');
    const response = await fetch(path, {method:'POST',headers:{'Content-Type':'application/json','X-Tiger-Token':token},body:JSON.stringify(data)});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || '请求未成功');
    $('[data-semantic="error"]', panel).hidden = true;
    await refresh();
  } catch (error) {
    panel.dataset.postError = String(error.message || error);
    const el = $('[data-semantic="error"]', panel); el.hidden = false; el.textContent = String(error.message || error);
  } finally {pending = false;}
}

function run(panel, all) {
  return post('/api/semantic/replay', {episode_id:$('[data-semantic="episode"]',panel).value,index:Number($('[data-semantic="index"]',panel).value),all},panel);
}

function render(data) {
  workspace = data;
  for (const panel of $$('[data-semantic-panel]')) {
    const text = (name, value) => {$('[data-semantic="' + name + '"]', panel).textContent = value;};
    const select = $('[data-semantic="episode"]', panel);
    const catalog = Array.isArray(data.catalog) ? data.catalog : [];
    const signature = JSON.stringify(catalog.map(x => x.episode_id));
    if (select.dataset.catalog !== signature) {
      const old = select.value; select.replaceChildren();
      for (const row of catalog) {
        const option = document.createElement('option'); option.value = row.episode_id;
        option.textContent = '第 ' + row.ordinal + ' 条 · ' + Number(row.duration_s).toFixed(1) + ' s · ' + row.observation_count + ' 个采样点';
        select.append(option);
      }
      if (catalog.some(x => x.episode_id === old)) select.value = old;
      select.dataset.catalog = signature;
      preview(panel);
    }
    const selected = catalog.find(x => x.episode_id === select.value);
    $('[data-semantic="index"]',panel).max = Math.max(0,(selected?.observation_count || 1)-1);
    const runState = data.run || {}, result = data.observer?.result;
    const active = ['running','cancelling'].includes(runState.status);
    text('status', statusNames[runState.status || data.observer?.status] || '尚未配置模型与录像');
    for (const key of ['sample','run']) $('[data-semantic="' + key + '"]',panel).disabled = active || !catalog.length;
    $('[data-semantic="cancel"]',panel).disabled = !active;
    const latestKey = data.latest_sample ? data.latest_sample.episode_id + ':' + data.latest_sample.index : '';
    if (data.latest_sample && (active || panel.dataset.lastRunSample !== latestKey)) {
      select.value = data.latest_sample.episode_id;
      $('[data-semantic="index"]',panel).value = data.latest_sample.index;
      preview(panel,data.latest_sample);
      panel.dataset.lastRunSample = latestKey;
    }
    const rawError = panel.dataset.postError || data.error || result?.error?.detail || '';
    const error = panel.dataset.postError || (!rawError ? '' : result?.error?.status === 'unavailable'
      ? '本地语义模型服务尚未就绪。本次输入与失败回执已保留，服务启动后可重新验证。'
      : result?.error?.status === 'timeout' ? '本次模型请求超时，未生成有效候选。详情和原始输入身份已保留。'
      : result?.error ? '模型响应未通过校验，当前不显示候选。请展开原始输出核对原因。' : rawError);
    const errorEl = $('[data-semantic="error"]',panel); errorEl.hidden = !error; errorEl.textContent = error || '';
    const names = {phase:phaseNames,holding:holdingNames,placement:placementNames};
    for (const key of Object.keys(names)) {
      const matchesImage = result?.frame_id === data.latest_sample?.frame_id &&
        panel.dataset.previewKey === latestKey && result?.matches_latest_request === true;
      const answer = result?.status === 'model_candidate' && matchesImage ? result.answers?.[key] : null;
      const view = semanticAnswerView(answer,names[key]);
      if (matchesImage && result?.status === 'model_candidate' && result.not_requested?.includes(key)) {
        view.label = '本方案未单独请求'; view.detail = '单问阶段方案不提供独立的持物或入桶概率';
      }
      text(key,view.label); text(key+'-prob',view.detail);
    }
    const last = Array.isArray(data.results) ? data.results.at(-1) : null;
    const sameView = result?.frame_id === data.latest_sample?.frame_id && panel.dataset.previewKey === latestKey && result?.matches_latest_request === true;
    text('identity',result && sameView ? '来源：历史录像 · 模型 ' + (result.model || '未核对') + ' · 端到端 ' + (typeof last?.end_to_end_ms === 'number' ? last.end_to_end_ms.toFixed(0)+' ms' : '等待完成') + ' · 已处理 ' + (data.result_count || 0) + ' 个采样点 · 物理结果未确认' : result ? '当前预览尚无匹配模型结果。最近已完成 ' + (data.result_count || 0) + ' 组；上一组耗时 ' + (typeof last?.end_to_end_ms === 'number' ? last.end_to_end_ms.toFixed(0)+' ms' : '未核对') + '，对应图像与输出保留在回执。' : '模型服务已配置时，首次真实推理将返回模型身份与耗时。');
    const profile = result?.input_profile || data.run?.input_profile || data.input_profile;
    if (profile) text('identity',$('[data-semantic="identity"]',panel).textContent + ' · 输入：' + (profileNames[profile] || profile));
    const temporal = sameView ? data.latest_sample?.temporal_actual_features : null;
    text('temporal',temporal ? '历史实际反馈：' + temporal.sample_count + ' 个源样本 / ' + Number(temporal.actual_span_s).toFixed(3) + ' s；只用当前及更早数据。夹爪开合不能单独证明持物。' : '');
    const warnings=sameView && Array.isArray(result?.consistency_warnings) ? [...result.consistency_warnings] : [];
    if(sameView && result?.phase_assessment) {
      const a=result.phase_assessment;
      warnings.push('运动证据复核：'+(phaseNames[a.choice] || '无法判断')+(a.gate_applied?'；历史不足或运动证据不足，单独降级，原模型概率保留。':'；未修改原模型候选。'));
    }
    text('consistency',warnings.join('；'));
    text('save',data.receipt_path ? '真实验证回执：' + data.receipt_path : '本轮验证回执在结束或取消后写入选定的 128GB U 盘。');
    text('raw',JSON.stringify({run:data.run,latest_sample:data.latest_sample,result,error:data.error,receipt_path:data.receipt_path},null,2));
  }
}

function renderPreflight(state) {
  const host = $('#semantic-mode-preflight');
  if (!host) return;
  host.replaceChildren();
  const p = state.mode_preflight || {};
  const labels = {mode_takeover:'电脑 CAN / MOVEJ',mode_high_follow:'原厂主从高跟随',mode_standby:'待机',mode_disable:'六关节失能',mode_reset:'受支撑单次复位'};
  for (const [key,label] of Object.entries(labels)) {
    const item = document.createElement('li');
    const available = p.actions?.[key]?.available === true;
    item.textContent = label + '：' + (available ? '网页预检查通过，执行时仍核验现场条件' : p.actions?.[key]?.reason || '等待真实连接与反馈');
    host.append(item);
  }
  const evidence = $('#semantic-mode-evidence');
  if (evidence) evidence.textContent = '原厂目标最大年龄：' + millis(p.oem_targets?.max_pair_age_s) + '；三组目标时间差：' + millis(p.oem_targets?.receipt_skew_s) + '；目标/反馈最大偏差：' + (typeof p.oem_targets?.max_joint_error_deg === 'number' ? p.oem_targets.max_joint_error_deg.toFixed(2)+'°' : '未核对') + '。持续静止与物理控制来源仍由执行层核验。';
}
function millis(value) { return typeof value === 'number' && Number.isFinite(value) ? (value*1000).toFixed(0)+' ms' : '未核对'; }

let refreshBusy = false, ticks = 0;
async function refresh() {
  if (refreshBusy || document.hidden) return;
  refreshBusy = true;
  try {
    if (!token || ticks++ % 3 === 0) {
      const response = await fetch('/api/state',{cache:'no-store'}); if (!response.ok) throw new Error('网页后端不可用');
      const state = await response.json(); token = state.csrf_token; renderPreflight(state);
    }
    const response = await fetch('/api/semantic',{cache:'no-store'});
    if (!response.ok) throw new Error('语义录像接口不可用');
    render(await response.json());
  } catch (error) {
    for (const panel of $$('[data-semantic-panel]')) {
      $('[data-semantic="status"]',panel).textContent = '网页通路未连接';
      const el=$('[data-semantic="error"]',panel);el.hidden=false;el.textContent=String(error.message || error);
      for (const key of ['sample','run','cancel']) $('[data-semantic="'+key+'"]',panel).disabled=true;
    }
  } finally {refreshBusy=false;}
}
makePanel('teleop','teleop'); makePanel('deploy','deploy');
refresh(); setInterval(refresh,1000);
