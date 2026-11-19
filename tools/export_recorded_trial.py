"""Export an evidence-linked dual-camera review on the verified source USB."""
from pathlib import Path
import argparse, bisect, ctypes, hashlib, json, struct, subprocess, sys
from io import BytesIO
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import imageio_ffmpeg

def records(path):
    with path.open("rb") as source:
        while header := source.read(12):
            if len(header) != 12:
                raise ValueError("Truncated native header")
            stamp, length = struct.unpack("<QI", header)
            if not 0 < length < 32 * 1024 * 1024:
                raise ValueError("Invalid native frame length")
            data=source.read(length)
            if len(data)!=length:
                raise ValueError("Truncated native frame")
            yield stamp,data

def sha(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for data in iter(lambda:f.read(1024*1024),b""):
            h.update(data)
    return h.hexdigest()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--episode",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    episode=a.episode.resolve()
    out=a.output.resolve()
    if episode.drive.upper()!="F:" or out.drive.upper()!="F:":
        raise ValueError("This export is bound to the selected F: USB")
    serial=ctypes.c_ulong()
    ok=ctypes.windll.kernel32.GetVolumeInformationW("F:\\",None,0,ctypes.byref(serial),None,None,None,0)
    if not ok or serial.value!=0x67452301:
        raise ValueError("Selected USB identity differs")
    out.mkdir(exist_ok=True,parents=True)
    files=[episode/(x+"-0000.bin") for x in ["front","right","state","can-right"]]+[episode/"operation.json"]
    before={p.name:sha(p) for p in files}
    state=[json.loads(data) for _,data in records(episode/"state-0000.bin")]
    stamps=np.array([s["sample_wall_ns"] for s in state],dtype=np.int64)
    if not np.all(np.diff(stamps)>0):
        raise ValueError("State timestamps do not advance")
    camera={role:dict(records(episode/(role+"-0000.bin"))) for role in ["front","right"]}
    for s in state:
        for role in camera:
            if s["camera_source_index_v1"]["cameras"][role]["wall_ns"] not in camera[role]:
                raise ValueError("Native camera reference is missing")
    start=int(stamps[0]);end=int(stamps[-1]);fps=30
    frames=int((end-start)*fps//1_000_000_000)+1
    width,height=1280,480
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    target=out/"workbench-recorded-dual-view.mp4"
    if target.exists():
        raise FileExistsError(target)
    command=[ffmpeg,"-hide_banner","-loglevel","error","-f","rawvideo","-pix_fmt","rgb24","-s",f"{width}x{height}",
             "-r",str(fps),"-i","-","-an","-c:v","libx264","-preset","medium","-crf","22","-pix_fmt","yuv420p",
             "-movflags","+faststart",str(target)]
    proc=subprocess.Popen(command,stdin=subprocess.PIPE,stderr=subprocess.PIPE)
    font=ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf",18)
    small=ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf",14)
    indices=[];samples=[];max_skew=0;image_cache={}
    try:
        for i in range(frames):
            wall=start+round(i*1e9/fps)
            index=min(len(state)-1,max(0,bisect.bisect_right(stamps,wall)-1))
            s=state[index];arm=s["sensors"]["arms"]["right"]
            panel=Image.new("RGB",(width,height),"#f7f9fc");d=ImageDraw.Draw(panel)
            d.text((20,10),"RECORDED WORKBENCH VIEW",font=font,fill="#172331")
            d.text((960,12),f"2026-10-24  |  t = {i/fps:05.2f} s",font=small,fill="#627183")
            for column,role in enumerate(["front","right"]):
                time=s["camera_source_index_v1"]["cameras"][role]["wall_ns"]
                max_skew=max(max_skew,abs(time-int(s["sample_wall_ns"])))
                key=(role,time)
                if key not in image_cache:
                    im=Image.open(BytesIO(camera[role][time])).convert("RGB")
                    im.thumbnail((616,360))
                    image_cache={k:v for k,v in image_cache.items() if k[0]!=role}
                    image_cache[key]=im
                im=image_cache[key];x=12+column*640
                panel.paste(im,(x+(616-im.width)//2,66+(360-im.height)//2))
                d.text((x+8,42),"THIRD-PERSON / FRONT" if role=="front" else "RIGHT WRIST",font=small,fill="#1c5aa6")
            q=arm["q_deg"]
            readout="   ".join(f"J{k+1} {v:7.2f} deg" for k,v in enumerate(q))
            d.text((20,432),readout,font=small,fill="#172331")
            d.text((20,455),f"G {arm['grip_m']*1000:.2f} mm  |  Source-timestamp alignment  |  Replay",font=small,fill="#627183")
            proc.stdin.write(panel.tobytes())
            if i in (0,180,360,720):
                panel.save(out/f"preview-{i:04d}.jpg",quality=92)
            indices.append(index)
            samples.append([round(i/fps,6),*q,round(arm["grip_m"]*1000,6)])
    finally:
        proc.stdin.close()
    error=proc.stderr.read().decode(errors="replace")
    code=proc.wait()
    if code:raise RuntimeError(error)
    assert before=={p.name:sha(p) for p in files},"Source changed during export"
    summary={
      "schema":"cobot-recorded-review-v1","episode_id":episode.name,"date":"2026-10-24",
      "model":{"precision":"FP32","checkpoint":10000,"route":"Realtime-VLA V2"},
      "requested_run_s":360,"native_episode_duration_s":47.02908510099951,
      "source_run_outcome":"FAILED_RETAINED",
      "source_run_error":"V2 evidence queue full; long-window evidence incomplete",
      "native_recording_status":"RECORDED_NATIVE_REVIEW_REQUIRES_QC",
      "task_outcome":"not scored","video_fps":fps,"video_frames":frames,"video_duration_s":frames/fps,
      "native_frames":{k:len(v) for k,v in camera.items()},"state_records":len(state),
      "camera_references_verified":len(state)*2,
      "alignment":"Each displayed state selects its recorded front/right frame references; 30 fps presentation uses previous recorded state.",
      "hardware_exposure_synchronization":False,"largest_camera_to_state_offset_ms":max_skew/1e6,
      "source_hashes":before,
      "public_fields":["relative time","J1-J6 degrees","G millimetres"],
      "data_file":"recorded-joints.json","video":"workbench-recorded-dual-view.mp4"}
    (out/"recorded-joints.json").write_text(json.dumps({"columns":["time_s","J1_deg","J2_deg","J3_deg","J4_deg","J5_deg","J6_deg","gripper_mm"],"samples":samples},separators=(",",":")))
    (out/"media-provenance.json").write_text(json.dumps(summary,indent=2))
    (out/"presentation-source-index.json").write_text(json.dumps(indices))
    check=subprocess.run([ffmpeg,"-v","error","-i",str(target),"-f","null","-"],capture_output=True,text=True)
    if check.returncode:raise RuntimeError("Export decode failed: "+check.stderr)
    print(json.dumps({"status":"EXPORTED_AND_DECODED","frames":frames,"camera_refs":len(state)*2,"duration_s":frames/fps,"video_bytes":target.stat().st_size,"source_unchanged":True}))
if __name__=="__main__":
    main()

