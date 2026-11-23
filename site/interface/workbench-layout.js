// Presentation only. Move existing controls; never clone handlers or call APIs.
const byId = id => document.getElementById(id);
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
};
function disclosure(title, className = '') {
  const node = element('details', 'compact-details ' + className);
  node.append(element('summary', '', title));
  return node;
}
function dock(page, label) {
  const node = element('div', 'command-dock');
  node.setAttribute('aria-label', label);
  const actions = element('div', 'command-actions');
  node.append(actions, element('p', 'click-declaration', '点击执行即确认现场条件。'));
  page.append(node);
  return {node, actions};
}
function move(target, nodes) { for (const node of nodes) if (node) target.append(node); }
function wrap(parent, title, nodes, className = '') {
  const actual = nodes.filter(Boolean);
  if (!actual.length) return null;
  const detail = disclosure(title, className);
  move(detail, actual); parent.append(detail); return detail;
}
function keepExistingIdentities(action) {
  const before = new Map([...document.querySelectorAll('[id]')].map(node => [node.id, node]));
  action();
  for (const [id, node] of before) {
    if (byId(id) !== node) throw new Error('Layout changed control identity: ' + id);
  }
}
function compactExplanations() {
  for (const node of document.querySelectorAll('.nav-number, .nav-item small, .eyebrow, .mini-tag, .sidebar-bottom p, .sidebar-bottom .version')) {
    // Remove decoration only; never remove a live ID or an interactive descendant.
    if (!node.id && !node.querySelector('[id],button,input,select,a')) node.remove();
  }
  for (const page of document.querySelectorAll('main > .page')) {
    const hasDataBinding = node => [node,...node.querySelectorAll('*')]
      .some(part => part.getAttributeNames().some(name => name.startsWith('data-')));
    const notes = [...page.querySelectorAll('p.footnote:not([id]),p.panel-copy:not([id]),.note:not([id]),.inline-note > p:not([id])')]
      .filter(node => !node.querySelector('[id],button,input,select,a') && !hasDataBinding(node) && !node.closest('dialog,details'));
    wrap(page, '操作说明', notes, 'explanation-details');
    wrap(page, '条件与诊断', [...page.querySelectorAll(':scope > .page-readiness')], 'readiness-details');
  }
}
function compactConnect() {
  const page = byId('page-connect'); if (!page) return;
  const bar = dock(page, '连接与模式操作');
  const find = action => page.querySelector(`[data-action="${action}"]`);
  move(bar.actions, [find('connect'),find('disconnect'),find('hub_start'),find('mode_inspect')]);
  const options = disclosure('右执行臂维护', 'dock-popover');
  move(options, [find('mode_high_follow'),find('mode_standby'),find('mode_reset'),page.querySelector('[data-open-gripper]')]);
  bar.actions.append(options);
  const controller = byId('controller-mode-panel');
  if (controller) {
    wrap(controller, '状态字段与请求回执', [...controller.children].filter(node =>
      node.matches('.mode-fields,.mode-raw,#mode-source,.mode-enable-section,.mode-diagnostic-grid,.mode-guide,#mode-position-mode,.mode-request-details')));
    wrap(controller,'模式操作说明',[byId('mode-reason'),byId('mode-history-context'),...controller.querySelectorAll(':scope > p')]);
  }
  const usb=page.querySelector('.usb-target-panel');
  if(usb)wrap(usb,'保存位置与身份详情',[usb.querySelector('[data-usb-target]'),usb.querySelector('[data-usb-detail]')]);
  wrap(page, '设备、坐标与校准信息', [...page.querySelectorAll(':scope > .grid')]);
}
function compactMotion() {
  const page = byId('page-motion'); if (!page) return;
  const core = page.querySelector('.motion-layout');
  const controls = page.querySelector('.motion-controls');
  const robot = page.querySelector('.robot-panel');
  const presets = page.querySelector('.motion-presets');
  const bar = dock(page, '关节与夹爪操作');
  move(bar.actions, [byId('target-from-actual'),byId('preview-target'),byId('execute-target'),byId('gripper-target-execute')]);
  const presetActions = [...page.querySelectorAll('[data-motion="apply"]')];
  move(bar.actions, presetActions);
  const options = disclosure('预设选项', 'dock-popover');
  if (presets) {
    for (const id of ['ready-close-gripper','zero-close-gripper']) move(options,[byId(id)?.closest('label')]);
    move(options,[presets.querySelector('label[for="preset-gripper-speed"]'),byId('preset-gripper-speed')?.closest('.inline-field')]);
  }
  bar.actions.append(options);
  if (core && controls && robot) { core.prepend(robot,controls); core.classList.add('compact-motion-core'); }
  const telemetry = page.querySelector('article[aria-label="双执行臂只读实时关节表"]');
  wrap(page,'双臂反馈与预设详情',[telemetry,presets,page.querySelector(':scope > .mode-link-strip')]);
  if (controls) {
    wrap(controls,'目标与驱动详情',[byId('gripper-readout-source'),byId('gripper-target-detail'),
      byId('gripper-health-motion')?.closest('.inline-note'),byId('target-draft-state'),byId('gripper-value')?.closest('.gripper-row')]);
    const motionSpeed = byId('motion-speed')?.closest('.inline-field');
    const speedLabel = controls.querySelector('label[for="motion-speed"]');
    if (motionSpeed) {
      const speed = element('div','compact-speed');
      move(speed,[speedLabel,motionSpeed]);controls.querySelector('.gripper-motion-controls')?.before(speed);
    }
  }
}
function compactDeploy() {
  const page=byId('page-deploy'); if(!page)return;
  const panel=page.querySelector('.precision-panel'); if(!panel)return;
  const bar=dock(page,'模型部署操作');
  move(bar.actions,['precision-load','precision-acceptance','precision-start','precision-stop','precision-release','v2-speed-dock'].map(byId));
  const extras=disclosure('更多操作','dock-popover');
  move(extras,['h50-shadow','h50-start'].map(byId));bar.actions.append(extras);
  const presets=page.querySelector('[aria-label="任务提示词预设"]');
  if(presets)byId('precision-prompt').after(presets);
  wrap(panel,'验收说明',[byId('precision-validation')]);
  const sidePanels=[...page.querySelectorAll(':scope > article')].filter(node=>node!==panel);
  wrap(page,'任务观察与诊断',sidePanels);
  wrap(page,'旧通路与其他功能',[...page.querySelectorAll(':scope > .grid')]);
  wrap(page,'其他运行状态',[...page.querySelectorAll(':scope > p[id],:scope > .inline-note,:scope > .mode-link-strip')]);
}
function compactRecording() {
  const page=byId('page-teleop'); if(!page)return;
  const bar=dock(page,'采集与录制操作');
  move(bar.actions,['record-start','collection-record-stop','collection-next','collection-record-retry'].map(byId));
  const setup=page.querySelector('.capture-open-setup');move(bar.actions,[setup]);
  const workspace=byId('capture-live-workspace');
  if(workspace)page.querySelector('.page-heading')?.after(workspace);
}
function labels() {
  const map={'precision-title':'模型与任务','h50-deployment-title':'FP32运行记录','controller-mode-heading':'右执行臂模式诊断',
    'precision-load':'加载模型','precision-acceptance':'无运动验收','precision-start':'开始运行','precision-stop':'停止运行','precision-release':'释放显存',
    'h50-load':'加载模型','h50-shadow':'影子推理（FP32）','h50-acceptance':'无运动验收','h50-start':'人工试验（FP32，未验收）','h50-stop':'停止运行',
    'preview-target':'查看J计划','execute-target':'执行J目标','gripper-target-execute':'执行G开合','target-from-actual':'读取当前目标',
    'stop-button':'停止新目标'};
  for(const [id,label]of Object.entries(map))if(byId(id))byId(id).textContent=label;
  const headings={connect:'设备连接',teleop:'采集与录制',motion:'关节与夹爪',deploy:'模型部署'};
  for(const [name,title]of Object.entries(headings)){const h=byId('page-'+name)?.querySelector('h1');if(h)h.textContent=title;}
}
function mountCompactWorkbench() {
  if(document.body.dataset.compactLayout==='ready')return;
  keepExistingIdentities(()=>{
    compactExplanations();compactConnect();compactMotion();compactDeploy();compactRecording();labels();
    const job=byId('job-summary')?.closest('section');
    if(job && !job.classList.contains('page'))wrap(document.querySelector('main'),'任务日志与原始回执',[job]);
  });
  document.body.dataset.compactLayout='ready';
}
mountCompactWorkbench();

// Read-only motion traces; isolated from deployment/controller integration.
void import("./motion-trace.js?v=20261024-1").catch(error => console.error("Motion trace unavailable", error));
