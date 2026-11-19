// Only explicit operator clicks POST. GET polling never creates a deploy job.
import {speedFeedback} from './speed-feedback.mjs';
import {createSpeedTeaching} from './speed-teaching.mjs';
const ENDPOINT = "/api/v2/speed";
const POLL_MS = 500;
const STALE_MS = 2000;
const AVAILABLE_STATUSES = new Set(["ready", "received", "frozen", "committed", "reference_active", "superseded", "rejected"]);
const finite = value => typeof value === "number" && Number.isFinite(value);
const scaleText = value => finite(value) ? value.toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:15, useGrouping:false}) + "×" : "未读取";
const msText = value => finite(value) && value >= 0 ? value.toFixed(1) + " ms" : "未测";
const statusText = {
  ready:"V2运行器已就绪，可发送目标倍率", waiting:"等待V2运行器",
  frozen:"下一包已采用目标倍率，等待参考提交", superseded:"已被更新的人工命令替代",
  closed:"运行已结束", reference_active:"参考已生效",
  sent:"已发送，等待运行器确认", plan_committed:"参考已提交，等待切点",
  reference_effective:"参考已生效",
  queued:"已排队，等待运行器确认", received:"运行器已收到，等待下一包",
  pending:"等待参考提交", committed:"参考已提交，等待切点",
  applied:"参考已生效", effective:"参考已生效",
  idle:"等待V2运行", unavailable:"运行器暂不可调速",
  failed:"命令失败", rejected:"命令被拒绝", stopped:"运行已停止",
  ack_timeout:"调速确认超时，结果未确认；未自动重发，暂不接受新调速。",
};

export function speedView(data, {fresh = true, sending = false, roundTripMs = null} = {}) {
  data = data || {};
  const key = String(data.status || "").toLowerCase();
  const hasRun = typeof data.run_id === "string" && data.run_id.length > 0;
  const hasCommand = typeof data.command_id === "string" && data.command_id.length > 0;
  const connected = hasRun && data.available !== false && !["unavailable", "waiting", "closed"].includes(key);
  // Availability must agree with both current identity and the protocol state.
  // SENT remains disabled until runtime ACK; unknown and terminal states fail closed.
  const available = data.available === true && hasRun && fresh && !sending && AVAILABLE_STATUSES.has(key);
  const effective = data.reference_effective === true || ["applied", "effective", "reference_effective", "reference_active"].includes(key);
  const uncertain = ["ack_timeout", "send_unknown"].includes(key);
  const hasCommitted = hasRun && (data.has_committed_reference === true ||
    (connected && hasCommand && (["committed", "plan_committed"].includes(key) || effective)) ||
    (finite(data.plan_committed_ns) && data.plan_committed_ns > 0));
  const hasApplied = hasRun && (data.has_applied_reference === true || (connected && hasCommand && effective) ||
    (finite(data.reference_effective_ns) && data.reference_effective_ns > 0));
  const requested = hasRun && hasCommand ? scaleText(data.target_scale) :
    scaleText(finite(data.limits?.default) ? data.limits.default : 1) + " · 默认，待请求";
  const staleNote = " · 上次已知（状态过期）";
  const historyNote = uncertain ? " · 上次确认倍率（新目标结果未确认）" :
    key === "closed" ? " · 上次确认倍率（通道关闭）" :
    !connected ? " · 上次确认倍率（通道不可用）" : "";
  return {
    available, target:requested + (!fresh && hasRun && hasCommand ? staleNote : ""),
    applied:hasCommitted && finite(data.committed_target_scale) ? scaleText(data.committed_target_scale) + (!fresh ? staleNote : historyNote) : uncertain ? "提交未确认" : "未提交",
    effective: hasApplied && finite(data.applied_target_scale)
      ? scaleText(data.applied_target_scale) + (!fresh ? staleNote : historyNote || (effective ? " · 已生效" : data.pending ? " · 新目标待生效" : " · 最近生效目标"))
      : uncertain ? "生效未确认" : data.pending ? "未生效 · 待切换" : "未生效",
    ack:msText(data.latency?.backend_ack_ms),
    switchLatency:msText(data.latency?.reference_switch_ms),
    roundTrip:msText(roundTripMs),
    message: !fresh ? "状态已过期，恢复运行器状态后可继续调速。" :
      sending ? "正在提交倍率；这不是参考已生效的确认。" :
      (data.reason || statusText[key] || (available ? "可实时调速；仅作用于当前V2运行。" : "只在可调速的V2运行窗口中接受命令。")),
  };
}

export function validTarget(value, limits = {}) {
  const min = finite(limits.min) ? Math.max(0.5, limits.min) : 0.5;
  const max = finite(limits.max) ? Math.min(4.0, limits.max) : 4.0;
  return finite(value) && value >= min && value <= max;
}

export function createV2SpeedControl({
  document, api, selected, active, context=()=>({}),
  now = () => performance.now(),
  schedule = (fn, ms) => setTimeout(fn, ms),
  cancel = timer => clearTimeout(timer),
  commandId = () => crypto.randomUUID(),
}) {
  const el = id => document.getElementById(id);
  const panel = el("v2-speed-panel");
  if (!panel) return {sync() {}, destroy() {}};
  const teaching=createSpeedTeaching({document,context});
  const presets = [...panel.querySelectorAll("[data-v2-speed]")];
  const input = el("v2-speed-input"), apply = el("v2-speed-apply");
  let data = null, receivedAt = null, timer = null, expiryTimer = null, polling = false, sending = false;
  let roundTripMs = null, pollController = null, generation = 0, errorText = "", errorSource = null, destroyed = false;
  let previousRun = null, requestVersion = 0, lastCommand = null;
  let sendingTarget = null;
  const listeners = [];
  const listen = (node, type, handler) => {
    node.addEventListener(type, handler);
    listeners.push(() => node.removeEventListener(type, handler));
  };
  const enabled = () => !destroyed && selected() && active();
  const fresh = () => receivedAt !== null && now() - receivedAt <= STALE_MS;
  function render() {
    panel.hidden = !selected();
    const view = speedView(data, {fresh:fresh(), sending, roundTripMs});
    const feedback = speedFeedback(data, {fresh:fresh(), sending, sendingTarget});
    const allowed = enabled() && view.available;
    for (const button of presets) {
      button.disabled = !allowed;
      const value = Number(button.dataset.v2Speed);
      const effective = value === feedback.effectiveScale;
      button.setAttribute("aria-pressed", String(effective));
      button.dataset.speedState = effective ? 'effective' : value === feedback.pendingScale ? 'pending' : 'idle';
      button.title = effective ? '已收到此参考目标的生效回执' : value === feedback.pendingScale ? '已请求，等待生效回执' : '点击请求 '+button.dataset.v2Speed+'倍参考目标';
    }
    el('v2-speed-feedback').dataset.state = feedback.tone;
    el('v2-speed-feedback').dataset.confirmed = String(feedback.effectiveScale !== null);
    el('v2-speed-feedback-label').textContent = feedback.label;
    el('v2-speed-feedback-value').textContent = feedback.value;
    el('v2-speed-feedback-badge').textContent = feedback.badge;
    el('v2-speed-feedback-detail').textContent = feedback.detail;
    el('v2-speed-progress').dataset.fresh = String(feedback.progressFresh);
    for (let i = 1; i <= 3; ++i) el('v2-speed-stage-'+i).dataset.done = String(feedback.stage >= i);
    const dock = el('v2-speed-dock');
    dock.hidden = !selected(); dock.dataset.state = feedback.tone;
    dock.textContent = feedback.dock;
    input.disabled = !allowed;
    apply.disabled = !allowed || !validTarget(input.value === "" ? NaN : Number(input.value), data?.limits);
    el("v2-speed-run").textContent = data?.run_id ? "本轮 " + data.run_id.slice(-12) : "等待运行器";
    el("v2-speed-target").textContent = view.target;
    el("v2-speed-applied").textContent = view.applied;
    el("v2-speed-effective").textContent = view.effective;
    el("v2-speed-rtt").textContent = view.roundTrip;
    el("v2-speed-ack").textContent = view.ack;
    el("v2-speed-switch").textContent = view.switchLatency;
    el("v2-speed-status").textContent = view.message;
    el("v2-speed-error").textContent = errorText;
    el("v2-speed-error").hidden = !errorText;
    teaching.render(data,fresh());
  }
  function accept(next) {
    if (!next || typeof next !== "object") throw new Error("调速接口返回了无效状态。");
    if (next.run_id !== previousRun) {
      previousRun = next.run_id;
      roundTripMs = null;
      lastCommand = null;
      if (finite(next.target_scale)) input.value = String(next.target_scale);
    }
    // Do not show measurements for a superseded command beside a new target.
    if (lastCommand && next.command_id && next.command_id !== lastCommand) roundTripMs = null;
    data = next;
    teaching.observe(next);
    receivedAt = now();
    if (expiryTimer !== null) cancel(expiryTimer);
    // Independent of the page's general refresh: a hanging GET must not leave controls enabled.
    expiryTimer = schedule(() => { expiryTimer = null; render(); }, STALE_MS + 1);
    if (errorSource === "submit" && lastCommand === next.command_id &&
        ["RECEIVED", "FROZEN", "COMMITTED", "REFERENCE_ACTIVE", "REJECTED", "SUPERSEDED", "CLOSED"].includes(String(next.status || "").toUpperCase())) {
      errorText = "";
      errorSource = null;
    }
  }
  function schedulePoll() {
    if (enabled() && timer === null) timer = schedule(() => {
      timer = null;
      poll();
    }, POLL_MS);
  }
  async function poll() {
    if (!enabled() || polling || sending) return;
    polling = true;
    const version = requestVersion, epoch = generation;
    pollController = new AbortController();
    const currentController = pollController;
    const pollTimeout = schedule(() => currentController.abort(), 6000);
    try {
      const result = await api(ENDPOINT, {signal:pollController.signal});
      if (enabled() && epoch === generation && version === requestVersion) {
        accept(result);
        // A rejected POST remains visible until the operator next submits.
        if (!lastCommand || errorSource === "read") { errorText = ""; errorSource = null; }
      }
    } catch (error) {
      if (enabled() && epoch === generation && version === requestVersion) {
        receivedAt = null;
        errorText = "读取调速状态失败：" + (currentController.signal.aborted ? "请求超过6秒，等待状态恢复。" : error.message);
        errorSource = "read";
      }
    } finally {
      cancel(pollTimeout);
      polling = false;
      pollController = null;
      render();
      schedulePoll();
    }
  }
  async function send(target) {
    if (!enabled() || !speedView(data, {fresh:fresh(), sending}).available) return;
    if (!validTarget(target, data?.limits)) {
      errorText = "倍率须在当前允许的0.5～4.0范围内。";
      render();
      return;
    }
    const runId = data.run_id, id = commandId(), started = now(), epoch = generation;
    requestVersion += 1;
    sending = true;
    sendingTarget = target;
    errorText = "";
    errorSource = null;
    roundTripMs = null;
    lastCommand = id;
    render();
    try {
      const result = await api(ENDPOINT, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({run_id:runId, command_id:id, target_scale:target}),
      });
      // A route change cannot redirect this request to a different running session.
      if (result.run_id && result.run_id !== runId) throw new Error("运行身份已变化；本次命令未确认。");
      if (enabled() && epoch === generation) {
        let next = result;
        if (result.run_id === data?.run_id && String(result.status).toUpperCase() === "SENT") {
          // HTTP 202 is a partial acknowledgement, not a new complete snapshot.
          // Preserve only established same-run reference facts; old command timing must not carry over.
          next = {available:data.available, limits:data.limits, pending:true,
            has_committed_reference:data.has_committed_reference === true,
            committed_target_scale:data.has_committed_reference === true ? data.committed_target_scale : undefined,
            has_applied_reference:data.has_applied_reference === true,
            applied_target_scale:data.has_applied_reference === true ? data.applied_target_scale : undefined,
            ...result, latency:result.latency || {}};
        }
        accept(next);
        lastCommand = id;
        roundTripMs = now() - started;
      }
    } catch (error) {
      receivedAt = null;
      if (enabled() && epoch === generation) {
        errorText = "调速未确认：" + error.message + "；不会自动重发。";
        errorSource = "submit";
      }
    } finally {
      sending = false;
      sendingTarget = null;
      render();
      schedulePoll();
    }
  }
  function sync() {
    if (!enabled()) {
      generation += 1;
      if (timer !== null) cancel(timer);
      timer = null;
      if (expiryTimer !== null) cancel(expiryTimer);
      expiryTimer = null;
      pollController?.abort();
      receivedAt = null;
      render();
      return;
    }
    render();
    if (timer === null && !polling && !sending) poll();
  }
  for (const button of presets) listen(button, "click", () => {
    input.value = button.dataset.v2Speed;
    send(Number(button.dataset.v2Speed));
  });
  listen(input, "input", render);
  listen(apply, "click", () => send(input.value === "" ? NaN : Number(input.value)));
  listen(document, "visibilitychange", sync);
  render();
  return {
    sync,
    destroy() {
      destroyed = true;
      sync();
      for (const remove of listeners) remove();
      teaching.destroy();
    },
  };
}

