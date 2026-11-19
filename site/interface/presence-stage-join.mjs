// Read-only clock/provenance bridge. No inference, requests or control commands.
export const phaseNames = {approaching:'接近',grasping:'抓取',transporting:'搬运',placing:'放置',returning:'撤离',unknown:'无法判断'};
const reviewSha = '78c4120b2a6e8266e7868f9692504c359812249934ab8d873050bf8fdaf07202';

export function reviewedJoinForClip(clip, metadata) {
  if (!clip || metadata?.schema !== 'tiger-reviewed-presence-stage-join-v1' || metadata.source_review_sha256 !== reviewSha) return null;
  const join = metadata.episodes?.find(item => item.presence_clip_id === clip.id);
  if (!join || !/^[a-f0-9]{64}$/u.test(join.source_front_sha256) ||
      clip.source_sha256 !== join.source_front_sha256 || clip.source_bytes !== join.source_front_bytes) return null;
  const parts = String(clip.source_path || '').replaceAll('\\', '/').split('/');
  if (parts.slice(-2).join('/') !== join.episode_id + '/front-0000.bin') return null;
  try {
    const shift = Number(BigInt(join.stage_origin_ns) - BigInt(join.presence_origin_ns)) / 1e9;
    if (!Number.isFinite(join.stage_to_presence_offset_s) || Math.abs(shift - join.stage_to_presence_offset_s) > 1e-12) return null;
  } catch { return null; }
  return join;
}

export function reviewedJoinForEpisode(episodeId, presenceData, metadata) {
  const matches = (presenceData?.clips || []).map(clip => ({clip, join: reviewedJoinForClip(clip, metadata)}))
    .filter(item => item.join?.episode_id === episodeId);
  return matches.length === 1 ? matches[0] : null;
}

export function stageDisplay(episode, rows, time, media = {}, maxAgeS = null) {
  const row = Number.isFinite(time) ? (rows || []).filter(item => Number.isFinite(item.offset_s) &&
    Number.isFinite(item.end_to_end_ms) && item.offset_s + item.end_to_end_ms / 1000 <= time).at(-1) ?? null : null;
  let blocked = media.error ? '录像不可用' : media.seeking ? '正在定位原画面' :
    media.waiting || (media.readyState ?? 4) < 2 ? '画面正在载入' :
    media.ended || time >= episode?.duration_s ? '录像已结束，等待新观察' :
    !row ? '尚无此前已完成的阶段判断' : null;
  if (!blocked && Number.isFinite(maxAgeS) && time - row.offset_s > maxAgeS) blocked = '阶段结果已过期，等待新图';
  const rawLabel = row ? phaseNames[row.choice] || '拒答／无法判断' : '无法判断';
  const reviewLabel = row ? phaseNames[row.phase_assessment ? row.phase_assessment.choice : row.choice] || '拒答／无法判断' : '无法判断';
  return {row, label: blocked ? '无法判断' : reviewLabel, rawLabel, reviewLabel, blocked};
}
