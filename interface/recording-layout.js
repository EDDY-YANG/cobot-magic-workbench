// Presentation only: move existing nodes, never clone controls or call device APIs.
const page = document.getElementById('page-teleop');

function mountRecordingLayout() {
  if (!page || page.dataset.recordingLayout === 'ready') return;
  const cameras = page.querySelector('.camera-grid');
  const recording = page.querySelector('.recording-panel');
  const jointPanel = page.querySelector('#joint-chart')?.closest('article');
  const historyPanel = page.querySelector('#collection-episode-list')?.closest('article');
  const usbPanel = page.querySelector('.usb-target-panel');
  const required = ['record-start','collection-record-stop','collection-next','collection-record-retry',
    'recording-timer','recording-state','episode-class','collection-notes','collection-recording-detail'];
  if (!cameras || !recording || required.some(id => !page.querySelector('#'+id))) {
    console.warn('Recording layout not mounted: required existing controls missing.');
    return;
  }
  const identity = new Map([...page.querySelectorAll('[id]')].map(node => [node.id, node]));
  const element = (tag, className, text) => {
    const node = document.createElement(tag); node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  const disclosure = (title, className = '', open = false) => {
    const details = element('details', 'capture-disclosure '+className);
    details.open = open; details.append(element('summary','',title));
    return details;
  };
  const workspace = element('div','capture-live-workspace');
  workspace.id = 'capture-live-workspace';
  workspace.setAttribute('aria-label','相机与本条录制操作');
  cameras.before(workspace);
  const rail = element('section','capture-camera-rail');
  rail.setAttribute('aria-label','真实相机预览');
  const railTitle = element('div','capture-rail-title');
  railTitle.append(element('strong','','观察画面'),element('span','','前方 + 右腕 · 原有预览频率'));
  rail.append(railTitle,cameras); workspace.append(rail,recording);
  for (const card of cameras.querySelectorAll('.camera-panel')) {
    const role = card.querySelector('[data-camera]')?.dataset.camera;
    card.dataset.cameraRole = role || 'unknown';
    if (role === 'left') {
      const extra = disclosure('左腕视角 · 展开查看','capture-extra-camera');
      extra.append(card); rail.append(extra);
    }
  }
  const progress = recording.querySelector('.collection-progress');
  if (progress) rail.append(progress);

  const title = recording.querySelector('.panel-title');
  const deck = element('div','capture-action-deck');
  const timerRow = element('div','capture-timer-row');
  timerRow.append(element('span','','本条录制时长'),document.getElementById('recording-timer'));
  const fields = element('div','capture-trial-fields');
  for (const id of ['episode-class','collection-notes']) {
    const field = element('div','capture-trial-field');
    const label = recording.querySelector(`label[for="${id}"]`);
    if (label) field.append(label);
    field.append(document.getElementById(id)); fields.append(field);
  }
  const actions = element('div','capture-primary-actions');
  actions.setAttribute('aria-label','本条录制操作');
  for (const id of ['record-start','collection-record-stop','collection-next','collection-record-retry']) {
    actions.append(document.getElementById(id));
  }
  const critical = element('dl','collection-live-stats capture-critical-stats');
  for (const id of ['collection-usb-free','collection-quality','collection-recording-error']) {
    const row = recording.querySelector('#'+id)?.parentElement;
    if (row) critical.append(row);
  }
  deck.append(title,timerRow,fields,actions,document.getElementById('collection-recording-detail'),critical);
  const stats = recording.querySelector('.collection-live-stats');
  const body = element('div','capture-settings-scroll');
  const setup = element('div','capture-session-settings');
  // Every remaining existing child stays accessible, including recovery notes.
  for (const child of [...recording.childNodes]) if (child !== stats) setup.append(child);
  const setupDialog = element('dialog','capture-setup-dialog');
  setupDialog.setAttribute('aria-label','准备本次采集与批次设置');
  const setupHeader = element('div','capture-setup-header');
  const closeSetup = element('button','button quiet','返回录制台'); closeSetup.type='button';
  closeSetup.addEventListener('click',()=>setupDialog.close());
  setupHeader.append(element('h2','','准备本次采集'),closeSetup);
  setupDialog.append(setupHeader,setup);
  const openSetup = element('button','button capture-open-setup','准备本次采集 / 批次设置'); openSetup.type='button';
  openSetup.addEventListener('click',()=>{if(!setupDialog.open)setupDialog.showModal();});
  setupDialog.addEventListener('close',()=>requestAnimationFrame(()=>{
    updateGeometry();
    workspace.scrollIntoView({block:'start',behavior:'auto'});
    openSetup.focus({preventScroll:true});
  }));
  body.append(openSetup);
  recording.append(deck,body);
  recording.append(setupDialog);
  recording.classList.add('capture-recording-console');

  const secondary = element('div','capture-secondary');
  workspace.after(secondary);
  if (stats) { const d=disclosure('实时计数、队列与保存详情','capture-extra-stats'); d.append(stats); secondary.append(d); }
  if (jointPanel) { const d=disclosure('关节时间线与监控设置','capture-joint-details'); d.append(jointPanel); secondary.append(d); }
  if (usbPanel) { const d=disclosure('U 盘位置、容量与目标详情','capture-usb-details'); d.append(usbPanel); secondary.append(d); }
  if (historyPanel) { const d=disclosure('批次记录与人工复核','capture-history-details'); d.append(historyPanel); secondary.append(d); }
  const oldGrid = page.querySelector(':scope > .grid.cols-3');
  if (oldGrid && oldGrid.children.length === 0) oldGrid.remove();
  const moveHistoricalStage = () => {
    const stage = page.querySelector(':scope > .semantic-workspace');
    if (stage) { const d=disclosure('任务阶段与历史回放','capture-stage-details'); d.append(stage); secondary.append(d); }
  };
  moveHistoricalStage();
  new MutationObserver(moveHistoricalStage).observe(page,{childList:true});

  const focus = element('button','button capture-focus-button','录制台');
  focus.type = 'button';
  focus.setAttribute('aria-label','定位画面与录制');
  focus.title = '收起消息历史，回到相机与录制按钮';
  function focusWorkspace() {
    if (!page.classList.contains('active')) return;
    const historyClose = document.querySelector('.attention-close');
    if (historyClose?.getClientRects().length) historyClose.click();
    requestAnimationFrame(() => { updateGeometry(); workspace.scrollIntoView({block:'start',behavior:'auto'}); });
  }
  focus.addEventListener('click',focusWorkspace);
  const toolbar = document.querySelector('.topbar-right');
  if (toolbar) toolbar.insertBefore(focus,document.getElementById('stop-button'));
  else page.querySelector('.page-heading').append(focus);
  const updateFocusVisibility = () => {focus.hidden=!page.classList.contains('active');};
  new MutationObserver(updateFocusVisibility).observe(page,{attributes:true,attributeFilter:['class']});
  updateFocusVisibility();
  document.querySelector('.nav-item[data-page="teleop"]')?.addEventListener('click',()=>requestAnimationFrame(focusWorkspace));
  page.dataset.recordingLayout = 'ready';
  page.classList.add('capture-layout-ready');
  for (const [id,node] of identity) {
    if (document.getElementById(id) !== node) throw new Error('Recording layout lost an original node: '+id);
  }

  let frame = null;
  const updateGeometry = () => {
    frame = null;
    const topbar = document.querySelector('.topbar');
    const attention = document.querySelector('.attention-center');
    const top = Math.ceil((topbar?.getBoundingClientRect().height || 73) +
      (attention?.getBoundingClientRect().height || 0) + 10);
    page.style.setProperty('--capture-sticky-top',top+'px');
    page.dataset.tightCaptureViewport = String(innerHeight-top < 330);
  };
  const queueGeometry = () => { if (frame === null) frame=requestAnimationFrame(updateGeometry); };
  const observer = new ResizeObserver(queueGeometry);
  const watched = new WeakSet();
  const historyControls = new WeakSet();
  const restoreCapturePosition = () => {
    if (!page.classList.contains('active')) return;
    requestAnimationFrame(()=>{updateGeometry();workspace.scrollIntoView({block:'start',behavior:'auto'});});
  };
  const observeChrome = () => {
    for (const node of document.querySelectorAll('.topbar,.attention-center')) {
      if (!watched.has(node)) { watched.add(node); observer.observe(node); }
    }
    const closeHistory = document.querySelector('.attention-close');
    if (closeHistory && !historyControls.has(closeHistory)) {
      historyControls.add(closeHistory); closeHistory.addEventListener('click',restoreCapturePosition);
    }
    const messageToggle = document.getElementById('attention-message-button');
    if (messageToggle && !historyControls.has(messageToggle)) {
      historyControls.add(messageToggle);
      messageToggle.addEventListener('click',()=>{
        if (messageToggle.getAttribute('aria-expanded')==='true') restoreCapturePosition();
      },true);
    }
    queueGeometry();
  };
  observeChrome();
  // The notification owner loads its UI independently; observe additions only.
  const workspaceRoot = document.querySelector('.workspace');
  if (workspaceRoot) new MutationObserver(observeChrome).observe(workspaceRoot,{childList:true});
  window.addEventListener('resize',queueGeometry,{passive:true});
  window.addEventListener('hashchange',queueGeometry);
  window.addEventListener('pagehide',()=>{if(frame!==null)cancelAnimationFrame(frame);frame=null;});
}

mountRecordingLayout();
