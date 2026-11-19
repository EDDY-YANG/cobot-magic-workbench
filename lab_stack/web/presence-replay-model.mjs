// Pure presentation helpers. These never run inference or issue robot commands.
export const stateLabels = {
  PAUSED: '离线暂停状态', CLEAR_CONFIRMING: '正在确认连续空场',
  REOBSERVE_REQUIRED: '等待下一张新图重新观察', RESUME_ELIGIBLE: '回放恢复条件满足',
  REFERENCE_PRESENT: '独立参考：有人／手', REFERENCE_CLEAR: '独立参考：已退出',
};
export const presenceWords = {present: '有人／有手', absent: '未见人／手', uncertain: '无法判断'};
const reasons = {
  semantic_presence_detected: '识别到真人或人手，关闭许可',
  presence_detected: '识别到真人或人手，关闭许可',
  person_detected: '识别到人体，关闭许可',
  collecting_continuous_clear_frames: '累计连续空场证据',
  clear_confirmed_reobserve_required: '空场确认完成，仍须取得更新原图',
  fresh_scene_receipt_accepted_replay_only: '更新原图复核通过，仅满足回放候选条件',
  detector_error_or_invalid_input: '模型拒答、检测错误或输入无效，保持暂停',
  clear_sequence_gap: '空场采样间隔过大，重新累计',
  clear_sequence_restarted: '空场证据中断，重新开始确认',
  fresh_clear_continues: '新的观察仍为空场，保持回放候选状态',
  source_context_reset: '切换录像，重新建立观察上下文',
  reobserve_requested: '已请求新的场景原图',
  reobserve_receipt_accepted: '已核对新原图回执',
  external_latch: '外部闭锁有效，许可保持关闭',
  external_latch_set: '设置外部闭锁',
  stale_frame: '观察结果过期，等待新图',
  watchdog_expired: '观察超时，看门狗关闭许可',
  waiting_for_first_observation: '等待首个有效观察',
};
export function reasonLabel(reason) {
  if (!reason) return '原因未提供，查看原始记录';
  return reasons[reason] || (/[\u3400-\u9fff]/u.test(reason) ? reason : '守卫事件：' + reason);
}

// Records must be sorted by actual application time; equal times select the last receipt.
export function latestAppliedRecord(records, time) {
  if (!Number.isFinite(time)) return null;
  let low = 0, high = records.length;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (records[mid].evaluated_at_pts_s <= time) low = mid + 1;
    else high = mid;
  }
  return low ? records[low - 1] : null;
}
export function modelRecords(clip) {
  return (clip.records || []).filter(row => !row.record_kind || row.record_kind === 'model');
}
export function sampleCategory(row) {
  if (row.assessment?.raw_top1_error === true) return 'error';
  if (row.valid === false || row.semantic_presence === 'uncertain' || row.assessment?.classification === 'invalid_or_uncertain') return 'uncertain';
  const assessment = row.assessment || {};
  if (assessment.is_error || assessment.classification === 'false_positive' || assessment.classification === 'false_negative') return 'error';
  if (assessment.matches_reference === true) return 'correct';
  return 'unreviewed';
}
export function missedIntervals(clip) {
  // Positive entry-to-result delay is not itself a completely missed human interval.
  // The independent continuous audit explicitly distinguishes this failure status.
  return (clip.validation?.cycles || []).filter(cycle => String(cycle.status || '').startsWith('MISSED_'));
}
export function caseInfo(clip) {
  const rows = modelRecords(clip), missed = missedIntervals(clip);
  const errorCount = rows.filter(row => sampleCategory(row) === 'error').length;
  const uncertainCount = rows.filter(row => row.valid === false || row.semantic_presence === 'uncertain' || row.assessment?.classification === 'invalid_or_uncertain').length;
  const rawErrorCount = rows.filter(row => row.assessment?.raw_top1_error === true).length;
  const rawReviewedCount = rows.filter(row => typeof row.assessment?.raw_top1_error === 'boolean' || typeof row.assessment?.raw_matches_reference === 'boolean').length;
  const acceptedErrorCount = rows.filter(row => row.valid !== false &&
    ['false_positive', 'false_negative'].includes(row.assessment?.classification)).length;
  const reviewedCount = rows.filter(row => row.assessment?.classification || row.assessment?.reference_presence || row.assessment?.reference).length;
  const total = clip.validation?.total_reference_cycles || 0;
  const complete = total > 0 && clip.validation.complete_cycles === total && missed.length === 0;
  const handDemo = clip.hand_demo === true || total > 0 || rows.some(row =>
    row.semantic_presence === 'present' || row.present === true || row.persons?.length || row.hands?.length);
  const failed = missed.length > 0 || errorCount > 0;
  return {
    handDemo, complete, failed, errorCount, uncertainCount, rawErrorCount, rawReviewedCount, acceptedErrorCount, reviewedCount, missedCount: missed.length,
    scene: clip.scene_type || (clip.long_validation ? '桌面第三视角' : handDemo ? '桌面手部演示' : '桌面机器人回放'),
    status: clip.validation_status || (failed ? '保留失败案例' : complete ? '完整链条通过' : total ? '完整链条未通过' : '短片／单帧验证'),
  };
}
export function caseMatches(clip, filter) {
  const info = caseInfo(clip);
  return filter === 'all' || (filter === 'hands' && info.handDemo) ||
    (filter === 'cycles' && info.complete) || (filter === 'issues' && (info.failed || info.uncertainCount > 0));
}
export function sampleMatches(row, filter) {
  const category = sampleCategory(row);
  if (filter === 'uncertain') return row.valid === false || row.semantic_presence === 'uncertain' || row.assessment?.classification === 'invalid_or_uncertain';
  return filter === 'all' || (filter === 'issues' && ['error', 'uncertain'].includes(category)) || filter === category;
}

// Media availability is independent of the recorded model decision. It can only close permission.
export function replayView(clip, time, media, fallbackConfig = {}) {
  const row = latestAppliedRecord(clip.records || [], time);
  let blocked = media?.error ? '视频不可用；不展示恢复许可' :
    media?.seeking ? '正在定位原画面；暂不展示恢复许可' :
    media?.waiting || (media?.readyState ?? 4) < 2 ? '画面正在载入；暂不展示恢复许可' : null;
  if (!blocked && (media?.ended || time >= clip.duration_s)) blocked = '视频已结束，等待新观察';
  if (!blocked && !row) blocked = '尚无此前已完成的检测结果';
  if (!blocked) {
    const ageLimit = row.gate?.config?.max_age_s ?? fallbackConfig.max_age_s;
    if (!Number.isFinite(ageLimit) || !Number.isFinite(row.source_pts_s) || time - row.source_pts_s > ageLimit)
      blocked = '当前观察结果已过期，等待新图';
  }
  return {row, state: blocked ? 'PAUSED' : row.gate.state, blocked,
    permit: !blocked && row.gate?.replay_permission === true};
}
