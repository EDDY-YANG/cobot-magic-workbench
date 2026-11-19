/** Read-only chart of the existing UI's actual right-arm feedback.
 * No fetch/XHR, new device owner, persistence, or robot command exists here.
 * The table supplies ° and mm at the existing ~2.5Hz page sampling rate.
 * Its missing/stale values stay gaps; target drafts are never inputs.
 */
import {JOINT_COLORS, renderedFeedback, TraceHistory, valueRange, lineSegments} from './motion-trace-core.mjs';

function mount() {
  const panel = document.querySelector('#page-motion .robot-panel');
  if (!panel || document.getElementById('motion-trace')) return;
  const css = document.createElement('link'); css.rel = 'stylesheet';
  css.href = new URL('./motion-trace.css', import.meta.url).href; document.head.append(css);
  const section = document.createElement('section'); section.id = 'motion-trace'; section.className = 'motion-trace';
  section.setAttribute('aria-label', '右臂六关节与夹爪实际反馈轨迹');
  section.innerHTML = `<div class="motion-trace-header"><h3>右臂实时轨迹</h3><div class="motion-trace-tools">
    <select aria-label="轨迹时间窗口"><option value="30">最近30秒</option><option value="60" selected>最近60秒</option><option value="120">最近120秒</option></select>
    <button type="button" class="button" data-trace-pause aria-pressed="false">暂停图表</button>
    <button type="button" class="button" data-trace-clear>清空图表</button></div></div>
    <div class="motion-trace-legend" aria-label="显示关节曲线"></div>
    <canvas class="motion-trace-joints" role="img" aria-label="六关节实际角度随时间变化，单位度"></canvas>
    <canvas class="motion-trace-gripper" role="img" aria-label="夹爪实际开口随时间变化，单位毫米"></canvas>
    <div class="motion-trace-footer"><span class="motion-trace-status">等待真实反馈</span><span>网页采样 ≈2.5Hz · 断流留空</span></div>
    <p class="motion-trace-hover">悬停查看时刻与实测值</p>`;
  panel.append(section);
  const $ = selector => section.querySelector(selector), history = new TraceHistory();
  const visible = Array(6).fill(true), legend = $('.motion-trace-legend');
  const legendButtons = JOINT_COLORS.map((color, i) => {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'motion-trace-series';
    button.style.setProperty('--trace-color', color); button.setAttribute('aria-pressed', 'true');
    button.setAttribute('aria-label', `显示J${i + 1}曲线`); button.textContent = `J${i + 1} —`;
    button.addEventListener('click', () => {visible[i] = !visible[i];button.setAttribute('aria-pressed', String(visible[i]));draw();});
    legend.append(button); return button;
  });
  const windowSelect = $('select'), pauseButton = $('[data-trace-pause]'), status = $('.motion-trace-status');
  const canvases = [$('.motion-trace-joints'), $('.motion-trace-gripper')];
  let paused = null, hover = null, lastObserved = null;
  const text = id => document.getElementById(id)?.textContent;
  function sample() {
    const now = performance.now();
    const online = !document.hidden && document.getElementById('page-refresh-status')?.dataset.health === 'live';
    const data = renderedFeedback(text, online);
    if (history.add(now, data)) lastObserved = now;
  }
  const observer = new MutationObserver(sample);
  // Reuse only existing display nodes. Never poll /api/state a second time.
  for (const id of [...Array.from({length: 6}, (_, i) => `follower-right-j${i}`), 'follower-right-gripper']) {
    const node = document.getElementById(id); if (node) observer.observe(node, {childList: true, characterData: true, subtree: true});
  }
  const freshness = document.getElementById('page-refresh-status');
  if (freshness) observer.observe(freshness, {attributes: true, attributeFilter: ['data-health']});
  function graph(canvas, rows, end, gripper) {
    const rect = canvas.getBoundingClientRect(); if (rect.width < 1) return;
    const w = rect.width, h = rect.height, dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext('2d'); if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);ctx.clearRect(0, 0, w, h);
    const margin = {left: 43, right: 10, top: 21, bottom: 21};
    const pw = Math.max(1, w - margin.left - margin.right), ph = Math.max(1, h - margin.top - margin.bottom);
    const seconds = Number(windowSelect.value), start = end - seconds * 1000;
    const points = rows.filter(row => row.time >= start - 1000 && row.time <= end);
    const values = points.flatMap(row => gripper ? [row.g] : row.q ? row.q.filter((_, i) => visible[i]) : []);
    const range = valueRange(values, gripper ? 5 : 2) || (gripper ? [0, 100] : [-1, 1]);
    const x = t => margin.left + (t - start) / (seconds * 1000) * pw;
    const y = value => margin.top + (range[1] - value) / (range[1] - range[0]) * ph;
    ctx.font = '10px ui-monospace, Consolas, monospace';ctx.fillStyle = '#536171';
    ctx.fillText(gripper ? '夹爪 / mm' : '关节 / °', 0, 11);
    ctx.lineWidth = 1;ctx.strokeStyle = '#e6eaf0';ctx.setLineDash([]);
    for (let i = 0; i < 3; i++) {
      const value = range[0] + (range[1] - range[0]) * i / 2, yy = y(value);
      ctx.beginPath();ctx.moveTo(margin.left, yy);ctx.lineTo(w - margin.right, yy);ctx.stroke();
      ctx.textAlign = 'right';ctx.fillText(value.toFixed(1), margin.left - 6, yy + 3);
    }
    for (let i = 0; i <= 4; i++) {
      const xx = margin.left + pw * i / 4;
      ctx.beginPath();ctx.moveTo(xx, margin.top);ctx.lineTo(xx, h - margin.bottom);ctx.stroke();
      ctx.textAlign = i === 0 ? 'left' : i === 4 ? 'right' : 'center';
      ctx.fillText(i === 4 ? '0s' : `−${seconds * (1 - i / 4)}s`, xx, h - 5);
    }
    ctx.save();ctx.beginPath();ctx.rect(margin.left, margin.top - 1, pw, ph + 2);ctx.clip();
    const series = gripper ? [{color:'#223a50', value: row => row.g}] : JOINT_COLORS.flatMap((color, i) => visible[i] ? [{color, value: row => row.q?.[i]}] : []);
    for (const item of series) {
      ctx.strokeStyle = item.color;ctx.fillStyle = item.color;ctx.lineWidth = gripper ? 1.8 : 1.5;
      for (const segment of lineSegments(points, item.value)) {
        if (segment.length === 1) {ctx.beginPath();ctx.arc(x(segment[0].time), y(segment[0].value), 1.6, 0, Math.PI * 2);ctx.fill();continue;}
        ctx.beginPath();segment.forEach((p, i) => i ? ctx.lineTo(x(p.time), y(p.value)) : ctx.moveTo(x(p.time), y(p.value)));ctx.stroke();
      }
    }
    if (hover !== null && hover >= start && hover <= end) {
      ctx.strokeStyle = '#727d89';ctx.setLineDash([3, 3]);ctx.beginPath();ctx.moveTo(x(hover), margin.top);ctx.lineTo(x(hover), h - margin.bottom);ctx.stroke();
    }
    ctx.restore();ctx.setLineDash([]);ctx.textAlign = 'left';
    if (!values.some(Number.isFinite)) {ctx.fillStyle = '#7b8590';ctx.fillText(gripper ? '等待夹爪反馈' : '等待关节反馈', margin.left + 10, margin.top + ph / 2);}
    canvas.dataset.sampleCount = String(points.filter(row => gripper ? Number.isFinite(row.g) : !!row.q).length);
  }
  const fmt = (n, unit) => Number.isFinite(n) ? n.toFixed(2) + unit : '—';
  function draw() {
    if (document.hidden || !document.getElementById('page-motion')?.classList.contains('active')) return;
    const now = performance.now(), rows = paused?.rows || history.rows, end = paused?.end ?? now;
    const last = rows.at(-1), fresh = !!last && (paused || (now - last.time < 1000 && now - (lastObserved ?? -Infinity) < 1000));
    legendButtons.forEach((button, i) => {button.textContent = `J${i + 1} ${fmt(fresh && last?.q ? last.q[i] : null, '°')}`;});
    const qReady = fresh && !!last?.q, gReady = fresh && Number.isFinite(last?.g);
    status.textContent = paused ? '图表已暂停 · 控制照常运行' : qReady && gReady ? `实时反馈 · G ${fmt(last.g, 'mm')}` : qReady ? '关节在线 · 夹爪无新反馈' : gReady ? '夹爪在线 · 关节无新反馈' : '等待新反馈 · 保留历史';
    status.dataset.health = qReady && gReady ? 'live' : 'gap';
    canvases.forEach((canvas, i) => graph(canvas, rows, end, i === 1));
    if (hover !== null) {
      const nearest = rows.reduce((best, row) => !best || Math.abs(row.time - hover) < Math.abs(best.time - hover) ? row : best, null);
      $('.motion-trace-hover').textContent = nearest && Math.abs(nearest.time - hover) <= 800
        ? new Date(nearest.wall).toLocaleTimeString('zh-CN', {hour12: false}) + '  ' + Array.from({length: 6}, (_, i) => `J${i + 1} ${fmt(nearest.q?.[i], '°')}`).join('  ') + `  G ${fmt(nearest.g, 'mm')}`
        : '此时无网页采样';
    }
  }
  for (const canvas of canvases) {
    canvas.addEventListener('pointermove', event => {
      const rect = canvas.getBoundingClientRect(), fraction = (event.clientX - rect.left - 43) / Math.max(1, rect.width - 53);
      hover = (paused?.end ?? performance.now()) - (1 - Math.max(0, Math.min(1, fraction))) * Number(windowSelect.value) * 1000;draw();
    });
    canvas.addEventListener('pointerleave', () => {hover = null;$('.motion-trace-hover').textContent = '悬停查看时刻与实测值';draw();});
  }
  windowSelect.addEventListener('change', () => {hover = null;draw();});
  pauseButton.addEventListener('click', () => {
    paused = paused ? null : {end: performance.now(), rows: history.snapshot()};
    pauseButton.textContent = paused ? '继续滚动' : '暂停图表';pauseButton.setAttribute('aria-pressed', String(!!paused));hover = null;draw();
  });
  $('[data-trace-clear]').addEventListener('click', () => {
    history.clear();if (paused) paused = {end: performance.now(), rows: []};hover = null;sample();draw();
  });
  document.addEventListener('visibilitychange', () => {if (document.hidden) history.add(performance.now(), {q:null, g:null});else draw();});
  window.addEventListener('hashchange', draw);
  const resize = new ResizeObserver(draw);resize.observe(section);
  // Small bounded RAM buffer; rendering throttled and skipped outside Motion.
  const timer = setInterval(draw, 250);
  window.addEventListener('pagehide', () => {clearInterval(timer);observer.disconnect();resize.disconnect();}, {once: true});
  sample();draw();
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, {once:true});else mount();
