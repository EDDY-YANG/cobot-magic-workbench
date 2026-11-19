// Display-only projection of the existing backend contract. No request/clock writes.
const finite = x => typeof x === 'number' && Number.isFinite(x);
const scale = x => finite(x) ? x.toLocaleString('en-US', {maximumFractionDigits:6, useGrouping:false}) + '×' : '—';
const positiveTime = x => finite(x) && x > 0;
const liveStates = new Set(['READY','SENT','RECEIVED','FROZEN','COMMITTED','REFERENCE_ACTIVE','REJECTED','SUPERSEDED']);

export function speedFeedback(data, {fresh = true, sending = false, sendingTarget = null} = {}) {
  data = data || {};
  const key = String(data.status || '').toUpperCase();
  const hasRun = typeof data.run_id === 'string' && data.run_id.length > 0;
  const uncertain = ['ACK_TIMEOUT','SEND_UNKNOWN'].includes(key);
  const closed = ['CLOSED','STOPPED'].includes(key);
  const live = fresh && hasRun && data.available === true && liveStates.has(key);
  // The default numeric 1 is not evidence of an applied command. Only the
  // backend's accepted runtime-event flag can establish this displayed fact.
  const known = hasRun && data.has_applied_reference === true && finite(data.applied_target_scale)
    && data.applied_target_scale >= 0.5 && data.applied_target_scale <= 4;
  const liveKnown = live && known;
  const pending = sending || (hasRun && (data.pending === true || ['SENT','RECEIVED','FROZEN','COMMITTED'].includes(key)));
  const target = sending && finite(sendingTarget) ? sendingTarget : data.target_scale;
  const hasCommand = hasRun && typeof data.command_id === 'string' && data.command_id.length > 0;
  const requested = sending || hasCommand;
  const currentText = known ? scale(data.applied_target_scale) : '—';
  let tone = 'idle', badge = '等待V2运行', detail = '运行开始后，点击挡位或输入倍率并应用。';
  let stage = 0;
  if (requested) stage = 1;
  if (hasCommand && positiveTime(data.plan_committed_ns)) stage = 2;
  if (hasCommand && key === 'REFERENCE_ACTIVE' && known && positiveTime(data.reference_effective_ns)
      && data.target_scale === data.applied_target_scale && !sending) stage = 3;

  if (!fresh && hasRun) {
    tone = 'stale'; badge = '状态已过期'; detail = '仅保留上次确认值；当前生效状态未知。';
  } else if (uncertain) {
    tone = 'uncertain'; badge = '生效未确认'; detail = '命令可能已执行；保留上次确认值，不自动重发。';
  } else if (closed) {
    tone = 'closed'; badge = '运行已结束'; detail = '这里是本轮历史确认值，不代表仍在运动。';
  } else if (hasRun && !live) {
    tone = 'stale'; badge = '通道不可用'; detail = '当前状态不能确认；保留已有历史回执。';
  } else if (live && pending) {
    tone = 'pending'; badge = '等待切换';
    const where = sending ? '正在发送' : key === 'SENT' ? '等待运行器确认' :
      key === 'COMMITTED' ? '参考已提交，等待切点与发送确认' : '等待下一包规划与切换';
    detail = `请求 ${scale(target)} · ${where}。` + (liveKnown ? ` 当前仍为 ${currentText}。` : ' 尚无调速生效回执。');
  } else if (live && ['REJECTED','SUPERSEDED'].includes(key)) {
    tone = 'pending'; badge = key === 'REJECTED' ? '新请求被拒绝' : '新请求已被替代';
    detail = `请求 ${scale(target)} 未确认生效。` + (liveKnown ? ` 当前已确认 ${currentText}。` : ' 尚无已确认倍率。');
  } else if (liveKnown) {
    tone = 'active'; badge = '✓ 已生效';
    detail = '参考已到切换点，发送器已返回成功回执。';
    if (finite(data.latency?.reference_switch_ms) && data.latency.reference_switch_ms >= 0)
      detail += ` 本次切换用时 ${Math.round(data.latency.reference_switch_ms)} ms。`;
  } else if (live) {
    badge = '等待调速确认'; detail = '默认目标1×；尚未收到调速生效回执。';
  }
  const label = liveKnown ? '当前已生效参考目标' : known ? '上次确认参考目标' : '当前参考目标';
  const dock = liveKnown ? (pending ? `当前 ${currentText} → 请求 ${scale(target)} · 待生效` : `当前 ${currentText} · 已生效`) :
    known ? `上次 ${currentText} · ${badge.replace('✓ ', '')}` : `V2 · ${badge.replace('✓ ', '')}`;
  return {tone, badge, label, value:currentText, detail, dock, stage,
    progressFresh:live, effectiveScale:liveKnown ? data.applied_target_scale : null,
    pendingScale:live && pending && finite(target) ? target : null};
}
