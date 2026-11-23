/* Render the pinned AgileX URDF at source zero pose. Model bytes stay upstream. */
import * as THREE from './vendor/three/three.module.js';
import { ColladaLoader } from './vendor/three/loaders/ColladaLoader.js';

const assembly = document.querySelector('[data-assembly-render]');
const decoder = new TextDecoder();
const collada = new ColladaLoader();

function verify(condition, message) {
  if (!condition) throw new Error(message);
}

async function checkedSource(root, path, expected, signal) {
  const response = await fetch(new URL(path, root), { signal, referrerPolicy: 'no-referrer' });
  verify(response.ok, 'Upstream asset unavailable: ' + path);
  const bytes = await response.arrayBuffer();
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  verify(hash === expected, 'Upstream asset identity differs: ' + path);
  return decoder.decode(bytes);
}

function xmlDocument(text) {
  verify(!/<!DOCTYPE/i.test(text), 'Document type declarations are not supported');
  const doc = new DOMParser().parseFromString(text, 'application/xml');
  verify(!doc.querySelector('parsererror'), 'Could not parse the upstream document');
  return doc;
}

function values(text, fallback) {
  const result = text ? text.trim().split(/\s+/).map(Number) : fallback;
  verify(result.length === 3 && result.every(Number.isFinite), 'Invalid URDF vector');
  return result;
}

function origin(node) {
  const position = new THREE.Vector3(...values(node?.getAttribute('xyz'), [0, 0, 0]));
  const angles = values(node?.getAttribute('rpy'), [0, 0, 0]);
  const rotation = new THREE.Quaternion().setFromEuler(new THREE.Euler(...angles, 'ZYX'));
  return new THREE.Matrix4().compose(position, rotation, new THREE.Vector3(1, 1, 1));
}

function readVisuals(robot) {
  verify(robot.tagName === 'robot', 'Expected a robot description');
  const links = Array.from(robot.querySelectorAll('robot > link'));
  const remaining = Array.from(robot.querySelectorAll('robot > joint'));
  verify(links.length === 50 && remaining.length === 49, 'Unexpected COBOT MAGIC assembly size');
  const childLink = joint => joint.querySelector('child').getAttribute('link');
  const children = new Set(remaining.map(childLink));
  const roots = links.filter(link => !children.has(link.getAttribute('name')));
  verify(roots.length === 1, 'Expected one robot root');
  const world = new Map([[roots[0].getAttribute('name'), new THREE.Matrix4()]]);
  while (remaining.length) {
    const count = remaining.length;
    for (let i = remaining.length - 1; i >= 0; i--) {
      const joint = remaining[i];
      const parent = world.get(joint.querySelector('parent').getAttribute('link'));
      if (!parent) continue;
      world.set(childLink(joint), parent.clone().multiply(origin(joint.querySelector('origin'))));
      remaining.splice(i, 1);
    }
    verify(count > remaining.length, 'Disconnected robot joints');
  }
  const visuals = links.flatMap(link => Array.from(link.querySelectorAll(':scope > visual'), visual => {
    const mesh = visual.querySelector('geometry > mesh');
    const uri = mesh?.getAttribute('filename') ?? '';
    verify(uri.startsWith('package://aloha_new_description/'), 'Unexpected mesh source');
    const scale = values(mesh.getAttribute('scale'), [1, 1, 1]);
    return {
      path: uri.slice('package://'.length),
      matrix: world.get(link.getAttribute('name')).clone()
        .multiply(origin(visual.querySelector('origin')))
        .scale(new THREE.Vector3(...scale)),
    };
  }));
  verify(visuals.length === 48, 'Expected all 48 upstream visuals');
  return visuals;
}

async function loadMeshes(root, hashes, signal) {
  const entries = Object.entries(hashes);
  verify(entries.length === 17, 'Expected all 17 upstream meshes');
  const models = new Map();
  let index = 0;
  async function worker() {
    while (index < entries.length) {
      const [path, hash] = entries[index++];
      const text = await checkedSource(root, path, hash, signal);
      // The pinned files use metre units, Z-up and no external textures.
      verify(!/<!DOCTYPE|<library_images>/i.test(text), 'Unexpected Collada resources');
      verify(/<up_axis>Z_UP<\/up_axis>/.test(text), 'Unexpected mesh axes');
      const model = collada.parse(text, new URL('.', new URL(path, root)).href)?.scene;
      verify(model?.children.length, 'Missing mesh: ' + path);
      // ColladaLoader adds Z-up to Y-up rotation. The surrounding URDF remains Z-up.
      model.rotation.set(0, 0, 0);
      models.set(path, model);
    }
  }
  await Promise.all(Array.from({ length: 3 }, worker));
  return models;
}

function studioScene(visuals, models, bounds) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#f2f2f2');
  const robot = new THREE.Group();
  for (const { path, matrix } of visuals) {
    const mesh = models.get(path);
    verify(mesh, 'Missing checked visual mesh: ' + path);
    const pose = new THREE.Group();
    pose.matrixAutoUpdate = false;
    pose.matrix.copy(matrix);
    pose.add(mesh.clone(true));
    robot.add(pose);
  }
  scene.add(robot);
  // Match the Blender review's orientation, crop and orthographic framing.
  const lower = new THREE.Vector3(...bounds.min);
  const upper = new THREE.Vector3(...bounds.max);
  const center = lower.clone().add(upper).multiplyScalar(0.5);
  const extent = upper.clone().sub(lower);
  const span = Math.max(extent.x, extent.y, extent.z);
  const width = span * 1.52;
  const height = width * 1000 / 1400;
  const camera = new THREE.OrthographicCamera(-width / 2, width / 2, height / 2, -height / 2, 0.01, 50);
  camera.up.set(0, 0, 1);
  camera.position.copy(center).addScaledVector(new THREE.Vector3(2.5, -3.2, 1.65), span);
  camera.lookAt(center);
  camera.setViewOffset(1400, 1000, 215, 32, 928, 976);
  scene.add(new THREE.HemisphereLight(0xffffff, 0xe5e8ed, 2.0));
  scene.add(new THREE.AmbientLight(0xffffff, 1.7));
  for (const [position, intensity] of [[[2, -3, 4], 2.8], [[-3, -1, 3], 1.2], [[0, 4, 3], 1.4]]) {
    const light = new THREE.DirectionalLight(0xffffff, intensity);
    light.position.copy(center).addScaledVector(new THREE.Vector3(...position), span);
    light.target.position.copy(center);
    scene.add(light, light.target);
  }
  return { scene, camera, robot };
}

async function renderAssembly() {
  if (!assembly) return;
  assembly.dataset.assemblyRender = "loading";
  const canvas = assembly.querySelector('canvas');
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), 45000);
  let renderer;
  let robot;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setSize(canvas.width, canvas.height, false);
    const response = await fetch(new URL('./cobot-assembly-source.json', import.meta.url), { signal: abort.signal });
    verify(response.ok, 'Assembly configuration unavailable');
    const source = await response.json();
    const root = 'https://raw.githubusercontent.com/agilexrobotics/mobile_aloha_sim/' + source.commit + '/';
    const urdf = await checkedSource(root, source.urdf, source.urdf_hash, abort.signal);
    const visuals = readVisuals(xmlDocument(urdf).documentElement);
    const models = await loadMeshes(root, source.meshes, abort.signal);
    const view = studioScene(visuals, models, source.bounds);
    robot = view.robot;
    renderer.render(view.scene, view.camera);
    const context = canvas.getContext('2d');
    verify(context, 'Canvas unavailable');
    context.drawImage(renderer.domElement, 0, 0);
    assembly.dataset.assemblyRender = 'ready';
    assembly.dataset.visualCount = String(visuals.length);
  } catch (error) {
    abort.abort();
    assembly.dataset.assemblyRender = 'fallback';
    console.warn('Using the official assembly preview:', error.message);
  } finally {
    clearTimeout(timer);
    if (robot) {
      const geometry = new Set();
      const materials = new Set();
      robot.traverse(item => {
        if (!item.isMesh) return;
        geometry.add(item.geometry);
        for (const material of Array.isArray(item.material) ? item.material : [item.material]) materials.add(material);
      });
      for (const item of [...geometry, ...materials]) item.dispose();
    }
    renderer?.dispose();
  }
}

if (assembly) {
  const observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) {
      observer.disconnect();
      void renderAssembly();
    }
  }, { rootMargin: '80px' });
  observer.observe(assembly);
}
