// Display existing operator commands and USB receipts; no new control endpoint.
export function teachingView(state, speed, {fresh=false, observed=0, activated=0}={}) {
  const profile=state?.precision_workbench?.v2_profile || {};
  const saved=profile.latest_physical?.speed_teaching;
  const sameRun=Boolean(saved?.run_id && speed?.run_id && saved.run_id===speed.run_id);
  const supported=profile.ready===true && profile.manual_speed_collection?.supported===true;
  const active=fresh && speed?.available===true && Boolean(speed?.run_id);
  let message='当前尚未连接采集版本；本机预览不会录制机器人数据。';
  let tone='unavailable';
  if (supported) {
    message='随V2运行自动记录。点击下方开始运行，然后用七挡或输入框示教；无需另开录制。';
    tone='ready';
  }
  if (active) {
    message=supported?'本轮人工调速接收中；命令及生效时间由运行器记录，结束后核对U盘保存回执。':
      '当前运行器支持调速；尚未确认训练标签导出版本，不能称训练清单已保存。';
    tone='recording';
  }
  if (!fresh && speed?.run_id) { message='运行器状态已过期；当前记录状态未确认，不能据此判断采集已停止或已保存。';tone='stale'; }
  if (sameRun && saved.saved===true && !active) {message='本轮人工调速标签已保存到U盘。图像/关节对齐与任务结果审核完成后，才能用于训练。';tone='saved';}
  if (sameRun && saved.saved===false) {message='本轮标签保存失败：'+(saved.error || '原因未提供');tone='failed';}
  return {message,tone,observed,activated,
    path:saved?.saved===true && typeof saved.path==='string'?saved.path:'',
    fileLabel:saved?.saved===true?(sameRun?'本轮U盘记录':'上次U盘记录'):'等待最终保存回执',
    count:sameRun && saved.saved===true?`${saved.human_command_count} 条人工命令 · ${saved.reference_activated_count} 条参考已生效`:
      `本页已观察 ${observed} 条确认 · ${activated} 条参考已生效（最终以U盘回执为准）`,
    learning:'神经网络：暂定采用视觉＋关节状态输入的速度回归；尚未训练，自动调速未启用。'};
}

export function createSpeedTeaching({document,context=()=>({}),copy=text=>navigator.clipboard.writeText(text)}) {
  const el=id=>document.getElementById(id), panel=el('v2-teaching');
  if (!panel) return {observe(){},render(){},destroy(){}};
  const records=new Map();let runId=null,path='';
  function observe(data) {
    if (!data?.run_id) return;
    if (runId!==data.run_id) {runId=data.run_id;records.clear();}
    if (!data.command_id || records.size>=4096 && !records.has(data.command_id)) return;
    const old=records.get(data.command_id)||{received:false,active:false};
    const received=data.runtime_acknowledged===true && Number.isFinite(data.runtime_received_ns);
    const active=Number.isFinite(data.reference_effective_ns) && data.reference_effective_ns>0;
    records.set(data.command_id,{received:old.received||received,active:old.active||active});
  }
  function render(data,fresh) {
    const view=teachingView(context(),data,{fresh,
      observed:[...records.values()].filter(x=>x.received).length,
      activated:[...records.values()].filter(x=>x.active).length});
    panel.dataset.state=view.tone;
    el('v2-teaching-status').textContent=view.message;
    el('v2-teaching-count').textContent=view.count;
    el('v2-teaching-model').textContent=view.learning;
    el('v2-teaching-file-label').textContent=view.fileLabel;
    el('v2-teaching-path').textContent=view.path||'未确认';
    path=view.path;el('v2-teaching-copy').disabled=!path;
  }
  const onCopy=async()=>{if(!path)return;try{await copy(path);el('v2-teaching-copy-result').textContent='已复制文件路径';}
    catch{el('v2-teaching-copy-result').textContent='复制失败，请选择上方路径手动复制';}};
  el('v2-teaching-copy').addEventListener('click',onCopy);
  return {observe,render,destroy(){el('v2-teaching-copy').removeEventListener('click',onCopy);}};
}
