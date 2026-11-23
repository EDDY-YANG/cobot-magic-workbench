import "./test_device_modes.mjs";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import vm from "node:vm";
import {deviceView} from "../lab_stack/web/device-mode-panel.mjs";

const read = path => readFileSync(new URL("../lab_stack/web/" + path, import.meta.url), "utf8");
const app = read("app.js");
const html = read("index.html");
const getFunction = name => {
  const start = app.indexOf("function " + name + "(");
  assert.notEqual(start,-1);
  return app.slice(start, app.indexOf("\n}",start)+2);
};
test("all mode changes have an explicit supported target",()=>{
  const ctx={};
  vm.runInNewContext(getFunction("modeConfirmationFields")+"\n"+getFunction("modeFieldConfirm"),ctx);
  for(const arm of ["left","LE","LT","RT","BASE",undefined])
    assert.throws(()=>ctx.modeFieldConfirm("mode_takeover",{arm}));
  const request=ctx.modeFieldConfirm("mode_takeover",{arm:"right"});
  assert.equal(request.arm,"right");
  assert.equal(request.leader_isolated,true);
  assert.equal(request.teaching_ended,true);
  for(const action of ["mode_high_follow","mode_standby","mode_reset","mode_inspect"])
    assert.match(html,new RegExp('data-action="'+action+'" data-params=\\x27\\{"arm":"right"'));
});
test("ordinary mode switches have one on-screen source, clear-stop has one global action",()=>{
  assert.doesNotMatch(html,/data-action="(?:mode_takeover|mode_disable)"/);
  assert.equal((html.match(/data-action="clear_stop"/g)||[]).length,1);
});
test("the card reflects the same feedback block as submission",()=>{
  const s={connected:true,controller_modes:{right:{fresh:true,classification:"CAN_MOVEJ"}},
    capabilities:{mode_takeover:{available:true},mode_disable:{available:true}}};
  const resolve=action=>({available:action!=="mode_takeover",reason:action==="mode_takeover"?"CAN feedback unavailable":""});
  const actions=deviceView(s,"RE",true,resolve).actions;
  assert.equal(actions.find(a=>a.key==="pc").enabled,false);
  assert.equal(actions.find(a=>a.key==="pc").reason,"CAN feedback unavailable");
  assert.equal(actions.find(a=>a.key==="disable").enabled,true);
  assert.equal(deviceView(s,"RT",true,resolve).actions.some(a=>a.enabled),false);
});
