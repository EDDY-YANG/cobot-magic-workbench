const el = id => document.getElementById("dg-" + id);
let session = null;
let busy = false;
let lastBinding = null;
let lastPhase = null;
const actions = {
  prepare: "dagger_prepare", enter: "dagger_intervene",
  resume: "dagger_resume", finish: "dagger_finish"
};

function message(text) {
  el("error").textContent = text || "";
  el("error").hidden = !text;
}

export function displayDagger(state) {
  session = state;
  if (lastBinding !== state?.binding || lastPhase !== state?.phase_code) {
    message("");
    lastBinding = state?.binding;
    lastPhase = state?.phase_code;
  }
  el("phase").textContent = state?.phase || "状态未更新";
  el("policy").textContent = state?.policy || "待核对";
  el("record").textContent = state?.record || "待核对";
  el("note").textContent = state?.note || "等待读取 4090 工作台状态。";
  el("validation").textContent = state?.hardware_validated === true ? "已验收" : "尚未真机验收";
  el("episode").hidden = !state?.episode;
  el("episode").textContent = state?.episode || "";
  for (const side of ["rear", "front"]) {
    const arm = state?.arms?.[side] || {};
    const fresh = (arm.feedback_fresh === true || arm.fresh === true) && Number.isFinite(arm.age_joint_s) && arm.age_joint_s <= .2;
    el(side + "-q").textContent = fresh && Array.isArray(arm.q_deg) && arm.q_deg.length === 6
      && arm.q_deg.every(Number.isFinite) ? arm.q_deg.map(value => value.toFixed(1) + "°").join("  ") : "—";
    const gripFresh = Number.isFinite(arm.age_gripper_s) && arm.age_gripper_s <= .2;
    el(side + "-g").textContent = gripFresh && Number.isFinite(arm.grip_m)
      ? (arm.grip_m * 1000).toFixed(1) + " mm" : "—";
    el(side + "-s").textContent = fresh ? "实时反馈" : side === "rear" && state?.phase_code === "human"
      ? "主臂角色，使用目标输入" : "反馈待核对";
  }
  for (const id of Object.keys(actions)) {
    el(id).disabled = busy || state?.["can_" + id] !== true;
  }
}

export function bindDagger(api) {
  for (const [id, action] of Object.entries(actions)) {
    el(id).addEventListener("click", async () => {
      if (busy || session?.["can_" + id] !== true) return;
      const params = id === "prepare" ? {onsite_confirmed: true} : {binding: session.binding, policy_job_id: session.policy_job_id};
      busy = true;
      displayDagger(session);
      message("");
      try {
        const result = await api("/api/jobs", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action, params})
        });
        displayDagger(result.job);
      } catch (error) {
        message(error.message);
      } finally {
        busy = false;
        displayDagger(session);
      }
    });
  }
}
