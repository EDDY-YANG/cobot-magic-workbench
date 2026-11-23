import test from "node:test";
import assert from "node:assert/strict";
import {deviceView} from "../lab_stack/web/device-mode-panel.mjs";
const state={connected:true,controller_modes:{right:{fresh:true,classification:"CAN_MOVEJ",enabled_fresh:true,enabled:[true,true,true,true,true,true],age_status_s:.02}},capabilities:{mode_takeover:{available:true},mode_disable:{available:true}}};
test("only native right controls can be used",()=>{assert.equal(deviceView(state,"RE",true).actions.find(a=>a.key==="pc").enabled,true);assert.equal(deviceView(state,"LE",true).actions.some(a=>a.enabled),false);});
test("teacher modes do not borrow follower feedback",()=>{const v=deviceView(state,"RT",true);assert.equal(v.fresh,false);assert.equal(v.actions.some(a=>a.enabled),false);});
test("offline state revokes all controls",()=>{assert.equal(deviceView(state,"RE",false).actions.some(a=>a.enabled),false);});
test("standby is not advertised as hand guiding",()=>{const v=deviceView({...state,controller_modes:{right:{fresh:true,classification:"STANDBY",label:"待机"}}},"RE",true);assert.equal(v.actions.find(a=>a.key==="drag").enabled,false);});
test("disabled motors distinguished from power-off",()=>{const s=structuredClone(state);s.controller_modes.right.enabled.fill(false);assert.equal(deviceView(s,"RE",true).status,"Motor disabled / 电机失能");});
