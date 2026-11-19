"""Render measured right-arm joint feedback in MuJoCo as kinematic replay."""
from pathlib import Path
import argparse, ctypes, hashlib, json, shutil, subprocess, sys
import xml.etree.ElementTree as E
from io import BytesIO

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent/"build-tools/mujoco-deps"))
import mujoco
import numpy as np
from PIL import Image,ImageDraw,ImageFont
import imageio_ffmpeg

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data",type=Path,required=True)
    p.add_argument("--assets",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    serial=ctypes.c_ulong()
    assert a.output.resolve().drive=="F:"
    assert ctypes.windll.kernel32.GetVolumeInformationW("F:\\",None,0,ctypes.byref(serial),None,None,None,0) and serial.value==0x67452301
    a.output.mkdir(exist_ok=True,parents=True)
    urdf=a.assets/"piper_description.urdf"
    root=E.parse(urdf).getroot()
    for link in root.findall("link"):
        for collision in link.findall("collision"):
            link.remove(collision)
    mesh_directory=a.output/"model-assets"/"meshes"
    mesh_directory.mkdir(parents=True,exist_ok=True)
    for mesh in root.findall(".//mesh"):
        name=Path(mesh.get("filename")).name
        file=a.assets/"meshes"/name
        assert file.is_file()
        destination=mesh_directory/name
        if not destination.exists():shutil.copyfile(file,destination)
        assert hashlib.sha256(destination.read_bytes()).digest()==hashlib.sha256(file.read_bytes()).digest()
        mesh.set("filename",name)
    ext=E.SubElement(root,"mujoco")
    E.SubElement(ext,"compiler",{"balanceinertia":"true","discardvisual":"false","fusestatic":"false",
                                "meshdir":mesh_directory.resolve().as_posix()})
    model=mujoco.MjModel.from_xml_string(E.tostring(root,encoding="unicode"))
    xml=a.output/"recorded-piper-model.xml"
    mujoco.mj_saveLastXML(str(xml),model)
    tree=E.parse(xml);mj=tree.getroot()
    visual=mj.find("visual")
    if visual is None:visual=E.SubElement(mj,"visual")
    E.SubElement(visual,"global",{"offwidth":"848","offheight":"560"})
    E.SubElement(visual,"headlight",{"ambient":".3 .3 .3","diffuse":".6 .6 .6","specular":".08 .08 .08"})
    for geom in mj.findall(".//geom"):
        geom.set("rgba", ".28 .35 .43 1")
    assets=mj.find("asset")
    E.SubElement(assets,"texture",{"type":"skybox","builtin":"gradient","rgb1":".93 .95 .97","rgb2":".98 .98 .99","width":"512","height":"512"})
    world=mj.find("worldbody")
    E.SubElement(world,"light",{"pos":"1 -1 2","dir":"-1 1 -2","diffuse":".3 .3 .3"})
    E.SubElement(world,"geom",{"name":"reference_floor","type":"plane","pos":"0 0 -.012","size":"2 2 .01","rgba":".96 .97 .98 1","contype":"0","conaffinity":"0"})
    option=mj.find("option")
    if option is None:option=E.SubElement(mj,"option")
    option.set("gravity","0 0 0")
    tree.write(xml,encoding="unicode")
    model=mujoco.MjModel.from_xml_path(str(xml))
    data=mujoco.MjData(model)
    samples=json.loads(a.data.read_text())["samples"]
    joints=[mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_JOINT,f"joint{i}") for i in range(1,7)]
    assert min(joints)>=0
    addr=[int(model.jnt_qposadr[i]) for i in joints]
    camera=mujoco.MjvCamera()
    camera.lookat[:]=[.12,0,.25];camera.distance=1.10;camera.azimuth=130;camera.elevation=-22
    renderer=mujoco.Renderer(model,height=560,width=848)
    font=ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf",18)
    small=ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf",14)
    target=a.output/"recorded-joint-replay.mp4"
    assert not target.exists()
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    cmd=[ffmpeg,"-hide_banner","-loglevel","error","-f","rawvideo","-pix_fmt","rgb24","-s","848x620","-r","30","-i","-","-an","-c:v","libx264","-crf","23","-preset","medium","-pix_fmt","yuv420p","-movflags","+faststart",str(target)]
    process=subprocess.Popen(cmd,stdin=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        for i,row in enumerate(samples):
            data.qpos[addr]=np.deg2rad(row[1:7])
            mujoco.mj_forward(model,data)
            renderer.update_scene(data,camera=camera)
            frame=Image.fromarray(renderer.render())
            panel=Image.new("RGB",(848,620),"white")
            panel.paste(frame,(0,32))
            d=ImageDraw.Draw(panel)
            d.text((16,6),"MEASURED JOINT REPLAY  |  MuJoCo",font=font,fill="#172331")
            d.text((675,9),f"t = {row[0]:05.2f} s",font=small,fill="#627183")
            d.text((16,590),f"J1-J6 recorded feedback  |  G {row[7]:.2f} mm (numeric)  |  Kinematic visualization",font=small,fill="#627183")
            process.stdin.write(panel.tobytes())
            if i in (0,180,360,720):panel.save(a.output/f"model-{i:04d}.jpg",quality=92)
    finally:
        process.stdin.close();renderer.close()
    err=process.stderr.read().decode(errors="replace");code=process.wait()
    if code:raise RuntimeError(err)
    check=subprocess.run([ffmpeg,"-v","error","-i",str(target),"-f","null","-"],capture_output=True,text=True)
    assert not check.returncode,check.stderr
    receipt={"schema":"cobot-kinematic-review-v1","engine":"MuJoCo "+mujoco.__version__,"frames":len(samples),"fps":30,
      "duration_s":len(samples)/30,"q_source":"recorded-joints.json; degrees converted to radians",
      "joints_replayed":["joint"+str(i) for i in range(1,7)],
      "joint_value_clipping":False,"physics_rollout":False,"contact_validated":False,
      "gripper":"Recorded opening shown numerically; finger mapping not calibrated; source zero finger pose retained.",
      "asset_source":"agilexrobotics/piper_ros","asset_commit":"ac41fcbcdda598f01b51cf6175ed9a24d0dacadc","license":"MIT",
      "source_urdf_sha256":hashlib.sha256(urdf.read_bytes()).hexdigest(),
      "source_data_sha256":hashlib.sha256(a.data.read_bytes()).hexdigest(),
      "video_sha256":hashlib.sha256(target.read_bytes()).hexdigest(),
      "visual_model_changes":"Visual meshes retained and recolored slate for clarity; collision geoms removed for rendering; a reference ground plane and lights added.",
      "physical_installation_mapping":"Joint-name and unit mapping only; calibrated as-built geometry not established."}
    (a.output/"model-replay-provenance.json").write_text(json.dumps(receipt,indent=2))
    print(json.dumps(receipt))
if __name__=="__main__":
    main()

