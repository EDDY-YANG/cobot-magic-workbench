// Operator declarations: explicit-button-click, authorized by Tiger 2026-10-23.
/* 通过项目工作台 HTTP 服务加载；不直接运行本文件。
 * 写入、录制和控制请求须来自明确按钮点击；恢复采集身份只发送只读回执核验。
 * 拖动与数字编辑只更新目标预览。
 * 本页不构造模拟实时反馈，不自动连接 SSH、打开相机或开始机械臂运动。
 */
import {createV2SpeedControl} from "./manual-speed-control.mjs?v=20261025-speed1";
let v2SpeedControl = null;
const $ = (id) => document.getElementById(id);
const pageNames = {
  connect: "连接与零点校准",
  teleop: "遥操作与采集",
  motion: "运动测试",
  deploy: "模型部署",
};
const actionNames = {
  precision_load:"加载精度与检查点",precision_acceptance:"无运动验收",precision_deploy:"手动部署一轮",precision_release:"释放模型显存",
  v2_acceptance:"V2 无运动验收（30秒）",v2_deploy:"V2 真机运行",
  connect: "连接 AgileX",
  disconnect: "断开页面通道",
  status: "读取真实状态",
  diagnose: "诊断 CAN / ROS",
  mode_inspect: "刷新模式诊断",
  mode_takeover: "切换到电脑 CAN / MOVEJ",
  mode_reset: "受支撑单次复位",
  mode_high_follow: "恢复原厂主从高跟随",
  mode_standby: "切到待机",
  mode_disable: "六关节失能",
  gripper_inspect: "夹爪只读核验",
  gripper_measure: "登记夹爪实测",
  gripper_disable: "仅夹爪失能",
  gripper_zero: "设置夹爪当前位置零点",
  limits: "读取原厂限位",
  hub_start: "启动共享监控",
  hub_stop: "停止共享监控",
  record_start: "开始分段录制",
  record_retry: "重试／核对本条开录",
  record_stop: "结束并保存",
  record_list: "读取录制列表",
  collection_create: "准备本次采集",
  collection_list: "读取批次列表",
  collection_review: "保存示范人工复核",
  teleop_enter: "进入遥操作",
  teleop_exit: "退出遥操作",
  move_plan: "生成运动计划",
  move_ready: "移动到原准备位",
  move_zero: "移动到六关节零位",
  move_target: "执行预览目标",
  move_gripper_plan: "查看G夹爪目标计划",
  move_gripper: "G夹爪开合",
  model_plan: "核对模型计划",
  model_stage: "准备原始权重",
  model_start: "加载模型服务",
  model_status: "读取模型状态",
  model_stop: "停止模型服务",
  shadow: "影子推理",
  shadow_fp32: "4090 FP32/H50 影子联调（不发动作）",
  h50_load: "加载所选4090 H50模型",
  h50_shadow: "所选4090 H50影子检查",
  h50_acceptance: "所选RTC无运动性能验收",
  h50_deploy: "4090 H50人工实验 · 真实运动",
  deploy_plan: "生成部署计划",
  deploy: "限时实机部署",
  task_review_success: "人工复核本次成功",
  task_review_failure: "人工复核本次失败",
  clear_stop: "清除停止锁存",
  sync: "同步当前源码",
};
const motionActions = new Set([
  "move_gripper",
  "move_ready",
  "move_zero",
  "move_target",
  "deploy",
  "h50_deploy",
  "precision_deploy",
  "v2_deploy",
]);
const knownUnavailable = new Set(["teleop_enter", "teleop_exit"]);
const pageActions = {
  connect: ["connect", "diagnose", "mode_inspect", "mode_takeover", "mode_reset", "mode_high_follow", "mode_standby", "mode_disable", "limits", "gripper_inspect", "gripper_measure", "gripper_disable", "gripper_zero", "hub_start", "sync"],
  teleop: ["hub_start", "collection_create", "collection_list", "collection_review", "record_start", "record_retry", "record_stop", "record_list"],
  motion: ["move_plan", "move_ready", "move_zero", "move_target", "move_gripper_plan", "move_gripper"],
  deploy: ["precision_load","precision_acceptance","precision_deploy","precision_release","v2_acceptance","v2_deploy","h50_load", "h50_shadow", "h50_acceptance", "h50_deploy", "model_plan", "model_stage", "model_start", "shadow", "shadow_fp32", "deploy_plan", "deploy"],
};
const jointHistory = [];
const events = [];
const seenEvents = new Set();
const readinessKeys = {};
const jobPages = new Map();
const pageActionErrors = new Map();
let attentionCenter = null, attentionSequence = 0;
let currentPage = "connect",
  lastState = {},
  backendOnline = false,
  robotView = null,
  pendingJob = null,
  lastSampleTime = null;
let target = [0, 0, 0, 0, 0, 0],
  actual = null,
  lastCameraStamp = {},
  lastCameraRequest = {},
  cameraRequestBusy = {},
  cameraAvailable = {},
  latestJob = null,
  toastTimer = null,
  submitting = false,
  targetInitialized = false,
  targetEdited = false,
  targetFromFreshActual = false,
  actualFresh = false,
  samplerDraftEdited = false;
let gripperTargetInitialized = false;
let taskDisplayReceivedAt = null, taskDisplayHub = {};
let collectionWorkbench = null;
const isNumber = (x) => typeof x === "number" && Number.isFinite(x);
const number = (v, n = 2) => (isNumber(v) ? v.toFixed(n) : "—");
const object = (v) =>
  v && typeof v === "object" && !Array.isArray(v) ? v : {};
const validQ = (q) => Array.isArray(q) && q.length === 6 && q.every(isNumber);
const text = (id, value) => {
  if ($(id)) $(id).textContent = value ?? "未知";
};
const readResult = (v) => object(object(v).result || object(v).data || v);
const unwrap = (v) => readResult(object(v).reported || v);
const showToast = (message, notify = true) => {
  if (notify) addEvent(message);
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => ($("toast").hidden = true), 6500);
};
function actionPage(action, fallback = currentPage) {
  if (action?.startsWith("model_") || action?.startsWith("h50_") || action?.startsWith("task_review_") || ["shadow", "shadow_fp32", "deploy", "deploy_plan"].includes(action)) return "deploy";
  return Object.keys(pageActions).find((p) => pageActions[p].includes(action)) || fallback;
}
// ACTION_ERROR_HISTORY_BEGIN: presentation only; never changes execution gates.
function actionErrorView(error, currentCapability, online) {
  const name = actionNames[error.action] || error.action || "相关操作";
  const at = error.at ? timeLabel(error.at) : "时间未提供";
  const available = online && currentCapability?.available === true;
  const kind = error.source === "job" ? "任务失败" : error.source === "precondition" ? "提交被拒绝" : "请求错误";
  let current = online ? "当前提交条件另见上方实时状态。" : "当前条件尚未核对。";
  if (error.source === "precondition" && available)
    current = "当前提交条件已恢复，尚未重新执行。";
  else if (error.source === "job")
    current += " 反馈恢复不代表该任务成功；失败原因保留。";
  else if (available)
    current = "当前提交条件已满足；上次请求仍没有成功回执，不能据此确认执行结果。";
  return { text: "历史记录 · 上次" + name + " · " + at + " · " + kind + "\n" + error.message + "\n" + current,
    historical: available && error.source !== "job" };
}
function renderActionErrors() {
  for (const [page, error] of pageActionErrors) {
    const el = $(page + "-action-error");
    if (!el) continue;
    const view = actionErrorView(error, error.action ? capability(error.action) : null, backendOnline);
    el.textContent = view.text;
    el.hidden = false;
    el.classList.toggle("history", view.historical);
  }
}
function rememberActionError(message, page, action, metadata = {}) {
  page = actionPage(action, page);
  pageActionErrors.set(page, { action, jobId: metadata.jobId || null,
    message: String(message), at: Object.hasOwn(metadata, "at") ? metadata.at : new Date().toISOString(),
    source: metadata.source || "request" });
  renderActionErrors();
}
// ACTION_ERROR_HISTORY_END
function showActionError(message, page = currentPage, action = null, metadata = {}) {
  rememberActionError(message, page, action, metadata);
  addEvent(message, "error", metadata.at, null, { page: actionPage(action, page) });
  showToast(message, false);
}
function timeLabel(value) {
  let t = value
    ? new Date(
        typeof value === "number"
          ? value < 1e12
            ? value * 1000
            : value
          : value,
      )
    : new Date();
  return Number.isNaN(t.valueOf())
    ? "—"
    : t.toLocaleTimeString("zh-CN", { hour12: false });
}
function setPill(id, label, kind = "neutral") {
  let e = $(id);
  if (e) {
    e.textContent = label;
    e.className = "pill " + kind;
  }
}
function addEvent(message, kind = "", stamp = null, key = null, metadata = {}) {
  if (key && seenEvents.has(key)) return;
  if (key) seenEvents.add(key);
  const entry = { message: String(message), kind, stamp: timeLabel(stamp),
    at: stamp || new Date().toISOString(), key: key || "local-message-" + (++attentionSequence), ...metadata };
  events.push(entry);
  if (events.length > 60) events.shift();
  renderEvents();
  if (typeof attentionCenter !== "undefined") attentionCenter?.publish({ ...entry,
    kind: kind || "info", title: kind === "error" ? "需要查看的错误消息" : "操作消息" });
}
function renderEvents() {
  const list = $("event-log");
  list.replaceChildren();
  for (const e of events.slice(-12).reverse()) {
    const row = document.createElement("div");
    row.className = "event-row " + e.kind;
    const stamp = document.createElement("time");
    stamp.textContent = e.stamp;
    const dot = document.createElement("span");
    dot.className = "event-dot";
    const value = document.createElement("span");
    value.textContent = e.message;
    row.append(stamp, dot, value);
    list.append(row);
  }
}
function openPage(page) {
  if (!pageNames[page]) page = "connect";
  currentPage = page;
  v2SpeedControl?.sync();
  for (const p of document.querySelectorAll(".page"))
    p.classList.toggle("active", p.id === "page-" + page);
  for (const n of document.querySelectorAll(".nav-item"))
    n.classList.toggle("active", n.dataset.page === page);
  text("page-breadcrumb", pageNames[page]);
  window.history.replaceState(null, "", "#" + page);
  requestAnimationFrame(() => {
    window.scrollTo({ top: 0, behavior: "auto" });
    robotView?.resize();
    drawChart();
  });
}
for (const el of document.querySelectorAll("[data-page],[data-open-page]"))
  el.addEventListener("click", () => {
    openPage(el.dataset.page || el.dataset.openPage);
    if (el.hasAttribute("data-open-mode"))
      requestAnimationFrame(() => $("controller-mode-panel").scrollIntoView({ block: "start", behavior: "smooth" }));
  });
window.addEventListener("hashchange", () => openPage(location.hash.slice(1)));
openPage(location.hash.slice(1));
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if ((options.method || "GET").toUpperCase() === "POST") {
    if (!lastState.csrf_token)
      throw new Error("页面尚未取得本地会话令牌，请等待后端状态刷新。");
    headers.set("X-Tiger-Token", lastState.csrf_token);
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(path, {
      cache: "no-store",
      ...options,
      headers,
      signal: options.signal || controller.signal,
    });
    let data;
    try {
      data = await response.json();
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new Error("后端未返回可读 JSON（HTTP " + response.status + "）");
    }
    if (!response.ok) {
      const error = new Error(
        data.error || data.message || "请求失败（HTTP " + response.status + "）",
      );
      if ([403, 404, 409].includes(response.status)) error.requestRejected = true;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError")
      throw new Error(
        (options.method || "GET").toUpperCase() === "POST"
          ? "任务提交等待超过 10 秒，结果尚未确认。请先查看任务回执再重试。"
          : "后端状态请求超过 10 秒，请检查服务连接。",
      );
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

// Metadata only. Selecting a disk never starts capture, installs a runtime or changes robot mode.
// USB_STORAGE_VIEW_BEGIN
let usbStorageBusy = false, usbStorageFeedback = "", usbStorageSnapshot = {}, usbStorageRenderKey = "";
function usbDisplayName(disk) {
  return [disk.label, disk.name].filter(value => typeof value === "string" && value.trim()).join(" · ")
    || disk.device || "未命名 USB";
}
function usbCapacityLabel(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? (value / 1073741824).toFixed(2) + " GiB" : "未核对";
}
function renderUsbStorage(value, online = backendOnline) {
  const storage = object(value);
  usbStorageSnapshot = storage;
  const candidates = Array.isArray(storage.candidates) ? storage.candidates : [];
  const selected = object(storage.selected);
  const chosen = typeof selected.uuid === "string" && Boolean(selected.uuid);
  const status = storage.status || "not_scanned";
  const labels = { not_scanned: "尚未扫描 AgileX 上的 USB", scanning: "正在检查 AgileX 的 USB 挂载",
    no_usb: "未发现可录制 USB · 暂不能录制", select_required: "发现多颗 USB · 请选择本轮录制盘",
    selected: "已选定本轮 USB", lost: "选定 USB 已离线 · 写入被阻止，不自动切盘",
    error: "USB 检查失败 · 暂不能录制" };
  const active = storage.selection_locked === true;
  const stateLabel = !online ? "本地后端未就绪 · USB 当前状态未核对"
    : (labels[status] || "USB 状态尚未核对") + (active ? " · 当前会话锁定" : "");
  // Age changes every poll; do not rebuild candidate buttons and steal focus for that alone.
  const renderKey = JSON.stringify([status, candidates, selected, active, storage.reason, storage.error,
    storage.checked_at, storage.runtime_ready, storage.record_root, online, usbStorageBusy, usbStorageFeedback]);
  if (Object.keys(storage).length) {
    text("usb-status", !online ? "USB 当前状态未核对" : chosen && status === "selected" ? usbDisplayName(selected) : labels[status] || "尚未核对");
    text("device-usb", !online ? "保留历史 USB · 当前未核对" : chosen ? usbDisplayName(selected) + " / " + selected.uuid : labels[status] || "尚未核对");
    text("device-usb-detail", !online ? stateLabel : chosen ? selected.mountpoint + (storage.runtime_ready ? " · 运行检查通过" : " · 运行包待核验") : stateLabel);
  }
  if (renderKey === usbStorageRenderKey) return;
  usbStorageRenderKey = renderKey;
  for (const panel of document.querySelectorAll("[data-usb-panel]")) {
    panel.querySelector("[data-usb-state]").textContent = stateLabel;
    const grid = panel.querySelector("[data-usb-candidates]");
    grid.replaceChildren();
    for (const disk of candidates) {
      const card = document.createElement("section"); card.className = "usb-device";
      const isSelected = chosen && disk.uuid === selected.uuid && disk.mountpoint === selected.mountpoint;
      card.dataset.selected = String(isSelected);
      const line = (tag, text) => { const node = document.createElement(tag); node.textContent = text; card.append(node); };
      line("strong", (isSelected ? "■ 本轮目标 · " : "□ ") + usbDisplayName(disk));
      line("small", "UUID：" + (disk.uuid || "缺失，不能选定"));
      line("small", "挂载：" + (disk.mountpoint || "未挂载") + " · " + (disk.filesystem || "文件系统未知"));
      line("small", "可用 " + usbCapacityLabel(disk.free_bytes) + " / 总计 " + usbCapacityLabel(disk.capacity_bytes));
      line("small", disk.eligible === true ? "挂载可写候选 · 录制仍须通过运行检查" : "不可选：" + (disk.reason || "尚未通过真实挂载检查"));
      if (!isSelected) {
        const button = document.createElement("button"); button.className = "button";
        button.textContent = active ? "当前会话锁定" : "选为本轮录制盘";
        button.disabled = !online || usbStorageBusy || active || disk.eligible !== true || status === "scanning";
        button.addEventListener("click", () => void submitUsbStorage("select", { uuid: disk.uuid, mountpoint: disk.mountpoint }));
        card.append(button);
      }
      grid.append(card);
    }
    const target = panel.querySelector("[data-usb-target]");
    target.textContent = chosen ? "录制目标：" + usbDisplayName(selected) + " · " + (selected.mountpoint || "挂载未核对")
      + (storage.record_root ? " · " + storage.record_root : " · 具体录制目录待运行包核验")
      : "录制目标：未选定，暂不能录制。";
    if (chosen && storage.runtime_ready !== true) target.textContent += " · 盘已选择，运行包尚未就绪。";
    if (chosen && status === "lost") target.textContent += " · 上述为原目标，当前不可写。";
    const detail = panel.querySelector("[data-usb-detail]");
    detail.textContent = [storage.reason, active ? "采集或共享运行期间禁止换盘。" : "一颗合格盘自动选定；多颗时手动选择。",
      storage.checked_at ? "挂载检查时间：" + storage.checked_at : "尚无挂载检查回执。",
      "仅检查 AgileX 本机；不读取 4090 上的盘，不转存内部硬盘。"].filter(Boolean).join(" ");
    const error = panel.querySelector("[data-usb-error]");
    error.textContent = usbStorageFeedback || storage.error || ""; error.hidden = !error.textContent;
    const scan = panel.querySelector("[data-usb-scan]");
    scan.disabled = !online || usbStorageBusy || status === "scanning";
    scan.textContent = usbStorageBusy || status === "scanning" ? "正在检查 USB…" : "↻ 检查已插 U 盘";
  }
}
async function submitUsbStorage(operation, params = {}) {
  if (usbStorageBusy || !backendOnline) return;
  if (operation === "select") {
    const candidate = (usbStorageSnapshot.candidates || []).find(item => item.uuid === params.uuid && item.mountpoint === params.mountpoint);
    if (usbStorageSnapshot.selection_locked === true || candidate?.eligible !== true) return;
  }
  usbStorageBusy = true; usbStorageFeedback = ""; renderUsbStorage(usbStorageSnapshot);
  try {
    const result = await api("/api/storage/" + operation, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(params),
    });
    if (result.usb_storage) { lastState.usb_storage = result.usb_storage; usbStorageSnapshot = result.usb_storage; }
    else if (typeof result.status === "string" && Array.isArray(result.candidates)) {
      lastState.usb_storage = result; usbStorageSnapshot = result;
    }
  } catch (error) {
    usbStorageFeedback = error.message;
    if (typeof addEvent === "function") addEvent("U 盘" + (operation === "select" ? "选择" : "检查") + "失败：" + error.message,
      "error", null, null, {page: "connect"});
  }
  finally { usbStorageBusy = false; renderUsbStorage(usbStorageSnapshot); void pollState(); }
}
// USB_STORAGE_VIEW_END
function modeConfirmationFields(action) {
  if (!["mode_takeover", "mode_reset", "mode_high_follow", "mode_standby", "mode_disable"].includes(action)) return [];
  if (action === "mode_high_follow") return [
    ["supported", "mode-supported", "可靠承托"],
    ["roles_configured", "mode-roles-configured", "原厂主从角色"],
    ["poses_aligned", "mode-poses-aligned", "主从姿态对应"],
    ["source_reviewed", "mode-source-reviewed", "主臂目标来源"],
    ["teaching_ended", "mode-teaching-ended", "示教已结束"],
  ];
  const fields = [
    ["leader_isolated", "mode-leader-isolated", "控制来源隔离"],
    ["supported", "mode-supported", "可靠承托"],
  ];
  if (action === "mode_reset") fields.push(["zero_pose_reviewed", "mode-zero-reviewed", "机械零位流程"]);
  else fields.push(["teaching_ended", "mode-teaching-ended", "示教已结束"]);
  return fields;
}
function withModeConfirmations(action, cap) {
  // Tiger's explicit execution click is the human declaration, not a sensor result.
  // Backend live mode, ownership and feedback gates remain authoritative.
  return cap;
}
function gripperConfirmationFields(action) {
  if (!["gripper_disable", "gripper_zero"].includes(action)) return [];
  const fields = [
    ["leader_isolated", "gripper-leader-isolated", "夹爪指令来源隔离"],
    ["empty_gripper", "gripper-empty", "夹爪空载及失能条件"],
  ];
  if (action === "gripper_zero") fields.push(
    ["physically_closed", "gripper-physically-closed", "物理闭合基准"],
    ["coordinate_change_reviewed", "gripper-coordinate-reviewed", "坐标变化审阅"],
  );
  return fields;
}
function gripperDraft() {
  const value = $("gripper-measured-gap").value.trim();
  return { measured_gap_mm: value === "" ? NaN : Number(value),
    label: $("gripper-measure-label").value,
    ...Object.fromEntries(gripperConfirmationFields("gripper_zero")
      .map(([key]) => [key, true])) };
}
function gripperActionParams(action, draft) {
  const result = { arm: "right" };
  if (action === "gripper_inspect") return { ...result, seconds: 2 };
  if (!["gripper_measure", "gripper_disable", "gripper_zero"].includes(action))
    throw new Error("未知夹爪核验操作。");
  if (["gripper_measure", "gripper_zero"].includes(action)) {
    if (!isNumber(draft.measured_gap_mm) || draft.measured_gap_mm < 0 || draft.measured_gap_mm > 120)
      throw new Error("请填写 0–120 mm 范围内的真实卡尺间距；不能用反馈坐标代替物理测量。");
    result.measured_gap_mm = draft.measured_gap_mm;
  }
  if (action === "gripper_measure") {
    if (!["closed", "mid", "open"].includes(draft.label)) throw new Error("请选择闭合、中间或全开测量姿态。");
    result.label = draft.label;
  }
  if (action === "gripper_zero" && (draft.measured_gap_mm > 0.2 || draft.label !== "closed"))
    throw new Error("夹爪设零须选择闭合测量，卡尺实测间距 ≤0.2 mm；这是物理基准验收，不改变命令限位。");
  for (const [key, , label] of gripperConfirmationFields(action)) {
    if (draft[key] !== true) throw new Error("现场声明待完成：" + label + "。");
    result[key] = true;
  }
  return result;
}
function withGripperConfirmations(action, cap) {
  if (!cap.available || !action.startsWith("gripper_")) return cap;
  try { gripperActionParams(action, gripperDraft()); return cap; }
  catch (error) { return { available: false, reason: error.message }; }
}
function withActionConfirmations(action, cap) {
  if (action.startsWith("precision_") || ["v2_acceptance","v2_deploy"].includes(action)) return precisionDraftCapability(action,cap,precisionDraft(),lastState.precision_workbench || {},lastState.h50_deployment || {});
  if (["move_gripper_plan", "move_gripper"].includes(action) && cap.available) {
    try {
      manualGripperParams(manualGripperDraft(), object(lastState.config));
    } catch (error) { return {available:false,reason:error.message}; }
  }
  if (action.startsWith("h50_") && cap.available) {
    try {
      h50ActionParams(action, h50Draft());
      const step = precisionDraft().step;
      const candidate = (lastState.h50_deployment?.candidates || []).find(item => item.step === step);
      if (candidate?.available !== true) return {available: false, reason: candidate?.error || "所选checkpoint尚未核实可用"};
      if (action === "h50_deploy" && lastState.h50_deployment?.selected_step !== step)
        return {available: false, reason: "请先加载所选checkpoint，核对模型就绪后再启动人工实验。"};
    } catch (error) { return {available: false, reason: error.message}; }
  }
  return withPresetGripperCapability(action,
    withGripperConfirmations(action, withModeConfirmations(action, cap)));
}
function presetGripperOptions(pose, close, speedMmS) {
  if (!["ready", "zero"].includes(pose)) return {};
  if (typeof close !== "boolean") throw new Error("请选择是否同时闭合空爪。");
  if (!close) return { close_gripper: false };
  if (!isNumber(speedMmS) || speedMmS <= 0 || speedMmS > 10)
    throw new Error("夹爪软件参考速度须 >0 且 ≤10 mm/s；不能与六关节的 °/s 混用。");
  return { close_gripper: true, gripper_speed_mm_s: speedMmS };
}
function presetGripperDraft(pose) {
  const input = $("preset-gripper-speed").value;
  return presetGripperOptions(pose, $(pose + "-close-gripper").value === "yes",
    typeof input === "string" && input.trim() !== "" ? Number(input) : NaN);
}
function withPresetGripperCapability(action, cap) {
  if (!cap.available || !["move_ready", "move_zero"].includes(action)) return cap;
  try {
    const options = presetGripperDraft(action.slice(5));
    if (!options.close_gripper) return cap;
    const close = lastState.capabilities?.gripper_close;
    if (close && typeof close === "object" && close.available === true) return cap;
    return { available: false, reason: close?.reason || "同时闭爪所需的真实夹爪条件未核对；请同步新版本并读取新鲜反馈。" };
  } catch (error) { return { available: false, reason: error.message }; }
}
function presetMotionTitle(pose, params, plan = false) {
  return (plan ? "查看" : "") + (pose === "ready" ? "准备位" : "六零位") +
    (params.close_gripper ? " + 闭爪" : "") + (plan ? "计划" : "");
}
function presetMotionSummary(pose, params, config) {
  const poseLabel = pose === "ready" ? "原准备位" : "六关节数值零位";
  const targetMm = isNumber(config.ready_gripper_m) && config.ready_gripper_m >= 0
    ? number(config.ready_gripper_m * 1000, 2) + " mm" : "未核对";
  const prefix = pose === "ready" ? "准备阶段：" : "";
  return prefix + (params.close_gripper
    ? poseLabel + "同时闭合空爪到 " + targetMm + "；夹爪软件参考速度上限 " + number(params.gripper_speed_mm_s, 1) + " mm/s，实际速度未验收。六关节速度以 °/s 独立设置；分别用真实反馈确认到位。"
    : poseLabel + "只发送六关节目标；不会发送夹爪开闭目标。") + "这些预设选项不改变模型运行中的夹爪动作。";
}
function renderGripperConfirmations() {
  const cap = capability("gripper_zero");
  text("gripper-confirmation-state", cap.available
    ? "实测值有效；点击设零即确认物理基准，后台仍核对驱动和反馈。"
    : cap.reason || "夹爪设零条件尚未核对。");
}
function capability(action) {
  if (!backendOnline) return { available: false, reason: document.hidden
    ? "后台页面已暂停刷新；回到本页并取得新状态后再操作"
    : "页面尚未取得当前后端状态，等待刷新或连接恢复" };
  if (motionActions.has(action)) {
    const latch = lastState.stop_latch?.latched;
    if (latch !== false)
      return { available: false, reason: latch === true ? "停止锁存已生效，请明确清除后再执行" : "停止锁存尚未核对，暂不能发送新运动目标" };
  }
  if (action === "move_target" && !targetInitialized)
    return { available: false, reason: "自定义目标未完成，请读取真实姿态或输入六个有效角度" };
  const cap = lastState.capabilities?.[action];
  if (cap === false) return { available: false, reason: "后端尚未开放该功能" };
  if (cap && typeof cap === "object")
    return withActionConfirmations(action, {
      available: cap.available ?? cap.supported ?? false,
      reason: cap.reason || cap.blocker || ((cap.available ?? cap.supported ?? false) ? "" : "功能尚未核对"),
    });
  if (cap === true) return withActionConfirmations(action, { available: true });
  if (knownUnavailable.has(action))
    return { available: false, reason: "原厂角色切换与主臂隔离未完成实机验证" };
  return { available: false, reason: "后端未提供此功能的就绪状态；请核对版本同步与连接" };
}
function refreshCapabilities() {
  const setAvailability = (e, c, action) => {
    e.disabled = !c.available || submitting;
    e.title = submitting ? "正在审核或提交任务，请等待回执" : !c.available ? c.reason || "等待本地后端" : actionNames[action] || action;
  };
  for (const e of document.querySelectorAll("[data-action]")) {
    const c = capability(e.dataset.action);
    setAvailability(e, c, e.dataset.action);
  }
  for (const [id, action] of [
    ["model-start", "model_start"],
    ["execute-target", "move_target"],
    ["gripper-target-plan", "move_gripper_plan"],
    ["gripper-target-execute", "move_gripper"],
    ["deploy-start", "deploy"],
    ["h50-shadow", "h50_shadow"],
    ["h50-start", "h50_deploy"],
    ["preview-target", "move_plan"],
  ]) {
    const c = capability(action);
    if (id === "preview-target" && !targetInitialized) {
      c.available = false;
      c.reason = "自定义目标未完成，请读取真实姿态或输入六个有效角度";
    }
    setAvailability($(id), c, action);
  }
  for (const e of document.querySelectorAll("[data-motion]")) {
    const action =
        e.dataset.motion === "plan" ? "move_plan" : "move_" + e.dataset.pose,
      c = capability(action);
    if (["ready", "zero"].includes(e.dataset.pose)) {
      try {
        const options = presetGripperDraft(e.dataset.pose);
        e.textContent = presetMotionTitle(e.dataset.pose, options, e.dataset.motion === "plan");
      } catch (error) { c.available = false; c.reason = error.message; }
    }
    setAvailability(e, c, action);
  }
  text("teleop-reason", "采集不会自动切换模式或移动机械臂。原厂主从已正常跟随时，可直接准备采集；需要恢复高跟随、待机或失能时，打开控制模式设置并核对该次条件。");
  // A paused or reconnecting display must not prevent an explicit stop attempt
  // through the last connected session. Its POST still needs a real receipt.
  $("stop-button").disabled = !backendOnline && !(lastState.connected === true &&
    typeof lastState.csrf_token === "string" && lastState.csrf_token.length > 0);
  refreshPrecision();
  $("target-from-actual").disabled = !actualFresh;
  $("target-reset").disabled = !actualFresh;
  renderReadiness();
  renderStopLatch();
  renderModeConfirmations();
  renderGripperConfirmations();
  if (typeof renderPresetGripperChoices === "function") renderPresetGripperChoices(object(lastState.config));
  if (typeof refreshTaskReviewButtons === "function") refreshTaskReviewButtons();
  if (typeof collectionWorkbench !== "undefined") collectionWorkbench?.renderControls();
  renderActionErrors();
  if (typeof refreshAttentionContext === "function") refreshAttentionContext();
}
function renderReadiness() {
  for (const [page, actions] of Object.entries(pageActions)) {
    const el = $(page + "-readiness");
    const available = [], blocked = [];
    for (const action of actions) {
      const c = capability(action);
      if (c.available) available.push(actionNames[action]);
      else blocked.push({ action, reason: c.reason || "尚未核对" });
    }
    const key = JSON.stringify({ available, blocked });
    if (readinessKeys[page] !== key) {
      readinessKeys[page] = key;
      const previousOpen = el.querySelector("details")?.open === true;
      el.replaceChildren();
      const title = document.createElement("strong");
      title.textContent = available.length ? "可提交操作（不代表已完成接管或已可运动）：" + available.join("、") : "当前没有满足提交条件的操作";
      el.append(title);
      if (blocked.length) {
        const details = document.createElement("details");
        details.open = previousOpen;
        const summary = document.createElement("summary");
        summary.textContent = "查看 " + blocked.length + " 项暂不可用操作及原因";
        const list = document.createElement("ul");
        list.className = "blocked";
        for (const { action, reason } of blocked) {
          const row = document.createElement("li");
          row.textContent = actionNames[action] + "：" + reason;
          list.append(row);
        }
        details.append(summary, list);
        el.append(details);
      }
    }
  }
  text("motion-confirmation-state", "点击执行即确认现场条件；编辑目标不会运动。");
  text("target-draft-state", targetInitialized
    ? targetFromFreshActual ? "目标已从真实姿态复制；编辑后需重新审阅路径。" : "当前为自定义目标草稿；编辑仅更新预览。"
    : targetEdited ? "目标输入未完成，请填写六个有限角度。" : "等待首个新鲜真实角度初始化目标；尚无可执行草稿。"
  );
}
function renderStopLatch() {
  const latch = object(lastState.stop_latch), latched = backendOnline ? latch.latched : null;
  $("stop-latch-banner").className = "stop-latch-banner " + (latched === true ? "error" : latched === false ? "good" : "warn");
  text("stop-latch-status", latched === true ? "停止锁存已生效 · 新运动目标被阻止" : latched === false ? "停止锁存已核对 · 当前未锁存" : "停止锁存状态未知 · 暂不能执行运动");
  const label = (v) => v === true ? "存在" : v === false ? "不存在" : "未核对";
  text("stop-latch-detail", "运行目录 STOP：" + label(backendOnline ? latch.runtime : null) + " · U 盘 STOP：" + label(backendOnline ? latch.usb : null)
    + (latched === true ? "。准备新一轮时手动清除；清除本身不移动机械臂。" : latched === false ? "。现场确认和控制器状态仍须满足执行条件。" : "。" + (backendOnline ? latch.error || "等待真实后端核对。" : "页面未取得当前状态，等待新反馈。")));
}
async function review(action, params) {
  const d = $("confirm-dialog");
  d.returnValue = "cancel";
  const takeover = action === "hub_start" && params.takeover_existing === true;
  const modeMutation = ["mode_takeover", "mode_reset", "mode_high_follow", "mode_standby", "mode_disable"].includes(action);
  $("takeover-review").hidden = !takeover;
  
  
  $("mode-review").hidden = !modeMutation;
  
  
  text("mode-review-copy", action === "mode_reset"
    ? "我确认机械臂已可靠承托，理解单次复位可能失去保持力，完成后须重新诊断并另行使能。"
    : action === "mode_high_follow"
      ? "我已核对原厂主从角色、当前姿态对应和主臂来源，理解恢复高跟随可能使执行臂响应主臂；请求提交不等于物理跟随已验收。"
    : action === "mode_disable"
      ? "我确认六关节已可靠承托、主臂信号已隔离，理解关节失能会失去保持力；夹爪不在这次失能范围。"
    : action === "mode_standby"
      ? "我确认机械臂已可靠承托、主臂信号已隔离；待机改变控制状态，不能替代电机失能或现场支撑。"
    : "我已核对接管所需现场声明，理解 CAN / MOVEJ 反馈不能证明控制权独占，也不能直接证明普通位置速度内部模式。"
  );
  text(
    "confirm-title",
    takeover ? "接管已识别的旧只读采集器" : ["move_ready", "move_zero"].includes(action)
      ? presetMotionTitle(action.slice(5), params) : actionNames[action] || action,
  );
  text(
    "confirm-copy",
    takeover
      ? "仅请求已配置路径和 SHA-256 都匹配的旧 Rerun 只读生产者正常退出，可能中断原监控。不会停止运动程序；其他相机占用者仍会阻止启动。请先确认当前没有旧数据流正在录制。"
      : action === "mode_reset"
        ? "本次只发送一次原厂复位请求，可能失去电机保持力。不会自动使能、发送位置目标、回位或再次复位。实际结果以原始发送回执和后续状态为准。"
        : action === "mode_takeover"
          ? "本次在真实示教已结束的前提下请求待机，再切到电脑 CAN / MOVEJ，并等待新鲜真实反馈。请求普通位置速度模式的值为 is_mit_mode=0x00；0x2A1 无法直接证明该内部状态。旧高跟随需要复位时，本任务将报告阻塞；受支撑复位有独立入口。"
        : action === "mode_high_follow"
          ? "本次仅向已有正确主从配置的右执行臂发送高跟随模式请求，不改主从角色、不发送位置目标、不自动使能。后端先核新鲜主臂目标与执行臂角度对应；CAN/MOVEJ反馈不能单独证明内部高跟随状态或实际遥操成功。"
        : action === "mode_standby"
          ? "本次只请求待机并核对新的状态反馈，不执行复位、设零或自动使能。待机与六关节失能分别判断，保持力变化需要可靠支撑。"
        : action === "mode_disable"
          ? "本次逐一请求右臂六个关节失能，并等待各关节新鲜失能反馈；不操作夹爪、不自动复位或重新使能。六关节会失去保持力，必须已可靠承托。"
      : action.startsWith("task_review_")
        ? "本次仅保存你对指定任务的人工复核。请核对任务 ID 与依据；不会发送运动目标，也不会改写程序执行、回位或录制保存结果。"
      : action === "gripper_zero"
        ? "本次把当前已失能夹爪位置设为零。坐标将改变；不会自动闭合、重新使能、移动六关节或修复驱动故障。请核对已登记的物理基准与旧模型坐标关系。"
      : action === "gripper_disable"
        ? "本次仅失能右夹爪；夹爪可能失去保持力。六关节不失能，不自动移动或设零；实际结果仍需新鲜夹爪反馈确认。"
      : ["move_ready", "move_zero"].includes(action)
        ? "本次向真实右执行臂发送运动目标。" + presetMotionSummary(action.slice(5), params, object(lastState.config)) + (params.close_gripper ? "请再次核对夹爪空载、指尖无人手或物体；同时闭爪选项不是现场安全确认。" : "") + (action === "move_zero" ? "六关节数值零位是位置目标，不是零点标定。" : "")
       : action === "move_gripper"
        ? "本次只向右夹爪发送 " + params.target_mm + " mm 开度目标，六关节不发送目标。请确认开合路径；健康但未使能的夹爪可随G指令使能，坐标零点保持。"
       : motionActions.has(action)
        ? "本次任务会向真实右执行臂发送运动请求，请确认现场状态和目标。"
        : "本次将显式启动或修改运行任务。",
  );
  text("confirm-params", JSON.stringify(params, null, 2));
  return new Promise((resolve) => {
    d.addEventListener("close", () => resolve(d.returnValue === "apply"), {
      once: true,
    });
    d.showModal();
  });
}
function motionParams(pose) {
  if (pose === "target" && !targetInitialized)
    throw new Error("目标草稿尚未完成。请读取真实姿态，或输入六个有效角度后再查看计划。");
  const speed = Number($("motion-speed").value);
  if (!Number.isFinite(speed) || speed < 1 || speed > 10)
    throw new Error(
      "预编程位姿运动请输入 1–10°/s；默认 10°/s。模型运动限速另行展示。",
    );
  return {
    pose,
    arm: "right",
    speed_deg_s: speed,
    target_deg: pose === "target" ? [...target] : undefined,
    ...(["ready", "zero"].includes(pose) ? presetGripperDraft(pose) : {}),
  };
}
function manualGripperParams(draft, cfg) {
  const bounds = cfg.gripper_bounds_m;
  if (!Array.isArray(bounds) || bounds.length !== 2 || !bounds.every(Number.isFinite) || bounds[0] >= bounds[1])
    throw new Error("G开度范围尚未核对。");
  const targetMm = draft.target_mm;
  if (!Number.isFinite(targetMm) || targetMm < bounds[0] * 1000 || targetMm > bounds[1] * 1000)
    throw new Error("G目标须在 " + bounds[0] * 1000 + "–" + bounds[1] * 1000 + " mm 范围内。");
  if (Math.abs(targetMm * 1000 - Math.round(targetMm * 1000)) > 1e-7)
    throw new Error("G目标精度须为0.001 mm或更粗。");
  return {target_mm: targetMm};
}
function manualGripperDraft() {
  const value = $("gripper-target-mm").value.trim();
  return {target_mm: value === "" ? NaN : Number(value)};
}
function editManualGripperTarget(value) {
  $("gripper-target-mm").value = String(value);
  $("gripper-target-range").value = String(value);
  gripperTargetInitialized = true;
  
  refreshCapabilities();
}
function fieldConfirm(params, deployment = false) {
  // Called by explicit execution handlers. These are operator intent flags only.
  return { ...params, leader_isolated: true, path_reviewed: true };
}
function renderModeConfirmations() {
  text("mode-confirmation-state", "点击执行即确认现场条件；实际模式以反馈为准。");
}
function modeFieldConfirm(action, params) {
  const result = { ...params, arm: "right" };
  for (const [key] of modeConfirmationFields(action)) result[key] = true;
  return result;
}
async function submit(action, params = {}) {
  const page = currentPage;
  let errorSource = "precondition";
  if (submitting) {
    showActionError("当前任务正在审核或提交，请等待任务回执。", page, action);
    return;
  }
  try {
    const cap = capability(action);
    if (!cap.available) throw new Error(cap.reason || "功能尚未开放。");
    submitting = true;
    refreshCapabilities();
    if (
      (motionActions.has(action) && !["h50_deploy","precision_deploy","v2_deploy"].includes(action)) ||
      action === "sync" ||
      (action === "hub_start" && params.takeover_existing === true) ||
      action === "teleop_enter" ||
      action === "teleop_exit" ||
      action === "mode_takeover" ||
      ["mode_reset", "mode_high_follow", "mode_standby", "mode_disable"].includes(action)
      || action === "gripper_disable"
      || action === "gripper_zero"
      || action.startsWith("task_review_")
    ) {
      if (!(await review(action, params))) return;
    }
    const currentCapability = capability(action);
    if (!currentCapability.available)
      throw new Error(currentCapability.reason || "任务条件已变化，请刷新后核对。");
    if (action.startsWith("task_review_") && taskStateView(taskPayload(lastState), backendOnline).sessionId !== params.session_id)
      throw new Error("当前任务身份已改变，未提交人工复核；请核对新任务后重新操作。");
    errorSource = "request";
    const response = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, params }),
    });
    const job = response.job || response;
    if (action === "h50_deploy") {
      
      
    }
    if (action === "gripper_zero") {
      
      
    }
    latestJob = job;
    pendingJob = job.id || job.job_id || null;
    if (pendingJob) {
      jobPages.set(pendingJob, page);
      if (jobPages.size > 60) jobPages.delete(jobPages.keys().next().value);
    }
    addEvent(
      (actionNames[action] || action) + "：请求已接受，等待实际结果。",
      "",
      null,
      pendingJob ? "submitted-" + pendingJob : null,
    );
    renderJob(job);
    showToast("任务已提交；结果以任务回执和实际反馈为准。", false);
    await pollState();
    return job;
  } catch (error) {
    showActionError(error.message, page, action, { source: errorSource });
    if (action === "collection_create" && (errorSource === "precondition" || error.requestRejected === true))
      return { request_rejected: true, error: error.message };
    return null;
  } finally {
    submitting = false;
    refreshCapabilities();
  }
}
for (const e of document.querySelectorAll("[data-action]"))
  e.addEventListener("click", () => {
    let params = {};
    try {
      params = e.dataset.params ? JSON.parse(e.dataset.params) : {};
      if (e.dataset.action === "deploy_plan")
        params = { duration: Number($("deployment-duration").value) };
      if (["mode_takeover", "mode_reset", "mode_high_follow", "mode_standby", "mode_disable"].includes(e.dataset.action))
        params = modeFieldConfirm(e.dataset.action, params);
      if (e.dataset.action.startsWith("gripper_"))
        params = gripperActionParams(e.dataset.action, gripperDraft());
    } catch (error) {
      showActionError(error.message, currentPage, e.dataset.action);
      return;
    }
    submit(e.dataset.action, params);
  });
for (const button of document.querySelectorAll("[data-open-gripper]"))
  button.addEventListener("click", () => {
    openPage("connect");
    $("gripper-calibration-panel").hidden = false;
    requestAnimationFrame(() => {
      $("gripper-calibration-panel").scrollIntoView({ block: "start", behavior: "smooth" });
      $("gripper-calibration-heading").focus({ preventScroll: true });
    });
  });
for (const button of document.querySelectorAll("[data-open-controller]"))
  button.addEventListener("click", () => {
    openPage("connect");
    requestAnimationFrame(() => $("controller-mode-panel").scrollIntoView({block:"start", behavior:"smooth"}));
  });
$("gripper-panel-close").addEventListener("click", () => { $("gripper-calibration-panel").hidden = true; });
for (const id of ["gripper-measure-label"])
  $(id).addEventListener("change", refreshCapabilities);
$("gripper-measured-gap").addEventListener("input", () => {
  
  refreshCapabilities();
});
for (const id of ["ready-close-gripper", "zero-close-gripper"])
  $(id).addEventListener("change", () => {
    
    refreshCapabilities();
  });
$("preset-gripper-speed").addEventListener("input", () => {
  
  refreshCapabilities();
});
for (const e of document.querySelectorAll("[data-motion]"))
  e.addEventListener("click", () => {
    try {
      const pose = e.dataset.pose;
      let params = motionParams(pose);
      if (e.dataset.motion === "plan") submit("move_plan", params);
      else submit("move_" + pose, fieldConfirm(params));
    } catch (error) {
      showActionError(error.message, currentPage, e.dataset.motion === "plan" ? "move_plan" : "move_" + e.dataset.pose);
    }
  });
for (const button of document.querySelectorAll("[data-usb-scan]")) {
  button.addEventListener("click", () => void submitUsbStorage("scan"));
}
import("./collection-workbench.js").then(({ CollectionWorkbench }) => {
  let storage = null;
  try { storage = window.localStorage; } catch { /* Optional input preferences only. */ }
  collectionWorkbench = new CollectionWorkbench({ document, submit, capability, storage,
    showError: message => showActionError(message, "teleop") });
  collectionWorkbench.onState(lastState, backendOnline);
}).catch(error => showActionError("采集工作台未加载：" + error.message, "teleop"));
$("model-start").addEventListener("click", () =>
  submit("model_start", { num_steps: Number($("sampler-steps").value) }),
);
$("sampler-steps").addEventListener("change", () => { samplerDraftEdited = true; });
function h50ActionParams(action, draft) {
  if (!Number.isInteger(draft.step) || ![5000, 10000, 15000, 20000, 2000].includes(draft.step))
    throw new Error("请选择已归档的5k/10k/15k/20k/2k checkpoint。");
  if (action === "h50_load" || action === "h50_acceptance") return {step: draft.step};
  const task = draft.task;
  if (typeof task !== "string" || !task.trim() || Array.from(task).length > 200 || task.includes("\u0000"))
    throw new Error("请填写1–200字符的非空任务提示词。");
  if (action === "h50_shadow") return {step: draft.step, count: 3, task};
  if (action !== "h50_deploy") throw new Error("未知H50操作。");
  if (!Number.isFinite(draft.duration) || draft.duration < 1 || draft.duration > 1800)
    throw new Error("H50策略时长必须是1–1800秒的有限数。");
  if (draft.leader_isolated !== true || draft.path_reviewed !== true)
    throw new Error("正式部署会真实运动；请完成本次控制源隔离、空爪和扫掠范围现场确认。");
  return {step: draft.step, duration: draft.duration, task, leader_isolated: true,
    path_reviewed: true, operator_skip_no_motion_acceptance: true};
}
function h50Draft() {
  const draft = precisionDraft();
  if (draft.precision !== "FP32") throw new Error("此可选操作仅用于FP32 + RTC路线。");
  return {step:draft.step,duration:draft.duration,task:draft.task,
    leader_isolated:draft.leader_isolated,path_reviewed:draft.path_reviewed};
}
for (const [id, action] of [["h50-shadow", "h50_shadow"], ["h50-start", "h50_deploy"]])
  $(id).addEventListener("click", () => {
    try { void submit(action, h50ActionParams(action, h50Draft())); }
    catch (error) { showActionError(error.message, "deploy", action); }
  });
for (const button of document.querySelectorAll("[data-h50-prompt]"))
  button.addEventListener("click", () => {
    $("precision-prompt").value = button.dataset.h50Prompt;
    $("precision-prompt").dispatchEvent(new Event("input", {bubbles: true}));
  });
$("deploy-start").addEventListener("click", () => {
  try {
    const duration = Number($("deployment-duration").value);
    if (!Number.isInteger(duration) || duration < 1 || duration > 300)
      throw new Error("运行时长必须是 1–300 的整数秒。");
    submit("deploy", fieldConfirm({ duration }, true));
  } catch (error) {
    showActionError(error.message, currentPage, "deploy");
  }
});
for (const button of document.querySelectorAll("[data-task-review]"))
  button.addEventListener("click", () => {
    try {
      const params = reviewTaskParams(taskPayload(lastState), $("task-review-note").value, backendOnline);
      submit("task_review_" + button.dataset.taskReview, params);
    } catch (error) { showActionError(error.message, "deploy"); }
  });
$("task-review-note").addEventListener("input", refreshTaskReviewButtons);
$("preview-target").addEventListener("click", () => {
  try {
    submit("move_plan", motionParams("target"));
  } catch (error) {
    showActionError(error.message, currentPage, "move_plan");
  }
});
$("execute-target").addEventListener("click", () => {
  try {
    submit("move_target", fieldConfirm(motionParams("target")));
  } catch (error) {
    showActionError(error.message, currentPage, "move_target");
  }
});
$("gripper-target-mm").addEventListener("input", () => {
  gripperTargetInitialized = true;
  const value = manualGripperDraft().target_mm;
  if (Number.isFinite(value)) $("gripper-target-range").value = String(value);
  
  refreshCapabilities();
});
$("gripper-target-range").addEventListener("input", () => editManualGripperTarget($("gripper-target-range").value));
$("gripper-target-current").addEventListener("click", () => {
  const right = object(lastState.status?.arms?.right);
  const view = gripperPositionView(right, backendOnline && lastState.connected === true);
  if (view.fresh) editManualGripperTarget(Number(Number(view.value).toFixed(1)));
});
for (const [id, action] of [["gripper-target-plan", "move_gripper_plan"], ["gripper-target-execute", "move_gripper"]])
  $(id).addEventListener("click", () => {
    try {
      let params = manualGripperParams(manualGripperDraft(), object(lastState.config));
      if (action === "move_gripper") params = fieldConfirm(params);
      void submit(action, params);
    } catch (error) { showActionError(error.message, "motion", action); }
  });
$("stop-button").addEventListener("click", async () => {
  try {
    const result = await api("/api/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    latestJob = result;
    renderJob(result);
    addEvent(
      "停止请求已提交：停止后续新目标；电机保持与最后目标由控制器决定。",
    );
    showToast(result.message || "停止已请求；这不是硬件急停。");
    pollState();
  } catch (error) {
    showActionError("停止请求失败：" + error.message, currentPage, "stop");
  }
});
$("show-details").addEventListener("click", () => {
  $("raw-details").open = !$("raw-details").open;
});
function updateTarget(q, { source = "manual", writeInputs = true } = {}) {
  if (!validQ(q)) return;
  target = [...q];
  targetInitialized = true;
  targetEdited = source !== "initial-actual";
  targetFromFreshActual = source === "actual" || source === "initial-actual";
  if (writeInputs)
    for (let i = 0; i < 6; i++)
      $("target-j" + i).value = Number(q[i].toFixed(3));
  
  robotView?.setTarget(target);
  updateTargetAvailability();
  refreshCapabilities();
}
function updateTargetAvailability() {
  robotView?.setEnabled(targetInitialized);
  const note = $("target-uninitialized");
  if (note) note.hidden = targetInitialized;
}
for (let i = 0; i < 6; i++) {
  const row = document.createElement("div");
  row.className = "joint-row";
  const label = document.createElement("label");
  label.textContent = "J" + (i + 1);
  label.htmlFor = "target-j" + i;
  const value = document.createElement("span");
  value.id = "actual-j" + i;
  value.className = "joint-actual";
  value.textContent = "—";
  const input = document.createElement("input");
  input.id = "target-j" + i;
  input.type = "number";
  input.step = "0.1";
  input.value = "";
  input.placeholder = "待初始化";
  input.setAttribute("aria-label", "J" + (i + 1) + "目标角度（度）");
  input.addEventListener("input", () => {
    targetEdited = true;
    targetFromFreshActual = false;
    const q = Array.from({ length: 6 }, (_, j) => {
      const value = $("target-j" + j).value;
      return value.trim() === "" ? NaN : Number(value);
    });
    
    if (validQ(q)) updateTarget(q, { writeInputs: false });
    else {
      targetInitialized = false;
      updateTargetAvailability();
      refreshCapabilities();
    }
  });
  row.append(label, value, input);
  $("joint-editor").append(row);
}
$("target-from-actual").addEventListener("click", () => {
  if (!actualFresh)
    return showActionError("尚无新鲜真实关节角，不能复制当前姿态。");
  updateTarget(actual, { source: "actual" });
  showToast("目标预览已复制实际角度，未发送运动请求。");
});
$("target-reset").addEventListener("click", () => {
  if (!actualFresh)
    return showActionError("尚无新鲜真实关节角，不能重置为当前姿态。");
  updateTarget(actual, { source: "actual" });
  showToast("目标预览已重置为当前真实姿态，执行前请确认新路径；未发送运动请求。");
});
$("motion-speed").addEventListener("input", () => {
  
  refreshCapabilities();
});
async function initRobot() {
  try {
    const module = await import("./robot-view.js");
    robotView = await module.createRobotView($("robot-view"), {
      manifestUrl: "./assets/robot/manifest.json",
      onTargetChange: (q) => updateTarget(q),
    });
    robotView.setTarget(target);
    const note = document.createElement("div");
    note.id = "target-uninitialized";
    note.className = "target-uninitialized";
    note.textContent = "目标尚未初始化。等待新鲜真实角度，或在关节窗口输入六个目标角度。";
    $("robot-view").append(note);
    updateTargetAvailability();
    renderState(lastState);
  } catch (error) {
    $("robot-view").replaceChildren();
    const note = document.createElement("div");
    note.className = "empty-line";
    note.textContent = "真实模型资产暂不可用：" + error.message;
    $("robot-view").append(note);
    addEvent("三维模型未加载：" + error.message, "error");
  }
}
initRobot();
window.addEventListener("resize", () => {
  robotView?.resize();
  drawChart();
});
function renderDualFollowerTelemetry(state) {
  const arms = object(object(state.status).arms);
  const connected = backendOnline && state.connected === true;
  for (const role of ["right", "left"]) {
    const arm = object(arms[role]);
    const declared = arm.stale_after_s ?? state.config?.can?.[role]?.stale_after ?? 0.2;
    const threshold = isNumber(declared) && declared > 0 ? declared : 0.2;
    const ageFresh = age => isNumber(age) && age >= 0 && age <= threshold;
    const sourceReady = connected && arm.observer_running === true &&
      !arm.observer_error && !arm.timestamp_failures;
    const jointsFresh = sourceReady && validQ(arm.q_deg) && ageFresh(arm.age_joint_s);
    const gripperMm = arm.grip_m * 1000;
    const gripperFresh = sourceReady && isNumber(arm.grip_m) && isNumber(gripperMm) && ageFresh(arm.age_gripper_s);
    for (let joint = 0; joint < 6; joint++)
      text("follower-" + role + "-j" + joint, jointsFresh ? number(arm.q_deg[joint], 3) : "--");
    text("follower-" + role + "-gripper", gripperFresh ? number(gripperMm, 3) : "--");
    setPill("follower-" + role + "-feedback", jointsFresh ? "关节新鲜" :
      !connected ? "未连接" : !sourceReady ? "观察通道未就绪" : "关节过期／缺失",
      jointsFresh ? "good" : "warn");
    const ageLabel = age => isNumber(age) && age >= 0 ? number(age, 3) + " s" : "--";
    text("follower-" + role + "-ages", "关节年龄 " + ageLabel(arm.age_joint_s) +
      " · 夹爪年龄 " + ageLabel(arm.age_gripper_s) + "（" + (gripperFresh ? "新鲜" : "非实时") +
      "）· 阈值 " + number(threshold, 3) + " s · " + (arm.interface || "接口未核对"));
    const mode = object(state.controller_modes?.[role]);
    const raw = mode.raw_status_hex ?? arm.status_hex;
    const rawValid = typeof raw === "string" && /^[0-9a-fA-F]{16}$/.test(raw);
    const modeAge = mode.age_status_s ?? arm.age_status_s;
    const modeFresh = sourceReady && mode.fresh === true && ageFresh(modeAge) && rawValid;
    text("follower-" + role + "-mode", (modeFresh ? mode.classification || "UNKNOWN" : "UNKNOWN / 非实时") +
      " · 状态年龄 " + ageLabel(modeAge) + " · 0x2A1 " +
      (rawValid ? (modeFresh ? "" : "历史 ") + raw : "--"));
  }
}
function renderPolicyTiming(config) {
  // Configuration arithmetic only. This neither selects a supported profile
  // nor certifies measured inference throughput or physical deployment.
  const hz = Number.isInteger(config.action_time_hz) && config.action_time_hz > 0
    ? config.action_time_hz : null;
  const horizon = Number.isInteger(config.horizon) && config.horizon > 0
    ? config.horizon : null;
  const delay = Number.isInteger(config.rtc_delay_steps) && config.rtc_delay_steps >= 0 &&
    horizon !== null && config.rtc_delay_steps < horizon ? config.rtc_delay_steps : null;
  const sendHz = isNumber(config.policy_send_hz) && config.policy_send_hz > 0
    ? config.policy_send_hz : null;
  const seconds = value => isNumber(value) ? number(value, 3) + " s" : "未核对";
  const milliseconds = value => isNumber(value) ? number(value * 1000, 1) + " ms" : "未核对";
  text("param-clock-summary", "模型训练标签 30 Hz · 执行动作 " +
    (hz === null ? "未核对" : hz + " Hz · 播放时长 " + number(30 / hz, 2) + "×"));
  text("param-action-hz", hz === null ? "未核对" : hz + " Hz");
  text("param-horizon", horizon === null ? "未核对" : "H" + horizon);
  text("param-block-window", seconds(hz !== null && horizon !== null ? horizon / hz : null));
  text("param-commit-window", seconds(hz !== null && delay !== null ? delay / hz : null));
  text("param-observation-budget", milliseconds(hz !== null && delay !== null
    ? (horizon - delay) / hz : null));
  const installBudget = hz !== null && delay !== null && sendHz !== null
    ? delay / hz - 1 / sendHz : null;
  text("param-install-budget", installBudget === null ? "未核对" : installBudget <= 0
    ? "无正预算 · " + milliseconds(installBudget) : "< " + milliseconds(installBudget));
}
// FEEDBACK_DELIVERY_VIEW_BEGIN: display only; never changes execution readiness.
function feedbackDisplayView(arm, online) {
  const numeric = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const age = arm?.age_joint_s ?? arm?.joint_age_s ?? arm?.age_s;
  const present = Array.isArray(arm?.q_deg || arm?.joints_deg)
    && (arm.q_deg || arm.joints_deg).length === 6
    && (arm.q_deg || arm.joints_deg).every(Number.isFinite);
  const recent = online && present && numeric(age) && age <= .5;
  const fresh = recent && arm.fresh === true && age <= (arm.stale_after_s ?? .2);
  return {recent, fresh, display_limit_s: .5,
    label: fresh ? "新鲜反馈" : recent ? "显示最近读数 · 执行时效未通过" : present ? "历史／过期反馈" : "未知",
    kind: fresh ? "good" : recent ? "neutral" : present ? "warn" : "neutral"};
}
function feedbackDeliveryText(state, online) {
  const d = state?.feedback_delivery;
  if (!online || d?.schema !== "tiger-feedback-delivery-v1") return "";
  const a = d.arms?.right || {};
  const ms = value => typeof value === "number" && Number.isFinite(value) && value >= 0
    ? (value * 1000).toFixed(0) + " ms" : "未核对";
  let result = "右臂反馈：直读 " + ms(a.direct_age_s) + "，采集通路 " + ms(a.hub_age_s)
    + "；PC 等待 " + ms(d.pc_cache_age_s) + "（已包含在前述年龄中）。";
  if (a.freshness_disagreement === true)
    result += " 两路时效不一致；缓存过期不表示控制器切换模式，执行仍等待即时核验。";
  return result + " 最近读数可展示至 500 ms；显示宽限不放宽录制和运动的反馈门槛。";
}
// FEEDBACK_DELIVERY_VIEW_END
function renderState(state) {
  lastState = state;
  renderDualFollowerTelemetry(state);
  const diagnostic = unwrap(state.status || {}),
    hub = unwrap(state.hub || diagnostic.shared_producer || {}),
    // The remote agent's current CAN snapshot wins over a cached hub report.
    // Joint angles, freshness, ages and controller mode share this one source.
    arms = object(diagnostic.arms || hub.arms || state.arms),
    right = object(arms.right),
    config = object(state.config),
    controller = object(right.status);
  const q = right.q_deg || right.joints_deg;
  actual = validQ(q) ? [...q] : null;
  const connected = state.connected === true;
  renderStageObserver(hub.stage_observer, state, connected && backendOnline);
  renderRecoveryState(hub.recovery, connected && backendOnline);
  taskDisplayReceivedAt = performance.now();
  taskDisplayHub = hub;
  renderTaskWorkspace(hub.task_state, connected && backendOnline);
  renderPerception(hub.perception || hub.alignment, connected && backendOnline, 0, hub.visual_servo);
  const observed = state.connected === false ? "未连接" : "尚未核对";
  text("connection-status", connected ? "已连接" : observed);
  $("connection-dot").classList.toggle("live", connected);
  text("host-label", config.ssh_alias || "tiger-agilex");
  text(
    "device-host",
    diagnostic.hostname || (connected ? config.hostname : "未核对"),
  );
  text(
    "device-identity",
    state.identity?.hostname
      ? "已核对：" + state.identity.hostname
      : "真实 SSH 身份由后端确认",
  );
  const mode =
    controller.ctrl_mode_name ??
    right.controller_mode ??
    controller.mode ??
    controller.ctrl_mode;
  const modeLabel =
    typeof mode === "string" ? mode : isNumber(mode) ? String(mode) : "未知";
  text("right-mode", modeLabel);
  renderControllerMode(state);
  renderGripperHealth(right, connected && backendOnline);
  renderGripperReadout(right, connected && backendOnline);
  text(
    "device-can",
    right.fresh === true
      ? "新鲜反馈"
      : right.fresh === false
        ? "反馈过期／缺失"
        : "未知",
  );
  const age = right.joint_age_s ?? right.age_joint_s ?? right.age_s;
  const ageLabel = isNumber(age) ? number(age, 3) + " s" : "—";
  text("feedback-age", ageLabel);
  const delivery = feedbackDeliveryText(state, connected && backendOnline);
  $("feedback-age").title = delivery || "反馈年龄包含传输与页面缓存等待";
  if (delivery) text("page-refresh-status",
    "当前页面自动读取状态；相机预览仅在前台采集页请求。切换标签页不会停止实机任务。 " + delivery);
  text(
    "device-can-detail",
    "反馈年龄 " +
      ageLabel +
      " · " +
      (right.interface || config.can?.right?.interface || "CAN 接口未核对"),
  );
  const usb = object(state.usb || diagnostic.usb || state.mount);
  const usbVerified =
    usb.verified === true || usb.mounted === true || usb.is_mount === true;
  const usbFalse =
    usb.verified === false || usb.mounted === false || usb.is_mount === false;
  text(
    "usb-status",
    usbVerified ? "已核对挂载" : usbFalse ? "未挂载" : "尚未核对",
  );
  text(
    "device-usb",
    usbVerified
      ? usb.uuid || config.usb_uuid || "真实挂载"
      : usbFalse
        ? "未挂载"
        : "未核对",
  );
  text(
    "device-usb-detail",
    usb.path || usb.mountpoint || config.usb || "诊断、采集和运行报告写入 U 盘",
  );
  renderUsbStorage(state.usb_storage, backendOnline);
  const ros = object(diagnostic.ros || state.ros);
  text(
    "device-ros",
    ros.master_reachable === true
      ? "Master 可达 · 通道待评估"
      : ros.master_reachable === false
        ? "Master 不可达"
        : "未知",
  );
  const hubReachable =
    state.hub?.api_reachable ??
    diagnostic.shared_producer?.api_reachable ??
    (hub.schema ? true : null);
  text(
    "hub-status",
    hubReachable === true
      ? "API 可达"
      : hubReachable === false
        ? "服务不可达"
        : "未知",
  );
  text("prep-identity", connected ? "●" : "○");
  text("prep-usb", usbVerified ? "●" : "○");
  text("prep-right", right.fresh === true ? "●" : "○");
  const cameras = object(hub.cameras || state.cameras);
  text(
    "prep-cameras",
    Object.values(cameras).some((c) => c.fresh === true) ? "●" : "○",
  );
  text(
    "last-refresh",
    state.updated_at
      ? "更新 " + timeLabel(state.updated_at)
      : diagnostic.sampled_wall_time_s
        ? "观察 " + timeLabel(diagnostic.sampled_wall_time_s)
        : "暂无实时检查",
  );
  for (let i = 0; i < 6; i++)
    text("actual-j" + i, actual ? number(actual[i], 3) : "—");
  const feedbackValid = validQ(actual) && right.fresh === true && connected;
  actualFresh = feedbackValid && backendOnline;
  if (actualFresh && !targetInitialized && !targetEdited)
    updateTarget(actual, { source: "initial-actual" });
  const displayFeedback = feedbackDisplayView(right, connected && backendOnline);
  setPill("motion-feedback-state", displayFeedback.label, displayFeedback.kind);
  text(
    "robot-source",
    actual
      ? "真实反馈 · " +
          (right.interface || "右臂 CAN") +
          " · " +
          ageLabel +
          (feedbackValid ? "" : " · 非新鲜状态")
      : "实际姿态：未接收到真实反馈",
  );
  robotView?.setActual(actual, {
    age_s: age,
    gripper_m: right.grip_m ?? right.gripper_m,
    source: right.interface || "CAN",
    valid: feedbackValid,
  });
  updateTargetAvailability();
  if (feedbackValid) {
    const stamp =
      right.joint_stamp_s ??
      right.sampled_monotonic_s ??
      right.last_monotonic_s ??
      state.sample_id ??
      state.updated_at ??
      Date.now();
    if (stamp !== lastSampleTime) {
      jointHistory.push({ time: Date.now(), q: [...actual] });
      if (jointHistory.length > 200) jointHistory.shift();
      lastSampleTime = stamp;
      drawChart();
    }
  }
  const expectedCapture = object(config.capture_profile === "right_only_v1" ? {
    profile: "right_only_v1", required_arm_roles: ["right"], required_camera_roles: ["front", "right"],
  } : null);
  const liveCapture = object(object(hub.recording_contract).capture_contract);
  const captureKnown = connected && hub.api_reachable === true && object(state.capture_scope).matched === true;
  text("capture-scope-status", captureKnown
    ? (liveCapture.profile === "right_only_v1" ? "已核对服务：仅右臂 · 头部和右腕图像；左侧不参与本次采集。" : "已核对服务：按本批冻结通道采集。")
    : expectedCapture.profile === "right_only_v1" ? "本机已配置仅右臂；服务范围待连接核对。" : "本次采集范围等待服务核对。");
  const imageRef = object(config.capture_image_reference);
  text("capture-image-status", imageRef.recording_pixels === "raw_rgb"
    ? "本机图像方案：原始 640×480 RGB 归档；内参参数已归档，去畸变候选用于后续统一派生。新训练读取链待接入，当前不自动去畸变。"
    : "图像处理与内参版本等待核对。");
  for (const role of ["front", "right", "left"])
    renderCamera(role, object(cameras[role]), connected,
      captureKnown && Array.isArray(liveCapture.excluded_camera_roles) && liveCapture.excluded_camera_roles.includes(role));
  const recording = object(hub.recording || state.recording);
  const recActive = recording.active === true || recording.recording === true;
  const recordingDisplay = recordingStateView(recording, connected && backendOnline);
  setPill("recording-state", recordingDisplay.label, recordingDisplay.kind);
  $("recording-state").title = recordingDisplay.detail;
  text(
    "recording-timer",
    isNumber(recording.elapsed_s)
      ? number(recording.elapsed_s, 1) + " s"
      : recActive
        ? "录制中"
        : "—",
  );
  collectionWorkbench?.onState(state, backendOnline);
  const model = unwrap(state.model || {});
  renderModelState(model);
  renderFP32Shadow(state.fp32_shadow, state.jobs);
  renderPrecision(state);
  renderH50Deployment(state.h50_deployment, state.jobs);
  if (isNumber(model.latency_s || model.rpc_elapsed_s))
    text(
      "param-latency",
      number((model.latency_s || model.rpc_elapsed_s) * 1000, 1) + " ms",
    );
  text(
    "param-horizon",
    isNumber(config.horizon) ? "H" + config.horizon : "H15 · 配置",
  );
  text(
    "param-action-hz",
    isNumber(config.action_time_hz)
      ? config.action_time_hz + " Hz"
      : "18 Hz · 配置",
  );
  text(
    "param-can-hz",
    isNumber(config.policy_send_hz)
      ? config.policy_send_hz + " Hz"
      : "50 Hz · 配置",
  );
  text(
    "param-speed",
    isNumber(config.policy_speed_deg_s)
      ? config.policy_speed_deg_s +
          "°/s · " +
          config.policy_accel_deg_s2 +
          "°/s²"
      : "30°/s · 50°/s² · 配置",
  );
  text(
    "param-rtc",
    isNumber(config.rtc_delay_steps)
      ? config.rtc_delay_steps + " / " + config.rtc_attention_extra
      : "8 / 3 · 配置",
  );
  renderPolicyTiming(config);
  if (validQ(config.ready_deg))
    text("ready-vector", config.ready_deg.map((v) => number(v, 3)).join(" / "));
  renderReadyGripper(config);
  const rerun = config.rerun_viewer || state.rerun_viewer;
  if (rerun && /^https?:\/\//.test(rerun)) {
    $("rerun-link").href = rerun;
    $("rerun-link").removeAttribute("aria-disabled");
  } else {
    $("rerun-link").href = "#";
    $("rerun-link").setAttribute("aria-disabled", "true");
  }
  for (const e of state.events || []) {
    const msg = e.message || e.summary || e.event || e.type;
    if (msg)
      addEvent(
        msg,
        e.level === "error" ? "error" : ["warn", "warning"].includes(e.level) ? "warn" : "",
        e.at || e.time,
        e.id || JSON.stringify(e),
      );
  }
  const jobs = Array.isArray(state.jobs)
    ? state.jobs
    : Object.values(object(state.jobs));
  if (jobs.length) {
    const current =
      jobs.find((j) =>
        ["running", "queued", "pending"].includes(j.state || j.status),
      ) || jobs.at(-1);
    renderJob(current);
  }
  text(
    "connection-source",
    connected
      ? "真实状态源：" + (state.source || "AgileX 后端")
      : "本地页面 / 尚未连接机器人",
  );
  text(
    "raw-state",
    JSON.stringify({ state, last_submitted_job: latestJob }, null, 2),
  );
  refreshCapabilities();
  if (typeof refreshAttentionJobs === "function") refreshAttentionJobs(state);
}
// TASK_VIEW_PURE_BEGIN: pure presentation contracts, also exported for offline tests.
const taskStageDefinitions = [
  ["prepare", "准备"], ["approach", "接近"], ["grasp", "抓取"],
  ["verify_grasp", "持物核验"], ["transport", "搬运"], ["release", "释放"],
  ["verify_place", "放置核验"], ["return", "回位"], ["finalize", "保存"],
];
function taskStateView(value, online, elapsedS = 0) {
  const task = object(value), known = task.schema === "tiger-task-state-v1";
  const strings = items => Array.isArray(items) ? items.filter(x => typeof x === "string").slice(0, 12) : [];
  const sessionId = known && typeof task.session_id === "string" && task.session_id.trim() ? task.session_id : null;
  const limit = isNumber(task.freshness_limit_s) && task.freshness_limit_s > 0 ? task.freshness_limit_s : 1;
  const age = isNumber(task.age_s) && task.age_s >= 0 && isNumber(elapsedS) && elapsedS >= 0 ? task.age_s + elapsedS : null;
  const fresh = known && online === true && task.fresh === true && age !== null && age <= limit;
  const historicalReceipts = known && !!sessionId && task.active === false;
  const rawResults = known ? object(task.results) : {};
  const statuses = {
    unknown: "未知", unverified: "未确认", not_started: "尚未开始", not_requested: "未请求",
    running: "执行中", checking: "链路检查中", preparing: "准备中", pending: "等待处理", success: "确认成功", confirmed_success: "确认成功",
    failure: "确认失败", confirmed_failure: "确认失败", failed: "失败", faulted: "异常中止",
    cancelled: "已取消", completed: "已完成", deadline_completed: "已到运行期限",
    verified_complete: "真实反馈已确认回位", capturing: "正在采集", stop_requested: "已请求结束采集",
    draining: "正在写完剩余记录", saved: "已保存", incomplete: "记录不完整", save_failed: "保存失败",
    finalization_pending: "保存结果待确认", blocked: "被阻止", skipped: "未执行",
  };
  const result = key => {
    const raw = rawResults[key], code = typeof raw === "string" ? raw : object(raw).status;
    if (typeof code !== "string") return "未知";
    if (code.toLowerCase() === "completed")
      return {execution: "运行流程已结束", return: "回位完成 · 执行回执", recording: "保存完成 · 录制回执"}[key] || "已完成";
    return statuses[code.toLowerCase()] || "未知";
  };
  const nodes = known && Array.isArray(object(task.graph).nodes) ? task.graph.nodes : [];
  const nodeStates = {pending: "未开始", waiting: "等待", running: "进行中", active: "进行中",
    completed: "已记录完成", complete: "已记录完成", success: "已记录完成", failed: "失败",
    failure: "失败", blocked: "被阻止", skipped: "未执行", unknown: "未知", not_observed: "未观测", candidate: "候选线索"};
  const phaseCode = known && typeof task.phase === "string" ? task.phase : "unknown";
  const phaseLabel = {idle: "尚未开始", preflight: "部署链路预检", prepare: "准备姿态", policy: "模型策略执行中", verify_grasp: "持物核验候选", verify_place: "放置核验候选", return: "正常回位", finalize: "录制收尾", attention: "需要处理异常", observing: "观察中", unknown: "阶段未确认"}[phaseCode.toLowerCase()] ||
    (typeof task.stage_label === "string" ? task.stage_label : taskStageDefinitions.find(([id]) => id === phaseCode.toLowerCase())?.[1] || "未知阶段");
  const recovery = known ? object(task.recovery) : {};
  const grasp = known ? object(task.perception || task.grasp || task.grasp_evidence) : {};
  const graspCode = grasp.held || grasp.state || grasp.status;
  const graspLabel = {held: "疑似持物 · 待独立复核", empty: "疑似空爪 · 待独立复核", not_held: "疑似未持物 · 待独立复核", unknown: "未知"};
  const operator = known ? object(task.operator_review) : {};
  const physical = object(rawResults.physical_task);
  const reviewed = physical.provenance === "operator_review" && operator.provenance === "operator_review" &&
    ["success", "failure"].includes(operator.verdict) && operator.verdict === physical.status &&
    typeof operator.reviewer === "string" && Array.isArray(operator.evidence_refs) && operator.evidence_refs.some(x => typeof x === "string" && x.trim());
  return {
    known, sessionId, fresh, canReview: known && !!sessionId && online === true,
    phase: fresh ? phaseLabel : historicalReceipts ? "最近任务记录 · " + phaseLabel : "未知 · 等待新鲜任务快照",
    freshness: !online ? "离线 · 历史记录" : !known ? "等待兼容任务数据" : fresh ? "任务快照新鲜" : "任务快照过期／时间未核对",
    source: sessionId ? "任务 " + sessionId + " · 修订 " + (Number.isInteger(task.revision) ? task.revision : "未知") +
      " · 快照年龄 " + (age !== null ? number(age, 3) + " s" : "未提供") +
      " · 来源 " + (typeof task.source === "string" ? task.source : "task_state 接口") : "尚无可复核任务身份",
    outcomes: {physical_task: reviewed ? result("physical_task") : "未确认 · 需独立复核", execution: result("execution"), return: result("return"), recording: result("recording")},
    outcomeHistorical: !fresh,
    physicalSource: reviewed
      ? "人工复核 · " + (typeof operator.reviewer === "string" ? operator.reviewer : "来源见任务回执")
      : typeof physical.source === "string" ? physical.source : "尚无独立确认来源",
    grasp: fresh && grasp.fresh === true ? graspLabel[graspCode] || "未知" : "未知 · 不能用历史线索判断当前持物",
    placement: fresh && grasp.fresh === true ? ({placed:"疑似已放置 · 待独立复核",not_placed:"疑似未放置 · 待独立复核",unknown:"未知"}[grasp.placed] || "未知") : "未知 · 不使用历史放置线索",
    evidence: fresh && grasp.fresh === true ? strings(grasp.evidence || task.evidence).concat(
      typeof grasp.source === "string" ? ["感知来源：" + grasp.source] : [],
      ["证据等级：" + (typeof grasp.evidence_level === "string" ? grasp.evidence_level : "未核对")]) : ["抓持证据不新鲜或缺失；不推断当前是否持物。"],
    failure: known && typeof task.failure_reason === "string" && task.failure_reason ? task.failure_reason : "尚无可核对记录",
    recoveryMode: typeof recovery.mode === "string" ? recovery.mode + " · " + (recovery.eligible === true ? "资格候选，是否执行以真实回执为准" : "尚不具备自动恢复条件") : "恢复能力待核对",
    blockers: known ? strings(recovery.blockers) : ["等待任务与恢复条件记录"],
    nodes: taskStageDefinitions.map(([id, label]) => {
      const raw = object(nodes.find(node => node?.id === id));
      const code = typeof raw.status === "string" ? raw.status.toLowerCase() : "unknown";
      const supported = Object.hasOwn(nodeStates, code) ? code : "unknown";
      const recorded = historicalReceipts && ["prepare", "return", "finalize"].includes(id) &&
        ["completed", "failed", "skipped"].includes(supported);
      return {id, label, status: fresh || recorded ? supported : "unknown",
        detail: fresh ? nodeStates[supported] : recorded ? "历史执行回执 · " + nodeStates[supported] : "状态待刷新"};
    }),
    history: known && Array.isArray(task.history) ? task.history.slice(-12).reverse().map(object)
      .map(item => {
        const detail = object(item.detail), receipt = object(detail.receipt);
        const event = typeof detail.event === "string" ? detail.event : null;
        const labels = {deployment_started:"部署链路预检", prepare_started:"开始准备姿态", prepare_completed:"准备姿态完成",
          policy_started:"开始模型策略", deadline:"策略运行到时", policy_completed:"策略流程完成",
          return_started:"开始正常回位", return_completed:"回位完成", return_failed:"回位失败", return_skipped:"回位未执行",
          recording_pending:"录制待保存", recording_completed:"录制已保存", recording_failed:"录制保存失败",
          fault:"执行异常", cancelled:"执行已取消"};
        return {at: item.at || item.wall_time || null,
          label: event ? labels[event] || event : typeof item.label === "string" ? item.label :
            typeof item.phase === "string" ? item.phase : typeof item.kind === "string" ? item.kind : "任务事件",
          reason: typeof receipt.reason === "string" ? receipt.reason : typeof receipt.error === "string" ? receipt.error :
            typeof item.reason === "string" ? item.reason : typeof item.detail === "string" ? item.detail :
            typeof detail.phase === "string" ? detail.phase : ""};
      }) : [],
  };
}
function perceptionView(value, online, elapsedS = 0) {
  const data = object(value), known = data.schema === "tiger-perception-v1";
  const age = isNumber(data.age_s) && data.age_s >= 0 && isNumber(elapsedS) && elapsedS >= 0 ? data.age_s + elapsedS : null;
  const limit = isNumber(data.freshness_limit_s) && data.freshness_limit_s > 0 ? data.freshness_limit_s : 1;
  const fresh = known && online === true && data.fresh === true && age !== null && age <= limit;
  const providers = known ? object(data.providers) : {}, cameras = known ? object(data.cameras) : {};
  const capabilities = known ? object(data.capabilities) : {};
  const providerLabels = {opencv: "OpenCV · 几何线索", sam2: "SAM2 · 分割候选", hil_serl: "HIL-SERL 式 · 抓持分类"};
  return {
    known, fresh,
    freshness: !online ? "离线 · 无当前图像证据" : !known ? "等待兼容感知数据" : fresh ? "感知快照新鲜" : "感知证据过期／未就绪",
    source: known ? "来源 " + (typeof data.source === "string" ? data.source : "perception 接口") +
      " · 年龄 " + (age !== null ? number(age, 3) + " s" : "未提供") : "尚无真实感知来源",
    cameras: ["front", "right"].map(role => {
      const camera = object(cameras[role]), alignment = object(camera.selected || camera.alignment || camera);
      const cameraFresh = fresh && camera.status === "candidate" && (camera.fresh !== false);
      const calibratedDelta = alignment.calibrated_center_error_px;
      const delta = calibratedDelta || alignment.image_center_error_px || alignment.error_px;
      const dx = Array.isArray(delta) ? delta[0] : object(delta).x ?? alignment.dx_px;
      const dy = Array.isArray(delta) ? delta[1] : object(delta).y ?? alignment.dy_px;
      const error = cameraFresh && isNumber(dx) && isNumber(dy) ?
        (calibratedDelta ? "相对像素参照：" : "相对图像中心：") + "Δx " + number(dx, 1) + " px · Δy " + number(dy, 1) + " px" : "像素误差未知";
      const calibration = object(camera.calibration);
      return {role, label: role === "front" ? "头部相机" : "右腕相机", error,
        calibration: typeof calibration.calibration_id === "string" ? "像素参照 " + calibration.calibration_id + " · 非完整手眼标定" : "未标定 · 不换算毫米",
        detail: cameraFresh ? (camera.fingertips_detected === true ? "指尖线索存在，仍需几何验收" : "尚未检测指尖；图像中心不等于夹爪抓取中心") :
          camera.status === "ambiguous" ? "目标存在歧义，不能计算可信对准误差" : "等待新鲜、有效的本相机证据"};
    }),
    providers: Object.entries(providerLabels).map(([id, label]) => {
      const provider = object(providers[id] || capabilities[id === "hil_serl" ? "classifier" : id]);
      const missing = Array.isArray(provider.missing) ? provider.missing.filter(x => typeof x === "string") : [];
      const status = typeof provider.status === "string" ? provider.status.toLowerCase() : "unknown";
      const ready = online === true && known && fresh && missing.length === 0 && (id === "opencv"
        ? capabilities.opencv_available === true && fresh : id === "sam2"
          ? provider.available === true && provider.loaded === true : provider.service_verified === true);
      const unavailable = provider.available === false || provider.configured === false || status === "unavailable" || missing.length > 0;
      return {id, label, ready, status: ready ? "已就绪 · 不等于任务验收" : !fresh ? "当前状态未核对" : unavailable ? "不可用" : provider.configured === true || provider.available === true ? "已配置 · 尚未确认加载／服务" : "尚未就绪",
        detail: [typeof provider.reason === "string" ? provider.reason : "", ...missing].filter(Boolean).join("；") ||
          (ready ? "能力来源见后端，推理结果须另行核对" : "缺少依赖／权重或能力回执；不推断已加载")};
    }),
  };
}
function reviewTaskParams(task, note, online) {
  const view = taskStateView(task, online);
  if (!view.canReview) throw new Error("尚无可复核的已存在任务；请取得任务身份后再提交。");
  if (typeof note !== "string" || !note.trim()) throw new Error("请填写现场观察或录像复核依据。");
  if (note.trim().length > 256) throw new Error("人工复核依据请保持在 256 字以内。");
  return {session_id: view.sessionId, note: note.trim()};
}
function visualServoView(value, online) {
  const wrapper = object(value), snapshot = object(wrapper.snapshot || wrapper);
  const caps = object(wrapper.capabilities || snapshot.capabilities);
  const known = snapshot.schema === "tiger-visual-servo-v1" || caps.schema === "tiger-visual-servo-v1";
  const backend = known ? caps.backend || snapshot.backend : null;
  const label = backend === "visp" ? "ViSP · 视觉伺服" : backend === "opencv_numpy" ? "OpenCV / NumPy · 视觉伺服" : "视觉伺服 · 后端未核对";
  const status = !known || !online ? "当前状态未核对" : caps.dependency_available === false ? "依赖不可用 · 仅影子入口" :
    caps.calibration_document_valid !== true ? "未标定 · 尚不可计算校正" : snapshot.status === "shadow_candidate" ? "影子候选 · 不可执行" :
      snapshot.status === "expired" ? "影子候选已过期" : "计算被阻止 · 查看原因";
  return {id: "visual_servo", label, ready: false, status,
    detail: [known && typeof snapshot.reason === "string" ? snapshot.reason :
      known && typeof caps.reason === "string" ? caps.reason : "等待视觉伺服能力回执",
      "仅影子计算，未授权运动；不发送 CAN", caps.backend_runtime_verified === true ? "后端运行核验已有记录" : "实际后端运行未验证"].join("；")};
}
// TASK_VIEW_PURE_END
function taskPayload(state) {
  return unwrap(state.hub || unwrap(state.status || {}).shared_producer || {}).task_state;
}
function refreshTaskDisplayAge() {
  if (taskDisplayReceivedAt === null || document.hidden) return;
  const elapsed = Math.max(0, (performance.now() - taskDisplayReceivedAt) / 1000);
  const online = backendOnline && lastState.connected === true;
  renderTaskWorkspace(taskDisplayHub.task_state, online, elapsed);
  renderPerception(taskDisplayHub.perception || taskDisplayHub.alignment, online, elapsed, taskDisplayHub.visual_servo);
}
function refreshTaskReviewButtons() {
  const view = taskStateView(taskPayload(lastState), backendOnline && lastState.connected === true);
  for (const suffix of ["success", "failure"]) {
    const button = $("task-review-" + suffix);
    if (!button) continue;
    const cap = capability("task_review_" + suffix);
    button.disabled = submitting || !view.canReview || !cap.available || !$("task-review-note").value.trim();
    button.title = !view.canReview ? "需要已存在的任务身份和在线通道" : !cap.available ? cap.reason : "人工复核只记结果，不发送运动";
  }
}
function replaceTaskList(id, rows, fallback) {
  const list = $(id); if (!list) return;
  list.replaceChildren();
  for (const label of rows.length ? rows : [fallback]) {
    const item = document.createElement("li"); item.textContent = label; list.append(item);
  }
}
function renderTaskWorkspace(value, online, elapsedS = 0) {
  const view = taskStateView(value, online, elapsedS);
  text("task-phase", view.phase); text("task-source", view.source);
  setPill("task-freshness", view.freshness, view.fresh ? "good" : "neutral");
  for (const [id, key] of [["physical", "physical_task"], ["execution", "execution"], ["return", "return"], ["recording", "recording"]])
    text("task-result-" + id, (view.outcomeHistorical && view.known ? "最近记录 · " : "") + view.outcomes[key]);
  text("task-result-physical-source", view.physicalSource);
  text("task-grasp", "是否持物：" + view.grasp);
  text("task-placement", "是否已放置：" + view.placement);
  text("task-failure", "失败原因：" + view.failure);
  text("task-recovery-mode", view.recoveryMode);
  replaceTaskList("task-evidence", view.evidence, "尚无足够抓持证据。");
  replaceTaskList("task-recovery-blockers", view.blockers, "未报告阻塞条件；不等于恢复已授权或已执行。");
  replaceTaskList("task-history", view.history.map(item => (item.at ? timeLabel(item.at) + " · " : "") + item.label + (item.reason ? "：" + item.reason : "")), "尚无任务事件");
  const flow = $("task-flow"); flow.replaceChildren();
  for (const node of view.nodes) {
    const item = document.createElement("li"), label = document.createElement("strong"), detail = document.createElement("span");
    item.className = "task-node " + node.status; label.textContent = node.label; detail.textContent = node.detail;
    item.append(label, detail); flow.append(item);
  }
  refreshTaskReviewButtons();
}
function renderPerception(value, online, elapsedS = 0, servo = null) {
  const view = perceptionView(value, online, elapsedS);
  text("perception-source", view.source);
  setPill("perception-freshness", view.freshness, view.fresh ? "good" : "neutral");
  for (const [id, records] of [["perception-cameras", view.cameras], ["perception-providers", [...view.providers, visualServoView(servo, online)]]]) {
    const container = $(id); container.replaceChildren();
    for (const record of records) {
      const item = document.createElement("section"), heading = document.createElement("h3"), main = document.createElement("strong"), detail = document.createElement("p");
      heading.textContent = record.label; main.textContent = record.error || record.status;
      detail.textContent = [record.calibration, record.detail].filter(Boolean).join(" · ");
      item.append(heading, main, detail); container.append(item);
    }
  }
}
// Presentation only: this observer never controls deployment or CAN targets.
// In particular, completed RPC/jobs are not evidence of a successful grasp.
function gripperPositionView(right, online = true) {
  const gripper = object(right.gripper_feedback);
  const declared = right.stale_after_s;
  const threshold = isNumber(declared) && declared > 0 ? declared : 0.2;
  const age = gripper.age_s;
  const mm = isNumber(gripper.aperture_m) ? gripper.aperture_m * 1000 : NaN;
  const known = isNumber(mm);
  const fresh = online && right.observer_running !== false && !right.observer_error &&
    !right.timestamp_failures && gripper.fresh === true && known &&
    isNumber(age) && age >= 0 && age <= threshold;
  return { fresh, value: fresh ? number(mm, 3) : "—",
    age: isNumber(age) && age >= 0 ? number(age, 3) + " s" : "未知",
    label: fresh ? "夹爪独立新鲜反馈" : !online ? "当前未核对" : "夹爪反馈过期／缺失",
    historical: known && !fresh ? "历史坐标 " + number(mm, 3) + " mm；不是当前读数。" : "",
    threshold: number(threshold, 3) + " s" };
}
function renderGripperReadout(right, online) {
  const view = gripperPositionView(right, online), gripper = object(right.gripper_feedback);
  const cfg = object(lastState.config), bounds = cfg.gripper_bounds_m;
  const knownBounds = Array.isArray(bounds) && bounds.length === 2 && bounds.every(Number.isFinite) && bounds[0] < bounds[1];
  $("gripper-target-range").disabled = !knownBounds;
  $("gripper-target-current").disabled = !view.fresh || !knownBounds;
  if (knownBounds) {
    for (const id of ["gripper-target-mm", "gripper-target-range"]) {
      $(id).min = String(bounds[0] * 1000); $(id).max = String(bounds[1] * 1000);
    }
    if (!gripperTargetInitialized && view.fresh) {
      const initial = Number(Number(view.value).toFixed(1));
      if (initial >= bounds[0] * 1000 && initial <= bounds[1] * 1000) {
        $("gripper-target-mm").value = String(initial); $("gripper-target-range").value = String(initial);
        gripperTargetInitialized = true;
      }
    }
    text("gripper-target-detail", "G控制器开度坐标范围 " + number(bounds[0] * 1000, 1) + "–" + number(bounds[1] * 1000, 1) + " mm；手动参考速度 " + number(cfg.ready_gripper_speed_m_s * 1000, 1) + " mm/s。数值和滑条只编辑目标，点击执行才开合。");
  }
  text("actual-gripper", view.value);
  text("gripper-value", view.fresh ? view.value + " mm" : "—");
  text("gripper-readout-source", view.label + " · 0x2A8 年龄 " + view.age +
    " · 阈值 " + view.threshold + (view.historical ? "。" + view.historical : "。保留坐标符号，独立于关节新鲜度。"));
  text("gripper-calibration-value", view.fresh ? view.value + " mm" : "—");
  text("gripper-calibration-age", view.age + (view.fresh ? " · 新鲜" : " · 当前未核对"));
  text("gripper-calibration-status", view.fresh
    ? (gripper.status_hex || "未知") + " · " + (gripper.enabled === true ? "已使能" : gripper.enabled === false ? "已失能" : "使能未知")
    : "当前未知" + (gripper.status_hex ? " · 历史 " + gripper.status_hex : ""));
  const raw = gripper.raw_payload_hex;
  text("gripper-calibration-raw", typeof raw === "string" && /^[0-9a-fA-F]{16}$/.test(raw)
    ? (view.fresh ? "" : "历史 ") + raw : "未知");
  text("gripper-calibration-source", view.label + (view.historical ? "。" + view.historical : "；反馈坐标不是独立卡尺测量。"));
}
function gripperHealthView(gripper, online = true, maxAge = 0.2) {
  const age = gripper.age_s;
  const fresh = online && gripper.fresh === true && isNumber(age) && age >= 0 && age <= maxAge;
  if (!fresh) return {label: "夹爪独立状态未核对", kind: "neutral", detail: "等待新的 0x2A8；六关节 CAN/MOVEJ 不等于夹爪驱动正常。"};
  const raw = gripper.status_hex || "未知";
  const value = isNumber(gripper.aperture_m) ? number(gripper.aperture_m * 1000, 2) + " mm" : "未知";
  const faults = Array.isArray(gripper.fault_labels) ? gripper.fault_labels : [];
  if (Array.isArray(gripper.fault_flags) && gripper.fault_flags.length)
    return {label: "夹爪异常 · " + raw, kind: "error", detail: faults.join("、") + "；" + (gripper.enabled === true ? "已使能" : "未使能") + "；反馈坐标 " + value + "。状态位是驱动器报告，不是独立测温；不自动清错或设零。"};
  return {label: gripper.enabled === true ? "夹爪驱动无故障位 · 已使能" : "夹爪驱动无故障位 · 未使能", kind: gripper.enabled === true ? "good" : "warn", detail: raw + " · 反馈坐标 " + value + "；到位仍需闭合目标与连续真实反馈确认。"};
}
function renderGripperHealth(right, online) {
  const view = gripperHealthView(object(right.gripper_feedback), online, right.stale_after_s || 0.2);
  for (const suffix of ["motion", "deploy"]) {
    setPill("gripper-health-" + suffix, view.label, view.kind);
    text("gripper-health-detail-" + suffix, view.detail);
  }
}

function renderReadyGripper(config) {
  const target = isNumber(config.ready_gripper_m) && config.ready_gripper_m >= 0
    ? number(config.ready_gripper_m * 1000, 2) + " mm" : "未核对";
  text("param-ready-gripper", target);
  renderPresetGripperChoices(config);
}
function renderPresetGripperChoices(config) {
  for (const pose of ["ready", "zero"]) {
    try {
      text(pose + "-gripper", presetMotionSummary(pose, presetGripperDraft(pose), config));
    } catch (error) { text(pose + "-gripper", error.message); }
  }
}
// Recovery display is read-only. Even a malformed upstream 'success' value
// cannot claim verified recovery or enable an automatic robot action here.
function recoveryView(value, online) {
  const recovery = object(value);
  const known = recovery.schema === "tiger-recovery-supervisor-v1";
  const states = {observing: "观察中 · 尚无疑似失败标记", suspected: "发现疑似问题 · 等待复核",
    pause_requested: "已记录暂停请求 · 尚未确认机器人停止",
    awaiting_safe_retreat: "已核对停止回执 · 等待安全退让证据",
    reobserve: "已核对退让回执 · 重新观察目标", retry_ready: "重试前证据已核对 · 等待独立启动",
    retrying: "已收到实际重试启动回执 · 结果待核对", needs_operator: "需要现场处理 · 不自动继续"};
  const active = recovery.deployment_active === true;
  const age = recovery.age_s;
  const limit = isNumber(recovery.freshness_limit_s) && recovery.freshness_limit_s > 0
    ? recovery.freshness_limit_s : 1;
  const fresh = known && online === true && active && recovery.fresh === true &&
    isNumber(age) && age >= 0 && age <= limit;
  const strings = items => Array.isArray(items)
    ? items.filter(item => typeof item === "string").slice(0, 8) : [];
  const current = known ? states[recovery.state] : null;
  const history = known && Array.isArray(recovery.history) ? recovery.history.slice(-6).reverse()
    .filter(item => item && typeof item === "object" && states[item.state]) : [];
  const retryCount = Number.isInteger(recovery.retry_count) && recovery.retry_count >= 0 ? recovery.retry_count : null;
  const maxRetries = Number.isInteger(recovery.max_retries) && recovery.max_retries >= 0 ? recovery.max_retries : null;
  const historical = !online || !active || !fresh;
  return {
    state: current ? (historical ? "最近记录 · " : "") + current : "尚无有效恢复状态",
    freshness: !online ? "通道未连接" : !known ? "等待监测数据" : !active
      ? "部署未运行 · 不累计线索" : fresh ? "只读监测 · 新鲜" : "监测证据不足／过期",
    fresh,
    source: known && isNumber(age) && age >= 0 ? "监测结果年龄 " + number(age, 3) + " s · " +
      (Number.isInteger(recovery.sample_count) ? recovery.sample_count + " 次有效观察" : "计数未提供") : "尚无有效连续观测",
    mode: "只读监测 · 自动退让重抓未启用",
    retries: known && retryCount !== null && maxRetries !== null ? retryCount + " / " + maxRetries + " 次 · 上限不是自动重试承诺" : "未核对",
    evidence: !online ? ["通道未连接；不能将历史线索当作当前现场状态。"] : !known ? ["等待真实恢复监测数据。"] : !active
      ? ["部署未运行；历史事件保留供复盘，不认定当前抓取失败。"] : fresh
        ? strings(recovery.evidence) : ["缺少新鲜证据；不推断当前失败，也不自动继续运动。"],
    missing: known ? strings(recovery.missing_evidence) : ["等待监测数据与独立执行回执。"],
    limits: "自动恢复未启用：" + (known && strings(recovery.automation_gaps).length
      ? strings(recovery.automation_gaps).join("；") : "退让路径、目标空间位置与独立持物判定尚未验收。"),
    history: history.map(item => ({...item, label: states[item.state]})),
  };
}
function renderRecoveryState(value, online) {
  const view = recoveryView(value, online);
  text("recovery-state", view.state);
  text("recovery-source", view.source);
  text("recovery-mode", view.mode);
  text("recovery-retries", view.retries);
  text("recovery-limits", view.limits);
  setPill("recovery-freshness", view.freshness, view.fresh ? "good" : "neutral");
  for (const [id, rows] of [["recovery-evidence", view.evidence], ["recovery-missing", view.missing]]) {
    const list = $(id);
    list.replaceChildren();
    for (const label of rows.length ? rows : ["尚无足够证据。"] ) {
      const row = document.createElement("li");
      row.textContent = label;
      list.append(row);
    }
  }
  const history = $("recovery-history");
  history.replaceChildren();
  for (const item of view.history) {
    const row = document.createElement("li");
    row.textContent = (item.at ? timeLabel(item.at) + " · " : "") + item.label +
      (typeof item.reason === "string" ? "：" + item.reason : "");
    history.append(row);
  }
  if (!view.history.length) {
    const row = document.createElement("li");
    row.textContent = "尚无恢复事件记录";
    history.append(row);
  }
}
function stageObserverView(value, state, online) {
  const observer = object(value);
  const age = observer.age_s;
  const declared = observer.freshness_limit_s ?? observer.stale_after_s;
  const limit = isNumber(declared) && declared > 0 ? declared : 1;
  const fresh = online === true && observer.fresh === true &&
    isNumber(age) && age >= 0 && age <= limit;
  const strings = items => Array.isArray(items)
    ? items.filter(item => typeof item === "string").slice(0, 8) : [];
  const history = Array.isArray(observer.history) ? observer.history.slice(-8).reverse()
    .filter(item => item && typeof item === "object" && typeof item.label === "string") : [];
  const jobs = Array.isArray(state.jobs) ? state.jobs : Object.values(object(state.jobs));
  const job = jobs.filter(item => item?.action === "deploy").at(-1);
  const programStates = {queued: "等待执行", pending: "等待执行", running: "执行中",
    succeeded: "流程已结束 · 抓放结果待复核", completed: "流程已结束 · 抓放结果待复核",
    failed: "程序失败 · 查看任务回执", blocked: "程序被阻止 · 查看任务回执",
    cancelled: "程序已取消", unknown: "执行状态未确认"};
  return {
    fresh,
    phase: fresh && typeof observer.label === "string" ? observer.label : "无法判断",
    freshness: !online ? "通道未连接" : fresh ? "真实观测 · 新鲜" :
      isNumber(age) && age > limit ? "观测已过期" : "数据不足／未就绪",
    age: isNumber(age) && age >= 0 ? "观察结果年龄 " + number(age, 3) + " s · " +
      (Number.isInteger(observer.sample_count) ? observer.sample_count + " 次观察" : "计数未提供") :
      "尚未收到观察器数据",
    evidence: fresh ? strings(observer.evidence) : [!online
      ? "通道未连接，不能将历史判断当作当前状态。"
      : "缺少新鲜且有效的观测；不推断当前抓放阶段。"],
    limitations: strings(observer.limitations),
    method: typeof observer.method === "string" ? observer.method : "未核对",
    // This first observer is a heuristic. No source can upgrade it to success.
    result: "未确认 · 需观察录像或现场复核",
    program: job ? (online ? "" : "历史回执 · ") +
      (programStates[job.state || job.status] || "执行状态未确认") +
      (job.id ? " · " + job.id : "") : "暂无实机部署回执",
    history,
  };
}
function renderStageObserver(observer, state, online) {
  const view = stageObserverView(observer, state, online);
  text("observer-phase", view.phase);
  text("observer-age", view.age);
  text("observer-program", view.program);
  text("observer-result", view.result);
  text("observer-method", "观察方法：" + view.method);
  setPill("observer-freshness", view.freshness, view.fresh ? "good" : "warn");
  $("stage-observer-panel").classList.toggle("observer-is-fresh", view.fresh);
  const evidence = $("observer-evidence");
  evidence.replaceChildren();
  for (const value of view.evidence.length ? view.evidence : ["尚无足够线索区分抓放阶段。"] ) {
    const row = document.createElement("li");
    row.textContent = value;
    evidence.append(row);
  }
  text("observer-limitations", view.limitations.length ? view.limitations.join("；") :
    "颜色线索不等于已抓住；夹爪闭合、程序结束都不能证明抓放成功。遮挡或反馈过期时显示“无法判断”。");
  const history = $("observer-history");
  history.replaceChildren();
  for (const item of view.history) {
    const row = document.createElement("li"), stamp = document.createElement("time"),
      label = document.createElement("span");
    const at = item.at ?? (isNumber(item.wall_ns) ? item.wall_ns / 1e9 : null);
    stamp.textContent = at ? timeLabel(at) : "—";
    label.textContent = item.label;
    row.append(stamp, label);
    history.append(row);
  }
  if (!view.history.length) {
    const row = document.createElement("li");
    row.className = "observer-empty";
    row.textContent = "尚无真实观测记录";
    history.append(row);
  }
}
// Recorder status is independent of physical task success. Active remains
// true while flushing; queue depth alone cannot identify that phase.
function recordingStateView(recording, online = true) {
  const active = recording.active === true || recording.recording === true;
  const inactive = recording.active === false || recording.recording === false;
  const error = typeof recording.error === "string" ? recording.error : "";
  const path = typeof recording.path === "string" ? recording.path : "";
  if (!online) return {label: "历史录制状态 · 当前未核对", kind: "warn", detail: path};
  if (error || recording.status === "RECORDING_FAILED")
    return {label: active ? "录制异常 · 尚未结束" : "录制失败", kind: "error",
      detail: [error || "录制回执报告失败", path].filter(Boolean).join(" · ")};
  if (active) return {label: "录制会话活动", kind: "good",
    detail: "可能正在采集或保存；结束并保存的回执确认后才算关闭。" + (path ? " " + path : "")};
  if (inactive && recording.status === "RECORDED_NATIVE_REVIEW_REQUIRES_QC")
    return {label: "已保存 · 待质量检查", kind: "neutral", detail: path};
  return {label: inactive ? "未录制" : "未核对", kind: "neutral", detail: path};
}

function fp32ShadowView(component = {}) {
  component = component && typeof component === "object" ? component : {};
  const ready = component.ready === true && component.api_reachable === true && component.health_fresh === true;
  const policy = component.policy || component.health?.policy || {};
  const detail = ["独立4090服务；PC影子路由，运动未授权"];
  if (Number.isInteger(policy.checkpoint_step)) detail.push("checkpoint " + policy.checkpoint_step);
  if (policy.precision) detail.push("参数 " + String(policy.precision));
  if (Number.isInteger(policy.horizon)) detail.push("H" + policy.horizon);
  if (typeof component.metadata_fingerprint === "string") detail.push("metadata SHA " + component.metadata_fingerprint.slice(0, 12));
  if (component.error) detail.push(String(component.error));
  return {label: ready ? "4090 FP32/H50 · 影子服务健康已核" : "4090 FP32/H50 · 尚未就绪",
    kind: ready ? "good" : "neutral", detail: detail.join(" · ")};
}
function renderFP32Shadow(component, jobs) {
  const view = fp32ShadowView(component);
  if (lastState.config?.h50_deployment?.enabled === true) {
    view.label = "历史固定2k快捷入口 · 已迁移";
    view.kind = "neutral";
    view.detail = "请选择上方4090 FP32/H50所选模型影子入口；本处只保留历史任务回执。";
  }
  if ($("fp32-shadow-pill")) setPill("fp32-shadow-pill", view.label, view.kind);
  text("fp32-shadow-detail", view.detail);
  const latest = (Array.isArray(jobs) ? jobs : []).filter(job => job.action === "shadow_fp32").at(-1);
  if (latest) {
    const result = object(latest.result);
    text("fp32-shadow-output", JSON.stringify({
      job_id: latest.id, state: latest.state, error: latest.error || null,
      report: latest.report || result.report || null,
      result: result.result || null,
    }, null, 2));
  }
}
function h50DeploymentView(component, requestedStep) {
  component = object(component);
  const fresh = component.api_reachable === true && component.manager_valid === true && component.health_fresh === true;
  const loaded = Number.isInteger(component.selected_step) ? component.selected_step : null;
  const ready = fresh && component.ready === true && component.manager_status === "READY";
  const detail = ["下次请求 " + requestedStep, "实际加载 " + (loaded ?? "未核实"), "manager " + (component.manager_status || "未知")];
  if (Number.isInteger(component.desired_step)) detail.push("加载目标 " + component.desired_step);
  if (component.lease?.active === true) detail.push("执行租约活动 · step " + component.lease.step);
  detail.push(component.rtc?.ready === true ? "新RTC FP32精度/能力已核" : "新RTC能力或精度审计未验收");
  if (ready && loaded !== requestedStep) detail.push("请求时将显式切换模型并等待实际就绪");
  if (component.error || component.manager_error) detail.push(String(component.error || component.manager_error));
  return {label: ready ? "实际加载 " + loaded + " · FP32/H50已核 · " + (component.rtc?.ready === true ? "RTC已核" : "RTC未核") : fresh ? "4090 manager可达 · 模型尚未就绪" : "4090 H50状态尚未核实",
    kind: ready && component.rtc?.ready === true ? "good" : ready ? "warn" : "neutral", detail: detail.join(" · ")};
}
function h50ConfigurationView(cfg) {
  cfg = object(cfg);
  const v = key => cfg[key] ?? "未核实";
  const velocity = (key, unit) => cfg[key] === null ? "无软件速度封顶" : v(key) + unit;
  const playbackHz = cfg.action_hz;
  const playbackMs = steps => Number.isFinite(playbackHz) && playbackHz > 0 && Number.isFinite(steps) ? steps / playbackHz * 1000 : NaN;
  const freezeMs = playbackMs(cfg.rtc_freeze_steps);
  const marginMs = Number.isFinite(freezeMs) && Number.isFinite(cfg.reply_deadline_ms) ? freezeMs - cfg.reply_deadline_ms : NaN;
  return {
    contract: "训练标签 " + v("native_training_hz") + "Hz；动作播放 " + v("action_hz") + "Hz；目标发送 " + v("send_hz") + "Hz；推理目标 " + number(cfg.inference_hz, 3) + "Hz、每 " + v("replan_steps") + " 点重规划（约 " + number(playbackMs(cfg.replan_steps), 1) + "ms）；" + (cfg.rtc_enabled === true ? "RTC配置已启用，实际能力与验收证据另核" : "RTC配置未启用或未知"),
    rtc: "首包native生成合法参考后，后续使用RTC；首包失败或晚回即终止，不隐式重试。RTC冻结 " + v("rtc_freeze_steps") + " 点≈" + number(freezeMs, 1) + "ms；结果截止 " + v("reply_deadline_ms") + "ms；冻结切点前理论余量 " + number(marginMs, 1) + "ms；上一计划已知 " + v("rtc_known_steps") + " 点；β=" + v("rtc_beta") + "；队列余量 " + v("queue_reserve_steps") + " 点≈" + number(playbackMs(cfg.queue_reserve_steps), 1) + "ms。无运动时仅诊断参考链推进，实体始终HOLD；余量为合同计算值，不是实测保证。",
    governor: "关节：" + velocity("reference_speed_deg_s", "°/s") + "，加速度 " + v("reference_accel_deg_s2") + "°/s²；夹爪：" + velocity("gripper_reference_speed_m_s", "m/s") + "，加速度 " + v("gripper_reference_accel_m_s2") + "m/s²；H50控制器速度比例 " + v("native_speed_percent") + "% 。控制器固有能力限制仍有效，目标速度不等于实测机械速度。",
    trial: "起点 " + (cfg.start_pose === "confirmed_current" ? "本次人工确认的当前姿态" : "合同未核实") + "；每检查点独立零基trial_index，seed=" + v("trial_seed_base") + "+trial_index（索引以新会话回执为准）；前录制 " + v("record_before_s") + "s、后录制 " + v("record_after_s") + "s；不自动使能/设零/回位。",
    storage: cfg.persistence_mode === "bounded_ram_async_usb" && cfg.commit_timing_basis === "packet_ready_to_validated_ram_commit_ready"
      ? "控制ready时延计至模型验证＋RAM计划就绪，独立于USB落盘；有界RAM " + number(cfg.journal_buffer_limit_bytes / 1048576, 0) + "MiB / " + v("journal_buffer_limit_records") + "条，后台保存到本次核验USB。CAN保留原内核整数时间戳；全部队列flush、文件读回和视频核验通过后才显示已保存；不使用内部SSD缓存。"
      : "有界RAM/USB后置保存合同尚未核实；不能用旧durable指标宣称新ready通过。"
  };
}
function h50AdmissionView(component, requestedStep) {
  component = object(component);
  const record = object(component.acceptance);
  const sameStep = record.step === requestedStep && component.selected_step === requestedStep;
  const passed = component.health_fresh === true && component.rtc?.ready === true && component.acceptance_passed === true && sameStep;
  return {passed, detail: passed ? "所选" + requestedStep + "：当前版本无运动验收PASS；仍检查本轮现场声明、USB、反馈、故障和STOP。" : "所选" + requestedStep + "：未完成当前版本无运动性能验收。人工实验入口可按你的选择跳过这项验收，回执如实记录未验收；位置、实时反馈、故障、STOP及600ms截止检查仍生效。" + (component.selected_step !== requestedStep ? "请先加载所选模型。" : "")};
}
function h50PersistenceView(jobs, recording) {
  const latest = (Array.isArray(jobs) ? jobs : []).filter(job => ["h50_shadow", "h50_acceptance", "h50_deploy"].includes(job.action)).at(-1);
  if (!latest) return "本轮USB保存状态待核；模型加载不等于录制完成。";
  const wrapper = object(latest.result), result = object(wrapper.result || wrapper);
  const evidence = object(result.acceptance || result.artifacts);
  if (evidence.usb_finalflush_validated === true)
    return "本轮数据已保存到本次核验USB，并完成最终flush与文件/视频读回核验。" + (evidence.episode ? "录制目录：" + evidence.episode + "。" : "") + "模型或物理任务结果另列。";
  if (["queued", "running"].includes(latest.state))
    return recording?.saving === true ? "USB保存中；等待全部队列flush与文件读回，尚未完成。" : "试验或后台USB保存/核验进行中；控制ready不等于数据已保存。";
  return "本轮USB最终保存未验收或失败；请查看完整回执，不据任务退出码推定已保存。";
}
function renderH50Deployment(component, jobs) {
  const view = h50DeploymentView(component, precisionDraft().step);
  setPill("h50-model-pill", view.label, view.kind);
  text("h50-model-detail", view.detail);
  const cfg = object(lastState.config?.h50_deployment);
  const configuration = h50ConfigurationView(cfg);
  text("h50-contract-detail", configuration.contract);
  text("h50-rtc-detail", configuration.rtc);
  text("h50-governor-detail", configuration.governor);
  text("h50-trial-detail", configuration.trial);
  text("h50-storage-detail", configuration.storage);
  text("h50-save-detail", h50PersistenceView(jobs, lastState.hub?.recording));
  text("h50-config-output", JSON.stringify(Object.fromEntries(Object.entries(cfg).filter(([key]) => key !== "approved_at_user_turn")), null, 2));
  const admission = h50AdmissionView(component, precisionDraft().step);
  text("h50-admission-detail", admission.detail);
  text("h50-admission-output", JSON.stringify(component?.acceptance || {status: "NOT_READ", reason: component?.acceptance_error || "尚无验收证据"}, null, 2));
  const latest = (Array.isArray(jobs) ? jobs : []).filter(job => ["h50_load", "h50_shadow", "h50_acceptance", "h50_deploy"].includes(job.action)).at(-1);
  if (latest) {
    const result = object(latest.result);
    const run = object(result.result?.run || result.run);
    const task = latest.params?.task || run.task;
    text("h50-run-prompt", task ? "本轮已提交提示词：" + task + "（" + latest.state + "）；输入框为下一次实验草稿。" : "本轮提示词详见USB任务回执；输入框为下一次实验草稿。");
    text("h50-output", JSON.stringify({job_id: latest.id, action: latest.action,
      state: latest.state, error: latest.error || null, report: latest.report || result.report || null,
      result: result.result || result}, null, 2));
  }
}
function renderModelState(model) {
  const mstatus = model.status || model.phase;
  const errorText = typeof model.error === "string" ? model.error : "";
  const transportError = model.api_reachable === false ||
    /^(?:URLError\(|<urlopen error)/i.test(errorText);
  const serviceUnavailable = transportError &&
    /(?:ConnectionRefusedError|ECONNREFUSED|connection refused|actively refused)/i.test(errorText);
  const process = model.process || {};
  const processKnown = process.fresh === true && process.identity_verified === true;
  const healthyReady = model.ready === true && model.api_reachable !== false && !model.error;
  const loading = !healthyReady && processKnown && process.alive === true &&
    (model.api_reachable === false || !model.error);
  const exited = !healthyReady && processKnown && process.registered === true && process.alive === false;
  let label = model.ready === true ? "模型已就绪" : model.ready === false ? "模型未就绪" : "模型状态未知";
  if (loading) label = "模型进程运行中 · 等待就绪";
  else if (exited) label = "模型进程已退出 · 尚未就绪";
  else if (serviceUnavailable) label = "旧 4060 / H15 服务未启动；4090 见独立影子入口";
  else if (model.error) label = "模型报错";
  else if (typeof mstatus === "string" && mstatus) label += " · " + mstatus;
  const steps = model.rtc?.num_steps;
  const actualSteps = healthyReady && Number.isInteger(steps) && [5, 10].includes(steps);
  if (actualSteps) {
    label += " · 实际服务 " + steps + " 步";
    if (!samplerDraftEdited) $("sampler-steps").value = String(steps);
    else if (Number($("sampler-steps").value) !== steps) label += "（选择框为下次加载草稿）";
  }
  $("sampler-steps").title = actualSteps ? "当前服务实际采样 " + steps + " 步；修改选择只编辑下次加载草稿" : "下次加载采样草稿；实际服务采样步数尚未核对";
  setPill("policy-pill", label, healthyReady ? "good" : exited ? "warn" : loading || serviceUnavailable ? "warn" : model.error ? "error" : "neutral");
  $("policy-pill").title = errorText;
  const detail = $("model-loading-detail");
  let explanation = healthyReady ? "模型接口已报告 ready；这不代表实机部署或抓放已通过。" :
    loading ? "已核对模型进程 PID " + process.pid + " 仍存活。校验、加载、预热的具体阶段未提供；等待接口报告 ready，请勿重复加载。" :
    exited ? "登记的模型进程已退出。可能是正常停止，也可能启动失败；请检查服务日志，不能把启动回执当作就绪。" :
    process.error ? "模型进程身份核验失败：" + process.error :
    process.fresh === false ? "进程信息已过期，无法确认是否仍在加载；等待刷新。" :
    "旧 4060 / H15 服务尚未就绪。4090 FP32/H50 的状态与按钮在下方独立影子入口；本页旧执行频率不代表 H50 部署参数。";
  if (process.log) explanation += " 服务日志：" + process.log;
  if (detail) detail.textContent = explanation;
}
function renderControllerMode(state) {
  const m = object(state.controller_modes?.right);
  const online = backendOnline && state.connected === true;
  const display = controllerModeDisplay(m, online);
  setPill("mode-state", display.stateLabel, display.kind);
  setPill("mode-freshness", display.freshnessLabel, display.fresh ? "good" : "warn");
  text("mode-actual", display.actualLabel);
  text("mode-reason", display.reason);
  text("right-mode", display.topLabel);
  text("right-mode-freshness", display.freshnessLabel);
  for (const el of document.querySelectorAll("[data-mode-summary]"))
    el.textContent = display.stateLabel + " · " + display.freshnessLabel;
  const hex = (v, width = 2) => Number.isInteger(v) && v >= 0 ? "0x" + v.toString(16).toUpperCase().padStart(width, "0") : "—";
  const field = (value, name) => hex(value) + (typeof name === "string" && name ? " · " + name : "");
  text("mode-ctrl", field(m.ctrl_mode, m.ctrl_mode_name || ({ 0: "待机", 1: "CAN 指令" })[m.ctrl_mode]));
  text("mode-arm", field(m.arm_status, m.arm_status_name || (m.arm_status === 0 ? "正常" : null)));
  text("mode-feed", field(m.mode_feed, m.mode_feed_name || (m.mode_feed === 1 ? "MOVEJ 关节位置" : null)));
  text("mode-teach", field(m.teach_status, m.teach_status_name || ({ 0: "关闭", 2: "记录已结束，不单独视为故障" })[m.teach_status]));
  text("mode-error", hex(m.err_code, 4) + (m.err_code === 0 ? " · 无错误位" : ""));
  text("mode-age", (isNumber(m.age_status_s) ? number(m.age_status_s, 3) + " s" : "—") + " · " + display.freshnessLabel);
  text("mode-raw-status", m.raw_status_hex || "尚无真实状态帧");
  const sourceStamp = m.source_wall_time_s ?? m.status_wall_time_s ?? m.sampled_wall_time_s;
  text("mode-source", "状态来源：" + (m.source || m.interface || "右臂 0x2A1（接口未核对）")
    + " · 状态源时刻：" + (sourceStamp ? timeLabel(sourceStamp) + " / " + sourceStamp : "未提供")
    + " · 页面缓存时刻：" + (state.updated_at ? timeLabel(state.updated_at) : "未提供")
    + (!online ? " · 当前离线，字段仅供历史核对" : ""));
  const enables = $("mode-enables");
  const enabledFresh = online && m.enabled_fresh === true;
  enables.replaceChildren();
  for (let j = 0; j < 6; j++) {
    const value = Array.isArray(m.enabled) ? m.enabled[j] : null;
    const chip = document.createElement("span");
    chip.className = "mode-enable " + (enabledFresh && value === true ? "good" : "neutral");
    chip.textContent = "J" + (j + 1) + " · " + (value === true ? "已使能" : value === false ? "未使能" : "未知") + (!enabledFresh && typeof value === "boolean" ? "（历史）" : "");
    enables.append(chip);
  }
  text("mode-enable-source", "低速反馈年龄：" + (isNumber(m.age_low_s) ? number(m.age_low_s, 3) + " s" : "未提供")
    + " · 源时刻：" + (m.enable_source_wall_time_s ? timeLabel(m.enable_source_wall_time_s) + " / " + m.enable_source_wall_time_s : "未提供")
    + (enabledFresh ? " · 新鲜" : " · 当前使能尚未核对"));
  const requested = m.requested_mode;
  const modeJobs = Array.isArray(state.jobs) ? state.jobs : Object.values(object(state.jobs));
  const lastModeJob = modeJobs.filter((job) => ["mode_takeover", "mode_reset", "mode_high_follow", "mode_standby", "mode_disable"].includes(job.action)).at(-1)
    || modeJobs.filter((job) => job.action === "mode_inspect").at(-1);
  const history = modeReceiptView(lastModeJob, requested);
  const requestedValues = object(history.receipt.requested_mode);
  text("mode-job-receipt", history.lines.join("\n"));
  text("mode-requested", history.headline);
  text("mode-history-context", history.context);
  text("mode-request-raw", lastModeJob || requested ? JSON.stringify(history.raw_details, null, 2) : "尚无模式请求回执。");
  text("mode-position-mode", "控制子模式请求：" + (Number.isInteger(requestedValues.is_mit_mode)
    ? "最近回执中的请求值 is_mit_mode=" + hex(requestedValues.is_mit_mode) + "。"
    : "尚无包含子模式请求值的执行回执。") + "0x2A1 无法直接证明 is_mit_mode 的内部状态；CAN / MOVEJ 反馈也不构成该项证明。");
  const activity = controllerActivityDisplay(m, online);
  setPill("mode-activity-status", activity.label, activity.kind);
  text("mode-activity-note", activity.note);
  text("mode-external", activity.details);
  // Use the same current gate as the action button. A historical mode-only
  // classification cannot override disconnection, USB, STOP or a busy task.
  const takeover = capability("mode_takeover");
  text("mode-switchability", !online
    ? "当前通道未连接，历史模式字段不能作为切换许可；请先连接并刷新诊断。"
    : takeover.available === true
    ? "当前后端条件与现场声明允许提出切换请求；实际结果仍须等待真实模式回执。"
      : takeover.reason || "后端尚未确认模式切换条件。");
}
// MODE_RECEIPT_VIEW_BEGIN: pure historical presentation; never changes live gates.
function modeReceiptView(job, requested) {
  const obj = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  job = obj(job); requested = obj(requested);
  const hasJob = typeof job.id === "string" || typeof job.action === "string";
  // Never attach a different request's receipt, time or report to a new job.
  const matches = !hasJob || (typeof job.id === "string" && job.id === requested.job_id
    && job.action === requested.action && job.state === requested.job_state);
  const linkedRequest = matches ? requested : {};
  const receipt = [job.mode_receipt, obj(job.result).mode_receipt, obj(job.result).result, linkedRequest.receipt]
    .map(obj).find(value => Object.keys(value).length) || {};
  const evidence = obj(receipt.evidence_summary);
  const action = job.action || linkedRequest.action;
  const operation = {mode_takeover:"CAN/MOVEJ 接管", mode_reset:"单次复位", mode_high_follow:"原厂高跟随请求", mode_standby:"待机请求", mode_disable:"六关节失能", mode_inspect:"只读模式诊断"}[action] || "模式请求";
  const state = job.state || linkedRequest.job_state;
  const labels = {queued:"已排队", pending:"待处理", running:"执行中", succeeded:"已完成", completed:"已完成", failed:"失败", blocked:"被阻止", cancelled:"已取消"};
  const timestamp = job.finished_at || job.created_at || linkedRequest.finished_at || linkedRequest.created_at;
  const parsed = typeof timestamp === "string" && timestamp.trim() ? new Date(timestamp) : null;
  const time = parsed && Number.isFinite(parsed.getTime())
    ? new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false}).format(parsed)
    : "时间未提供";
  const hasRequest = !!action;
  const headline = hasRequest ? operation + " · " + (labels[state] || "结果待核对") + " · " + time : "尚无模式请求回执";
  const context = "本栏是该次请求的历史结果。当前模式、使能与控制来源分别以新鲜反馈为准；不会据此自动重试、复位或发目标。";
  const lines = hasRequest ? [headline] : ["尚无模式请求回执。"];
  if (action === "mode_high_follow") lines.push("高跟随仅显示请求与CAN/MOVEJ回传；内部高跟随状态及物理主从同步仍须现场核验。");
  if (action === "mode_disable") lines.push(receipt.motor_disable_confirmed === true
    ? "该次六关节失能已由新鲜电机反馈确认；夹爪不在本次范围。"
    : "六关节失能结果尚未由本次回执确认；请核对原始回执和当前使能反馈。");
  const analysed = evidence.schema_version === 1 && typeof evidence.title === "string";
  if (analysed) {
    lines.push(evidence.title);
    if (typeof evidence.detail === "string") lines.push(evidence.detail);
    if (typeof obj(evidence.scope).label === "string") lines.push(evidence.scope.label);
    const counts = Object.entries(obj(obj(evidence.feedback).counts))
      .filter(([, count]) => Number.isInteger(count) && count >= 0);
    const modeNames = {STANDBY:"待机", CAN_MOVEJ:"CAN/MOVEJ", FAULT_OR_OTHER:"异常或其他状态", TEACH_OR_OTHER:"示教或其他状态", OTHER:"其他模式"};
    if (counts.length) lines.push("保留样本统计：" + counts.map(([key, count]) => (modeNames[key] || key) + " " + count + " 帧").join("；"));
    const activity = obj(evidence.external_commands);
    lines.push(activity.evidence_available !== true || ![true, false].includes(activity.observed_positive_counters)
      ? "这份回执未提供可核对的控制报文计数；不能据此判断是否存在其他发送者。"
      : activity.observed_positive_counters === true
      ? "保留样本内有控制报文计数；发送设备身份未确认，不直接认定为主臂抢占。"
      : "保留样本内其他控制报文计数为 0；未见报文不等于已证明物理隔离。");
    if (typeof evidence.next_step === "string") lines.push("下一步：" + evidence.next_step);
  } else if (["failed", "blocked"].includes(state)) {
    lines.push("请求未完成；当前回执缺少结构化过程统计，尚不能判断是否从未进入 CAN、短暂进入后退出或存在其他原因。完整原因保留在原始字段。");
  } else if (["succeeded", "completed"].includes(state)) {
    lines.push("该请求已完成；这不证明现在仍保持同一模式或已经允许运动。");
  } else if (hasRequest) {
    lines.push("等待该次任务的最终回执。");
  }
  const report = job.report || obj(job.result).report || linkedRequest.report;
  if (typeof report === "string" && report) lines.push("原始报告：" + report);
  return {headline, context, lines, analysed, timestamp: timestamp || null, receipt,
    raw_details: {job: hasJob ? job : null, matching_requested: matches ? requested : null}};
}
// MODE_RECEIPT_VIEW_END
function controllerModeDisplay(m, online) {
  // Presentation only: keep the last decoded identity separate from freshness.
  // Do not change backend classification/capabilities or smooth actual changes.
  const labels = { UNKNOWN: "未知", FAULT: "控制器故障", TEACH: "示教", STANDBY: "待机", CAN_MOVEJ: "CAN / MOVEJ", OTHER: "其他模式" };
  const rawValid = typeof m.raw_status_hex === "string" && /^[0-9a-fA-F]{16}$/.test(m.raw_status_hex);
  const reportedValid = rawValid && (m.last_reported_valid === true ||
    // A fresh response from an older backend is still usable during upgrades.
    (m.last_reported_valid === undefined && m.fresh === true && Object.hasOwn(labels, m.classification)));
  const reported = reportedValid && Object.hasOwn(labels, m.last_reported_classification)
    ? m.last_reported_classification : reportedValid && Object.hasOwn(labels, m.classification) ? m.classification : "UNKNOWN";
  const fresh = online && reported !== "UNKNOWN" && m.fresh === true &&
    m.classification === reported && isNumber(m.age_status_s) && m.age_status_s >= 0;
  const known = reported !== "UNKNOWN";
  const name = labels[reported];
  const freshnessLabel = !online ? "通道未连接 · 当前未核对" : fresh ? "新鲜反馈" : known ? "反馈过期 · 等待刷新" : "待读数 · 模式未核对";
  return {
    fresh,
    stateLabel: !online ? "UNKNOWN · 通道未连接" : known ? reported + " · " + (fresh ? name : "最后报告") : "UNKNOWN · 未知",
    kind: !online ? "neutral" : fresh ? reported === "CAN_MOVEJ" ? "good" : reported === "FAULT" ? "error" : reported === "STANDBY" ? "neutral" : "warn" : "warn",
    freshnessLabel,
    actualLabel: known ? (fresh ? name : "最后真实报告：" + name) : "尚无可识别的真实模式",
    topLabel: known ? (fresh ? name : "最后报告 " + name) : "未知",
    reason: !online ? "通道未连接；保留的原始字段只供历史核对，不能作为执行依据。" : fresh
      ? m.reason || "来自完整真实状态帧；运动许可仍由后端单独核验。"
      : known ? "模式名称来自最后一帧真实 0x2A1；当前反馈已过期，等待新读数。过期不表示控制器切回待机，也不能作为执行依据。"
        : m.reason || "尚无完整、可核对的模式反馈。",
  };
}
function controllerActivityDisplay(m, online) {
  // This is an observation window, not an identification of the transmitting node.
  const windowS = isNumber(m.external_activity_window_s) && m.external_activity_window_s > 0
    ? m.external_activity_window_s : null;
  const known = windowS !== null && Array.isArray(m.external_activity);
  const rows = known ? m.external_activity : [];
  const windowLabel = windowS === null ? "" : "近 " + number(windowS, 0) + " s";
  const header = !online ? "通道未连接 · 活动待核对" : !known ? "等待控制来源活动观察" : rows.length
    ? windowLabel + " 仍有控制活动 · 等待安静" : windowLabel + " 未见控制帧 · 安静观察";
  const details = rows.length ? rows.map(row => {
    const age = isNumber(row.last_age_s) && row.last_age_s >= 0 ? number(row.last_age_s, 3) + " s" : "未核对";
    const count = Number.isInteger(row.count) && row.count >= 0 ? row.count : "未核对";
    return (row.can_id || "CAN ID 未核对") + " · 最近接收 " + age + " · 观察器累计 " + count + " 帧" +
      (row.high_follow_observed === true ? "\n高跟随候选：该帧请求子模式 0xAD" : "") +
      "\n载荷：" + (row.payload_hex || "未提供") + "\n来源：总线观测，发送者未识别";
  }).join("\n\n") : !online ? "当前未连接，控制报文统计尚未核对；不能据此认定总线安静。"
    : known ? "本观察窗没有可列出的控制报文。" : "尚无包含时间窗的控制来源活动数据。";
  return { label: header, kind: rows.length || !online || !known ? "warn" : "neutral", details,
    note: (!online ? "下列内容属于历史快照。" : "") + "总线观测可能包含本机回环副本，不能按 CAN ID 确认发送者。观察窗口与执行所需的新鲜度是两项独立判断。累计帧数不是窗口内帧数；未见控制帧不能证明物理隔离。切换许可仍以右侧后端条件为准。" };
}
function renderCamera(role, c, connected, excluded = false) {
  const span = document.querySelector('[data-camera-state="' + role + '"]'),
    detail = document.querySelector('[data-camera-detail="' + role + '"]'),
    img = document.querySelector('[data-camera="' + role + '"]'),
    empty = img.nextElementSibling;
  const emptyLabel = empty.querySelector("span");
  if (emptyLabel) emptyLabel.textContent = excluded ? "本次不采集此相机" : "等待真实相机图像";
  if (excluded) {
    cameraAvailable[role] = false;
    span.textContent = "本次未采集";
    detail.textContent = "不在当前录制范围 · 不补帧";
    img.hidden = true;
    empty.hidden = false;
    return;
  }
  const available = connected && c.fresh === true;
  cameraAvailable[role] = available;
  span.textContent = available
    ? "真实图像"
    : c.fresh === false
      ? "图像过期／缺失"
      : "无实时源";
  detail.textContent =
    "图像年龄 " +
    (isNumber(c.age_s) ? number(c.age_s, 3) + " s" : "—") +
    (isNumber(c.frames) ? " · " + c.frames + " 帧" : "");
  if (!available) {
    img.hidden = true;
    empty.hidden = false;
    return;
  }
  const stamp = c.frames ?? c.wall_ns ?? Date.now();
  if (
    !document.hidden && currentPage === "teleop" && backendOnline &&
    lastCameraStamp[role] !== stamp &&
    !cameraRequestBusy[role] &&
    Date.now() - (lastCameraRequest[role] || 0) >= 500
  ) {
    lastCameraStamp[role] = stamp;
    lastCameraRequest[role] = Date.now();
    cameraRequestBusy[role] = true;
    const requestEpoch = stateEpoch;
    img.onload = () => {
      cameraRequestBusy[role] = false;
      img.hidden = requestEpoch !== stateEpoch || document.hidden || currentPage !== "teleop" || !cameraAvailable[role] || !backendOnline;
      empty.hidden = !img.hidden;
    };
    img.onerror = () => {
      cameraRequestBusy[role] = false;
      if (requestEpoch !== stateEpoch || document.hidden) return;
      img.hidden = true;
      empty.hidden = false;
      span.textContent = "图像请求失败";
    };
    img.src = "/api/image/" + role + "?v=" + encodeURIComponent(stamp);
  }
}
function renderJob(job) {
  if (!job || typeof job !== "object") return;
  latestJob = job;
  const action = job.action || "任务",
    state = job.state || job.status || "已提交",
    name = jobActionLabel(job),
    id = job.id || job.job_id;
  const outcome = readResult(job.result || job.output || {});
  if (typeof collectionWorkbench !== "undefined") collectionWorkbench?.onJob(job);
  if (["mode_takeover", "mode_reset", "mode_high_follow", "mode_standby", "mode_disable"].includes(action)) {
    const history = modeReceiptView(job);
    text("mode-job-receipt", history.lines.join("\n"));
    text("mode-requested", history.headline);
    text("mode-history-context", history.context);
  }
  const states = {
    queued: "等待执行",
    pending: "等待执行",
    running: "执行中",
    succeeded: "任务完成",
    completed: "任务完成",
    failed: "失败",
    blocked: "阻塞",
    cancelled: "已取消",
  };
  if (action === "model_start") {
    states.succeeded = "启动请求完成 · 就绪另行核对";
    states.completed = states.succeeded;
  }
  text("current-job", (states[state] || state) + " · " + name);
  text(
    "job-summary",
    name +
      " / " +
      (states[state] || state) +
      (id ? " / " + id : "") +
      (job.error
        ? " — " +
          (typeof job.error === "string"
            ? job.error
            : JSON.stringify(job.error))
        : "") +
      (job.report ? " / 报告：" + job.report : ""),
  );
  if (
    ["succeeded", "completed", "failed", "blocked", "cancelled"].includes(state)
  ) {
    const key = "terminal-" + id + "-" + state;
    const firstTerminal = !seenEvents.has(key);
    addEvent(
      name +
        "：" +
        (states[state] || state) +
        (job.error ? " · " + String(job.error) : "") +
        (["succeeded", "completed"].includes(state)
          ? "。请查看回执确认实际结果。"
          : ""),
      ["failed", "blocked"].includes(state) ? "error" : state === "cancelled" ? "warn" : "success",
      job.finished_at || job.updated_at || job.created_at,
      key,
    );
    if (firstTerminal && ["failed", "blocked"].includes(state)) {
      const page = actionPage(action, jobPages.get(id) || currentPage);
      const message = ["mode_takeover", "mode_reset"].includes(action)
        ? "历史请求结果 · " + modeReceiptView(job).lines.join("；")
        : name + "：" + (states[state] || state) + (job.error ? " · " + (typeof job.error === "string" ? job.error : JSON.stringify(job.error)) : "。请查看完整回执。");
      rememberActionError(message, page, action, { source: "job", jobId: id,
        at: job.finished_at || job.updated_at || job.created_at || null });
    }
    if (firstTerminal && ["succeeded", "completed"].includes(state)) {
      const page = actionPage(action, jobPages.get(id) || currentPage);
      const error = pageActionErrors.get(page);
      if (error?.action === action && (!error.jobId || error.jobId !== id)) {
        $(page + "-action-error").hidden = true;
        pageActionErrors.delete(page);
      }
    }
    if (action === "shadow" || action === "shadow_fp32")
      text(
        "shadow-output",
        JSON.stringify(job.result || job.output || job, null, 2),
      );
    if (action === "record_list")
      renderEpisodes(
        outcome.episodes || outcome.recordings || job.result || job.output,
      );
    if (id === pendingJob) pendingJob = null;
  }
  text(
    "raw-state",
    JSON.stringify(
      { state: lastState, last_submitted_job: latestJob },
      null,
      2,
    ),
  );
}
function renderEpisodes(data) {
  const list = Array.isArray(data)
    ? data
    : Array.isArray(data?.result)
      ? data.result
      : [];
  const el = $("episode-list");
  el.replaceChildren();
  if (!list.length) {
    el.className = "empty-line";
    el.textContent = "本次回执未列出 episode；请查看完整回执。";
    return;
  }
  el.className = "";
  for (const ep of list) {
    const row = document.createElement("div");
    row.className = "episode-row";
    const main = document.createElement("div"),
      label = document.createElement("span"),
      small = document.createElement("small"),
      meta = document.createElement("span");
    const rawPath = typeof ep === "object" && ep !== null
      ? typeof ep.raw_storage === "string" ? ep.raw_storage : object(ep.raw_storage).path || object(ep.raw_storage).episode : null;
    label.textContent =
      typeof ep === "string" ? ep : ep.episode_id || ep.id || ep.name || ep.path || rawPath || "回执未列出身份";
    small.textContent =
      typeof ep === "string" ? "U 盘保存路径" : ep.task || ep.path || rawPath || "";
    meta.textContent =
      typeof ep === "object" ? ep.status || ep.category || "已列出" : "";
    main.append(label, small);
    row.append(main, meta);
    el.append(row);
  }
}
function drawChart() {
  const canvas = $("joint-chart");
  if (!canvas.clientWidth) return;
  const ratio = window.devicePixelRatio || 1,
    w = canvas.clientWidth,
    h = 200;
  canvas.width = w * ratio;
  canvas.height = h * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "#bcbcbc";
  ctx.lineWidth = 1;
  for (let i = 1; i < 5; i++) {
    const y = (i * h) / 5;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }
  ctx.fillStyle = "#444444";
  ctx.font = "10px Consolas";
  if (jointHistory.length < 2) {
    ctx.fillText("等待真实关节样本", 20, 30);
    return;
  }
  const values = jointHistory.flatMap((s) => s.q),
    lo = Math.min(...values) - 5,
    hi = Math.max(...values) + 5,
    first = jointHistory[0].time,
    last = jointHistory.at(-1).time;
  const patterns = [[], [7, 3], [2, 3], [10, 3, 2, 3], [4, 3, 1, 3], [12, 4]];
  for (let j = 0; j < 6; j++) {
    ctx.strokeStyle = "#111111";
    ctx.setLineDash(patterns[j]);
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    jointHistory.forEach((s, i) => {
      const x = 20 + ((s.time - first) / Math.max(1, last - first)) * (w - 40),
        y = 22 + ((hi - s.q[j]) / (hi - lo)) * (h - 44);
      if (i === 0 || s.time - jointHistory[i - 1].time > 3500) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.fillStyle = "#444444";
  ctx.fillText(number(hi, 1) + "°", 7, 14);
  ctx.fillText(number(lo, 1) + "°", 7, h - 8);
}
let stateBusy = false, stateEpoch = 0, stateRequest = null;
const PAGE_SNAPSHOT_TIMEOUT_MS = 2000;
let pageSnapshotReceivedAt = null, pageSnapshotRequestAt = null;
let pageStatePollTimer = null;
const PAGE_STATE_PERIOD_MS = 400;
function statePollDelay(startedAt, finishedAt) {
  return Math.max(0, PAGE_STATE_PERIOD_MS - (finishedAt - startedAt));
}
function scheduleStatePoll(delay = 0) {
  clearTimeout(pageStatePollTimer);
  pageStatePollTimer = null;
  if (!document.hidden) pageStatePollTimer = setTimeout(() => {
    pageStatePollTimer = null;
    void pollState();
  }, delay);
}
let stateRequestStartedAt = null, pageSnapshotExpired = false;
function expirePageSnapshot() {
  pageSnapshotExpired = true;
  stateEpoch += 1;
  stateRequest?.abort();
  stateRequest = null;
  stateRequestStartedAt = null;
  stateBusy = false;
  pausePageRefresh(false, true);
  scheduleStatePoll();
}
function pageSnapshotWatchdog() {
  if (document.hidden) return;
  const now = performance.now();
  const requestExpired = stateRequestStartedAt !== null && now - stateRequestStartedAt > PAGE_SNAPSHOT_TIMEOUT_MS;
  const snapshotExpired = !pageSnapshotExpired && pageSnapshotReceivedAt !== null && now - pageSnapshotReceivedAt > PAGE_SNAPSHOT_TIMEOUT_MS;
  if (requestExpired) expirePageSnapshot();
  else if (snapshotExpired) {
    // Expire old display data without cancelling a newer GET still in budget.
    pageSnapshotExpired = true;
    pausePageRefresh(false, true);
    if (!stateBusy) scheduleStatePoll();
  }
}
function pausePageRefresh(hidden, expired = false) {
  // Suspend only this tab's display and read requests, never the robot task.
  backendOnline = false;
  renderUsbStorage(lastState.usb_storage, false);
  if (typeof collectionWorkbench !== "undefined") collectionWorkbench?.onConnection(false);
  actualFresh = false;
  lastCameraStamp = {};
  text("feedback-age", expired ? "历史年龄 · 等待新状态" : "历史年龄 · 当前未核对");
  const waiting = hidden ? "后台页面暂停刷新" : expired ? "页面快照过期 · 等待新状态" : "正在重新获取状态";
  const explanation = hidden
    ? "本标签页已在后台，暂停状态和预览请求。已下发的实机任务继续由机器人端执行；这不是停止命令。"
    : expired
      ? "页面超过 2 秒未取得可用新状态，等待新的只读响应。保留的数字属于历史快照；常规操作暂不可用，已有会话仍可单独请求停止新目标。实机任务不受页面暂停影响。"
      : "页面已恢复前台，等待新的只读状态响应。保留的数字属于历史快照；常规操作暂不可用，已有会话仍可单独请求停止新目标。";
  text("page-refresh-status", explanation);
  $("page-refresh-status").dataset.health = "waiting";
  setPill("backend-pill", waiting, "warn");
  renderControllerMode(lastState);
  const oldRight = object(object(unwrap(lastState.status || {})).arms?.right);
  renderGripperReadout(oldRight, false);
  renderGripperHealth(oldRight, false);
  setPill("mode-freshness", waiting + " · 当前未核对", "warn");
  text("right-mode-freshness", waiting + " · 当前未核对");
  text("mode-reason", explanation);
  text("connection-status", waiting);
  $("connection-dot").classList.remove("live");
  setPill("motion-feedback-state", "保留历史数字 · " + waiting, "warn");
  text("robot-source", explanation);
  robotView?.setActual(actual, { valid: false, source: "page-refresh-paused" });
  for (const role of ["right", "left"]) {
    setPill("follower-" + role + "-feedback", "历史数字 · " + waiting, "warn");
    text("follower-" + role + "-ages", "页面暂停或等待新响应，当前反馈年龄尚未核对。");
    text("follower-" + role + "-mode", "历史状态 · 当前模式尚未核对；恢复刷新后读取新反馈。");
  }
  const oldHub = unwrap(lastState.hub || unwrap(lastState.status || {}).shared_producer || {});
  renderStageObserver(oldHub.stage_observer, lastState, false);
  renderRecoveryState(oldHub.recovery, false);
  renderTaskWorkspace(oldHub.task_state, false);
  renderPerception(oldHub.perception || oldHub.alignment, false, 0, oldHub.visual_servo);
  for (const role of ["front", "right", "left"]) renderCamera(role, {}, false);
  refreshCapabilities();
}
function handlePageVisibility() {
  clearTimeout(pageStatePollTimer);
  pageStatePollTimer = null;
  // Invalidate old GET responses before aborting them. POST submissions have
  // their own controllers and are not cancelled by changing browser tabs.
  stateEpoch += 1;
  stateRequest?.abort();
  stateRequest = null;
  stateRequestStartedAt = null;
  pageSnapshotExpired = true;
  stateBusy = false;
  pausePageRefresh(document.hidden);
  if (!document.hidden) void pollState();
}
async function pollState() {
  // Explicit button follow-up calls obey this guard too. Submitted jobs remain
  // on the server and their receipts are fetched when this tab becomes visible.
  refreshTaskDisplayAge();
  if (document.hidden || stateBusy) return;
  clearTimeout(pageStatePollTimer);
  pageStatePollTimer = null;
  stateBusy = true;
  const epoch = stateEpoch;
  const request = new AbortController();
  stateRequest = request;
  const requestedAt = performance.now();
  stateRequestStartedAt = requestedAt;
  const requestTimeout = setTimeout(() => request.abort(), 10000);
  const current = () => epoch === stateEpoch && !document.hidden;
  try {
    const state = await api("/api/state", { signal: request.signal });
    if (!current()) return;
    if (performance.now() - requestedAt > PAGE_SNAPSHOT_TIMEOUT_MS) {
      expirePageSnapshot();
      return;
    }
    pageSnapshotReceivedAt = performance.now();
    pageSnapshotRequestAt = requestedAt;
    stateRequestStartedAt = null;
    pageSnapshotExpired = false;
    backendOnline = true;
    $("page-refresh-status").dataset.health = "live";
    setPill("backend-pill", "本地后端在线", "good");
    text("page-refresh-status", "当前页面自动读取状态；相机预览仅在前台的“遥操作与采集”页请求。切换标签页不会停止实机任务。");
    renderState(state);
    if (pendingJob) {
      const cachedJob = Array.isArray(state.jobs) ? state.jobs.find(job => job.id === pendingJob) : null;
      if (cachedJob) renderJob(cachedJob);
      else {
        const job = await api("/api/jobs/" + encodeURIComponent(pendingJob), { signal: request.signal });
        if (!current()) return;
        renderJob(job.job || job);
      }
    }
  } catch (error) {
    if (!current()) return;
    if (backendOnline)
      addEvent("页面与后端连接中断：" + error.message, "error");
    backendOnline = false;
    renderUsbStorage(lastState.usb_storage, false);
    if (typeof collectionWorkbench !== "undefined") collectionWorkbench?.onConnection(false);
    renderControllerMode(lastState);
    renderDualFollowerTelemetry(lastState);
    const oldRight = object(object(unwrap(lastState.status || {})).arms?.right);
    renderGripperReadout(oldRight, false);
    renderGripperHealth(oldRight, false);
    const oldHub = unwrap(lastState.hub || unwrap(lastState.status || {}).shared_producer || {});
    renderStageObserver(oldHub.stage_observer, lastState, false);
    renderRecoveryState(oldHub.recovery, false);
    renderTaskWorkspace(oldHub.task_state, false);
    renderPerception(oldHub.perception || oldHub.alignment, false, 0, oldHub.visual_servo);
    actualFresh = false;
    $("page-refresh-status").dataset.health = "error";
    text("page-refresh-status", "页面刷新失败：" + error.message + "。保留历史数字，等待新响应；常规操作暂不可用，停止新目标单独判断。");
    setPill("backend-pill", "后端不可达", "error");
    text("connection-status", "后端不可达");
    $("connection-dot").classList.remove("live");
    setPill("motion-feedback-state", "连接中断 · 无新反馈", "warn");
    text("robot-source", "连接中断：保留历史数字，不作为当前真实姿态。");
    robotView?.setActual(null, { valid: false, source: "disconnected" });
    for (const role of ["front", "right", "left"])
      renderCamera(role, {}, false);
    refreshCapabilities();
  } finally {
    clearTimeout(requestTimeout);
    if (epoch === stateEpoch && stateRequest === request) {
      stateBusy = false;
      stateRequest = null;
      stateRequestStartedAt = null;
      scheduleStatePoll(statePollDelay(requestedAt, performance.now()));
    }
  }
}
// Visibility lifecycle never invokes connect, a POST, a stop or a robot reset.
// ATTENTION_VIEW_BEGIN: display summaries only; never change execution gates.
function attentionContext(state, online, hidden) {
  if (hidden) return {kind: "warn", title: "页面已暂停刷新", message: "切回页面后重新核对当前状态。页面暂停不会停止机器人任务。"};
  if (!online) return {kind: "warn", title: "等待新的页面状态", message: "保留的数字和回执属于历史记录；当前条件尚未核对。"};
  const storage = state.usb_storage || {};
  if (state.connected === true && storage.status !== "selected")
    return {kind: "warn", title: "录制目标尚未就绪", message: storage.error || storage.reason || "请检查实际 U 盘与挂载；暂不能确认录制位置。"};
  if (state.stop_latch?.latched === true)
    return {kind: "warn", title: "停止锁存已生效", message: "新的运动目标受阻；消息“已阅”不会解除停止锁存。"};
  if (state.connected !== true) return {kind: "info", title: "下一步：连接 AgileX，核对只读状态",
    message: storage.selected ? "已选择 " + (storage.selected.display_name || storage.selected.uuid) + "；设备通路与运行条件仍待核验。" : "先检查实际挂载的 U 盘，再建立只读连接。"};
  const jobs = Array.isArray(state.jobs) ? state.jobs : Object.values(state.jobs || {});
  const active = jobs.find(job => ["running", "queued", "pending", "unknown"].includes(job.state || job.status));
  if (active) return {kind: "running", title: "当前有任务等待实际结果", message: "请先查看任务回执；任务已提交不等于执行成功。"};
  if (state.hub?.api_reachable !== true) return {kind: "warn", title: "下一步：核对共享采集通路",
    message: "SSH 已连接；共享采集与真实新鲜反馈尚未核对。最近失败原因保留在消息记录中。"};
  return {kind: "info", title: "设备通路已连接，按当前页面核对任务条件",
    message: "相机、关节、夹爪与录制条件分别验收；Jev 实时联合监督尚未接入。"};
}
function attentionJobEvent(job, labels = {}) {
  const state = job?.state || job?.status;
  if (!["succeeded", "completed", "failed", "blocked", "cancelled"].includes(state)) return null;
  const id = job.id || job.job_id || [job.action, job.created_at].filter(Boolean).join(":");
  if (!id) return null;
  const failed = ["failed", "blocked"].includes(state);
  const name = labels[job.action] || job.action || "任务";
  const result = failed ? (state === "blocked" ? "被阻塞" : "失败") : state === "cancelled" ? "已取消" : job.action === "model_start" ? "启动请求完成 · 就绪另行核对" : "任务完成 · 实际结果见回执";
  const error = job.error ? (typeof job.error === "string" ? job.error : JSON.stringify(job.error)) : "";
  const reason = error.includes("FileNotFoundError")
    ? "程序报告找不到文件或路径；当前回执未提供缺失路径。\n原始原因：" + error : error;
  return {key: "terminal-" + id + "-" + state, kind: failed ? "error" : state === "cancelled" ? "warn" : "success",
    title: name + " · " + result, message: (reason || "这是任务回执，不代表其他设备条件或任务已经通过。") + "\n任务：" + id,
    at: job.finished_at || job.updated_at || job.created_at};
}
// ATTENTION_VIEW_END
function refreshAttentionContext() {
  if (!attentionCenter) return;
  attentionCenter.updateContext(attentionContext(lastState, backendOnline, document.hidden));
}
function refreshAttentionJobs(state) {
  if (!attentionCenter) return;
  const jobs = Array.isArray(state.jobs) ? state.jobs : Object.values(state.jobs || {});
  for (const job of jobs) {
    const entry = attentionJobEvent(job, actionNames);
    if (entry) attentionCenter.publish({...entry, page: actionPage(job.action), actionLabel: "查看相关页面"});
  }
}
// UI-only module. A failed load leaves the existing error cards and event log intact.
import("./attention-center.mjs").then(({createAttentionCenter}) => {
  attentionCenter = createAttentionCenter({document, openPage});
  for (const entry of events) attentionCenter.publish({...entry, kind: entry.kind || "info",
    title: entry.kind === "error" ? "需要查看的错误消息" : "操作消息"});
  refreshAttentionContext();
  if (backendOnline) refreshAttentionJobs(lastState);
}).catch(error => {
  addEvent("重点消息组件未加载：" + error.message + "。原任务与事件记录仍可查看。", "error");
});
// PRECISION_WORKBENCH_BEGIN
let precisionDraftInitialized = false;
let precisionDurationRoute = null, precisionLegacyDuration = null, precisionV2Duration = "40";
function precisionAction(action, draft) {
  return draft.precision === "V2_FP32"
    ? ({precision_acceptance:"v2_acceptance",precision_deploy:"v2_deploy"}[action] || action) : action;
}
function precisionRouteLabel(route) {
  return {FP32:"FP32 + RTC",BF16:"BF16 + 轨迹优化",V2_FP32:"FP32 + Realtime-VLA V2"}[route] || route;
}
function jobActionLabel(job) {
  const name=actionNames[job.action] || job.action || "任务", duration=job.params?.duration;
  return job.action === "v2_deploy" && Number.isFinite(duration) && duration>=1 && duration<=1800
    ? name+"（"+duration+"秒）" : name;
}
function syncPrecisionDuration() {
  const route=$("precision-mode").value, input=$("precision-duration"), v2=route === "V2_FP32";
  if (route !== precisionDurationRoute) {
    if (v2) {
      precisionLegacyDuration=input.value;
      input.value=precisionV2Duration;
    } else if (precisionDurationRoute === "V2_FP32") {
      precisionV2Duration=input.value;
      input.value=precisionLegacyDuration ?? "30";
    }
    precisionDurationRoute=route;
  }
  input.readOnly=false;
  input.min="1";
  input.max="1800";
  input.title="1～1800秒，最长半小时；启动、停止和保存另计。";
  $("precision-duration-presets").hidden=!v2;
  const preset=$("precision-duration-preset");
  preset.value=["40","60","300","600","1800"].includes(input.value) ? input.value : "";
}
function precisionDraft() {
  const draft={precision:$("precision-mode").value,step:Number($("precision-step").value),task:$("precision-prompt").value,
    duration:Number($("precision-duration").value),leader_isolated:true,path_reviewed:true};
  if (draft.precision === "V2_FP32" && lastState.precision_workbench?.v2_profile?.manual_run_without_nosend_supported === true)
    draft.operator_skip_no_motion_acceptance=true;
  return draft;
}
function precisionParams(action,draft) {
  if (action === "precision_release") return {};
  if (!["FP32","BF16","V2_FP32"].includes(draft.precision) || ![2000,5000,10000,15000,20000].includes(draft.step)) throw new Error("请选择已登记的路线和检查点。");
  const v2=draft.precision === "V2_FP32";
  if ((["v2_acceptance","v2_deploy"].includes(action) && !v2) || (v2 && ["precision_acceptance","precision_deploy"].includes(action))) throw new Error("部署路线与操作不一致，请刷新后重试。");
  if (typeof draft.task !== "string" || !draft.task.trim() || draft.task.length > 200 || /[\u0000-\u001f\u007f]/.test(draft.task)) throw new Error("Prompt须为1～200字的有效单行文本。");
  const value={precision:v2 ? "FP32" : draft.precision,step:draft.step,task:draft.task};
  if (["precision_deploy","v2_deploy"].includes(action)) {
    if (!Number.isFinite(draft.duration) || draft.duration < 1 || draft.duration > 1800) throw new Error("运行时长须为1～1800秒，最长半小时。");
    if (draft.leader_isolated !== true || draft.path_reviewed !== true) throw new Error("执行前请确认主从隔离和现场条件。");
    Object.assign(value,{duration:draft.duration,leader_isolated:true,path_reviewed:true});
    if (action === "v2_deploy" && draft.operator_skip_no_motion_acceptance === true)
      value.operator_skip_no_motion_acceptance=true;
  }
  return value;
}
function precisionV2ExecutionView(component, draft) {
  const profile=component.v2_profile || {}, row=component.models?.FP32, acceptance=profile.acceptance;
  if (component.fresh !== true || component.error) return {passed:false,label:"V2当前状态待刷新",reason:component.error || "当前模型状态尚未新鲜核实，请等待刷新。"};
  if (profile.fresh !== true || profile.ready !== true || profile.error) return {passed:false,label:"V2运行器待核实",reason:profile.error || "独立V2运行器尚未新鲜核实。"};
  if (!precisionV2WindowContractMatches(profile)) return {passed:false,label:"V2窗口合同待核实",reason:"独立V2运行器1～1800秒窗口合同尚未匹配，请等待刷新。"};
  if (!row?.ready || row.step !== draft.step || typeof row.fingerprint !== "string" || !row.fingerprint)
    return {passed:false,label:"V2模型身份待核实",reason:"请先加载所选FP32检查点并核实模型身份。"};
  if (draft.operator_skip_no_motion_acceptance === true) {
    if (profile.manual_run_without_nosend_supported !== true)
      return {passed:false,label:"V2人工直接运行未支持",reason:"当前V2运行器不支持人工跳过NO_SEND。"};
    return {passed:false,manualReady:true,label:"本版本跳过NO_SEND，人工直接运行（未验收）",reason:null};
  }
  if (acceptance?.validated !== true || acceptance.step !== draft.step || acceptance.task !== draft.task || acceptance.model_fingerprint !== row.fingerprint)
    return {passed:false,label:"V2尚无匹配验收",reason:"此V2执行版本、FP32模型实例和Prompt尚无匹配NO_SEND验收；请点击无运动验收。"};
  return {passed:true,label:"V2当前版本无运动验收通过",reason:null};
}
function precisionV2WindowContractMatches(profile) {
  const contract=profile?.window_contract || {};
  return contract.min_duration_s===1 && contract.max_duration_s===1800 && contract.default_duration_s===40 && contract.acceptance_duration_s===30;
}
function precisionV2TrackingNote(profile) {
  const policy=profile?.tracking_policy;
  if (profile?.fresh!==true || policy?.scope!=="V2_ONLY" || !Number.isFinite(policy.threshold_deg) || !Number.isFinite(policy.duration_ms))
    return "当前跟踪保护参数待核实。";
  const action=policy.action === undefined ? "stop" : policy.action;
  if (!["stop","diagnostic_only"].includes(action)) return "当前跟踪报警处理方式待核实。";
  const trigger=`误差超过${policy.threshold_deg}°并持续${policy.duration_ms} ms`;
  return action === "diagnostic_only"
    ? `当前J1–J6跟踪报警：${trigger}仅记录报警，继续运行。`
    : `当前J1–J6跟踪保护：${trigger}停止新增目标。`;
}
function precisionV2PhysicalText(result) {
  if (!result) return "V2最近运动：尚无真实运行回执。";
  const elapsed=typeof result.elapsed_s === "number" && Number.isFinite(result.elapsed_s) ? "；实际运行 "+result.elapsed_s.toFixed(2)+" 秒" : "";
  const duration=[result.policy_window_s,result.window_s,result.requested_duration_s,result.duration_s].find(value=>Number.isFinite(value) && value>=1 && value<=1800)
    ?? (result.status === "PHYSICAL_40S_COMPLETED_AND_POSITION_SETTLED" ? 40 : null);
  const window=duration === null ? "请求窗口（时长未核实）" : duration+"秒窗口";
  return "V2最近运动："+(result.completed_window === true ? "完整"+window+"已完成；任务成败需实物核验" : "未完成"+window)+elapsed+(result.error ? "；"+result.error : "")+"。";
}
function precisionFP32ExecutionView(execution, step) {
  const current = execution || {};
  if (current.health_fresh !== true && /Connection refused|Errno 111|拒绝连接/i.test(String(current.error || "")))
    return {passed:false,label:"机器人模型通路未连接",reason:"4090模型可能已加载，但机器人侧通路未连通；点击无运动验收会先检查并恢复通路，无需卸载模型。"};
  if (current.selected_step !== step || current.health_fresh !== true)
    return {passed:false,label:"当前执行验收待核对",reason:"当前FP32执行状态尚未新鲜核实；请等待状态刷新。"};
  if (current.acceptance_passed !== true) {
    const stale = String(current.acceptance_reason || "").includes("acceptance is stale");
    return {passed:false,label:stale ? "执行版本已更新，请重新验收" : "当前执行版本尚未通过验收",
      reason:stale ? "执行代码已更新，旧验收不能用于当前版本；请点击无运动验收。"
        : "当前FP32执行版本尚未通过验收；请点击无运动验收。"};
  }
  return {passed:true,label:"当前执行版本验收通过",reason:null};
}
function precisionDraftCapability(action,cap,draft,component,execution) {
  if (!cap.available) return cap;
  try {
    const p=precisionParams(action,draft);
    if (action === "precision_release") return cap;
    if (!(component.candidates || []).some(c=>c.step===p.step && c.available===true)) throw new Error("所选checkpoint文件尚未核实可用。");
    if (action === "precision_load") return cap;
    const row=component.models?.[p.precision];
    if (!row?.ready || row.step !== p.step) throw new Error("选择与实际加载不同；请先加载所选模型。");
    if (["v2_acceptance","v2_deploy"].includes(action)) {
      if (component.fresh !== true || component.error) throw new Error(component.error || "当前模型状态尚未新鲜核实，请等待刷新。");
      if (component.v2_profile?.fresh !== true || component.v2_profile.ready !== true || component.v2_profile.error) throw new Error(component.v2_profile?.error || "独立V2运行器尚未新鲜核实。");
      if (!precisionV2WindowContractMatches(component.v2_profile)) throw new Error("独立V2运行器1～1800秒窗口合同尚未匹配，请等待刷新。");
      if (action === "v2_deploy") {
        const validated=precisionV2ExecutionView(component,draft);
        if (!validated.passed && !validated.manualReady) throw new Error(validated.reason);
      }
      return cap;
    }
    if (!component.robot_profile?.ready) throw new Error(component.robot_profile?.error || "独立运行器尚未核实。");
    if (action === "precision_deploy") {
      if (p.precision === "BF16" && !(component.robot_profile.admissions || []).some(a=>a.validated===true && a.step===p.step && a.task===p.task && a.model_fingerprint===row.fingerprint)) throw new Error("此BF16模型实例和Prompt尚无匹配NO_SEND验收；请先验证。");
      if (p.precision === "FP32") {
        const validated=precisionFP32ExecutionView(execution,p.step);
        if (!validated.passed) throw new Error(validated.reason);
        if (row.acceptance_passed !== true) throw new Error("当前FP32检查点验收尚未同步完成；请等待状态刷新。");
      }
    }
    return cap;
  } catch(error) { return {available:false,reason:error.message}; }
}
function refreshPrecision() {
  if (!$("precision-start")) return;
  for (const [id,baseAction] of [["load","precision_load"],["acceptance","precision_acceptance"],["start","precision_deploy"],["release","precision_release"]]) {
    const action=precisionAction(baseAction,precisionDraft());
    const cap=capability(action),button=$("precision-"+id);
    button.disabled=submitting || !cap.available;button.title=cap.reason || actionNames[action];
  }
  $("precision-stop").disabled=$("stop-button").disabled;
  const ready=capability(precisionAction("precision_deploy",precisionDraft()));
  text("precision-next",ready.available ? (precisionDraft().operator_skip_no_motion_acceptance === true
    ? "本版本跳过NO_SEND，人工直接运行。点击开始运行会真实运动。" : "已就绪，点击开始运行。") : ready.reason || "等待实际条件。");
}
function precisionBridgeStepWarning(job) {
  const r=job?.runner_progress;
  if (job?.action !== "v2_deploy" || job.state !== "running" ||
      r?.schema !== "tiger-v2-reference-step-warning-v1" || r.status !== "V2_REFERENCE_STEP_WARNING" ||
      r.diagnostic?.action !== "diagnostic_only" || r.diagnostic?.exceeded !== true ||
      !Number.isFinite(r.diagnostic?.maximum_added_step_deg) || typeof r.message !== "string") return null;
  return r.message;
}
function renderPrecision(state) {
  if (!$("precision-start")) return;
  const component=state.precision_workbench || {};
  if (!precisionDraftInitialized && component.fresh === true && !component.error) {
    const actual=Object.values(component.models || {}).filter(row=>row.present && row.ready);
    if (actual.length === 1) {
      $("precision-mode").value=actual[0].precision;
      $("precision-step").value=String(actual[0].step);
      precisionDraftInitialized=true;
    }
  }
  syncPrecisionDuration();
  v2SpeedControl?.sync();
  const selected=precisionDraft();
  const v2=selected.precision === "V2_FP32", modelPrecision=v2 ? "FP32" : selected.precision;
  $("precision-route-note").hidden=!v2;
  const trackingNote=precisionV2TrackingNote(component.v2_profile);
  const timingBudget=component.v2_profile?.timing_policy?.observation_to_commit_budget_ms;
  const timingNote=component.v2_profile?.fresh===true && Number.isFinite(timingBudget) ? `模型与规划提交预算${timingBudget} ms；候选完成后事件唤醒发布。关节参考越界先修正；当前执行参考连续修正3秒停止新增目标（非硬件急停）。` : "";
  const stepPolicy=component.v2_profile?.bridge_step_policy;
  const stepNote=component.v2_profile?.fresh===true && stepPolicy?.action==="diagnostic_only" && Number.isFinite(stepPolicy.threshold_deg)
    ? `衔接额外步幅超过${stepPolicy.threshold_deg}°仅报警，不因此终止。` : "";
  const manual=component.v2_profile?.fresh===true && component.v2_profile?.manual_run_without_nosend_supported===true;
  text("precision-route-note",v2 ? "V2独立实验路线："+(manual ? "本版本跳过NO_SEND，人工直接运行（未验收）；" : "先做30秒无运动验收；")+"运行时长可选或输入1～1800秒。H50、guided RTC和夹爪100 mm上限保留。"+trackingNote+timingNote+stepNote+"选择路线和时长仅修改下一轮草稿。" : "");
  $("precision-v2-result").hidden=!v2;
  text("precision-v2-result",v2 ? precisionV2PhysicalText(component.v2_profile?.latest_physical) : "");
  if ($("h50-details")) $("h50-details").hidden=selected.precision !== "FP32";
  const loaded=Object.values(component.models || {}).filter(row=>row.present);
  const label=loaded.map(row=>row.precision+" / "+(row.step ?? "待确认")+"步 / "+({READY:"模型已加载",STOPPED:"已停止",STARTING:"加载中",LOADING:"加载中",SELECTING:"切换中",STOPPING:"停止中",FAILED:"失败"}[row.status] || row.status)).join("；") || "未加载模型";
  setPill("precision-pill",component.fresh && !component.error ? label : "模型状态待核实",loaded.some(r=>r.ready) && component.fresh && !component.error ? "good" : "neutral");
  const fp32Execution = v2 ? "执行验收："+precisionV2ExecutionView(component,selected).label+"。"
    : selected.precision === "FP32" && component.models?.FP32?.ready && component.models.FP32.step === selected.step
      ? "执行验收："+precisionFP32ExecutionView(state.h50_deployment,selected.step).label+"。" : "";
  text("precision-loaded","实际模型："+label+"。"+fp32Execution+((component.models?.[modelPrecision]?.ready && component.models[modelPrecision].step===selected.step) ? "" : "下次选择："+precisionRouteLabel(selected.precision)+" / "+selected.step+"步。")+(component.error || ""));
  text("precision-validation",v2 ? (manual ? "当前按人工授权跳过NO_SEND。启动仍核对真实模型身份、反馈、STOP与USB；这不是无运动验收通过。只有点击开始运行才发起运动；无运动验收按钮仍可单独使用。" : "V2使用上方FP32检查点与Prompt做30秒真实NO_SEND，核对时序、夹爪边界和停止/保存链路；不发运动目标。更换模型实例、检查点、Prompt或执行版本须重新验收；无运动通过不代表抓放任务完成。") : selected.precision === "BF16"
    ? "BF16验收使用上方Prompt：3次预热＋100次真实推理、平滑轨迹计算和USB保存核验，运动发送为零。更换模型实例、检查点或Prompt后重新核验。"
    : "FP32保留原路线：无运动性能验收使用原始红色方块任务；实际部署使用上方编辑的Prompt。验收不是其他任务能力保证。");
  const jobs=(state.jobs || []).filter(j=>j.action?.startsWith("precision_") || ["v2_acceptance","v2_deploy"].includes(j.action) || ["h50_load","h50_shadow","h50_acceptance","h50_deploy"].includes(j.action)),job=jobs.at(-1);
  const jobState=job?.action === "v2_deploy" && job.state === "succeeded" ? "已返回，运动完成情况见V2回执" : ({succeeded:"已完成",failed:"失败",running:"进行中",queued:"等待中",cancelled:"已取消"}[job?.state] || job?.state);
  text("precision-job",job ? jobActionLabel(job)+"："+jobState+"。"+(job.error || (job.state === "succeeded" ? "实际结果已保存" : job.progress) || "查看回执")+
    (job.params?.duration ? "；本轮 "+job.params.duration+"秒，Prompt："+job.params.task : "") : "尚未从本入口提交任务。");
  const stepWarning=precisionBridgeStepWarning(job);
  $("precision-job").classList.toggle("v2-reference-step-warning",Boolean(stepWarning));
  if (stepWarning) $("precision-job").textContent+=" 提示："+stepWarning;
  text("precision-receipt",JSON.stringify({actual_models:component.models,robot_profile:component.robot_profile,v2_profile:component.v2_profile,last_job:job},null,2));
}
function initializePrecision() {
  if (!$("precision-start")) return;
  for (const [id,baseAction] of [["load","precision_load"],["acceptance","precision_acceptance"],["start","precision_deploy"],["release","precision_release"]])
    $("precision-"+id).addEventListener("click",()=>{
      const draft=precisionDraft(),action=precisionAction(baseAction,draft);
      try { submit(action,precisionParams(action,draft)); }
      catch(error) { showActionError(error.message,"deploy",action); }
    });
  $("precision-stop").addEventListener("click",()=>$("stop-button").click());
  $("precision-duration-preset").addEventListener("change",()=>{
    const value=$("precision-duration-preset").value;
    if (!value) return;
    $("precision-duration").value=value;
    precisionDraftInitialized=true;
    renderPrecision(lastState);renderH50Deployment(lastState.h50_deployment,lastState.jobs);refreshCapabilities();
  });
  for (const id of ["mode","step","prompt","duration"])
    $("precision-"+id).addEventListener("input",()=>{
      precisionDraftInitialized=true;
      
      renderPrecision(lastState);renderH50Deployment(lastState.h50_deployment,lastState.jobs);refreshCapabilities();
    });
}
// PRECISION_WORKBENCH_END

initializePrecision();
v2SpeedControl = createV2SpeedControl({document, api,
  context: () => lastState,
  selected: () => $("precision-mode").value === "V2_FP32",
  active: () => currentPage === "deploy" && !document.hidden});
v2SpeedControl.sync();
document.addEventListener("visibilitychange", handlePageVisibility);
refreshCapabilities();
if (document.hidden) pausePageRefresh(true);
else pollState();
// Serialized completion scheduling avoids skipping 400ms ticks after slow GETs.
setInterval(pageSnapshotWatchdog, 50);
export { taskStateView, perceptionView, reviewTaskParams };

