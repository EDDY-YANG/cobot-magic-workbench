"""Build the concise bilingual release page from measured replay assets."""
from pathlib import Path
import re, shutil
from build_project_page import ROOT, SITE, t, p, h, top, frame, footer, figure

REPO='https://github.com/EDDY-YANG/cobot-magic-workbench'
DOWNLOAD=REPO+'/releases/download/v0.1.2/cobot-magic-sdk-windows-x64.zip'

def write_generated(path, content):
    existing = path.read_bytes() if path.exists() else b''
    ending = b'\r\n' if b'\r\n' in existing else b'\n'
    encoded = content.replace('\r\n', '\n').encode('utf8').replace(b'\n', ending)
    path.write_bytes(encoded)


UPSTREAM_ASSEMBLY_IMAGE = "https://raw.githubusercontent.com/agilexrobotics/mobile_aloha_sim/f799ae0192c0c2bbd502ec4d9bbec8c2633acd1a/img/img.png"

def architecture_figure():
    figure_path = "assets/figures/system-architecture.svg"
    return (
        '<figure class="diagram"><div class="architecture-art">'
        '<img class="architecture-base" src="' + figure_path + '" width="1160" height="680" '
        'alt="Operator PC, GPU host, robot computer, verified USB and COBOT MAGIC" loading="lazy">'
        '<a class="architecture-assembly" href="' + UPSTREAM_ASSEMBLY_IMAGE + '" target="_blank" rel="noopener noreferrer">'
        '<img src="' + UPSTREAM_ASSEMBLY_IMAGE + '" width="1134" height="819" loading="lazy" '
        'referrerpolicy="no-referrer" alt="AgileX COBOT MAGIC whole-machine digital asset with four arms and a mobile base"></a>'
        '</div><figcaption>' + t(
            "The PC provides access. The GPU host runs inference. The robot computer owns the devices and local execution. Whole-machine preview hosted by AgileX Robotics.",
            "PC 提供入口，GPU 主机运行推理，机器人电脑负责设备和本地执行。整机图由 AgileX Robotics 提供。"
        ) + '</figcaption><div class="figure-actions">'
        '<a href="' + figure_path + '">SVG schematic</a>'
        '<a href="assets/figures/system-architecture.png">PNG schematic</a></div></figure>'
    )

def player(full=False):
    body='<section class="replay" id="replay"><div class="section-heading">'+h('One recording, three views','同一次录制，三个视图')
    body+=p('Scrub the timeline to inspect both real cameras and measured joint motion.','拖动时间轴，同步查看真实双视角与实测关节运动。')+'</div>'
    body+='<div class="replay-meta"><span>RECORDED · 2026-10-24</span><span>FP32 / step 10000 / Realtime-VLA V2</span><span id="replay-time">0.00 s</span></div>'
    body+='<div class="single-replay"><video id="camera-video" playsinline muted controlslist="nodownload noremoteplayback" disablepictureinpicture disableremoteplayback preload="metadata" poster="assets/demo/showcase-poster.jpg" aria-label="Synchronized recorded front, right wrist and measured joint replay"><source src="assets/demo/showcase-sync.mp4" type="video/mp4"></video><p class="view-label">FRONT + RIGHT WRIST · Recorded images &nbsp; | &nbsp; MuJoCo · Measured J1–J6</p></div>'
    body+='<p class="video-rights">' + t('© 2026 Yang Taige. Watch on this project site.','© 2026 Yang Taige。请在原项目网页观看。') + ' <a href="assets/demo/LICENSE.txt">' + t('Video rights','视频使用条款') + '</a></p>'
    body+='<div class="replay-controls"><button class="button primary" id="replay-play" disabled>'+t('Play recording','播放记录')+'</button><input id="replay-seek" type="range" min="0" max="46.8" value="0" step="0.033333" aria-label="Recording time"><select id="replay-speed" aria-label="Playback speed"><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option></select></div><div id="joint-readouts" class="joint-readouts"></div><div class="trace-grid"><canvas id="joint-chart" aria-label="Measured joint angles over time"></canvas><canvas id="gripper-chart" aria-label="Recorded gripper aperture over time"></canvas></div>'
    body+=p('Example record: the original run ended on an evidence-queue error; this is not a scored success. Native camera/state streams were retained. The model follows measured joints, without contact simulation; gripper opening is shown numerically.','示例原运行因证据队列错误结束，未评定任务成功。原生相机与状态流已保留。模型按实测关节回放，不模拟接触；夹爪开度以数值显示。','note')
    body+='<details class="details"><summary>'+t('Recording and model provenance','记录与模型来源')+'</summary><div class="inside">'+p('30 fps presentation, 46.83 s. Camera selection follows the native state-to-frame references; hardware exposure synchronization is not claimed. PiPER geometry is from the MIT-licensed piper_ros source. Finger geometry remains at its source pose because the aperture mapping has not been calibrated.','30 fps 展示、46.83 秒。相机选帧依据原生状态到帧引用，不宣称硬件曝光同步。PiPER 几何来自 MIT 许可的 piper_ros。夹爪映射未标定，指爪几何保留源姿态。')+'<a href="assets/demo/media-provenance.json">Recording manifest</a> · <a href="assets/demo/model-replay-provenance.json">Model manifest</a> · <a href="assets/demo/recorded-joints.json" download>Joint samples</a></div></details></section>'
    return body

def build():
    body=top()+'<main><header class="hero"><p class="eyebrow">EMERGE ROBOTICS · OPEN OPERATOR WORKSPACE</p><h1>COBOT MAGIC Workbench</h1><div class="authors"><span>Yang Taige</span><span>Ye Hanzhi</span></div><p class="affiliation">HKU Faculty of Engineering · Tam Wing Fam Innovation Wing</p>'
    body+=p('Connect. Demonstrate. Deploy. Review.','连接、示教、部署、复核。','hero-line')
    body+=p('One browser workspace for COBOT MAGIC, an RTX 4090 edge host and your Windows PC.','面向 COBOT MAGIC、RTX 4090 端侧主机与 Windows PC 的统一网页工作台。','lede')
    body+='<div class="hero-links"><a class="button primary" href="'+DOWNLOAD+'">'+t('Download Windows SDK','下载 Windows SDK')+'</a><a class="button" href="replay.html">'+t('Open recorded demo','打开记录回放')+'</a><a class="button" href="manual.html">'+t('User manual','使用手册')+'</a><a class="button" href="'+REPO+'">GitHub</a></div>'+p('v0.1.2 · Research release. The Windows package opens offline; live operation connects to your authorized lab server.','v0.1.2 · 研究版本。Windows 包可离线打开；真机操作连接到你已获授权的实验室服务器。','scope')+'</header>'
    body+=player()
    body+='<section id="capabilities"><div class="section-heading">'+h('The controls you need, together','把需要的操作放在一起')+'</div><div class="features">'
    for a,b,c,d in [
      ('Connect & inspect','连接与检查','SSH, cameras, CAN mode, feedback age and recording-drive status.','SSH、相机、CAN 模式、反馈年龄与录制盘状态。'),
      ('Teleoperate & record','遥操作与录制','Leader–follower demonstration with video, measured joints and trial-linked records.','主从示教，同步保存视频、实测关节及关联试验记录。'),
      ('Deploy & compare','部署与对比','Select prompt, checkpoint and FP32 + RTC, BF16 optimization or Realtime-VLA V2.','选择 Prompt、检查点和 FP32 + RTC、BF16 优化或 Realtime-VLA V2。'),
      ('Move & review','运动与复核','Joint and gripper targets, measured trajectories, retained commands and failure receipts.','关节与夹爪目标、实测轨迹、实际命令和保留的失败回执。')]:
        body+='<article class="feature"><h3>'+t(a,b)+'</h3>'+p(c,d)+'</article>'
    body+='</div>'+p('Current policy experiments use the right follower arm. DAgger intervention tools are under development.','当前策略实验使用右执行臂；DAgger 人工介入工具仍在开发。','note')
    body+='<figure class="diagram"><a href="interface/index.html#deploy"><img src="assets/interface-deploy.jpg" alt="Actual operator interface rendered from published source, disconnected preview" loading="lazy"></a><figcaption>'+t('English-first operator interface with Chinese labels and state highlights. Open the source preview to inspect each page.','英文主行、中文小字与重要状态高光。打开源码预览可逐页查看。')+'</figcaption></figure><a class="button" href="interface/index.html#deploy">'+t('Explore the actual interface','浏览实际操作界面')+'</a></section>'
    body+='<section id="architecture">'+h('Three computers, clear responsibilities','三端分工清楚')+architecture_figure()
    body+=p('Source assembly: <a href="https://github.com/agilexrobotics/mobile_aloha_sim/tree/f799ae0192c0c2bbd502ec4d9bbec8c2633acd1a">AgileX mobile_aloha_sim · f799ae0</a>.','整机来源：<a href="https://github.com/agilexrobotics/mobile_aloha_sim/tree/f799ae0192c0c2bbd502ec4d9bbec8c2633acd1a">AgileX mobile_aloha_sim · f799ae0</a>。','note')+'</section>'
    body+='<section id="workflow">'+h('Use it in six steps','六步完成一次试验')+figure('workflow','Connect inspect select start record review','An explicit operator click starts a trial. Reconnecting SSH never repeats a command.','试验由操作员明确点击开始，SSH 重连不会重发命令。')+'</section>'
    body+='<section id="data">'+h('Trace the data','追踪数据链路')+figure('data-link','Images and feedback to inference, local execution and storage','Video, feedback, predictions and commands retain their timestamps and identities.','视频、反馈、预测和命令保留各自时间戳与身份。')+'</section>'
    body+='<section class="download-band">'+h('Start with the recorded demo','从离线回放开始')+p('Extract the Windows ZIP and double-click Start-Workbench.cmd. Python and OpenSSH are included. Use Connect for an already configured 4090 server; no model weights or private machine profiles are bundled.','解压 Windows ZIP，双击 Start-Workbench.cmd，内置 Python 与 OpenSSH。在 Connect 页面接入已配置的 4090 服务；不包含模型权重或私有机器配置。')+'<a class="button primary" href="'+DOWNLOAD+'">Windows x64 SDK</a> <a class="button" href="'+REPO+'/releases/tag/v0.1.2">'+t('Release & checksums','版本与校验和')+'</a></section></main>'+footer()
    write_generated(SITE/'index.html',frame(body,'COBOT MAGIC Workbench | Emerge Robotics','COBOT MAGIC 工作台 | Emerge Robotics').replace('</body>','<script src="assets/replay.js" defer></script></body>'))
    replay=top()+'<main><header class="compact-heading"><p class="eyebrow">RECORDED SESSION · READ ONLY</p><h1>'+t('Experiment replay','实验回放')+'</h1>'+p('Recorded camera views, measured angles and model visualization on one timeline.','同一时间轴上的真实相机画面、实测角度与模型可视化。')+'</header>'+player(True)+'</main>'+footer()
    write_generated(SITE/'replay.html',frame(replay,'Recorded experiment | Emerge Robotics','实验回放 | Emerge Robotics').replace('</body>','<script src="assets/replay.js" defer></script></body>'))

def interface_preview(src=None,dst=None):
    src=src or ROOT/'lab_stack/web';dst=dst or SITE/'interface'
    shutil.copytree(src,dst,dirs_exist_ok=True,ignore=shutil.ignore_patterns('*.png','__pycache__'))
    for file in dst.rglob('*'):
        if file.suffix not in ('.html','.js','.mjs','.css'):continue
        s=file.read_text(encoding='utf8')
        if file.suffix=='.html':
            s=re.sub(r'(src|href)="/(?!/)',r'\1="./',s)
            s=s.replace('href="http://127.0.0.1:8891/"', 'href="./review/index.html"')
            if dst.name == 'review':
                s=s.replace('href="http://127.0.0.1:8873/', 'href="../index.html')
            s=s.replace('<head>','<head><meta http-equiv="Content-Security-Policy" content="default-src \'self\' data: blob:; script-src \'self\'; style-src \'self\' \'unsafe-inline\'; connect-src \'self\'; form-action \'none\'; object-src \'none\'"><script src="preview-only.js"></script>')
        else:
            s=re.sub(r'([\'"`])/(assets/[^\'"`]+)',r'\1./\2',s)
        write_generated(file,s)
    write_generated(dst/'preview-only.js','''"use strict";
const nativeFetch=window.fetch.bind(window);
window.fetch=async(input,options)=>{const url=new URL(typeof input==='string'?input:input.url,location.href);if(url.origin===location.origin&&url.pathname.includes('/assets/')&&(!options?.method||options.method==='GET'))return nativeFetch(input,options);throw new Error("Read-only public source preview: no robot connection");};
window.addEventListener("DOMContentLoaded",()=>{
 const banner=document.createElement("div");banner.textContent="READ-ONLY SOURCE PREVIEW / 离线源码预览 · No robot connection";
 banner.style.cssText="position:fixed;top:0;left:0;right:0;z-index:99999;background:#eaf0f7;padding:7px 20px;text-align:center;font:14px system-ui;color:#172331";document.body.prepend(banner);
 document.body.style.paddingTop="36px";
 const style=document.createElement('style');style.textContent='#attention-center,#stop-latch-banner{display:none!important}';document.head.append(style);
 document.addEventListener("click",e=>{const button=e.target.closest("button");const a=e.target.closest("a");
 if(button&&!button.matches("[data-page],#show-details,#refresh-state,[data-attention-open],[data-attention-close]")){e.preventDefault();e.stopImmediatePropagation();}
 if(a&&a.getAttribute("href")&&!a.getAttribute("href").startsWith("#")&&!a.getAttribute("href").startsWith("./review/")&&!a.getAttribute("href").startsWith("../index.html")){e.preventDefault();e.stopImmediatePropagation();}},true);
});
''')

if __name__=='__main__':
    build();interface_preview();interface_preview(ROOT/'lab_stack/review_web',SITE/'interface/review')
