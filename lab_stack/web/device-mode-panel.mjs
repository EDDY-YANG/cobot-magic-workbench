// Copyright (c) 2026 Yang Taige
// SPDX-License-Identifier: MIT
const devices = {
 LE: {title:"Left execution arm", zh:"左执行臂", role:"left", pair:"LT → LE"},
 RE: {title:"Right execution arm", zh:"右执行臂", role:"right", pair:"RT → RE"},
 LT: {title:"Left teaching arm", zh:"左示教臂", role:null, pair:"LT → LE"},
 RT: {title:"Right teaching arm", zh:"右示教臂", role:null, pair:"RT → RE"},
 BASE: {title:"Mobile base", zh:"移动底盘", role:null, pair:"Independent device / 独立设备"}
};
export function deviceView(state, id, online, capabilityFor) {
 const d=devices[id] || devices.RE;
 const mode=d.role ? state.controller_modes?.[d.role] : null;
 const connected=online && state.connected === true;
 const fresh=connected && mode?.fresh === true;
 let status="Unknown / 状态未知", tone="unknown";
 let reason=id==="BASE" ? "底盘状态接口尚未接入。" : !d.role ? "独立示教臂接口尚未绑定；不能从配对臂推断状态。" :
  !connected ? "连接后读取真实设备反馈。" : !fresh ? mode?.reason || "尚无新鲜、有效的模式反馈。" : "";
 if(fresh) {
   status=mode.label || mode.classification;
   tone="observed";
   if(mode.enabled_fresh && mode.enabled?.every(x=>x===false)) {status="Motor disabled / 电机失能";tone="disabled";}
   else if(mode.classification==="CAN_MOVEJ") status="CAN / MOVEJ";
 }
 const actions=[
  {key:"teleop",name:"Teleoperation",zh:"遥操作联动",enabled:false,reason:"主从通道绑定与软件联动尚未完成验收。"},
  {key:"pc",name:"PC control",zh:"电脑接管 · CAN/MOVEJ",action:"mode_takeover"},
  {key:"drag",name:"Hand guiding",zh:"自由拖动",enabled:false,reason:"自由拖动尚未单独验证；STANDBY不等于重力补偿。"},
  {key:"disable",name:"Motor disable",zh:"电机失能",action:"mode_disable"}
 ].map(a=>{
   if(!a.action) return a;
   const cap=connected && fresh && id==="RE" && capabilityFor
     ? capabilityFor(a.action) : state.capabilities?.[a.action];
   const ready=id==="RE" && fresh && cap?.available===true;
   return {...a,enabled:ready,reason:ready ? "" : id!=="RE" ? "当前执行入口只验收了右执行臂；该实体接口尚未接入。" :
     reason || cap?.reason || "等待执行条件核验。"};
 });
 return {...d,id,status,tone,reason,fresh,actions,
   interface:mode?.interface || "Not bound / 未绑定",
   age:fresh && Number.isFinite(mode.age_status_s) ? (mode.age_status_s*1000).toFixed(0)+" ms" : "—",
   raw:mode?.last_reported_label || mode?.label || "—"};
}
const armIds = ["LE", "RE", "LT", "RT"];
const shortStatus = {unknown: "状态未知", disabled: "电机失能"};

function setText(node, value) {
  if (node.textContent !== value) node.textContent = value;
}

function setPressed(button, pressed) {
  const value = String(pressed);
  if (button.getAttribute("aria-pressed") !== value) button.setAttribute("aria-pressed", value);
}

function buildArmCard(document, id, select, request) {
  const device = devices[id];
  const card = document.createElement("section");
  card.className = "arm-card";
  card.dataset.arm = id;
  card.setAttribute("aria-label", device.zh + " · " + id);
  card.innerHTML =
    '<button type="button" class="arm-select"><b class="arm-code"></b>' +
    '<span class="arm-title"><strong></strong><small></small></span></button>' +
    '<div class="arm-reading"><span class="arm-status"></span><span class="arm-channel"></span></div>' +
    '<div class="arm-modes"></div>';

  const heading = card.querySelector(".arm-select");
  heading.setAttribute("aria-label", "查看" + device.zh + "详情");
  card.querySelector(".arm-code").textContent = id;
  card.querySelector(".arm-title strong").textContent = device.zh;
  card.querySelector(".arm-title small").textContent = device.title;
  heading.addEventListener("click", () => select(id));

  const status = card.querySelector(".arm-status");
  const channel = card.querySelector(".arm-channel");
  const buttons = new Map();
  const modes = card.querySelector(".arm-modes");
  for (const action of deviceView({}, id, false).actions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "arm-mode";
    button.dataset.mode = action.key;
    button.disabled = true;
    button.setAttribute("aria-label", device.zh + " · " + action.zh);
    button.innerHTML = "<strong></strong><small></small>";
    button.firstElementChild.textContent = action.key === "pc" ? "电脑接管" : action.zh;
    button.lastElementChild.textContent = action.key === "pc" ? "CAN / MOVEJ" : action.name;
    button.addEventListener("click", () => request(id, action.key));
    modes.append(button);
    buttons.set(action.key, button);
  }

  return {
    card,
    update(view, selected) {
      setPressed(heading, selected);
      card.classList.toggle("is-selected", selected);
      setText(status, shortStatus[view.tone] || view.status);
      if (status.dataset.tone !== view.tone) status.dataset.tone = view.tone;
      if (status.title !== view.status) status.title = view.status;
      const iface = view.interface === "Not bound / 未绑定" ? "未绑定" : view.interface;
      setText(channel, view.age === "—" ? iface : iface + " · " + view.age);
      for (const action of view.actions) {
        const button = buttons.get(action.key);
        if (button.disabled !== !action.enabled) button.disabled = !action.enabled;
        if (button.title !== action.reason) button.title = action.reason;
      }
    },
  };
}

export function bindDeviceModePanel({document, submit, capabilityFor}) {
  const root = document.getElementById("device-mode-panel");
  if (!root) return {update() {}};

  // Upgrade the existing overview in place. Keep the installation image, its
  // fallback, the five selection controls and the original status detail fields.
  const layout = root.querySelector(".device-layout");
  const inspector = root.querySelector(".device-side-head").parentElement;
  const oldActions = inspector.querySelector("#device-mode-actions");
  const reasons = document.createElement("dl");
  reasons.className = "arm-mode-reasons";
  const reasonRows = new Map();
  for (const action of deviceView({}, "RE", false).actions) {
    const row = document.createElement("div");
    row.innerHTML = "<dt></dt><dd></dd>";
    row.firstElementChild.textContent = action.zh;
    reasons.append(row);
    reasonRows.set(action.key, row);
  }
  oldActions.replaceWith(reasons);

  const details = document.createElement("details");
  details.className = "arm-inspector";
  const summary = document.createElement("summary");
  details.append(summary, inspector);
  layout.after(details);
  root.classList.add("arm-overview");
  root.querySelector(".panel-title h2").textContent = "Arm controls";
  root.querySelector(".panel-title p").textContent = "机械臂状态与模式";

  const fields = {};
  for (const key of ["title", "code", "state", "reason", "interface", "age", "pair", "raw"]) {
    fields[key] = document.getElementById("device-selected-" + key);
  }
  const markers = Array.from(layout.querySelectorAll("[data-device-id]"));
  const cards = new Map();
  let selected = "RE";
  let state = {};
  let online = false;

  function select(id) {
    selected = id;
    details.open = true;
    render();
  }
  function request(id, key) {
    // Resolve against the clicked card and the latest state even if a different
    // hotspot is selected. Use the same action and payload as the single panel.
    const view = deviceView(state, id, online, capabilityFor);
    const action = view.actions.find(item => item.key === key);
    if (action?.enabled && action.action && view.role) submit(action.action, {arm: view.role});
  }

  for (const id of armIds) {
    const entry = buildArmCard(document, id, select, request);
    cards.set(id, entry);
  }
  layout.replaceChildren(cards.get("LE").card, cards.get("RE").card, layout.querySelector(".device-top"),
    cards.get("LT").card, cards.get("RT").card);
  for (const marker of markers) marker.addEventListener("click", () => select(marker.dataset.deviceId));

  function render() {
    let active;
    for (const [id, entry] of cards) {
      const view = deviceView(state, id, online, capabilityFor);
      entry.update(view, id === selected);
      if (id === selected) active = view;
    }
    if (!active) active = deviceView(state, "BASE", online);
    for (const marker of markers) setPressed(marker, marker.dataset.deviceId === selected);
    setText(summary, "设备详情 · " + selected + " " + active.zh);
    setText(fields.title, active.title + " / " + active.zh);
    setText(fields.code, selected);
    setText(fields.state, active.status);
    fields.state.dataset.tone = active.tone;
    setText(fields.reason, active.reason);
    fields.reason.hidden = !active.reason;
    for (const key of ["interface", "age", "pair", "raw"]) setText(fields[key], active[key]);
    reasons.hidden = selected === "BASE";
    for (const action of active.actions) {
      const row = reasonRows.get(action.key);
      row.hidden = action.enabled || !action.reason;
      setText(row.lastElementChild, action.reason);
    }
  }

  const img = document.getElementById("device-top-image");
  function showImageFallback() {
    img.hidden = true;
    document.getElementById("device-image-unavailable").hidden = false;
  }
  if (img) {
    img.addEventListener("error", showImageFallback);
    if (img.complete && !img.naturalWidth) showImageFallback();
  }
  render();
  return {
    update(next, isOnline) {
      state = next || {};
      online = isOnline;
      render();
    },
  };
}
