# BF16＋guided RTC 独立服务：PC入口

本包只提供独立模型服务、PC源码上传/启停/回退和历史输入验收。当前完成的是本机离线开发；真实4090启动、GPU编译和历史服务验收尚未执行。它不接入原网页、不启动机器人，也不包含Realtime-VLA V2、QP或运动控制改动。

原FP32源码、配置、权重和检查点保持。BF16复用已验证的混合精度加载器：大backbone BF16，小投影及外部RTC数学FP32；网络内部求导仍为混合精度。输出保持float32 `[50,7]`，前六维绝对rad、第七维m；H50、10steps、guided RTC12/35/beta5、18Hz标签保持。

## 入口与默认行为

- PC入口：`pc/2026-10-21-10-59-BF16-RTC.ps1`。
- 默认只打印dry-run计划。只有显式加 `-ExecuteOnline` 才会连接远端；本轮没有执行此标志。
- 新服务：4090本机 `127.0.0.1:18960`，独立目录 `/opt/cobot-workbench/lab-packages/2026-10-21-10-59-BF16-RTC-Service`。
- 不对外开放HTTP监听，不占原FP32的18882/18884端口。PC管理通过既有SSH进行。
- 旧BF16依赖：`2026-10-20-13-15-BF16-Offline`；不复制权重、不修改旧源码。依赖SHA、主机、SSD UUID与绝对解释器不符就停止。
- 一次只运行一个GPU模型；不会自动暂停FP32。回退只能恢复本次handover捕获的原检查点，不默认恢复5000。

## 现在可以运行的离线命令

以下在本电脑PowerShell运行，仅查看计划，不联网：

```powershell
$entry = 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-21-10-59-BF16-RTC独立服务\pc\2026-10-21-10-59-BF16-RTC.ps1'
& $entry -Action inspect
& $entry -Action start -Step 5000
```

## 4090恢复后：先上传与只读检查

以下在线步骤本轮未执行。继续使用现有 `lab-4090` 和 `C:/Users/Administrator/.ssh/lab-4090-only/config`；不重新配置认证或改主机密钥。若地址已变化，可在PC入口加 `-HostName '实际已核对的新地址'`，只对本次命令覆盖；不能把旧IP快照当成当前连通证据。

```powershell
& $entry -Action upload -ExecuteOnline
& $entry -Action inspect -ExecuteOnline
```

上传只接受本包`source_manifest.json`列出的源码/配置/文档，核主机和SSD后创建新目录、逐文件校验SHA。已有目录或部分上传结果不会被覆盖；SSH中断结果不明时先检查，不能重复上传来掩盖失败。

`inspect`必须能够确认正确主机/SSD/解释器/本包及旧依赖SHA。原FP32仍运行时，GPU锁占用是预期现象；此时不能直接启动BF16。

## 保存原路线，然后手工完成GPU交接

先确保原模型测试已经结束。`prepare-handover`必须看到原服务READY、busy=false、无lease；只读捕获当前step、精确进程身份和源码SHA，不暂停服务。

```powershell
$handover = (& $entry -Action prepare-handover -ExecuteOnline) | ConvertFrom-Json
$handoverPath = $handover.result.handover_receipt
$handover.result.captured_step
$handoverPath
```

只有返回 `MANUAL_HANDOVER_PREPARED_NOT_PAUSED` 才继续。在这次交接完成前不要再切换FP32检查点或启动其他模型测试。下面明确调用原有身份核验停止入口；它会拒绝活动推理/lease。此命令仅在4090恢复且准备交接时执行，本轮未执行。

```powershell
& ssh -F 'C:/Users/Administrator/.ssh/lab-4090-only/config' -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 lab-4090 '/home/robot/openpi-workspace/pi05-215abfb217db/env/bin/python -I -B /opt/cobot-workbench/lab-packages/2026-10-19-18-12-H50-Model-Supervisor/scripts/supervisor_control.py stop'
```

若PC入口使用了HostName覆盖，此手工SSH命令也要使用相同的 `-o HostName=实际地址`；HostKeyAlias继续来自既有配置。它只停止原模型服务，不触碰Hub、相机、CAN、STOP或机器人。

## 启动独立BF16，等待实际READY

第一次服务验收使用5000，因为已有16条真实历史输入及BF16黄金输出只对应这个checkpoint。原路线即使先前是10000或15000，回退仍恢复handover捕获的原step。

```powershell
$started = (& $entry -Action start -Step 5000 -Handover $handoverPath -ExecuteOnline) | ConvertFrom-Json
$run = $started.result.run_dir
$started.result.status
$run
& $entry -Action status -RunDir $run -ExecuteOnline
```

`SERVICE_STARTING_NOT_YET_READY`仅代表启动及身份登记。服务会取得两把原GPU owner锁、加载BF16、做真实混合精度IR审计及native/RTC历史暖机，完成后才发布READY。不要把进程存在当READY，也不要重复start。编译时间没有在新服务中实测；用status检查同一run。

若原FP32/其worker仍在、GPU锁被占、boot或handover选择历史改变，start应拒绝。不得删锁、改断言或改源码来绕过。启动失败也保留run目录和日志；不覆盖重试。

## 历史真实输入服务验收

```powershell
& $entry -Action accept -RunDir $run -ExecuteOnline
```

此命令在4090本机读取原16条真实历史输入，不从PC上传假观测，不调用机器人。它核验原manifest/JPEG/数组SHA，再执行3次warmup＋100次测量；重复循环使用新lease并保持原trial/request/seed/前缀不变。输入验证、HTTP传输、完整输出校验计入本客户端时延；编译和服务启动暖机另列。

通过条件：当前run/nonce/config字节SHA/fingerprint一致、原输出`float32[50,7]`、RTC回显正确、与保存的同seed BF16输出逐字节一致、100次完整测量P95≤550ms/max≤600ms、租约完整释放。一次失败就保留已执行次数和原错误，不补成功次数。

只有 `HISTORICAL_SERVICE_ACCEPTANCE_PASS` 代表这项服务验收通过。它不等于新观测闭环、真实NO_SEND、BF16相对FP32的任务精度或机器人运动准入。原5k BF16与FP32最坏0.722°/夹爪2.668mm的差异仍需后续任务评估。其他checkpoint可显式启动做结构验证，但本套黄金输入验收不能替它们证明数值正确性。

## 停止与回退

只停止本实验、暂不恢复FP32：

```powershell
& $entry -Action stop -RunDir $run -ExecuteOnline
```

停止本实验并按handover恢复原FP32检查点：

```powershell
& $entry -Action rollback -RunDir $run -ExecuteOnline
```

回退逐项核boot/PID/start_ticks/entrypoint/nonce；只给本实验精确进程发送TERM，不按名称杀进程、不SIGKILL。必须确认推理停止、进程退出、两GPU锁释放、原源码SHA未变且没有新owner，才调用原launch_supervisor恢复捕获的step。最终必须看到 `ORIGINAL_FP32_RESTORED_READY`。已有别的原服务实例时保留它，不切模型、不重启。

`STOP_UNCONFIRMED_RESTORE_BLOCKED`、`FP32_RESTORE_UNCONFIRMED`、启动身份不明或SSH中断均是未完成状态；不能据SSH退出就宣布GPU释放或原服务恢复。所有失败回执保留在新目录。异常构造或硬故障无法证明清理时，入口会拒绝自动恢复，需根据保留的run/owner/原程序回执检查，不能盲目重复start/rollback。

## 可选：PC访问服务HTTP

通常用上述PC入口即可。若要用PC客户端直接调用服务，可单独建立前台SSH转发；关闭该终端只关闭转发，不停止模型服务：

```powershell
& ssh -F 'C:/Users/Administrator/.ssh/lab-4090-only/config' -N -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -L 127.0.0.1:18961:127.0.0.1:18960 lab-4090
```

此后PC端地址为`http://127.0.0.1:18961`。调用方先GET health，使用其`service_nonce`作为`X-Tiger-Service-Nonce`申请lease；后续`/infer_rtc`保留原五字段请求和X-Tiger-Deployment-Session/Lease头。输出动作格式不变。原FP32客户端的精度健康检查不会被修改来接受新服务。

## 本机测试与依赖边界

```powershell
$stage = Split-Path -Parent (Split-Path -Parent $entry)
$stamp = Get-Date -Format 'yyyy-MM-dd-HH-mm-ss'
& 'C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe' -I -B -X utf8 "$stage/scripts/run_local_tests.py" --receipt "$stage/receipts/$stamp-local-tests.json"
```

测试使用明确标记的合成后端和本机loopback，守卫拒绝外部网络、SSH进程、JAX/Torch/SDK导入。真实模型模块只在固定Linux主机、显式在线开关和GPU owner核验后惰性导入。本机无需安装JAX、模型或4090依赖。完整测试中的历史元数据检查仍需要本项目已有的13-15 evidence目录；源码包不包含原始图片和权重。
