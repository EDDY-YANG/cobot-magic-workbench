/* Visual decoration only. Native text, ARIA attributes, form values and event handlers stay intact. */
(() => {
  'use strict';
  if (!document.querySelector('#page-connect, #verdict-buttons') || document.documentElement.classList.contains('operator-themed')) return;
  document.documentElement.classList.add('operator-themed');
  const words = new Map();
  const tones = {'人工片段已保存':'good','已写入 U 盘':'good','等待模型退出':'warn','等待现场交接':'warn','保存结果待核实':'warn','交接状态待核实':'warn'};
  const glossary = `
连接与零点校准|Connection
遥操作与采集|Teleoperation
运动测试|Motion tests
模型部署|Policy deployment
数据复核|Data review
DAgger 介入|DAgger
设备连接|Connection & calibration
采集与录制|Teleoperation & capture
关节与夹爪|Joints & gripper
DAgger 人工介入|DAgger intervention
每一条记录，都有你的判断。|Data review
先看清机器，再建立连接。|Connection & calibration
观察、操作，记录每一条。|Teleoperation & capture
看见当前姿态，预览下一步。|Joints & gripper
先确认模型，再运行真实闭环。|Policy deployment
AgileX 录制 U 盘|Recording storage
夹爪零点与行程核验|Gripper zero & travel
底层控制模式 · 遥操、电脑控制与待机|Controller modes
控制模式|Controller mode
连接与设备|Devices
设备准备|Device setup
六关节零点校准向导|Six-joint zero calibration
坐标基准|Coordinate reference
当前数据保存位置|Recording destination
关节时间线|Joint timeline
右臂示范采集|Right-arm demonstrations
批次 Episode 与人工复核|Episode review
双执行臂实时读数 · 只读|Follower feedback
PiPER 右执行臂|PiPER right arm
关节姿态窗口|Joint pose
预编程动作|Preset motions
模型与任务|Model & task
FP32运行记录|FP32 run history
状态监督器准备度|Supervisor readiness
本次任务进程|Task progress
阶段观察器|Stage observer
失败线索与恢复记录|Failure & recovery
对准证据与感知方案|Alignment evidence
旧 4060 / H15 · 右臂动作通路|Legacy right-arm control
旧通路部署设置 · 非 4090 H50|Legacy deployment settings
影子推理|Shadow inference
旧 4060/H15 通路任务顺序|Legacy task sequence
任务与事件|Jobs & events
双臂交接|Arm handover
确认任务|Confirm action
操作说明|Operating notes
条件与诊断|Readiness & diagnostics
更多模式|More modes
状态字段与请求回执|State & receipts
模式操作说明|Mode notes
保存位置与身份详情|Storage identity
设备、坐标与校准信息|Devices & calibration
预设选项|Preset options
双臂反馈与预设详情|Feedback & presets
目标与驱动详情|Targets & drives
任务日志与原始回执|Job log & receipts
刷新状态|Refresh status
停止新目标|Stop new targets
清除停止锁存|Clear stop latch
检查已插 U 盘|Inspect USB drives
收起核验|Collapse inspection
保存 2 秒只读核验|Capture two-second check
登记实测与当前反馈|Record measurement
仅夹爪失能|Disable gripper only
设当前位置为夹爪零点|Set gripper zero here
刷新模式诊断|Refresh mode diagnostics
切换到电脑 CAN / MOVEJ|Request CAN / MOVEJ
恢复原厂主从高跟随|Restore OEM following
切到待机|Request standby
六关节失能|Disable six joints
受支撑单次复位|Reset supported arm once
连接 AgileX|Connect AgileX
断开页面通道|Disconnect workspace
检查 CAN / ROS|Inspect CAN / ROS
读取原厂限位|Read factory limits
启动共享监控|Start sensor hub
接管已识别的旧只读采集器|Take over identified reader
停止共享监控|Stop sensor hub
显式同步当前源码|Sync current source
保存当前只读状态|Save current state
执行零点校准 · 未接通|Zero calibration unavailable
打开运动测试|Open motion tests
启动监控|Start monitoring
遥操／待机／失能设置|Teleop & drive settings
准备本次采集|Prepare collection
准备新的采集|New collection
核对最近一次采集|Inspect last collection
开始本条|Start episode
结束并保存|Finish & save
下一条 · 仅准备新身份|Prepare next episode
重试／核对本条开录|Verify episode start
读取批次列表|Load batches
上一页|Previous page
下一页|Next page
保存人工合格判定|Accept episode
保存拒绝判定|Reject episode
读取原始录制列表|List raw recordings
查看模式诊断与切换|Open mode diagnostics
目标 ← 当前角度|Copy measured angles
G目标 ← 当前开度|Copy gripper opening
查看G计划|Preview gripper plan
执行G开合|Move gripper
读取真实姿态|Read measured pose
读取当前目标|Read current target
重置预览|Reset preview
查看自定义目标计划|Preview target plan
执行预览目标|Execute target
查看J计划|Preview joint plan
执行J目标|Move joints
查看准备位＋闭爪计划|Preview ready pose
准备位＋闭合夹爪|Move to ready pose
查看六关节零位计划|Preview zero pose
移动到六零位|Move joints to zero
应用倍率|Apply speed
复制U盘文件路径|Copy USB path
加载模型|Load model
无运动验收|No-motion validation
开始运行|Start run
停止后续新目标|Stop new targets
停止运行|Stop run
释放显存|Release model
红色方块 · 原任务|Red block
蓝色方块|Blue block
圆柱盒子|Cylinder
所选模型影子检查 · 3次|Run three shadow checks
开始人工实验 · 会真实运动|Start physical trial
人工试验（FP32，未验收）|Manual FP32 trial
影子推理（FP32）|FP32 shadow run
复核本次成功|Mark success
复核本次失败|Mark failure
查看原始相机与录制|Open cameras & recording
核对模型计划|Review model plan
准备原始权重|Stage weights
加载模型服务|Load model service
模型状态|Model status
停止模型服务|Stop model service
查看完整部署计划|Review deployment plan
开始限时实机部署|Start timed run
运行 3 次影子推理|Run three shadow checks
运行 10 次影子推理|Run ten shadow checks
4090 FP32/H50 影子联调（不发动作）|FP32/H50 shadow integration
查看原始回执|Inspect receipt
失败介入|Intervene
返回|Back
提交执行|Submit action
更新目录|Refresh catalog
编辑分类|Edit categories
导出评价|Export reviews
部署真机数据|Deployment records
遥操作数采数据|Teleoperation records
播放|Play
暂停|Pause
标记此刻|Mark timestamp
查看源记录摘要|Inspect record metadata
上一条|Previous record
保存评价|Save review
保存并下一条|Save & next
保存分类|Save categories
保存时间标记|Save timestamp
复核队列|Review queue
人工评价|Operator review
没有符合当前筛选的记录。|No matching records
编辑复核分类|Edit review categories
标记这一刻。|Mark timestamp
带走你的评价。|Export reviews
目录覆盖与读取状态|Catalog coverage
程序结果与原始证据|Run results & evidence
查看评价修改历史|Review history
SSH 通道|SSH link
右臂 / 最后报告模式|Last right-arm mode
反馈年龄|Feedback age
共享采集|Sensor hub
U 盘|USB storage
当前任务|Current job
公共任务描述|Task prompt
操作人|Operator
人工合格目标|Target accepted episodes
本条类别|Episode category
本条备注|Notes
本页类别|Category filter
本页复核|Review filter
复核人|Reviewer
复核依据|Review evidence
部署路线|Execution route
模型检查点|Checkpoint
运行秒数 · 最长1800|Duration (s)
目标参考倍率 · 0.5～4×|Target speed
任务提示词 Prompt|Task prompt
G 开合距离 · mm|Gripper opening (mm)
运动速度上限 · °/s|Joint speed limit (°/s)
等待时间|Wait time
模型执行|Policy
纠正录制|Recorder
搜索记录|Find records
人工结果|Human verdict
记录范围|Record scope
数据来源|Source
仅显示有录像的记录|Video only
查看副本|Source copy
证据版本|Evidence version
测试类型|Test type
测哪一项功能|Function tested
预期行为 / 成功判据|Success criteria
评论与观察|Notes
失败 / 异常原因|Failure reason
标签|Tags
实际任务结果|Task outcome
测试功能|Function
异常原因|Failure reason
录像时间 / 秒|Timestamp (s)
这个时间点的观察|Observation
本地后端在线|Backend online
后端不可达|Backend unavailable
等待后端|Waiting for backend
等待交接|Waiting for handover
等待现场交接|Awaiting operator handover
等待模型退出|Waiting for runner exit
待介入|Standby
机器人离线|Robot offline
无可接管任务|No active policy
未录制人工片段|No correction recorded
无实时反馈|No live feedback
未连接|Disconnected
未核验|Unverified
已连接|Connected
空闲|Idle
通道|Channel
关节偏差|Joint error
夹爪偏差|Gripper error
反馈状态|Feedback
消息|Messages
关闭|Close
`;
  const more = `AgileX 主机|Robot computer
CAN 目标发送|CAN target output
HIL-SERL 式 · 抓持分类|Grasp classification
OpenCV · 几何线索|OpenCV geometry
ROS 通路|ROS link
RTC 延迟 / 额外点|RTC delay / extra steps
SAM2 · 分割候选|SAM2 segmentation
U 盘位置、容量与目标详情|USB storage details
UNKNOWN · 通道未连接|Unknown · disconnected
不发送运动目标|No motion targets
不计入|Exclude
人工复核依据 · 描述现场观察或录像时间点|Review evidence
仅准备阶段 · 夹爪目标|Preparation gripper target
仅显示 · 不发控制指令|Display only
任务观察与诊断|Task diagnostics
任务阶段 · 只读观察|Task stage
任务阶段与历史回放|Stages & replay
六关节数值零位|Six-joint zero
六零位|Move to zero
关节时间线与监控设置|Timeline & monitoring
其他运行状态|Runtime details
准备位 + 闭爪|Ready pose + close
准备位夹爪|Ready-pose gripper
零位夹爪|Zero-pose gripper
准备本次采集 / 批次设置|Prepare collection
判断依据|Evidence
判断这一采样点|Evaluate sample
动作块|Action chunk
历史固定2k快捷入口 · 已迁移|Legacy 2k shortcut
历史录像验证资料 · 原图、概率与完整回执|Archived validation evidence
原厂写入尚未接通|OEM writes unavailable
去噪采样步数|Denoising steps
取消本次验证|Cancel validation
右执行臂|Right follower
右腕相机|Right wrist
右臂 CAN|Right CAN
右臂实时轨迹|Right-arm trajectory
各模式当前可用条件与阻止原因|Mode requirements
同时闭合空爪 · 软件参考速度上限 · mm/s|Gripper speed limit (mm/s)
复位视角|Reset view
失败|Failed
头部相机|Front camera
夹爪开度 · mm|Gripper opening (mm)
夹爪独立状态未核对|Gripper unverified
完整 0x2A1 原始载荷|Raw 0x2A1 payload
完整状态 / 任务回执|Full state & receipts
实时计数、队列与保存详情|Capture counts & saving
实测量|Measured
实际推理延迟|Inference latency
尚未配置模型与录像|Model / video unconfigured
展开原始字段与失败原因|Raw values & failure reason
左执行臂|Left follower
左腕视角 · 展开查看|Left wrist view
已记录提交耗时 P50|Median commit latency
已记录提交耗时 P95|P95 commit latency
当前录制未核对|Recording unverified
当前观测阶段 · 不是模型口述|Observed stage
当前阶段 · 来自任务协调器|Current task stage
当前阶段候选 · 历史录像|Archived stage candidate
录制保存|Recording finalization
录制时长|Recording duration
待复核|Pending review
待确认|Unconfirmed
恢复事件 · 最近 6 条|Recent recovery events
恢复条件|Recovery conditions
成功|Success
执行动作时间尺度|Execution time scale
执行配置|Execution settings
批次记录与人工复核|Batch records & review
抓持与放置证据|Grasp & placement evidence
抓放结果 · 需要复核|Grasp outcome
持物候选|Holding candidate
控制来源活动 · 最近接收时间窗|Recent control sources
提交条件与历史诊断|Requirements & diagnostics
放置候选|Placement candidate
文件保存核验|Check saved files
旧通路与其他功能|Legacy controls
暂停图表|Pause chart
继续图表|Resume chart
更多操作|More actions
最后真实报告的模式 · 新鲜度单独核对|Last reported mode
最近一次模式请求 · 历史结果|Last mode request
最近核对的恢复状态|Last recovery state
有效耗时样本|Valid latency samples
未加载模型|No model loaded
未知|Unknown
机械臂回位|Return to pose
查看任务阶段记录|Task stage history
查看六零位计划|Preview zero pose
查看准备位 + 闭爪计划|Preview ready pose
查看未归入批次的历史原始录制|Unassigned recordings
查看本次实际模型输出与原始时间字段|Model output & timing
查看精度服务与本轮回执|Model service & receipts
校准与模型兼容性|Calibration compatibility
清空图表|Clear chart
物理抓放|Physical outcome
状态样本|State samples
真实 U 盘挂载|USB mount
离线 · 历史记录|Offline · archived
离线 · 无当前图像证据|Offline · no current image
程序执行|Run status
程序执行 · 任务回执|Run receipt
红色方块 → 绿色桶|Red block to green bin
线索与待补证据|Missing evidence
观测变化 · 最近 8 条|Recent observations
视觉伺服 · 后端未核对|Visual servo unverified
训练准备位来源|Training reference
起始中位数|Median start pose
运行时长 · 秒|Duration (s)
通道未连接|Disconnected
通道未连接 · 当前未核对|Disconnected · unverified
通道未连接 · 活动待核对|Activity unverified
速度 / 加速度|Speed / acceleration
部分成功|Partial success
部署方案|Deployment plan
采样点|Sample
顺序验证整条|Validate sequence
验收说明|Validation notes
验证录像|Validation recording
状态字段与请求回执|State & receipts
采集存储标识与恢复详情|Storage & recovery
查看厂家 CPV 恢复流程与适用条件|OEM CPV recovery
FP32参数与历史回执|FP32 parameters & receipts
查看完整批准参数|Approved parameters
查看验收原始回执|Validation receipt
复核队列|Review queue
部署真机数据|Deployment records
遥操作数采数据|Teleoperation records
卡尺实测指尖间距 · mm|Measured finger gap (mm)
G|G
通道|Channel
左侧|Left
右侧|Right
预设选项|Preset options
`;
  for(const line of (glossary+more).trim().split('\n')) {
    const [zh,en]=line.split('|');
    words.set(zh,en);
  }
  const selector='button, summary, h1, h2, h3, label, th, .nav-item, .pill, .dg-summary label, .dg-th span, #dg-phase, #dg-policy, #dg-record, #dg-left-s, #dg-right-s, #usb-status, #connection-status, #save-state';
  function labelOf(el) {
    // Ignore nested form values and counts. Never replace the node or its text.
    return Array.from(el.childNodes).filter(node=>node.nodeType===Node.TEXT_NODE)
      .map(node=>node.textContent.trim()).filter(Boolean).join(' ')
      .replace(/^[⌘◉◇↗⇄▤↻●■▶＋←→\s]+|[↗←→\s]+$/gu,'').trim();
  }
  function decorate(el) {
    if (!el.matches(selector)) return;
    const isNav=el.classList.contains('nav-item');
    const label=isNav ? Array.from(el.children).find(c=>!c.classList.contains('nav-icon') && !c.classList.contains('nav-number')) || el : el;
    const source=labelOf(label) || (isNav ? el.getAttribute('aria-label') : '');
    const nestedLabel=Array.from(el.children).find(child=>child.tagName==='SPAN' && !child.childElementCount);
    const usbFailure=el.id==='usb-status' && /USB\s*检查失败/.test(source);
    const english=(usbFailure ? 'Storage check failed' : '') || words.get(source) || (el.tagName==='LABEL' && nestedLabel && words.get(nestedLabel.textContent.trim())) ||
      (el.id==='attention-message-button' ? 'Messages' : /^查看 \d+ 项暂不可用操作及原因$/.test(source) ? 'Unavailable actions' : '');
    const tone=usbFailure ? 'error' : tones[source];
    if (tone && el.dataset.uiTone!==tone) el.dataset.uiTone=tone;
    else if(!tone && el.hasAttribute('data-ui-tone')) el.removeAttribute('data-ui-tone');
    if(english && label.dataset.uiEn!==english) label.dataset.uiEn=english;
    if(!english && label.hasAttribute('data-ui-en')) label.removeAttribute('data-ui-en');
  }
  function subtree(el){
    if(el.nodeType!==Node.ELEMENT_NODE) return;
    decorate(el);
    el.querySelectorAll(selector).forEach(decorate);
  }
  function init(){
    const readOnly=document.body.hasAttribute('data-ui-review');
    const nativePort=['8873','8875','8891'].includes(location.port);
    const gateway=['8910','8880'].includes(location.port) && location.pathname.startsWith('/workbench/');
    if(readOnly || (location.hostname==='127.0.0.1' && (nativePort || gateway))){
      const flag=document.createElement('span');
      flag.className=readOnly ? 'appearance-review' : 'appearance-review appearance-native';
      flag.textContent=readOnly ? 'Read-only / 只读检视' : 'Native controls / 原接口 · 真实操作';
      document.querySelector('.topbar-right')?.prepend(flag);
    }
    const titles={connect:'Connection & calibration',teleop:'Teleoperation & capture',motion:'Joints & gripper',deploy:'Policy deployment'};
    for(const [key,title] of Object.entries(titles)){
      const page=document.getElementById('page-'+key);
      if(!page) continue;
      const heading=document.createElement('div');
      heading.className='operator-page-title';
      heading.setAttribute('role','heading');
      heading.setAttribute('aria-level','1');
      heading.append(document.createTextNode(title));
      const caption=document.createElement('small');
      caption.lang='zh-CN';
      caption.textContent={connect:'连接与校准',teleop:'遥操作与采集',motion:'关节与夹爪',deploy:'模型部署'}[key];
      heading.append(caption);
      page.prepend(heading);
    }
    // Relocate only static explanatory paragraphs; never touch inputs, safety gates or live status.
    for(const note of document.querySelectorAll('.review-heading > div > p, #page-dagger .dg-panel > p')){
      if(note.id || note.attributes.length || note.childElementCount) continue;
      const container=note.closest('.review-main,#page-dagger');
      if(!container) continue;
      note.classList.add('operator-footnote');
      container.append(note);
    }
    subtree(document.body);
    // Only inspect changed text and inserted elements, never poll robot state.
    const observer=new MutationObserver(records=>{
      const updated=new Set();
      for(const r of records){
        const parent=r.target.nodeType===Node.ELEMENT_NODE?r.target:r.target.parentElement;
        const target=parent?.closest(selector); if(target) updated.add(target);
        for(const node of r.addedNodes) subtree(node);
      }
      updated.forEach(decorate);
    });
    observer.observe(document.body,{subtree:true,childList:true,characterData:true});
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',init,{once:true});
  else init();
})();
