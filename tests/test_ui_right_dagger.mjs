import test from "node:test";
import assert from "node:assert/strict";
import {pathToFileURL} from "node:url";
import {resolve} from "node:path";

async function setup() {
  const elements = new Map();
  global.document = {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {
      textContent: "", hidden: false, disabled: false, events: {},
      addEventListener(name, callback) { this.events[name] = callback; }
    });
    return elements.get(id);
  }};
  const url = pathToFileURL(resolve("lab_stack/web/dagger-view.mjs"));
  const ui = await import(url.href + "?case=" + Math.random());
  return {ui, get: id => document.getElementById("dg-" + id)};
}

function state(fields = {}) {
  return {
    binding: "control-session", policy_job_id: "model-job", phase: "待准备", phase_code: "idle",
    policy: "等待模型任务", record: "待录制", arms: {},
    hardware_validated: false, can_prepare: true, can_enter: false,
    can_resume: false, can_finish: false, ...fields
  };
}

test("disconnection disables all commands", async () => {
  const {ui, get} = await setup();
  ui.displayDagger(null);
  for (const id of ["prepare", "enter", "resume", "finish"]) assert.equal(get(id).disabled, true);
  assert.equal(get("validation").textContent, "尚未真机验收");
});

test("preparation forwards the explicit button declaration through existing jobs API", async () => {
  const {ui, get} = await setup();
  const sent = [];
  ui.bindDagger(async (url, options) => {
    sent.push([url, JSON.parse(options.body)]);
    return {job: state({phase: "准备中", can_prepare: false})};
  });
  ui.displayDagger(state());
  await get("prepare").events.click();
  assert.deepEqual(sent, [["/api/jobs", {action: "dagger_prepare", params: {onsite_confirmed: true}}]]);
});

test("intervention resume and finish preserve the control session binding", async () => {
  for (const [id, action] of [["enter", "dagger_intervene"], ["resume", "dagger_resume"], ["finish", "dagger_finish"]]) {
    const {ui, get} = await setup();
    let sent;
    const view = state({["can_" + id]: true});
    ui.bindDagger(async (_, options) => {
      sent = JSON.parse(options.body);
      return {job: view};
    });
    ui.displayDagger(view);
    await get(id).events.click();
    assert.deepEqual(sent, {action, params: {binding: "control-session", policy_job_id: "model-job"}});
  }
});

test("double click does not submit a second transition", async () => {
  const {ui, get} = await setup();
  let release;
  let calls = 0;
  ui.bindDagger(() => {
    calls++;
    return new Promise(resolve => { release = () => resolve({job: state()}); });
  });
  ui.displayDagger(state());
  const first = get("prepare").events.click();
  await get("prepare").events.click();
  assert.equal(calls, 1);
  release();
  await first;
});

test("missing or stale joint input stays unknown", async () => {
  const {ui, get} = await setup();
  ui.displayDagger(state({phase_code: "human", arms: {
    rear: {q_deg: [1, 2, 3, 4, 5, 6], age_joint_s: 3, fresh: false},
    front: {q_deg: [1, 2, 3, 4, 5, 6], age_joint_s: .01, fresh: true}
  }}));
  assert.equal(get("rear-q").textContent, "—");
  assert.match(get("rear-s").textContent, /主臂角色/);
  assert.match(get("front-q").textContent, /1.0°/);
});

test("RPC error is shown and does not become success feedback", async () => {
  const {ui, get} = await setup();
  ui.bindDagger(async () => { throw new Error("role acknowledgement missing"); });
  ui.displayDagger(state());
  await get("prepare").events.click();
  assert.equal(get("error").hidden, false);
  assert.equal(get("error").textContent, "role acknowledgement missing");
  assert.equal(get("validation").textContent, "尚未真机验收");
});
