// Copyright (c) 2026 Yang Taige
// SPDX-License-Identifier: MIT
const devices = {
 LE: {title:"Left execution arm", zh:"左执行臂", role:"left", pair:"LT → LE"},
 RE: {title:"Right execution arm", zh:"右执行臂", role:"right", pair:"RT → RE"},
 LT: {title:"Left teaching arm", zh:"左示教臂", role:null, pair:"LT → LE"},
 RT: {title:"Right teaching arm", zh:"右示教臂", role:null, pair:"RT → RE"},
 BASE: {title:"Mobile base", zh:"移动底盘", role:null, pair:"Independent device / 独立设备"}
};
export function deviceView(state, id, online) {
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
   const cap=state.capabilities?.[a.action];
   const ready=id==="RE" && fresh && cap?.available===true;
   return {...a,enabled:ready,reason:ready ? "" : id!=="RE" ? "当前执行入口只验收了右执行臂；该实体接口尚未接入。" :
     reason || cap?.reason || "等待执行条件核验。"};
 });
 return {...d,id,status,tone,reason,fresh,actions,
   interface:mode?.interface || "Not bound / 未绑定",
   age:fresh && Number.isFinite(mode.age_status_s) ? (mode.age_status_s*1000).toFixed(0)+" ms" : "—",
   raw:mode?.last_reported_label || mode?.label || "—"};
}
export function bindDeviceModePanel({document,submit}) {
 const root=document.getElementById("device-mode-panel");
 if(!root) return {update(){}};
 let selected="RE",state={},online=false;
 const el=id=>document.getElementById(id);
 function render() {
  const v=deviceView(state,selected,online);
  el("device-selected-title").textContent=v.title+" / "+v.zh;
  el("device-selected-code").textContent=v.id;
  el("device-selected-state").textContent=v.status;
  el("device-selected-state").dataset.tone=v.tone;
  el("device-selected-reason").textContent=v.reason || "状态来自设备反馈；点击操作后等待实际回执。";
  el("device-selected-interface").textContent=v.interface;
  el("device-selected-age").textContent=v.age;
  el("device-selected-pair").textContent=v.pair;
  el("device-selected-raw").textContent=v.raw;
  for(const b of root.querySelectorAll("[data-device-id]")) {
   const active=b.dataset.deviceId===selected;
   b.setAttribute("aria-pressed",String(active));b.classList.toggle("selected",active);
  }
  for(const a of v.actions) {
   const b=el("device-mode-"+a.key);
   b.disabled=!a.enabled;b.title=a.reason;
   b.querySelector("small").textContent=a.reason || "使用现有受控入口执行";
  }
  el("device-mode-actions").hidden=selected==="BASE";
 }
 for(const b of root.querySelectorAll("[data-device-id]")) b.addEventListener("click",()=>{selected=b.dataset.deviceId;render();});
 for(const key of ["pc","disable"]) el("device-mode-"+key).addEventListener("click",()=>{
   const v=deviceView(state,selected,online),a=v.actions.find(x=>x.key===key);
   if(a?.enabled) submit(a.action,{arm:"right"});
 });
 const img=el("device-top-image");
 img.addEventListener("error",()=>{img.hidden=true;el("device-image-unavailable").hidden=false;});
 render();
 return {update(next,isOnline){state=next||{};online=isOnline;render();}};
}
