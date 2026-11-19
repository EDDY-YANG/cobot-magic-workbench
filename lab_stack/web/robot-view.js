/**
 * Official PiPER URDF viewer. Browser only: no network motion request exists here.
 * FK uses original URDF origins, local axes and SI units; rpy is Rz(yaw)Ry(pitch)Rx(roll).
 * CAD corresponds to the public PiPER description, NOT a verified digital twin of
 * the mounted Cobot rig. Calibration/base mounting/TCP/collision remain unverified.
 * Dragging the local-axis ring edits a target draft; it never changes real feedback.
 * This is joint-space editing, not Cartesian IK. Firmware command limits belong to
 * the backend; URDF limits shown here describe the asset only.
 * Run via the PC web launcher, not by opening this ES module with file://.
 */
import * as THREE from './assets/robot/vendor/three.module.js';
import { OrbitControls } from './assets/robot/vendor/OrbitControls.js';
import { TransformControls } from './assets/robot/vendor/TransformControls.js';
import { STLLoader } from './assets/robot/vendor/STLLoader.js';

const RAD = Math.PI / 180;
const DEG = 180 / Math.PI;
const qValid = q => Array.isArray(q) && q.length === 6 && q.every(Number.isFinite);
const numbers = (s, fallback = [0, 0, 0]) => s ? s.trim().split(/\s+/).map(Number) : fallback;
const byTag = (node, name) => [...node.children].find(c => c.tagName === name);

function setOrigin(object, node) {
  const origin = byTag(node, 'origin');
  const xyz = numbers(origin?.getAttribute('xyz'));
  const rpy = numbers(origin?.getAttribute('rpy'));
  object.position.fromArray(xyz);
  object.quaternion.setFromEuler(new THREE.Euler(...rpy, 'ZYX'));
}

/** Parse geometry/kinematics only; dynamics and collision are intentionally unused. */
export function parseUrdf(xml) {
  const doc = new DOMParser().parseFromString(xml, 'application/xml');
  if (doc.querySelector('parsererror')) throw new Error('官方 URDF XML 解析失败');
  const robot = doc.documentElement;
  const links = [...robot.children].filter(x => x.tagName === 'link');
  const joints = [...robot.children].filter(x => x.tagName === 'joint').map(node => ({
    name: node.getAttribute('name'), type: node.getAttribute('type'), node,
    parent: byTag(node, 'parent')?.getAttribute('link'),
    child: byTag(node, 'child')?.getAttribute('link'),
    axis: numbers(byTag(node, 'axis')?.getAttribute('xyz'), [1, 0, 0]),
    lower: Number(byTag(node, 'limit')?.getAttribute('lower') ?? 0),
    upper: Number(byTag(node, 'limit')?.getAttribute('upper') ?? 0),
  }));
  const children = new Set(joints.map(j => j.child));
  const roots = links.map(l => l.getAttribute('name')).filter(n => !children.has(n));
  if (roots.length !== 1) throw new Error('URDF 根链接不唯一');
  return {links, joints, root: roots[0]};
}

async function buildRobot(description, assetBase, style) {
  const links = new Map(description.links.map(n => [n.getAttribute('name'), new THREE.Group()]));
  const joints = new Map();
  for (const spec of description.joints) {
    const origin = new THREE.Group(); setOrigin(origin, spec.node);
    const motion = new THREE.Group();
    origin.add(motion); motion.add(links.get(spec.child)); links.get(spec.parent).add(origin);
    joints.set(spec.name, {...spec, origin, motion, axisVector: new THREE.Vector3(...spec.axis).normalize()});
  }
  const loader = new STLLoader();
  const materials = [];
  const meshes = [];
  await Promise.all(description.links.map(async node => {
    const link = links.get(node.getAttribute('name'));
    for (const visual of [...node.children].filter(n => n.tagName === 'visual')) {
      const mesh = byTag(byTag(visual, 'geometry'), 'mesh');
      if (!mesh) continue;
      const packagePath = mesh.getAttribute('filename');
      if (!packagePath?.startsWith('package://piper_description/meshes/')) throw new Error(`不支持的 mesh 引用：${packagePath}`);
      const filename = packagePath.split('/').pop();
      const geometry = await loader.loadAsync(new URL(`meshes/${filename}`, assetBase).href);
      geometry.computeVertexNormals();
      const material = new THREE.MeshStandardMaterial({
        color: style.color, metalness: 0.34, roughness: 0.42,
        transparent: style.ghost, opacity: style.ghost ? 0.34 : 1,
        depthWrite: !style.ghost, polygonOffset: style.ghost,
        polygonOffsetFactor: style.ghost ? -1 : 0,
      });
      materials.push(material);
      const shape = new THREE.Mesh(geometry, material);
      setOrigin(shape, visual);
      shape.scale.fromArray(numbers(mesh.getAttribute('scale'), [1, 1, 1]));
      shape.castShadow = !style.ghost; shape.receiveShadow = !style.ghost;
      shape.userData.link = node.getAttribute('name');
      const parentJoint = description.joints.find(j => j.child === shape.userData.link);
      shape.userData.joint = parentJoint?.name;
      shape.renderOrder = style.ghost ? 2 : 0;
      link.add(shape); meshes.push(shape);
    }
  }));
  const root = links.get(description.root);
  return {root, links, joints, materials, meshes, setQ(q, gripper = null) {
    q.forEach((deg, i) => {
      const j = joints.get(`joint${i + 1}`);
      if (!j) throw new Error(`URDF 缺失 joint${i + 1}`);
      j.motion.quaternion.setFromAxisAngle(j.axisVector, deg * RAD);
    });
    if (Number.isFinite(gripper)) {
      // Display-only candidate mapping from the ROS symmetric two-finger convention.
      // Never clamp or rewrite real sensor width; the mechanical mapping is unverified.
      for (const [name, value] of [['joint7', gripper / 2], ['joint8', -gripper / 2]]) {
        const j = joints.get(name);
        if (j) j.motion.position.copy(j.axisVector).multiplyScalar(value);
      }
    }
    root.updateMatrixWorld(true);
  }};
}

export async function createRobotView(container, options = {}) {
  const {onTargetChange = () => {}, manifestUrl = './assets/robot/manifest.json'} = options;
  const manifestAddress = new URL(manifestUrl, document.baseURI);
  const assetBase = new URL('.', manifestAddress);
  const response = await fetch(manifestAddress); if (!response.ok) throw new Error('机器人资产清单加载失败');
  const manifest = await response.json();
  const urdfResponse = await fetch(new URL(manifest.urdf, assetBase));
  if (!urdfResponse.ok) throw new Error('官方 URDF 加载失败');
  const description = parseUrdf(await urdfResponse.text());
  container.style.position = 'relative';
  container.style.overflow = 'hidden';
  const frame = document.createElement('div');
  // Reserve real layout space for HUD/footer instead of drawing behind them.
  Object.assign(frame.style, {position:'absolute', top:'74px', bottom:'62px', left:'0', right:'0', background:'#f6f6f6'});
  container.append(frame);
  const renderer = new THREE.WebGLRenderer({antialias:true, alpha:true});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.35;
  frame.append(renderer.domElement);
  // setSize(..., false) changes physical pixels only. Explicit CSS size avoids
  // a devicePixelRatio=2 canvas becoming twice its viewport and being cropped.
  Object.assign(renderer.domElement.style, {display:'block',width:'100%',height:'100%'});
  renderer.domElement.setAttribute('aria-label', 'PiPER 三维目标预览；拖动关节圆环只编辑目标，不发送机械臂指令');
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, 1, 0.005, 10);
  camera.up.set(0, 0, 1); camera.position.set(0.92, -0.94, 0.8);
  const orbit = new OrbitControls(camera, renderer.domElement);
  orbit.target.set(0.14, 0, 0.24); orbit.enableDamping = true;
  orbit.minDistance = 0.25; orbit.maxDistance = 2.8;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 2.1));
  const key = new THREE.DirectionalLight(0xffffff, 3.5); key.position.set(-0.3, -0.6, 1); scene.add(key);
  const fill = new THREE.DirectionalLight(0xdddddd, 1.8); fill.position.set(0.5, 0.6, 0.3); scene.add(fill);
  const grid = new THREE.GridHelper(1.4, 28, 0x777777, 0xcccccc);
  grid.rotation.x = Math.PI / 2; grid.position.z = -0.004;
  grid.material.transparent = true; grid.material.opacity = 0.65; scene.add(grid);
  const baseAxes = new THREE.AxesHelper(0.12); baseAxes.setColors(0x111111, 0x777777, 0xbbbbbb); scene.add(baseAxes);
  const [actual, target] = await Promise.all([
    buildRobot(description, assetBase, {color:0x555555, ghost:false}),
    buildRobot(description, assetBase, {color:0x111111, ghost:true}),
  ]);
  scene.add(actual.root, target.root); actual.root.visible = false;
  let actualQ = null, targetQ = [0,0,0,0,0,0], selected = 0, enabled = true;
  let actualReceivedAt = 0, feedbackAgeAtReceipt = Infinity, feedbackValid = false, disposed = false;
  let sourceName = '未连接反馈';
  let fitPending = true, firstActual = true, firstTarget = true, hasSized = false;
  target.setQ(targetQ);

  const gizmo = new TransformControls(camera, renderer.domElement);
  gizmo.setMode('rotate'); gizmo.setSpace('local'); gizmo.setSize(1.25);
  gizmo.showX = false; gizmo.showY = false; gizmo.showZ = true;
  // Paint only the renderer's editing handles; never filter camera or source-media pixels.
  // TransformControls changes its own highlight colours before rendering, so apply ink here.
  gizmo.traverse(node => {
    if (!node.material) return;
    node.onBeforeRender = () => {
      for (const material of Array.isArray(node.material) ? node.material : [node.material]) {
        if (material.color) material.color.setHex(node.name === 'Z' ? 0x111111 : 0x666666);
      }
    };
  });
  scene.add(gizmo);
  const hud = document.createElement('div');
  Object.assign(hud.style, {position:'absolute',top:'12px',left:'12px',right:'12px',pointerEvents:'none',font:'12px monospace',color:'#111111'});
  const jointBar = document.createElement('div');
  jointBar.style.cssText = 'display:flex;gap:5px;flex-wrap:wrap;pointer-events:auto';
  const buttons = Array.from({length:6}, (_,i) => {
    const b = document.createElement('button'); b.type='button'; b.textContent=`J${i+1}`;
    b.title = `选择 J${i+1}，在模型圆环上拖动旋转；只编辑目标`;
    b.style.cssText='border:1px solid #111;border-radius:0;padding:6px 10px;background:#fff;color:#111;cursor:pointer';
    b.onclick=()=>selectJoint(i); jointBar.append(b); return b;
  });
  const viewButton = document.createElement('button'); viewButton.textContent='复位视角'; viewButton.type='button';
  viewButton.style.cssText=buttons[0].style.cssText; viewButton.onclick=()=>{fitPending=true};jointBar.append(viewButton);
  const status = document.createElement('div'); status.style.cssText='margin-top:8px;line-height:1.6';
  hud.append(jointBar,status); container.append(hud);
  const footer = document.createElement('div');
  footer.style.cssText='position:absolute;bottom:10px;left:12px;right:12px;color:#111;font:11px/1.6 monospace;pointer-events:none';
  footer.textContent='灰色是实际姿态，黑色是目标预览。拖动只改预览，现场坐标标定另行核对。';container.append(footer);
  const flange = document.createElement('div'); flange.style.cssText='margin-top:4px;color:#444';footer.append(flange);

  function selectJoint(i) {
    if (!Number.isInteger(i) || i < 0 || i > 5) return;
    selected = i;
    buttons.forEach((b,k)=>{b.style.color=k===i?'#fff':'#111';b.style.background=k===i?'#111':'#fff';b.style.borderColor='#111'});
    const j=target.joints.get(`joint${i+1}`);
    // Official PiPER revolute axes are local +Z. Do not silently permit other axes.
    if (j.axisVector.distanceTo(new THREE.Vector3(0,0,1)) > 1e-8) throw new Error('资产关节轴不支持此旋转编辑器');
    gizmo.attach(j.motion); gizmo.enabled=enabled;
  }
  selectJoint(0);
  gizmo.addEventListener('dragging-changed', event=>{orbit.enabled=!event.value});
  gizmo.addEventListener('objectChange', ()=>{
    const j=target.joints.get(`joint${selected+1}`);
    const q=j.motion.quaternion;
    let angle=2*Math.atan2(q.z,q.w)*DEG;
    // unwrap relative to draft; preserves continuity near +/-180 degrees.
    while(angle-targetQ[selected]>180) angle-=360;
    while(angle-targetQ[selected]<-180) angle+=360;
    targetQ[selected]=angle; target.setQ(targetQ);
    onTargetChange([...targetQ],{source:'3d-joint-drag',joint:selected+1,commandSent:false});
  });
  const raycaster = new THREE.Raycaster(); let pointerStart = null;
  function pointerDown(e) {pointerStart=[e.clientX,e.clientY]}
  function pointerUp(e) {
    if (!pointerStart || gizmo.dragging || Math.hypot(e.clientX-pointerStart[0],e.clientY-pointerStart[1])>4) return;
    const rect=renderer.domElement.getBoundingClientRect();
    raycaster.setFromCamera(new THREE.Vector2((e.clientX-rect.left)/rect.width*2-1,-(e.clientY-rect.top)/rect.height*2+1),camera);
    const hits=raycaster.intersectObjects(target.meshes.concat(actual.root.visible?actual.meshes:[]),false);
    const hit=hits.find(h=>/^joint[1-6]$/.test(h.object.userData.joint||''));
    if(hit)selectJoint(Number(hit.object.userData.joint.slice(5))-1);
  }
  renderer.domElement.addEventListener('pointerdown',pointerDown);
  renderer.domElement.addEventListener('pointerup',pointerUp);
  function fitView() {
    // The box contains robot meshes only: gizmo/grid/axes do not inflate it.
    // Neither mesh geometry nor FK values are modified by camera framing.
    scene.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(target.root);
    if(actual.root.visible) box.expandByObject(actual.root);
    if(box.isEmpty())return;
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const halfVertical = THREE.MathUtils.degToRad(camera.fov)/2;
    const halfHorizontal = Math.atan(Math.tan(halfVertical)*camera.aspect);
    const distance = Math.max(sphere.radius,0.04)/Math.sin(Math.min(halfVertical,halfHorizontal))*1.16;
    const direction = new THREE.Vector3(.85,-1,.68).normalize();
    orbit.target.copy(sphere.center);
    camera.position.copy(sphere.center).addScaledVector(direction,distance);
    camera.near = Math.max(.001,distance/200);
    camera.far = Math.max(10,distance*8);
    camera.updateProjectionMatrix(); camera.lookAt(sphere.center);
    orbit.minDistance=Math.max(.1,sphere.radius*.5);
    orbit.maxDistance=Math.max(2.8,distance*4);
    orbit.update();fitPending=false;
  }
  function resize() {
    const w=frame.clientWidth,h=frame.clientHeight;
    // A hidden tab has zero layout dimensions; defer fit until first visibility.
    if(w<2||h<2)return;
    renderer.setSize(w,h,false); camera.aspect=w/h;camera.updateProjectionMatrix();
    if(!hasSized){fitPending=true;hasSized=true}
  }
  const observer=new ResizeObserver(resize);observer.observe(container);resize();
  let frameId,lastRendered=-Infinity,lastHudUpdate=-Infinity,lastStale=null;
  function animate(now=performance.now()) {
    if(disposed)return;
    frameId=requestAnimationFrame(animate);
    // Render at <=30 fps; hidden tabs/pages perform no DOM/material/render work.
    if(document.hidden||!container.offsetParent||!hasSized||now-lastRendered<1000/30)return;
    lastRendered=now;
    if(fitPending)fitView();
    const age=feedbackAgeAtReceipt+(performance.now()-actualReceivedAt)/1000;
    const stale=feedbackValid&&age>0.5;
    if(stale!==lastStale){actual.materials.forEach(m=>m.color.setHex(stale?0x999999:0x555555));lastStale=stale}
    if(now-lastHudUpdate>=200){
      lastHudUpdate=now;
      const feedback=feedbackValid ? `实际反馈${stale?'已过期':'在线'} · 年龄含页面等待 ${age.toFixed(2)} s · ${sourceName}` : '尚无有效实际反馈；模型显示目标草稿';
      const j=target.joints.get(`joint${selected+1}`);
      const out=targetQ[selected]*RAD<j.lower||targetQ[selected]*RAD>j.upper;
      const caption=`${feedback}　|　J${selected+1} 目标 ${targetQ[selected].toFixed(2)}°${out?' · 超出此 URDF 范围（未发送）':''}`;
      if(status.textContent!==caption)status.textContent=caption;
      status.style.color='#111';status.style.fontWeight=stale||out?'700':'400';
      const world=target.links.get('gripper_base').getWorldPosition(new THREE.Vector3());
      const coordinateText=`目标法兰 FK（基座坐标，非已标定 TCP）：x ${world.x.toFixed(3)} · y ${world.y.toFixed(3)} · z ${world.z.toFixed(3)} m`;
      if(flange.textContent!==coordinateText)flange.textContent=coordinateText;
    }
    orbit.update();renderer.render(scene,camera);
  }
  animate();
  return {
    manifest,
    setActual(q, meta={}) {
      if(typeof meta==='number')meta={gripper_m:meta,age_s:0,valid:true};
      if(!qValid(q)||meta.valid===false){feedbackValid=false;actual.root.visible=false;return}
      const age=meta.age_s??meta.feedback_age_s;
      if(!Number.isFinite(age)||age<0){feedbackValid=false;actual.root.visible=false;return}
      actualQ=[...q];actual.setQ(q,meta.gripper_m);actual.root.visible=true;
      feedbackValid=true;actualReceivedAt=performance.now();feedbackAgeAtReceipt=age;sourceName=String(meta.source||'CAN');
      if(firstActual){fitPending=true;firstActual=false}
    },
    setTarget(q) {if(!qValid(q))throw new Error('目标必须是六个有限角度（度）');targetQ=[...q];target.setQ(targetQ);if(firstTarget){fitPending=true;firstTarget=false}},
    getTarget(){return [...targetQ]},
    getActual(){return actualQ?[...actualQ]:null},
    selectJoint, resize, fitView:()=>{fitPending=true},
    setEnabled(value){enabled=Boolean(value);gizmo.enabled=enabled;buttons.forEach(b=>b.disabled=!enabled)},
    dispose(){disposed=true;cancelAnimationFrame(frameId);observer.disconnect();orbit.dispose();gizmo.dispose();
      renderer.domElement.removeEventListener('pointerdown',pointerDown);renderer.domElement.removeEventListener('pointerup',pointerUp);
      for(const robot of [actual,target]){robot.meshes.forEach(m=>m.geometry.dispose());robot.materials.forEach(m=>m.dispose())}
      grid.geometry.dispose();grid.material.dispose();renderer.dispose();frame.remove();hud.remove();footer.remove();},
  };
}

export const RobotView = {mount:createRobotView};
