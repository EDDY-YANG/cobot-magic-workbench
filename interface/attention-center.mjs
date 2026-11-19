/* Display only. No network, storage, device access, retry or control commands. */
const KINDS = new Set(['error', 'warn', 'running', 'success', 'info']);
const LABELS = Object.freeze({error: '失败', warn: '需注意', running: '进行中', success: '完成', info: '消息'});
const PRIORITY = Object.freeze({error: 5, warn: 4, running: 3, info: 2, success: 1});

export function normalizeAttentionKind(kind) {
  const value = String(kind || 'info').toLowerCase();
  const aliases = {failed: 'error', failure: 'error', warning: 'warn', pending: 'running', working: 'running', completed: 'success'};
  return KINDS.has(value) ? value : (aliases[value] || 'info');
}

export function createAttentionState(maxEvents = 80) {
  const limit = Number.isFinite(Number(maxEvents)) ? Math.floor(Number(maxEvents)) : 80;
  return {events: [], context: {title: '当前状态尚未核对', message: '等待新的只读状态，尚不能判断操作条件。', kind: 'warn'}, expanded: false, nextId: 1, maxEvents: Math.min(200, Math.max(5, limit))};
}

function payloadEqual(a, b) {
  return ['kind', 'title', 'message', 'page', 'actionLabel'].every(key => a[key] === b[key]);
}

export function attentionReducer(state, action) {
  if (action.type === 'publish') {
    const input = action.event || {};
    const key = String(input.key == null ? `message-${state.nextId}` : input.key);
    const previous = state.events.find(event => event.key === key);
    const timestamp = typeof input.at === 'number' ? input.at : Date.parse(input.at || '');
    const event = {
      key, kind: normalizeAttentionKind(input.kind), title: String(input.title || LABELS[normalizeAttentionKind(input.kind)]),
      message: String(input.message || ''), page: typeof input.page === 'string' ? input.page : '',
      actionLabel: String(input.actionLabel || '查看相关页面'), at: Number.isFinite(timestamp) ? timestamp : Number(action.now || Date.now()),
      read: false, id: previous ? previous.id : state.nextId,
    };
    // Polling an unchanged event neither marks it unread again nor adds rows.
    if (previous && payloadEqual(previous, event)) return state;
    const events = [event, ...state.events.filter(item => item.key !== key)];
    while (events.length > state.maxEvents) {
      let disposable = events.length - 1;
      while (disposable >= 0 && events[disposable].kind === 'error' && !events[disposable].read) disposable -= 1;
      // A bounded record cannot retain unlimited distinct failures, but routine
      // success/info traffic must never evict an unread failure.
      events.splice(disposable >= 0 ? disposable : events.length - 1, 1);
    }
    return {...state, events, nextId: previous ? state.nextId : state.nextId + 1};
  }
  if (action.type === 'read') {
    const events = state.events.map(event => action.key == null || String(action.key) === event.key ? {...event, read: true} : event);
    return {...state, events};
  }
  if (action.type === 'context') {
    const input = action.context || {};
    const context = {title: String(input.title || '当前状态'), message: String(input.message || ''), kind: normalizeAttentionKind(input.kind)};
    if (['title', 'message', 'kind'].every(key => state.context[key] === context[key])) return state;
    return {...state, context};
  }
  if (action.type === 'expand') return {...state, expanded: Boolean(action.expanded)};
  return state;
}

export function selectAttention(state) {
  const unread = state.events.filter(event => !event.read);
  const focus = [...unread].sort((a, b) => PRIORITY[b.kind] - PRIORITY[a.kind] || b.id - a.id)[0] || null;
  return {
    unreadCount: unread.length,
    failureCount: state.events.filter(event => event.kind === 'error').length,
    readFailureCount: state.events.filter(event => event.kind === 'error' && event.read).length,
    unreadFailureCount: unread.filter(event => event.kind === 'error').length,
    focus,
  };
}

function element(document, tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content != null) node.textContent = String(content);
  return node;
}

function replaceChildren(node, ...children) {
  if (typeof node.replaceChildren === 'function') node.replaceChildren(...children);
  else {
    while (node.firstChild) node.removeChild(node.firstChild);
    children.forEach(child => node.appendChild(child));
  }
}

function formatTime(at) {
  const date = new Date(at);
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleTimeString('zh-CN', {hour12: false});
}

export function createAttentionCenter({document, openPage, maxEvents = 80} = {}) {
  if (!document || !document.createElement) throw new TypeError('Attention center requires a document');
  const existing = document.getElementById('attention-center');
  if (existing) throw new Error('Attention center already mounted');
  let state = createAttentionState(maxEvents);
  let destroyed = false;
  const stylesheetId = 'attention-center-styles';
  if (!document.getElementById(stylesheetId)) {
    const link = element(document, 'link');
    link.id = stylesheetId;
    link.rel = 'stylesheet';
    link.href = new URL('./attention-center.css', import.meta.url).href;
    (document.head || document.body).appendChild(link);
  }

  const root = element(document, 'section', 'attention-center');
  root.id = 'attention-center';
  root.setAttribute('aria-label', '重点与消息');
  const context = element(document, 'div', 'attention-context');
  const contextLabel = element(document, 'span', 'attention-eyebrow', '当前重点');
  const contextCopy = element(document, 'div', 'attention-context-copy');
  const contextTitle = element(document, 'strong');
  const contextMessage = element(document, 'p');
  contextCopy.appendChild(contextTitle);
  contextCopy.appendChild(contextMessage);
  context.appendChild(contextLabel);
  context.appendChild(contextCopy);
  const banner = element(document, 'div', 'attention-banner');
  banner.id = 'attention-banner';
  banner.setAttribute('role', 'status');
  banner.setAttribute('aria-live', 'polite');
  const readNotice = element(document, 'p', 'attention-read-failure');
  const drawer = element(document, 'section', 'attention-drawer');
  drawer.id = 'attention-message-history';
  drawer.setAttribute('aria-label', '消息记录');
  const drawerHeading = element(document, 'div', 'attention-drawer-heading');
  const heading = element(document, 'h2', '', '消息记录');
  const readAllButton = element(document, 'button', 'button attention-read-all', '全部标为已阅');
  readAllButton.type = 'button';
  const closeButton = element(document, 'button', 'button quiet attention-close', '收起');
  closeButton.type = 'button';
  drawerHeading.appendChild(heading);
  drawerHeading.appendChild(readAllButton);
  drawerHeading.appendChild(closeButton);
  const explanation = element(document, 'p', 'attention-history-note', '“已阅”只表示看过消息，不代表故障解除或任务成功。最近消息仅保存在当前网页内存中。');
  const list = element(document, 'ol', 'attention-message-list');
  const empty = element(document, 'p', 'attention-empty', '暂无操作消息。当前系统条件见上方“当前重点”。');
  drawer.appendChild(drawerHeading);
  drawer.appendChild(explanation);
  drawer.appendChild(list);
  drawer.appendChild(empty);
  root.appendChild(context);
  root.appendChild(banner);
  root.appendChild(readNotice);
  root.appendChild(drawer);
  const topbar = document.querySelector('.topbar');
  const parent = topbar && topbar.parentNode;
  if (parent) parent.insertBefore(root, topbar.nextSibling);
  else (document.querySelector('.workspace') || document.body).appendChild(root);
  const view = document.defaultView;
  function syncTopbarOffset() {
    if (!topbar || !topbar.getBoundingClientRect || !root.style) return;
    const height = Math.ceil(topbar.getBoundingClientRect().height);
    if (Number.isFinite(height) && height > 0) root.style.setProperty('--attention-topbar-height', `${height}px`);
  }
  syncTopbarOffset();
  const observer = view && view.ResizeObserver && topbar ? new view.ResizeObserver(syncTopbarOffset) : null;
  if (observer) observer.observe(topbar);
  else if (view && view.addEventListener) view.addEventListener('resize', syncTopbarOffset);

  const entry = element(document, 'button', 'button attention-open-button', '消息 0');
  entry.type = 'button';
  entry.id = 'attention-message-button';
  entry.setAttribute('aria-controls', drawer.id);
  const toolbar = document.querySelector('.topbar-right');
  // Keep STOP at its existing location; insert the message entry before it.
  const stop = document.getElementById('stop-button');
  if (toolbar) toolbar.insertBefore(entry, stop && stop.parentNode === toolbar ? stop : null);
  else context.appendChild(entry);

  function navigate(page) {
    if (typeof openPage === 'function') openPage(page);
  }
  function button(text, callback, className = 'button') {
    const node = element(document, 'button', className, text);
    node.type = 'button';
    node.addEventListener('click', callback);
    return node;
  }
  function render() {
    if (destroyed) return;
    const selected = selectAttention(state);
    context.dataset.kind = state.context.kind;
    contextTitle.textContent = state.context.title;
    contextMessage.textContent = state.context.message;
    entry.textContent = `消息 ${selected.unreadCount}`;
    entry.dataset.urgent = selected.unreadFailureCount ? 'true' : 'false';
    entry.setAttribute('aria-label', `消息，${selected.unreadCount} 条未读，${selected.failureCount} 条失败记录`);
    entry.setAttribute('aria-expanded', String(state.expanded));
    drawer.hidden = !state.expanded;
    readAllButton.disabled = !selected.unreadCount;
    readNotice.hidden = !selected.readFailureCount;
    readNotice.textContent = `已阅失败记录 ${selected.readFailureCount} 条 · 任务结果仍为失败；恢复情况以新的真实状态为准。`;
    const event = selected.focus;
    banner.hidden = !event;
    replaceChildren(banner);
    if (event) {
      banner.dataset.kind = event.kind;
      const title = element(document, 'strong', 'attention-banner-title', `${LABELS[event.kind]} / ${event.title}`);
      const occurredAt = element(document, 'time', 'attention-banner-time', `原始消息时间 ${formatTime(event.at)}`);
      const date = new Date(event.at);
      if (Number.isFinite(date.getTime())) occurredAt.setAttribute('datetime', date.toISOString());
      const message = element(document, 'p', 'attention-banner-message', event.message);
      const copy = element(document, 'div', 'attention-banner-copy');
      copy.appendChild(title);
      copy.appendChild(occurredAt);
      copy.appendChild(message);
      const actions = element(document, 'div', 'attention-banner-actions');
      if (event.page && typeof openPage === 'function') actions.appendChild(button(event.actionLabel, () => navigate(event.page)));
      actions.appendChild(button('我已看到', () => markRead(event.key), 'button attention-ack'));
      banner.appendChild(copy);
      banner.appendChild(actions);
    }
    replaceChildren(list);
    state.events.forEach(event => {
      const item = element(document, 'li', 'attention-message-item');
      item.dataset.kind = event.kind;
      item.dataset.read = String(event.read);
      const row = element(document, 'div', 'attention-message-row');
      row.appendChild(element(document, 'span', 'attention-kind', LABELS[event.kind]));
      row.appendChild(element(document, 'strong', '', event.title));
      row.appendChild(element(document, 'time', '', formatTime(event.at)));
      item.appendChild(row);
      item.appendChild(element(document, 'p', '', event.message));
      const actions = element(document, 'div', 'attention-message-actions');
      actions.appendChild(element(document, 'span', 'attention-read-state', event.read ? '已阅' : '未读'));
      if (event.page && typeof openPage === 'function') actions.appendChild(button(event.actionLabel, () => navigate(event.page), 'button quiet'));
      if (!event.read) actions.appendChild(button('标为已阅', () => markRead(event.key), 'button quiet'));
      item.appendChild(actions);
      list.appendChild(item);
    });
    empty.hidden = Boolean(state.events.length);
  }
  function dispatch(action) {
    if (destroyed) return;
    const next = attentionReducer(state, action);
    if (next === state) return;
    state = next;
    render();
  }
  function markRead(key) { dispatch({type: 'read', key}); }
  function setExpanded(expanded) { dispatch({type: 'expand', expanded}); }
  function toggle() { setExpanded(!state.expanded); }
  entry.addEventListener('click', toggle);
  readAllButton.addEventListener('click', () => markRead());
  closeButton.addEventListener('click', () => setExpanded(false));
  render();
  return {
    publish(event) { dispatch({type: 'publish', event}); },
    updateContext(context) { dispatch({type: 'context', context}); },
    markRead,
    markAllRead() { markRead(); },
    open() { setExpanded(true); }, close() { setExpanded(false); }, toggle,
    snapshot() {
      const selected = selectAttention(state);
      return {events: state.events.map(event => ({...event})), context: {...state.context}, expanded: state.expanded, ...selected, focus: selected.focus ? {...selected.focus} : null};
    },
    destroy() {
      destroyed = true;
      if (observer) observer.disconnect();
      else if (view && view.removeEventListener) view.removeEventListener('resize', syncTopbarOffset);
      root.remove(); entry.remove();
    },
  };
}
