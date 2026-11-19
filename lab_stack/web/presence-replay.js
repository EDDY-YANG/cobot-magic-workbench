// Read-only recorded detector output. This module has no command or POST path.
import {stateLabels as labels, presenceWords, reasonLabel, modelRecords, sampleCategory,
  missedIntervals, caseInfo, caseMatches, sampleMatches, replayView} from './presence-replay-model.mjs';
import {reviewedJoinForClip, stageDisplay} from './presence-stage-join.mjs';

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function number(value, digits = 1) { return Number.isFinite(value) ? value.toFixed(digits) : '—'; }
function makeTable(headers) {
  const table = el('table'), head = el('thead'), row = el('tr'), body = el('tbody');
  for (const title of headers) row.append(el('th', title));
  head.append(row); table.append(head, body);
  return {table, body};
}
function makeSelect(label, entries) {
  const select = el('select'); select.setAttribute('aria-label', label);
  for (const [value, title] of entries) { const option = el('option', title); option.value = value; select.append(option); }
  return select;
}
function panel(host, data) {
  const root = el('article', undefined, 'panel presence-replay'), title = el('div', undefined, 'panel-title');
  title.append(el('h2', '任务阶段与人员／手状态 · 同屏观察'), el('span', '仅显示 / 未接控制', 'pill neutral'));
  root.append(title, el('p', data.detector_summary, 'panel-copy'));
  if (data.comparison_text) root.append(el('p', data.comparison_text, 'panel-copy'));
  root.append(el('p', '检测到人或手时只显示提示，不发暂停、恢复或其他机器人指令。录像继续播放，才能观察人手离开；“恢复条件满足”只表示这段回放的新图复核完成。', 'presence-display-only'));

  const controls = el('div', undefined, 'presence-controls');
  const caseFilter = makeSelect('人员暂停案例筛选', [['all', '全部案例'], ['hands', '有人手的桌面视频'], ['cycles', '完整暂停／恢复链条'], ['issues', '误判／拒答／漏采样']]);
  const select = makeSelect('人员暂停验证视频', []), caseCount = el('span', '', 'footnote');
  controls.append(caseFilter, select, caseCount); root.append(controls);
  const casesDetails = el('details'); casesDetails.append(el('summary', '选择其他案例 · ' + data.clips.length + ' 个视频视图'));
  const cases = el('div', undefined, 'presence-case-grid'); cases.setAttribute('aria-label', '可播放的桌面视频案例'); casesDetails.append(cases); root.append(casesDetails);
  const source = el('p', '', 'presence-source footnote'); root.append(source);
  const video = el('video'); video.controls = true; video.preload = 'metadata'; video.playsInline = true;
  video.setAttribute('aria-label', '人员和人手检测的真实本地录像');
  const state = el('div', '', 'presence-status'); state.setAttribute('role', 'status'); state.setAttribute('aria-live', 'polite'); root.append(state);
  const cards = el('div', undefined, 'presence-cards'), values = {};
  for (const [key, name] of Object.entries({stage: '同源任务阶段', persons: '人员／人手识别', clear: '离开确认', permit: '重新观察／恢复'})) {
    const card = el('div'), value = el('strong', '—'); card.append(el('label', name), value); cards.append(card); values[key] = value;
  }
  root.append(cards);
  const recoveryHistory = el('p', '', 'presence-recovery-history'); root.append(recoveryHistory, video);
  const timing = el('p', '', 'presence-current-timing'), summary = el('p', '', 'footnote'), review = el('p', '', 'presence-review-summary');
  const clipStats = el('p', '', 'presence-clip-stats');
  root.append(timing, clipStats, summary, review);
  root.append(el('h3', '关键时刻 · 点击查看原画面'));
  const timeline = el('div', undefined, 'presence-timeline'); timeline.setAttribute('aria-label', '暂停恢复与独立参考时间线'); root.append(timeline);
  const eventsDetails = el('details', undefined, 'presence-event-details'); eventsDetails.append(el('summary', '展开全部事件与原始原因'));
  const events = el('div', undefined, 'presence-events'), eventTable = makeTable(['结果／参考时刻', '状态', '原因／事件']);
  events.append(eventTable.table); eventsDetails.append(events); root.append(eventsDetails);
  const detail = el('details'); detail.append(el('summary', '查看本帧检测、来源与重新观察回执'));
  const raw = el('pre', '', 'presence-raw'); detail.append(raw); root.append(detail);

  const sampleReview = el('details', undefined, 'presence-sample-review');
  sampleReview.append(el('summary', '逐次判断 · 误判、拒答、漏采样与实际耗时'));
  sampleReview.append(el('p', '源帧时间表示输入图像，结果时间表示判断实际生效；看早期原图时不会提前显示未来结果。原始首选与通过守卫的判断分开展示：首选误判即使被拒绝也保留；拒答不能计作检出。独立参考未覆盖的时刻保持未标注。', 'footnote'));
  const sampleTools = el('div', undefined, 'presence-controls');
  const sampleFilter = makeSelect('逐次人员判断筛选', [['all', '全部采样与系统漏采样'], ['issues', '全部问题'], ['error', '模型误判'], ['uncertain', '拒答／无效'], ['missed', '系统漏采样']]);
  const sampleCount = el('span', '', 'footnote'); sampleTools.append(sampleFilter, sampleCount); sampleReview.append(sampleTools);
  const sampleScroll = el('div', undefined, 'presence-sample-scroll');
  const sampleTable = makeTable(['输入源帧', '实际结果时刻', '已接受判断／原始首选', '独立参考／比较', '客户端 ms', '源→应用 ms', '完整记录']);
  sampleScroll.append(sampleTable.table); sampleReview.append(sampleScroll); root.append(sampleReview);

  if (data.long_review) {
    const overviewDetails = el('details', undefined, 'presence-long-review');
    overviewDetails.append(el('summary', '长桌面视频 · 1 Hz完整链条与失败检查'), el('p', data.long_review.description, 'panel-copy'));
    const overview = el('div', undefined, 'presence-long-overview');
    const table = makeTable(['录像／作者倍速', '长度', '取样／有效结果', '模型误判／拒答', '完整恢复链条', '1Hz超期', '处理P50／P95']);
    for (const item of data.long_review.rows) {
      const row = el('tr'); for (const value of [item.title, item.duration, item.sampling, item.errors, item.cycles, item.deadlines, item.timing]) row.append(el('td', value)); table.body.append(row);
    }
    overview.append(table.table); overviewDetails.append(overview, el('p', '“恢复”只表示回放候选条件。作者加速录像按编码时钟验证，不能换算成真实制动时间。', 'footnote')); root.append(overviewDetails);
  }
  if (data.images?.length) {
    const gallery = el('details', undefined, 'presence-image-comparison'); gallery.append(el('summary', '桌面图片识别对照（' + data.images.length + '张原图）'));
    const grid = el('div', undefined, 'presence-image-grid');
    for (const item of data.images) {
      const card = el('article', undefined, 'presence-image-card'); card.append(el('strong', item.title));
      const link = el('a'); link.href = item.image.url; link.target = '_blank'; link.rel = 'noopener'; link.setAttribute('aria-label', '打开原图 ' + item.id);
      const picture = el('img'); picture.src = item.image.url; picture.alt = item.title; picture.loading = 'lazy'; link.append(picture); card.append(link);
      card.append(el('p', '独立看图参考：' + item.reference + ' · ' + item.source_description, 'footnote'));
      for (const result of item.results) card.append(el('p', result.model + '：' + result.decision + ' · ' + number(result.timing_ms) + ' ms', result.matches_reference ? 'presence-correct' : 'presence-incorrect'));
      const details = el('details'); details.append(el('summary', '实际模型原始输出'), el('pre', JSON.stringify(item.results, null, 2), 'presence-raw')); card.append(details); grid.append(card);
    }
    gallery.append(grid); root.append(gallery);
  }

  let clip = null, waiting = true, switching = true, expectedSource = '', pendingSeek = null, lastRendered = '';
  const visiblePresence = {present: '检测到人员／人手', absent: '当前未见人员／人手', uncertain: '无法确认是否有人／手'};
  function stageText(time) {
    const join = reviewedJoinForClip(clip, data.stage_join);
    if (!join) return '本片未评估任务阶段';
    const episode = data.stage_review?.episodes?.find(item => item.episode_id === join.episode_id);
    if (!episode) return '同源任务阶段资料不可用';
    const phaseTime = time - join.stage_to_presence_offset_s;
    return stageDisplay(episode, episode.series[data.stage_review.default_profile] || [], phaseTime,
      {error:video.error,seeking:video.seeking,waiting,readyState:video.readyState,ended:video.ended}, data.gate_config.max_age_s).label;
  }
  function closeDisplay(message) {
    state.dataset.state = 'PAUSED'; state.textContent = '离线暂停状态 · ' + message;
    for (const value of Object.values(values)) value.textContent = '—';
    values.permit.textContent = '关闭'; timing.textContent = ''; raw.textContent = ''; lastRendered = '';
  }
  function render() {
    if (!clip || switching) return closeDisplay(clip ? '切换录像，等待原画面载入' : '该筛选下没有案例');
    const t = video.currentTime || 0;
    const view = replayView(clip, t, {error: video.error, seeking: video.seeking, waiting, readyState: video.readyState, ended: video.ended}, data.gate_config);
    state.dataset.state = view.state;
    const row = view.row, gate = row?.gate, semantic = row?.semantic_presence || gate?.semantic_presence;
    const observations = {CLEAR_CONFIRMING: '当前未见人员／手，正在确认离开', REOBSERVE_REQUIRED: '离开确认完成，等待下一张新图复核', RESUME_ELIGIBLE: '离开已确认，新图复核通过：观察恢复条件满足（仅显示）'};
    const stateText = view.blocked || observations[view.state] || (semantic === 'present' ? '检测到人员／人手 · 仅提示，未发送控制指令' : '人员状态无法确认，保留观察暂停提示');
    if (state.textContent !== stateText) state.textContent = stateText;
    values.stage.textContent = stageText(t);
    values.permit.textContent = view.permit ? '可恢复（仅显示，不发指令）' : view.state === 'REOBSERVE_REQUIRED' ? '等待更新原图' : '尚未满足';
    if (!row) { for (const key of ['persons', 'clear']) values[key].textContent = '等待有效观察'; timing.textContent = ''; raw.textContent = ''; return; }
    values.persons.textContent = view.blocked ? '等待有效观察' : visiblePresence[semantic] || (row.record_kind && row.record_kind !== 'model' ? '无新识别结果' : '检测到人体 ' + gate.person_count + '／手 ' + gate.hand_count);
    values.clear.textContent = number(gate.continuous_clear_s, 2) + ' / ' + number(gate.config?.clear_confirm_s ?? data.gate_config.clear_confirm_s, 2) + ' s';
    timing.textContent = '输入源帧 ' + number(row.source_pts_s, 3) + ' s → 实际结果 ' + number(row.evaluated_at_pts_s, 3) + ' s · 检测 ' + number(row.timing_ms?.detector_total) + ' ms · 全图人员判断，未输出定位框 · 控制指令未接入';
    // Serializing a full response is only necessary when the active record changes.
    const recordKey = clip.id + ':' + row.frame_id + ':' + row.evaluated_at_pts_s;
    if (recordKey !== lastRendered) { raw.textContent = JSON.stringify(row, null, 2); lastRendered = recordKey; }
  }
  function seek(time) {
    if (!clip) return;
    const target = Math.max(0, Math.min(time + .002, Math.max(0, clip.duration_s - .001)));
    waiting = true; closeDisplay('正在定位原画面；暂不展示恢复许可');
    if (video.readyState < 1 || switching) pendingSeek = target;
    else video.currentTime = target;
  }
  function timeButton(time, text, cls = 'button quiet') {
    const button = el('button', text, cls); button.type = 'button'; button.onclick = () => seek(time); return button;
  }
  function renderSamples() {
    sampleTable.body.replaceChildren();
    if (!clip) return;
    const all = modelRecords(clip), missed = missedIntervals(clip), filter = sampleFilter.value;
    const rows = filter === 'missed' ? [] : all.filter(row => sampleMatches(row, filter));
    const intervals = ['all', 'issues', 'missed'].includes(filter) ? missed : [];
    sampleCount.textContent = rows.length + ' 个采样／' + intervals.length + ' 个漏采样区间；原片共 ' + all.length + ' 次模型输入';
    for (const cycle of intervals) {
      const tr = el('tr', undefined, 'presence-error-row'), td = el('td');
      const start = cycle.known_present_start_s ?? cycle.entry_latest_s;
      const end = cycle.known_present_end_s ?? cycle.exit_earliest_s;
      td.append(timeButton(start, number(start, 3) + '–' + number(end, 3) + ' s')); tr.append(td);
      for (const text of ['无对应阳性结果', '未捕获该伸手区间', '独立参考有人；期间许可未关闭', '—', '—']) tr.append(el('td', text));
      const details = el('details'); details.append(el('summary', '查看系统失败证据'), el('pre', JSON.stringify(cycle, null, 2), 'presence-raw'));
      const last = el('td'); last.append(details); tr.append(last); sampleTable.body.append(tr);
    }
    for (const row of rows) {
      const tr = el('tr'), category = sampleCategory(row); if (category === 'error' || category === 'uncertain') tr.className = 'presence-error-row';
      const input = el('td'), result = el('td'); input.append(timeButton(row.source_pts_s, number(row.source_pts_s, 3) + ' s')); result.append(timeButton(row.evaluated_at_pts_s, number(row.evaluated_at_pts_s, 3) + ' s'));
      const output = row.valid === false ? '无法判断／无效' : presenceWords[row.semantic_presence] || (row.present === true ? '有人／有手' : row.present === false ? '未见人／手' : '查看原始输出');
      const assessment = row.assessment || {}, reference = assessment.reference_presence || assessment.reference;
      const decisionCell = el('td'); decisionCell.append(el('span', output));
      const rawChoice = assessment.raw_choice || row.raw_semantic_choice;
      if (rawChoice) decisionCell.append(el('small', '原始首选：' + (presenceWords[rawChoice] || rawChoice) + (assessment.raw_top1_error ? '（与参考不符）' : ''), 'presence-raw-choice'));
      tr.append(input, result, decisionCell, el('td', (presenceWords[reference] || '未标注') + ' · ' + (assessment.label || '未做独立逐帧评分')),
        el('td', number(row.timing_ms?.client)), el('td', number(row.timing_ms?.source_to_result)));
      const last = el('td'), details = el('details'); details.append(el('summary', labels[row.gate.state] || row.gate.state), el('pre', JSON.stringify(row, null, 2), 'presence-raw')); last.append(details); tr.append(last); sampleTable.body.append(tr);
    }
    if (!rows.length && !intervals.length) { const tr = el('tr'), td = el('td', '该筛选下没有记录。未标注不等于已确认正确。'); td.colSpan = 7; tr.append(td); sampleTable.body.append(tr); }
  }
  function choose() {
    const firstSelection = clip === null;
    video.pause(); switching = true; waiting = true; pendingSeek = null; closeDisplay('切换录像，等待原画面载入');
    clip = data.clips.find(item => item.id === select.value) || null;
    eventTable.body.replaceChildren(); timeline.replaceChildren(); sampleTable.body.replaceChildren(); recoveryHistory.textContent = '';
    video.removeAttribute('src'); video.load();
    if (!clip) { expectedSource = ''; source.textContent = ''; summary.textContent = ''; review.textContent = ''; clipStats.textContent = ''; return render(); }
    expectedSource = new URL(clip.video.url, location.href).href; video.src = clip.video.url;
    if (firstSelection && caseInfo(clip).complete) {
      const example = clip.records.find(row => row.valid !== false && row.semantic_presence === 'present');
      if (example) pendingSeek = Math.min(example.evaluated_at_pts_s + .05, clip.duration_s - .001);
    }
    source.textContent = clip.source_description + ' · ' + number(clip.duration_s, 2) + ' s · ' + clip.reference_note;
    summary.textContent = clip.summary_text || ''; review.textContent = clip.validation_summary || '';
    const info = caseInfo(clip), stats = clip.metrics?.timing_ms?.source_to_result;
    clipStats.textContent = (info.reviewedCount ? '已接受误判 ' + info.acceptedErrorCount : '已接受判断未评分') +
      ' · ' + (info.rawReviewedCount ? '原始首选误判 ' + info.rawErrorCount : '原始首选未评分') +
      ' · 拒答／无效 ' + info.uncertainCount + ' · 系统漏采样 ' + (clip.validation?.cycles ? info.missedCount : '未评分') +
      ' · 源→应用 P50 / P95：' + number(stats?.p50) + ' / ' + number(stats?.p95) + ' ms';
    for (const card of cases.children) card.setAttribute('aria-pressed', String(card.dataset.clipId === clip.id));
    for (const event of clip.events || []) {
      const row = el('tr'), cell = el('td'); cell.append(timeButton(event.time_s, number(event.time_s, 3) + ' s'));
      row.append(cell, el('td', labels[event.state] || event.state), el('td', reasonLabel(event.description))); eventTable.body.append(row);
      if (event.state.startsWith('REFERENCE_') || event.detail?.event === 'state_changed' || !event.detail) {
        const point = timeButton(event.time_s, number(event.time_s, 2) + ' s · ' + (labels[event.state] || event.state), 'presence-timeline-point');
        point.dataset.state = event.state; point.title = reasonLabel(event.description); timeline.append(point);
      }
    }
    renderSamples(); render();
    const first = stateName => clip.records.find(row => row.gate?.state === stateName && (stateName !== 'RESUME_ELIGIBLE' || row.gate?.replay_permission === true));
    const seen = clip.records.find(row => row.valid !== false && row.semantic_presence === 'present');
    const steps = [[seen, '查看识别到人员／手'], [first('CLEAR_CONFIRMING'), '查看离开确认'], [first('REOBSERVE_REQUIRED'), '查看新图复核'], [first('RESUME_ELIGIBLE'), '查看恢复条件满足']];
    const quickSteps = document.createDocumentFragment();
    for (const [row, label] of steps) if (row) {
      const button = timeButton(row.evaluated_at_pts_s + .05, label + ' · ' + number(row.evaluated_at_pts_s, 2) + ' s', 'presence-timeline-point');
      button.dataset.state = row.gate.state; quickSteps.append(button);
    }
    timeline.prepend(quickSteps);
    const recovered = first('RESUME_ELIGIBLE');
    recoveryHistory.textContent = recovered ? (seen && seen.evaluated_at_pts_s < recovered.evaluated_at_pts_s ? '' : '开场清场确认，不是人手离开事件。') + '本片在 ' + number(recovered.evaluated_at_pts_s, 3) + ' s 已满足新图复核后的恢复候选。片尾会再次关闭当前观察状态，历史恢复记录仍保留；没有下发机器人指令。' : '本片尚无完整恢复候选。尾部空场不足、仍可见手指或模型拒答时，继续等待；不能直接把“手看起来离开”当作已恢复。';
  }
  function filterCases() {
    const filtered = data.clips.filter(item => caseMatches(item, caseFilter.value)), previous = select.value;
    cases.replaceChildren(); select.replaceChildren(); select.disabled = filtered.length === 0;
    caseCount.textContent = filtered.length + ' / ' + data.clips.length + ' 个真实视频视图';
    for (const item of filtered) {
      const info = caseInfo(item), option = el('option', item.title); option.value = item.id; select.append(option);
      const card = el('button', undefined, 'presence-case'); card.type = 'button'; card.dataset.clipId = item.id; card.dataset.issue = String(info.failed);
      card.setAttribute('aria-pressed', 'false'); card.setAttribute('aria-label', '播放案例 ' + item.title);
      card.append(el('strong', item.title), el('span', info.scene + ' · ' + number(item.duration_s, 1) + ' 秒', 'footnote'),
        el('span', info.status, 'presence-case-status'), el('small', (info.reviewedCount ? '已接受误判 ' + info.acceptedErrorCount : '已接受判断未评分') + '／' + (info.rawReviewedCount ? '原始首选误判 ' + info.rawErrorCount : '原始首选未评分') + ' · 拒答 ' + info.uncertainCount + ' · 漏采样区间 ' + (item.validation?.cycles ? info.missedCount : '未评分')));
      card.onclick = () => { select.value = item.id; choose(); }; cases.append(card);
    }
    if (filtered.some(item => item.id === previous)) select.value = previous;
    else if (!clip && filtered.some(item => item.id === 'long-hilserl-usb-1x')) select.value = 'long-hilserl-usb-1x';
    if (clip?.id !== select.value) choose();
    else for (const card of cases.children) card.setAttribute('aria-pressed', String(card.dataset.clipId === clip.id));
  }
  select.onchange = choose; caseFilter.onchange = filterCases; sampleFilter.onchange = renderSamples;
  video.ontimeupdate = render;
  video.onloadedmetadata = () => {
    if (video.currentSrc !== expectedSource) return;
    switching = false;
    if (pendingSeek !== null) { video.currentTime = pendingSeek; pendingSeek = null; }
    render();
  };
  const available = () => { if (video.currentSrc !== expectedSource) return; waiting = false; render(); };
  video.onloadeddata = available; video.oncanplay = available; video.onplaying = available; video.onseeked = available;
  video.onwaiting = () => { waiting = true; render(); }; video.onseeking = render; video.onstalled = () => { waiting = true; render(); };
  video.onended = render; video.onerror = render;
  video.onplay = () => { for (const other of document.querySelectorAll('.presence-replay video, .semantic-playback video')) if (other !== video) other.pause(); };
  const anchor = host.querySelector('.semantic-workspace'); if (anchor) anchor.before(root); else host.prepend(root);
  filterCases();
}
async function load() {
  try {
    const response = await fetch('/api/presence', {cache: 'no-store'}); if (!response.ok) return;
    const data = await response.json();
    const extras = await Promise.allSettled([fetch('/api/semantic/review', {cache:'no-store'}).then(r => r.ok ? r.json() : null), fetch('/2026-10-18-13-20-reviewed-presence-stage-join.json', {cache:'no-store'}).then(r => r.ok ? r.json() : null)]);
    data.stage_review = extras[0].status === 'fulfilled' ? extras[0].value : null;
    data.stage_join = extras[1].status === 'fulfilled' ? extras[1].value : null;
    for (const id of ['teleop', 'deploy']) { const host = document.querySelector('#page-' + id); if (host) panel(host, data); }
  } catch (error) { console.error('Presence replay unavailable', error); }
}
load();
