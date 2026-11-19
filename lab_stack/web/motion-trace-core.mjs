// Pure display helpers. Values come from existing rendered real feedback (°/mm).
export const JOINT_COLORS = ['#1768b3', '#cf4c28', '#23845c', '#884caa', '#9a7513', '#237f91'];
export function readNumber(text) {
  const value = String(text ?? '').trim();
  return /^[+-]?(?:\d+\.?\d*|\.\d+)$/.test(value) && Number.isFinite(Number(value)) ? Number(value) : null;
}
export function renderedFeedback(read, online) {
  if (!online) return {q: null, g: null};
  const q = Array.from({length: 6}, (_, i) => readNumber(read(`follower-right-j${i}`)));
  return {q: q.every(Number.isFinite) ? q : null, g: readNumber(read('follower-right-gripper'))};
}
export class TraceHistory {
  constructor(capacity = 1024, retentionMs = 122000) {
    this.capacity = capacity; this.retentionMs = retentionMs; this.rows = [];
  }
  add(time, sample, wall = Date.now()) {
    if (!Number.isFinite(time) || !Number.isFinite(wall)) return false;
    const q = Array.isArray(sample.q) && sample.q.length === 6 && sample.q.every(Number.isFinite) ? [...sample.q] : null;
    const g = Number.isFinite(sample.g) ? sample.g : null;
    const previous = this.rows.at(-1);
    if (previous && time <= previous.time) return false;
    // Keep gaps even inside the sampling interval; never extend old readings.
    if (previous && time - previous.time < 180 && !!q === !!previous.q && (g !== null) === (previous.g !== null)) return false;
    this.rows.push({time, wall, q, g});
    const cutoff = time - this.retentionMs;
    while (this.rows.length > this.capacity || (this.rows.length > 1 && this.rows[1].time < cutoff)) this.rows.shift();
    return true;
  }
  clear() { this.rows.length = 0; }
  snapshot() { return this.rows.map(row => ({...row, q: row.q && [...row.q]})); }
}
export function valueRange(values, minimumSpan) {
  const finite = values.filter(Number.isFinite);
  if (!finite.length) return null;
  let low = Math.min(...finite), high = Math.max(...finite);
  const span = Math.max(minimumSpan, high - low), mid = (high + low) / 2;
  return [mid - span * .6, mid + span * .6];
}
export function lineSegments(rows, value, maxGapMs = 1000) {
  const result = []; let segment = [];
  for (const row of rows) {
    const y = value(row);
    if (!Number.isFinite(y)) { if (segment.length) result.push(segment); segment = []; continue; }
    if (segment.length && row.time - segment.at(-1).time > maxGapMs) { result.push(segment); segment = []; }
    segment.push({time: row.time, value: y});
  }
  if (segment.length) result.push(segment);
  return result;
}
