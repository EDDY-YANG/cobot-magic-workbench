"use strict";
const holder = document.querySelector("[data-robot-viewer]");
if (holder) {
  const loadButton = holder.querySelector("[data-load-model]");
  const resetButton = holder.querySelector("[data-reset-model]");
  const poster = holder.querySelector("img");
  const status = holder.querySelector("[role=status]");
  let reset;
  loadButton.addEventListener("click", async () => {
    loadButton.disabled = true;
    status.textContent = "Loading verified source geometry…";
    try {
      const THREE = await import("./vendor/three/three.module.js");
      const { GLTFLoader } = await import("./vendor/three/GLTFLoader.js");
      const renderer = new THREE.WebGLRenderer({antialias:true,alpha:false});
      renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
      renderer.setClearColor(0xfafbfc);
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = 1.2;
      renderer.domElement.setAttribute("aria-label", "Upstream COBOT MAGIC whole-machine model; drag to orbit, scroll to zoom");
      renderer.domElement.tabIndex = 0;
      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(35, 1, .01, 100);
      scene.add(new THREE.HemisphereLight(0xffffff,0x7d8793,2.2));
      const key = new THREE.DirectionalLight(0xffffff,2.5);
      key.position.set(3,5,4); scene.add(key);
      const fill = new THREE.DirectionalLight(0xffffff,1.5);
      fill.position.set(-4,3,-2); scene.add(fill);
      const gltf = await new GLTFLoader().loadAsync("assets/robot/cobot-magic-source-zero.glb");
      scene.add(gltf.scene);
      const bounds = new THREE.Box3().setFromObject(gltf.scene);
      const center = bounds.getCenter(new THREE.Vector3());
      const size = bounds.getSize(new THREE.Vector3());
      const span = Math.max(size.x,size.y,size.z);
      let yaw = .66, pitch = .35, distance = span*2.35;
      function render() {
        camera.position.set(center.x + distance*Math.cos(pitch)*Math.sin(yaw),
                            center.y + distance*Math.sin(pitch),
                            center.z + distance*Math.cos(pitch)*Math.cos(yaw));
        camera.lookAt(center);
        renderer.render(scene,camera);
      }
      reset = () => {yaw=.66;pitch=.35;distance=span*2.35;render();};
      const stage = holder.querySelector(".robot-stage");
      stage.appendChild(renderer.domElement);
      poster.hidden = true;
      function resize() {
        const width=stage.clientWidth, height=stage.clientHeight;
        renderer.setSize(width,height,false);camera.aspect=width/height;
        camera.updateProjectionMatrix();render();
      }
      new ResizeObserver(resize).observe(stage);
      let dragging=null;
      const canvas=renderer.domElement;
      canvas.style.touchAction="none";
      canvas.addEventListener("pointerdown",event=>{
        dragging={x:event.clientX,y:event.clientY,id:event.pointerId};
        canvas.setPointerCapture(event.pointerId);
      });
      canvas.addEventListener("pointermove",event=>{
        if(!dragging)return;
        yaw-=(event.clientX-dragging.x)*.008;
        pitch=Math.max(-.35,Math.min(1.15,pitch+(event.clientY-dragging.y)*.008));
        dragging.x=event.clientX;dragging.y=event.clientY;render();
      });
      canvas.addEventListener("pointerup",()=>{dragging=null;});
      canvas.addEventListener("pointercancel",()=>{dragging=null;});
      canvas.addEventListener("wheel",event=>{
        event.preventDefault();distance=Math.max(span,Math.min(span*6,distance*Math.exp(event.deltaY*.001)));
        render();
      },{passive:false});
      canvas.addEventListener("keydown",event=>{
        if(event.key==="ArrowLeft")yaw-=.12;
        else if(event.key==="ArrowRight")yaw+=.12;
        else if(event.key==="ArrowUp")pitch=Math.min(1.15,pitch+.1);
        else if(event.key==="ArrowDown")pitch=Math.max(-.35,pitch-.1);
        else if(event.key==="Home"){reset();event.preventDefault();return;}
        else return;
        event.preventDefault();render();
      });
      canvas.addEventListener("webglcontextlost",()=>{
        poster.hidden=false;canvas.hidden=true;
        status.textContent="3D context unavailable. The source-derived render is shown.";
      });
      resize();
      loadButton.hidden=true;
      resetButton.hidden=false;
      status.textContent="Source model loaded · Drag to orbit · Scroll to zoom";
    } catch (error) {
      status.textContent="3D view unavailable. The verified source rendering remains available.";
      loadButton.disabled=false;
      console.error("Robot viewer could not load:",error.message);
    }
  });
  resetButton.addEventListener("click",()=>reset?.());
}
