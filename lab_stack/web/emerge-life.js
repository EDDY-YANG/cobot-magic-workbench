import { evolve, seed } from './emerge-life-core.mjs';

// Decorative simulation only. No fetch, WebSocket, storage, or control APIs.
const width = 84, height = 26, cellSize = 5, intervalMs = 180;
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
let cells = seed(width, height), generation = 0, timer = null;
let userPaused = reducedMotion.matches, inView = true, pageRestored = true;
const panel = document.createElement('aside');
panel.className = 'emerge-life';
panel.setAttribute('aria-label', '生命游戏，独立装饰演示');
panel.innerHTML = `<div class="emerge-life-top"><span>EMERGENCE STUDY / 01</span><span class="emerge-life-rule">B3 · S23</span></div>
  <canvas class="emerge-life-canvas" width="420" height="130" role="img" aria-label="康威生命游戏：黑色细胞按邻居规则演化，非机器人数据"></canvas>
  <div class="emerge-life-bottom"><span class="emerge-life-generation">GEN 00000</span><span class="emerge-life-note">生命游戏 · 装饰演示</span>
  <div class="emerge-life-actions"><button type="button" class="emerge-life-toggle" aria-label="暂停生命游戏装饰">暂停图案</button><button type="button" class="emerge-life-reset" aria-label="重置生命游戏初态">↺</button></div></div>`;
const canvas = panel.querySelector('canvas'), ctx = canvas.getContext('2d');
const generationLabel = panel.querySelector('.emerge-life-generation');
const toggle = panel.querySelector('.emerge-life-toggle');

function draw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#fafafa'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#e1e1e1';
  for (let y = 0; y <= height; y += 2) for (let x = 0; x <= width; x += 2) ctx.fillRect(x * cellSize, y * cellSize, 1, 1);
  ctx.fillStyle = '#202020';
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    if (cells[y * width + x]) ctx.fillRect(x * cellSize + 1, y * cellSize + 1, cellSize - 1, cellSize - 1);
  }
  generationLabel.textContent = 'GEN ' + String(generation).padStart(5, '0');
  panel.dataset.generation = String(generation);
  panel.dataset.population = String(cells.reduce((sum, v) => sum + v, 0));
}

function canRun() {
  return panel.isConnected && !document.hidden && inView && pageRestored && !userPaused;
}

function updateState() {
  clearTimeout(timer); timer = null;
  toggle.textContent = userPaused ? '继续图案' : '暂停图案';
  toggle.setAttribute('aria-label', userPaused ? '继续生命游戏装饰' : '暂停生命游戏装饰');
  toggle.setAttribute('aria-pressed', String(userPaused));
  panel.dataset.running = String(canRun());
  if (canRun()) timer = setTimeout(tick, intervalMs);
}

function tick() {
  timer = null;
  if (!canRun()) return updateState();
  cells = evolve(cells, width, height); generation++; draw();
  timer = setTimeout(tick, intervalMs);
}

function mount() {
  const host = document.querySelector('[data-emerge-life]') || document.querySelector('.page.active .page-heading');
  if (host && panel.parentElement !== host) {
    host.classList.add('emerge-heading'); host.append(panel); inView = true;
  }
  updateState();
}

toggle.addEventListener('click', () => { userPaused = !userPaused; updateState(); });
panel.querySelector('.emerge-life-reset').addEventListener('click', () => { cells = seed(width, height); generation = 0; draw(); updateState(); });
document.addEventListener('visibilitychange', updateState);
window.addEventListener('pagehide', () => { pageRestored = false; updateState(); });
window.addEventListener('pageshow', () => { pageRestored = true; updateState(); });
reducedMotion.addEventListener('change', () => { userPaused = reducedMotion.matches; updateState(); });
new IntersectionObserver(entries => { inView = entries.some(entry => entry.isIntersecting); updateState(); }, { threshold: .05 }).observe(panel);
const pageObserver = new MutationObserver(mount);
document.querySelectorAll('main > section.page').forEach(page => pageObserver.observe(page, { attributes: true, attributeFilter: ['class'] }));
window.addEventListener('hashchange', mount);
draw(); mount();
