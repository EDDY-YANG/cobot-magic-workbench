/* USB receipts are authoritative. This module opens no sensor or hardware stream. */
const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const finite = value => typeof value === "number" && Number.isFinite(value);
const count = value => Number.isInteger(value) && value >= 0 ? value : null;
const categories = new Set(["A", "B", "C", "D", "random", "recovery", "test"]);
const terminalStatuses = new Set(["RECORDED_NATIVE_REVIEW_REQUIRES_QC", "RECORDING_FAILED"]);
const batchPattern = /^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-batch-[0-9a-f]{32}$/;
const trialPattern = /^[0-9a-f]{32}$/;
const episodePattern = /^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-episode-(?:(\d{6}|1000000)-(A|B|C|D|random|recovery|test)-)?[0-9a-f]{8}$/;
const lastBatchKey = "tiger-collection-last-verified-batch-v1";
const boundBatchKey = "tiger-collection-last-verified-batch-v2:";
export function collectionStorageIdentity(state, online) {
  const usb=object(state.usb), selected=object(object(state.usb_storage).selected), hostname=object(state.identity).hostname;
  if (online!==true || state.connected!==true || usb.verified!==true || usb.fresh!==true ||
      typeof hostname!=="string" || !hostname) return null;
  if(selected.eligible!==true || usb.path!==selected.mountpoint || typeof usb.uuid!=="string" ||
     usb.uuid.toUpperCase()!==String(selected.uuid).toUpperCase())return null;
  const fields=[hostname,...["uuid","mountpoint","source","device","major_minor","physical_usb_path"].map(key=>selected[key])];
  if (fields.some(value=>typeof value!=="string" || !value) || !Number.isSafeInteger(selected.size_bytes) || selected.size_bytes<=0) return null;
  return JSON.stringify([...fields,selected.size_bytes]);
}
export function collectionTimeTitle(instant = new Date()) {
  const date = instant instanceof Date ? instant : new Date(instant);
  if (!Number.isFinite(date.getTime())) throw new Error("当前时间无效，未创建采集。");
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai",
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    hourCycle: "h23" }).formatToParts(date).map(part => [part.type, part.value]));
  return [parts.year, parts.month, parts.day, parts.hour, parts.minute, parts.second].join("-") + "-遥操采集";
}
function collectionTimeLabel(value) {
  const date = typeof value === "string" && value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime())
    ? collectionTimeTitle(date).replace("-遥操采集", "") + "（北京时间）" : "创建时间待回执";
}
function episodeTimeLabel(episode) {
  if (episode.started_at) return collectionTimeLabel(episode.started_at);
  return episodePattern.test(episode.episode_id || "")
    ? episode.episode_id.slice(0, 19) + "（北京时间）" : "开录时间待回执";
}
const safeText = (value, name, maximum, empty = false) => {
  if (typeof value !== "string" || value.length > maximum || /[\x00-\x1f\x7f]/.test(value) || (!empty && !value.trim()))
    throw new Error(name + "须填写真实文字，最多 " + maximum + " 字且不含控制字符。");
  return value.trim();
};
export function collectionResult(value) {
  let result = object(value);
  for (let index = 0; index < 4; index++) {
    const nested = result.reported || result.result || result.data;
    if (!nested || Array.isArray(nested) || typeof nested !== "object") break;
    result = object(nested);
  }
  return result;
}
export function createBatchParams(draft, instant = new Date()) {
  if (!Number.isInteger(draft.target_count) || draft.target_count < 1 || draft.target_count > 10000)
    throw new Error("人工合格目标须为 1–10000 的整数；保存条数不等于合格条数。");
  return { task: safeText(draft.task, "任务", 240), title: safeText(draft.title || collectionTimeTitle(instant), "采集名称", 120),
    operator: safeText(draft.operator, "操作人", 80), target_count: draft.target_count };
}
export function startTrialParams(batch, draft) {
  if (!batchPattern.test(batch.batch_id || "") || !trialPattern.test(draft.trial_id || ""))
    throw new Error("须先核对真实批次与本条唯一身份。");
  if (!categories.has(draft.category)) throw new Error("请选择实际示范类别。");
  return { task: safeText(batch.task, "批次任务", 240), batch_id: batch.batch_id,
    trial_id: draft.trial_id, category: draft.category, notes: safeText(draft.notes, "本条备注", 512, true) };
}
export function isFinalizedEpisode(episode) {
  const identity = episodePattern.exec(episode.episode_id || "");
  return episode.finalized === true && terminalStatuses.has(episode.status) &&
    identity !== null && (!identity[1] || (Number.isInteger(episode.ordinal) && episode.ordinal >= 1 &&
      episode.ordinal <= 1000000 && episode.ordinal === Number(identity[1]) && episode.category === identity[2])) &&
    typeof episode.path === "string" && episode.path.startsWith("/") &&
    episode.path.split("/").at(-1) === episode.episode_id;
}
export function reviewEpisodeParams(batchId, episode, reviewer, verdict, note) {
  if (!batchPattern.test(batchId || "") || !isFinalizedEpisode(episode))
    throw new Error("仅可复核本批次真实已收尾、身份明确的 episode。");
  if (!["accepted", "rejected"].includes(verdict)) throw new Error("复核判定无效。");
  if (verdict === "accepted" && episode.accept_allowed !== true)
    throw new Error(episode.accept_blocked_reason || "真实录制质量尚不支持人工合格判定。");
  return { batch_id: batchId, episode_id: episode.episode_id,
    reviewer: safeText(reviewer, "复核人", 80), verdict, note: safeText(note, "复核依据", 512) };
}
export function collectionCountView(list, online) {
  const known = online === true && list.schema === "tiger-collection-list-v1";
  const counters = object(list.counts), batch = object(list.batch);
  const accepted = known ? count(counters.human_accepted) : null;
  const target = known && Number.isInteger(batch.target_count) && batch.target_count > 0 ? batch.target_count : null;
  return { native: known ? count(counters.native_saved) : null, accepted,
    rejected: known ? count(counters.human_rejected ?? counters.rejected) : null,
    unreviewed: known ? count(counters.human_unreviewed ?? counters.unreviewed) : null,
    failed: known ? count(counters.recording_failed) : null, target,
    progress: accepted !== null && target !== null ? Math.min(100, accepted / target * 100) : null };
}
export function collectionRecordingView(recording, online) {
  const known = online === true && typeof recording.active === "boolean";
  const saving=recording.saving===true || recording.finalizing===true;
  const finalized = known && !saving && recording.active === false && recording.finalized === true && terminalStatuses.has(recording.status);
  const stage = !known ? "unknown" : saving ? "flushing" : recording.active === true
    ? (recording.finalizing === true || recording.stop_requested === true ? "flushing" : "active")
    : finalized ? "finalized" : recording.path ? "flushing" : "idle";
  const labels = { unknown: "当前录制未核对", active: "正在录制", flushing: "正在收尾保存 · 结果待确认",
    finalized: recording.status === "RECORDING_FAILED" ? "录制失败 · 已收尾" : "原生数据已保存 · 待人工复核", idle: "未录制" };
  return { stage, finalized, known, label: labels[stage],
    elapsed: known && finite(recording.elapsed_s) && recording.elapsed_s >= 0 ? recording.elapsed_s.toFixed(1) + " s" : "—",
    samples: known ? count(recording.samples) : null,
    queue: known ? count(recording.queue_depth) : null, canQueue: known ? count(recording.raw_CAN_queue_depth) : null,
    overflows: known ? count(recording.overflows) : null,
    error: !known ? "当前未核对" : recording.error || recording.failure || (recording.overflows ? "有队列溢出" : "未报告错误"),
    cameras: known ? object(recording.camera_frame_counts) : {}, can: known ? object(recording.raw_CAN_counts) : {} };
}
export function collectionCapacityView(usb, online) {
  const known = online === true && usb.verified === true;
  const free = known ? count(usb.free_bytes) : null, capacity = known ? count(usb.capacity_bytes) : null;
  return { value: free === null ? "—" : (free / 1073741824).toFixed(2) + " GiB" +
    (capacity === null ? " · 总容量未列出" : " / " + (capacity / 1073741824).toFixed(2) + " GiB"),
    source: known && typeof usb.checked_at === "string" && usb.checked_at
      ? "USB 容量快照 · " + usb.checked_at : "容量检查时间当前未核对" };
}
export function filterCollectionPage(episodes, category, verdict) {
  return (Array.isArray(episodes) ? episodes : []).filter(episode =>
    (category === "all" || episode.category === category) &&
    (verdict === "all" || (object(episode.review).verdict || "unreviewed") === verdict));
}

export class CollectionWorkbench {
  constructor({ document, submit, capability, showError, storage = null, uuid = () => crypto.randomUUID().replaceAll("-", ""), now = () => new Date() }) {
    this.document = document; this.submit = submit; this.capability = capability; this.showError = showError;
    this.storage = storage; this.uuid = uuid; this.now = now; this.online = false; this.batch = null; this.list = {};
    this.current = null; this.recording = {}; this.usb = {}; this.scope = {}; this.phase = "idle"; this.busy = null;
    this.selected = null; this.processed = new Set(); this.page = 1; this.refreshNeeded = false; this.listAt = null;
    this.allowRestoreFinalized = true; this.restoreCandidate = null; this.restoreAttempted = false;
    this.storageIdentity=null; this.observedStorageIdentity=null; this.legacyCandidate=null;
    this.restoreFailure=null; this.ownedBatchJobs=new Map();
    this.$ = id => document.getElementById(id);
    this.restorePreferences(); this.bind(); this.render();
  }
  text(id, value) { const node = this.$(id); if (node) node.textContent = value ?? "—"; }
  available(action) {
    const bound=["collection_create","collection_list","collection_review","record_start","record_retry"].includes(action);
    return this.online && !this.busy && (!bound || this.storageIdentity && this.storageIdentity===this.observedStorageIdentity)
      && this.capability(action).available === true;
  }
  input(id) { return this.$(id).value; }
  setInput(id, value) { this.$(id).value = typeof value === "string" || finite(value) ? String(value) : ""; }
  get active() { return ["starting", "stopping", "active", "flushing"].includes(this.phase) || this.recording.active === true || this.recording.saving===true || this.recording.finalizing===true; }
  guarded(action) {
    try { return action(); } catch (error) { this.showError(error.message); this.render(); return null; }
  }
  bind() {
    const click = (id, handler) => this.$(id).addEventListener("click", () => this.guarded(handler));
    click("collection-create", () => {
      if (this.active || this.busy || this.batch || this.phase !== "idle" ||
        !["idle", "finalized"].includes(collectionRecordingView(this.recording, this.online).stage))
        throw new Error("当前数据流或采集身份尚未核对，不能重复准备采集。");
      const params = createBatchParams({ task: this.input("task-name"), title: collectionTimeTitle(this.now()),
        operator: this.input("collection-operator"), target_count: Number(this.input("collection-target")) });
      this.setInput("collection-title", params.title); this.phase = "preparing";
      return this.send("collection_create", params);
    });
    click("collection-resume", () => this.requestList(1, this.batch?.batch_id || this.restoreCandidate || this.legacyCandidate));
    click("collection-list-refresh", () => this.requestList(this.page));
    click("collection-list-prev", () => this.requestList(this.page - 1));
    click("collection-list-next", () => this.requestList(this.page + 1));
    click("collection-new-draft", () => {
      if (this.active || this.busy || !this.online || typeof this.recording.active !== "boolean" ||
        ["unresolved", "preparing", "restoring", "batch_unresolved"].includes(this.phase)) throw new Error("须先核对当前录制并完成收尾。");
      this.batch = null; this.list = {}; this.current = null; this.selected = null; this.phase = "idle";
      this.allowRestoreFinalized = false; this.restoreCandidate = null; this.restoreAttempted = true;
      try { if(this.storageIdentity)this.storage?.removeItem(boundBatchKey+this.storageIdentity); } catch { /* No storage dependency. */ }
      this.setInput("collection-batch-id", ""); this.setInput("collection-title", ""); this.render();
    });
    click("record-start", () => {
      const view = collectionRecordingView(this.recording, this.online);
      if (!this.batch || this.phase !== "draft" || this.active || !["idle", "finalized"].includes(view.stage))
        throw new Error("须先完成真实批次核对并明确准备本条；活动或未确认录制不能重新开录。");
      const params = startTrialParams(this.batch, { trial_id: this.current?.trial_id,
        category: this.input("episode-class"), notes: this.input("collection-notes") });
      this.current = { ...params, task: this.batch.task }; this.phase = "starting";
      return this.send("record_start", params);
    });
    click("collection-record-stop", () => {
      if (this.recording.active !== true || typeof this.recording.path !== "string" || !this.recording.path.startsWith("/"))
        throw new Error("没有已核实的活动录制路径，不能停止猜测的 episode。");
      this.phase = "stopping";
      return this.send("record_stop", { episode: this.recording.path });
    });
    click("collection-next", () => {
      if (!this.online || this.active || this.recording.active !== false || !this.current?.finalized)
        throw new Error("“下一条”须等待真实收尾完成；不会略过活动或未知记录。");
      this.prepareTrial(); this.render();
    });
    click("collection-record-retry", () => {
      if (!this.online || this.phase !== "unresolved" || this.recording.active !== false ||
        !batchPattern.test(this.batch?.batch_id || "") || !trialPattern.test(this.current?.trial_id || "") ||
        !this.available("record_retry")) throw new Error("须先核对无活动录制及本条冻结身份；重试能力尚未就绪。");
      this.phase = "starting";
      return this.send("record_retry", { batch_id: this.batch.batch_id, trial_id: this.current.trial_id });
    });
    for (const [id, verdict] of [["collection-accept", "accepted"], ["collection-reject", "rejected"]])
      click(id, () => this.send("collection_review", reviewEpisodeParams(this.batch?.batch_id, this.selected || {},
        this.input("collection-reviewer"), verdict, this.input("collection-review-note"))));
    for (const id of ["collection-category-filter", "collection-review-filter"])
      this.$(id).addEventListener("change", () => this.renderList());
    for (const id of ["task-name", "collection-operator", "collection-target", "episode-class", "collection-reviewer"])
      this.$(id).addEventListener("change", () => { this.savePreferences(); this.renderControls(); });
    for (const id of ["collection-notes", "collection-review-note"])
      this.$(id).addEventListener("input", () => this.renderControls());
  }
  restorePreferences() {
    try {
      const value = object(JSON.parse(this.storage?.getItem("tiger-collection-input-preferences-v1") || "{}"));
      for (const [id, key] of [["task-name", "task"], ["collection-operator", "operator"],
        ["collection-target", "target_count"], ["collection-reviewer", "reviewer"]])
        if (typeof value[key] === "string" || finite(value[key])) this.setInput(id, value[key]);
      if (categories.has(value.category)) this.setInput("episode-class", value.category);
      const recent = object(JSON.parse(this.storage?.getItem(lastBatchKey) || "{}"));
      if (batchPattern.test(recent.batch_id || "")) this.legacyCandidate = recent.batch_id;
    } catch { /* Input preferences are optional and never evidence. */ }
  }
  savePreferences() {
    try { this.storage?.setItem("tiger-collection-input-preferences-v1", JSON.stringify({
      task: this.input("task-name"), operator: this.input("collection-operator"),
      target_count: this.input("collection-target"),
      category: this.input("episode-class"), reviewer: this.input("collection-reviewer") })); } catch { /* No storage dependency. */ }
  }
  rememberBatch() {
    this.restoreCandidate = this.batch.batch_id; this.restoreAttempted = true;
    if (!this.storageIdentity || this.storageIdentity!==this.observedStorageIdentity) return;
    try { this.storage?.setItem(boundBatchKey+this.storageIdentity, JSON.stringify({ batch_id: this.batch.batch_id,
      storage_identity:this.storageIdentity })); } catch { /* Receipt remains usable without storage. */ }
  }
  updateStorageIdentity(state) {
    this.observedStorageIdentity=collectionStorageIdentity(state,this.online);
    if (!this.observedStorageIdentity || this.observedStorageIdentity===this.storageIdentity) return;
    if (this.active || this.busy || !["idle","finalized"].includes(collectionRecordingView(this.recording,this.online).stage) ||
        ["unresolved","preparing","batch_unresolved"].includes(this.phase) ||
        this.current && !this.current.finalized && this.phase!=="draft") return;
    const changed=this.storageIdentity!==null;
    this.storageIdentity=this.observedStorageIdentity;
    this.batch=null;this.list={};this.current=null;this.selected=null;this.phase="idle";
    this.restoreCandidate=null;this.restoreAttempted=false;this.restoreFailure=null;this.listAt=null;
    this.ownedBatchJobs.clear();if(changed)this.allowRestoreFinalized=false;
    this.setInput("collection-batch-id","");this.setInput("collection-title","");
    try {
      const saved=object(JSON.parse(this.storage?.getItem(boundBatchKey+this.storageIdentity)||"{}"));
      if(saved.storage_identity===this.storageIdentity && batchPattern.test(saved.batch_id||""))this.restoreCandidate=saved.batch_id;
    } catch { /* A malformed preference is not a verified batch. */ }
  }
  restoreVerifiedBatch() {
    // A local preference is only a lookup candidate. USB collection_list must verify it.
    if (!this.storageIdentity || this.storageIdentity!==this.observedStorageIdentity || this.restoreAttempted || this.batch || this.active || this.busy || this.phase !== "idle" ||
      this.recording.active !== false || !batchPattern.test(this.restoreCandidate || "") || !this.available("collection_list")) return;
    this.restoreAttempted = true;
    const identity=this.storageIdentity,batchId=this.restoreCandidate;
    queueMicrotask(() => {
      if (identity!==this.storageIdentity || identity!==this.observedStorageIdentity || batchId!==this.restoreCandidate)return;
      if (!this.batch && !this.active && !this.busy && this.phase === "idle" && this.recording.active === false && this.available("collection_list")) {
        this.phase = "restoring";
        this.guarded(() => this.requestList(1, batchId));
      } else this.restoreAttempted = false;
    });
  }
  prepareTrial() {
    const trial = this.uuid();
    if (!trialPattern.test(trial)) throw new Error("浏览器未能生成有效 UUID；没有开录。");
    this.current = { trial_id: trial, category: this.input("episode-class"), finalized: false };
    this.phase = "draft"; this.selected = null;
    this.setInput("collection-notes", ""); this.setInput("collection-review-note", "");
  }
  adoptBatch(batch, fromRecording = false) {
    if (!batchPattern.test(batch.batch_id || "")) throw new Error("批次回执身份无效。");
    if (this.active && this.batch?.batch_id !== batch.batch_id && !fromRecording) throw new Error("当前录制所属批次不能中途切换。");
    const changed = this.batch?.batch_id !== batch.batch_id;
    this.batch = { ...batch };
    for (const [id, key] of [["task-name", "task"], ["collection-title", "title"], ["collection-operator", "operator"],
      ["collection-target", "target_count"], ["collection-batch-id", "batch_id"]]) this.setInput(id, batch[key]);
    if (changed) { this.list = {}; this.selected = null; if (!this.active) this.current = null; }
    this.savePreferences();
  }
  async send(action, params) {
    if (!this.available(action)) { this.showError(this.capability(action).reason || "功能条件尚未核对。");
      if (action === "record_start") this.phase = "draft";
      if (action === "record_retry") this.phase = "unresolved";
      if (action === "record_stop") this.phase = this.recording.active === true ? "active" : "flushing";
      if (action === "collection_create") this.phase = "idle";
      if (action === "collection_list" && this.phase === "restoring") this.phase="idle";
      this.render(); return; }
    this.busy = action; this.render();
    const submittedIdentity=this.storageIdentity;
    try {
      const job = await this.submit(action, params);
      if (["collection_create","collection_list"].includes(action) && (job?.id || job?.job_id) && job?.request_rejected!==true) {
        this.ownedBatchJobs.set(String(job.id||job.job_id),{action,identity:submittedIdentity,batch_id:params.batch_id});
        if(this.ownedBatchJobs.size>32)this.ownedBatchJobs.delete(this.ownedBatchJobs.keys().next().value);
      }
      if (action === "collection_create" && job?.request_rejected === true) this.phase = "idle";
      else if (action === "collection_list" && job?.request_rejected === true && this.phase === "restoring")this.phase="idle";
      else if (job) this.onJob(job);
      else if (["record_start", "record_retry"].includes(action) && this.recording.active !== true) this.phase = "unresolved";
      else if (action === "record_stop") this.phase = "flushing";
      else if (action === "collection_create") this.phase = "batch_unresolved";
      else if (action === "collection_list" && this.phase === "restoring") this.phase = "idle";
    } catch (error) {
      if (["record_start", "record_retry"].includes(action) && this.recording.active !== true) this.phase = "unresolved";
      if (action === "collection_create") this.phase = "batch_unresolved";
      if (action === "collection_list" && this.phase === "restoring") this.phase = "idle";
      this.showError(error.message);
    }
    finally {
      this.busy = null; this.render();
      if (this.refreshNeeded && this.online && this.batch) {
        this.refreshNeeded = false;
        queueMicrotask(() => this.guarded(() => this.requestList(this.page)));
      }
    }
  }
  requestList(page, batchId = this.batch?.batch_id) {
    if (!batchPattern.test(batchId || "")) throw new Error("暂无可核对的历史采集，请先准备本次采集。");
    if (!Number.isInteger(page) || page < 1) throw new Error("页码无效。");
    if (this.active && this.batch?.batch_id !== batchId) throw new Error("活动录制期间不能切换批次。");
    if (!this.batch && this.phase==="idle")this.phase="restoring";
    return this.send("collection_list", { batch_id: batchId, page, page_size: 10 });
  }
  onState(state, online) {
    this.online = online === true && state.connected === true;
    if ("usb" in state) this.usb = object(state.usb);
    const diagnostic = collectionResult(state.status), hub = collectionResult(state.hub || diagnostic.shared_producer);
    this.recording = object(hub.recording || state.recording);
    const view = collectionRecordingView(this.recording, this.online), meta = object(this.recording.collection_v1);
    const metadataKnown = meta.schema === "tiger-collection-metadata-v1" &&
      batchPattern.test(meta.batch_id || "") && trialPattern.test(meta.trial_id || "");
    // V2/NO_SEND may record through the shared Hub without collection metadata.
    // Its finalized receipt releases only UI waiting, never an unresolved trial.
    if(view.finalized && !metadataKnown && (!this.current || this.current.finalized===true) &&
       ["active","flushing","stopping"].includes(this.phase))this.phase=this.batch?"finalized":"idle";
    if ("usb" in state || "usb_storage" in state) this.updateStorageIdentity(state);
    if (this.storageIdentity && this.observedStorageIdentity && this.storageIdentity!==this.observedStorageIdentity) {this.render();return;}
    if ('capture_scope' in state) {
      const scope = object(state.capture_scope);
      this.scope = object(scope.published || scope.configured);
    }
    if (this.online && this.recording.active === true) {
      this.phase = this.phase === "stopping" ? "stopping" : view.stage;
      if (metadataKnown) {
        if (!this.batch || this.batch.batch_id !== meta.batch_id) this.adoptBatch(meta, true);
        this.current = { ...meta, path: this.recording.path, finalized: false };
        this.setInput("episode-class", meta.category); this.setInput("collection-notes", meta.notes);
      }
    } else if (view.finalized && this.recording.path && metadataKnown &&
      (this.batch || this.allowRestoreFinalized)) {
      if ((!this.current || this.current.trial_id === meta.trial_id) && (!this.batch || this.batch.batch_id === meta.batch_id)) {
        if (!this.batch) this.adoptBatch(meta);
        this.current = { ...meta, path: this.recording.path, episode_id: this.recording.path.split("/").at(-1),
          status: this.recording.status, finalized: true };
        this.phase = "finalized";
        this.setInput("episode-class", meta.category); this.setInput("collection-notes", meta.notes);
      }
    } else if (this.online && this.recording.active === false && !this.recording.path &&
      ["active", "flushing", "stopping"].includes(this.phase)) {
      // An empty restarted Hub does not prove the prior episode finalized.
      // Keep its trial identity until an explicit USB list receipt resolves it.
      this.phase = "unresolved";
    }
    this.render();
    this.restoreVerifiedBatch();
  }
  onConnection(online) { this.online = online === true; this.render(); }
  onJob(job) {
    const status = job.state || job.status;
    if (!["succeeded", "completed", "failed", "blocked", "cancelled"].includes(status)) return;
    if (["collection_create","collection_list"].includes(job.action)) {
      const owner=this.ownedBatchJobs.get(String(job.id||job.job_id||""));
      if (!owner || owner.action!==job.action || owner.identity!==this.storageIdentity || owner.identity!==this.observedStorageIdentity)return;
      if (job.action==="collection_list" && ["succeeded","completed"].includes(status) &&
          collectionResult(job.result||job.output).batch?.batch_id!==owner.batch_id)return;
    }
    const key = String(job.id || job.job_id || "") + ":" + status;
    if (this.processed.has(key)) return;
    this.processed.add(key); if (this.processed.size > 100) this.processed.delete(this.processed.values().next().value);
    const result = collectionResult(job.result || job.output), success = ["succeeded", "completed"].includes(status);
    if (job.action === "collection_create" && success) {
      this.adoptBatch(object(result.batch || result)); this.list = {};
      this.rememberBatch();
      this.prepareTrial(); this.refreshNeeded = true;
    } else if (job.action === "collection_create" && !success && !this.batch) {
      this.phase = "idle";
    } else if (job.action === "collection_list" && success) {
      if (result.schema !== "tiger-collection-list-v1") { this.showError("批次列表版本未核对。"); return; }
      if (this.phase === "restoring") this.phase = "idle";
      if (this.active && this.batch?.batch_id !== result.batch?.batch_id) return;
      this.adoptBatch(object(result.batch)); this.list = result;
      this.rememberBatch();
      this.listAt = job.finished_at || job.updated_at || null;
      if (this.selected) this.selected = result.episodes?.find(item => item.episode_id === this.selected.episode_id) || this.selected;
      const pagination = object(result.pagination);
      this.page = count(pagination.page ?? result.page) || 1;
      if (!this.active) {
        const episodes = Array.isArray(result.episodes) ? result.episodes : [];
        const matched = this.current && episodes.find(item => item.trial_id === this.current.trial_id);
        if (!this.current && (pagination.total_items ?? result.total) === 0) this.prepareTrial();
        else if (matched || !this.current && episodes.length) {
          this.current = { ...this.current, ...(matched || episodes.at(-1)) }; this.phase = this.current.finalized === true ? "finalized" : "unresolved";
          this.setInput("episode-class", this.current.category);
          this.setInput("collection-notes", this.current.notes || "");
        }
      }
    } else if (job.action === "collection_list" && !success && this.phase === "restoring") {
      this.phase = "idle";
      this.restoreFailure=job.error || "此盘未能读取历史批次；可准备新的采集。";
    } else if (["record_start", "record_retry"].includes(job.action)) {
      if (!success && this.recording.active !== true) {
        this.phase = "unresolved";
        this.text("collection-recording-detail", "开录失败或结果未知，保留本条身份；读取批次与实际录制回执后再处理。");
      } else if (success) {
        const recording = object(result.recording || result);
        if (typeof recording.active === "boolean") this.onState({ connected: this.online, recording }, this.online);
      }
    } else if (job.action === "record_stop") {
      const recording = object(result.recording || result);
      if (typeof recording.active === "boolean") this.onState({ connected: this.online, recording }, this.online);
      this.refreshNeeded = true;
    } else if (job.action === "collection_review" && success) {
      if (this.selected?.episode_id === result.episode_id) this.selected = { ...this.selected, review: result };
      this.refreshNeeded = true;
    }
    this.render();
  }
  render() {
    const view = collectionRecordingView(this.recording, this.online);
    const state = this.$("recording-state"); state.textContent = view.label;
    state.className = "pill " + (!view.known ? "neutral" : this.recording.error || this.recording.status === "RECORDING_FAILED" ? "error" : view.stage === "active" ? "good" : "warn");
    this.text("recording-timer", view.elapsed);
    this.text("collection-samples", view.samples);
    const capture = object(this.recording.capture_contract || object(this.batch).capture_contract || this.scope);
    const scopedRoles = (key, allowed) => Array.isArray(capture[key]) && capture[key].length &&
      capture[key].every(role => allowed.includes(role)) ? capture[key] : allowed;
    this.text("collection-camera-counts", scopedRoles("required_camera_roles", ["front", "right", "left"])
      .map(role => role + " " + (count(view.cameras[role]) ?? "—")).join(" · "));
    this.text("collection-can-counts", scopedRoles("required_arm_roles", ["right", "left"])
      .map(role => role + " " + (count(view.can[role]) ?? "—")).join(" · "));
    this.text("collection-backlog", "状态 " + (view.queue ?? "—") + " · CAN " + (view.canQueue ?? "—"));
    this.text("collection-raw-bytes", view.known ? count(this.recording.raw_saved_bytes) : null);
    const capacity = collectionCapacityView(this.usb, this.online);
    this.text("collection-usb-free", capacity.value); this.text("collection-usb-checked", capacity.source);
    const quality = object(this.recording.quality_summary);
    this.text("collection-quality", view.known && quality.schema === "tiger-recording-quality-v1"
      ? "夹爪故障样本 " + (count(quality.gripper_fault_samples) ?? "—") + " · 未知样本 " + (count(quality.gripper_unknown_samples) ?? "—") +
        " · 坐标复核 " + (quality.coordinate_pending === true ? "待处理" : quality.coordinate_pending === false ? "未报告待处理" : "未知")
      : "采集质量当前未核对");
    this.text("collection-recording-error", view.error + " · 溢出 " + (view.overflows ?? "—"));
    this.text("collection-recording-detail", !this.online ? "连接状态未确认；历史录制任务不会因页面断线而停止或重新启动。"
      : this.phase === "unresolved" ? "本条结果尚未确认，保留原身份；显式读取批次 USB 回执后处理，不自动生成下一条或开录。"
      : ["starting", "stopping"].includes(this.phase) ? (this.phase === "starting" ? "开录请求已提交，等待真实录制身份；元数据已冻结。" : "结束请求已提交，正在等待真实收尾完成。")
      : view.label + "。元数据开录后冻结，下一条只准备新身份，不自动开录或移动。");
    this.text("collection-batch-state", this.batch ? (this.batch.title || "采集名称待回执") + " · " + collectionTimeLabel(this.batch.created_at) +
      (this.online ? " · 公共信息已核对。" : " · 当前连接未核对。") : this.phase === "restoring" ? "正在核对上次采集的 USB 回执，不会自动开录。"
      : ["preparing", "batch_unresolved"].includes(this.phase) ? "本次采集准备结果待核对，请勿重复创建。"
      : this.restoreFailure ? "历史批次读取失败；可准备新的采集。原始数据未修改。"
      : this.legacyCandidate ? "保留旧盘批次线索 "+this.legacyCandidate+"；未绑定当前U盘，不自动恢复。可准备新的采集。"
      : "准备本次采集后按北京时间命名，无需填写批次 ID。");
    this.$("collection-batch-state").title = this.batch ? "内部批次身份：" + this.batch.batch_id : "";
    const ordinal = count(this.current?.ordinal);
    const phases = { draft: "待开录", starting: "等待开录回执", active: "正在录制", stopping: "正在结束", flushing: "正在收尾", finalized: "已收尾", unresolved: "结果待核对" };
    this.text("collection-trial-state", this.current ? (ordinal && ordinal > 0 ? "第 " + ordinal + " 条" : "下一条 · 条序待开录回执") + " · " + (phases[this.phase] || this.phase) +
      (this.current.episode_id ? " · " + episodeTimeLabel(this.current) : "")
      : "准备本次采集后，再点击“开始本条”。");
    this.$("collection-trial-state").title = this.current ? "内部试次身份：" + this.current.trial_id + (this.current.path ? " · " + this.current.path : "") : "";
    const counters = collectionCountView(this.list, this.online);
    for (const [id, value] of [["collection-native-count", counters.native], ["collection-accepted-count", counters.accepted],
      ["collection-rejected-count", counters.rejected], ["collection-unreviewed-count", counters.unreviewed]]) this.text(id, value);
    this.text("collection-progress-text", (counters.accepted ?? "—") + " / " + (counters.target ?? "—") + " 人工合格");
    this.$("collection-progress").hidden = counters.progress === null;
    if (counters.progress !== null) this.$("collection-progress").value = counters.progress;
    this.text("collection-counts-source", counters.accepted === null ? "统计当前未核对，输入偏好不能作为保存或合格数量。"
      : "USB 批次查询快照 · 回执时间 " + (this.listAt || "未列出") + "；录制失败 " + (counters.failed ?? "—") + "。目标只按人工合格计数，保存后仍待复核与训练导出。");
    this.renderControls(); this.renderList(); this.renderReview();
  }
  renderControls() {
    const frozen = this.active || !!this.batch || !!this.busy || this.phase !== "idle";
    for (const id of ["task-name", "collection-title", "collection-operator", "collection-target"]) this.$(id).disabled = frozen;
    for (const id of ["episode-class", "collection-notes"]) this.$(id).disabled = this.active || this.phase !== "draft" || !!this.busy;
    this.$("collection-batch-id").disabled = true;
    this.$("collection-create").disabled = !this.available("collection_create") || !!this.batch || this.active || this.phase !== "idle" ||
      !["idle", "finalized"].includes(collectionRecordingView(this.recording, this.online).stage);
    this.$("collection-resume").disabled = !this.available("collection_list") || this.active || !batchPattern.test(this.batch?.batch_id || this.restoreCandidate || this.legacyCandidate || "");
    this.$("collection-new-draft").disabled = !this.online || this.active || !!this.busy || typeof this.recording.active !== "boolean" ||
      ["unresolved", "preparing", "restoring", "batch_unresolved"].includes(this.phase);
    this.$("record-start").disabled = !this.available("record_start") || !this.batch || this.phase !== "draft" ||
      !["idle", "finalized"].includes(collectionRecordingView(this.recording, this.online).stage);
    this.$("collection-record-stop").disabled = !this.available("record_stop") || this.recording.active !== true || typeof this.recording.path !== "string";
    this.$("collection-next").disabled = !this.online || !!this.busy || this.active || this.recording.active !== false || this.current?.finalized !== true;
    this.$("collection-record-retry").disabled = !this.available("record_retry") || this.phase !== "unresolved" ||
      this.recording.active !== false || !batchPattern.test(this.batch?.batch_id || "") || !trialPattern.test(this.current?.trial_id || "");
    this.$("collection-list-refresh").disabled = !this.available("collection_list") || !this.batch;
    const pagination = object(this.list.pagination), totalPages = count(pagination.total_pages);
    this.$("collection-list-prev").disabled = !this.available("collection_list") || !this.batch || this.page <= 1;
    this.$("collection-list-next").disabled = !this.available("collection_list") || !this.batch || totalPages === null || this.page >= totalPages;
    for (const [id, verdict] of [["collection-accept", "accepted"], ["collection-reject", "rejected"]]) {
      let valid = false;
      try { reviewEpisodeParams(this.batch?.batch_id, this.selected || {}, this.input("collection-reviewer"), verdict, this.input("collection-review-note")); valid = true; } catch { /* Explain on explicit click. */ }
      this.$(id).disabled = !this.available("collection_review") || !valid;
    }
  }
  renderList() {
    const node = this.$("collection-episode-list"); node.replaceChildren();
    const known = this.online && this.list.schema === "tiger-collection-list-v1";
    if (!known) { node.className = "empty-line"; node.textContent = "真实批次列表当前未核对；恢复连接后显式读取。"; this.text("collection-list-page", "—"); return; }
    const pagination = object(this.list.pagination), episodes = Array.isArray(this.list.episodes) ? this.list.episodes : [];
    const totalPages = count(pagination.total_pages);
    this.text("collection-list-page", totalPages === 0 ? "空批次 · 第 1 页" : "第 " + this.page + " / " + (totalPages ?? "—") + " 页");
    this.text("collection-list-source", "本页返回 " + episodes.length + " 条 / 批次 " + (count(pagination.total_items) ?? "—") + " 次尝试；筛选仅当前页，不代表整个批次筛选结果。");
    const filtered = filterCollectionPage(episodes, this.input("collection-category-filter"), this.input("collection-review-filter"));
    if (!filtered.length) { node.className = "empty-line"; node.textContent = episodes.length ? "本页没有符合筛选的条目。" : "本页 USB 回执没有列出条目。"; return; }
    node.className = "";
    for (const episode of filtered) {
      const row = this.document.createElement("article"); row.className = "collection-episode-row";
      const main = this.document.createElement("div"), label = this.document.createElement("strong"), detail = this.document.createElement("p"), review = this.document.createElement("small");
      label.textContent = (count(episode.ordinal) > 0 ? "第 " + episode.ordinal + " 条 · " : "条序未列出 · ") +
        episodeTimeLabel(episode);
      label.title = episode.episode_id || episode.trial_id || episode.id || "身份缺失，不能复核";
      detail.textContent = (episode.category || "类别未知") + " · " + (episode.status || "保存结果未知") + " · 状态样本 " + (count(episode.samples) ?? "—") + " · " + (episode.path || "尚无原生文件路径");
      review.textContent = "人工复核：" + (object(episode.review).verdict || "待复核") + (episode.failure ? " · 失败：" + episode.failure : "") + (episode.accept_blocked_reason ? " · 合格条件：" + episode.accept_blocked_reason : "");
      main.append(label, detail, review);
      const button = this.document.createElement("button"); button.className = "button quiet"; button.textContent = "选择本条复核";
      button.disabled = !isFinalizedEpisode(episode) || !this.available("collection_review");
      button.addEventListener("click", () => { this.selected = { ...episode }; this.setInput("collection-review-note", ""); this.renderControls(); this.renderReview(); });
      row.append(main, button); node.append(row);
    }
  }
  renderReview() {
    this.$("collection-review-panel").hidden = !this.selected;
    this.text("collection-review-identity", this.selected ? this.selected.episode_id + " · " + this.selected.path +
      " · " + (this.selected.status || "未知") : "未选择真实已收尾 episode。");
  }
}
