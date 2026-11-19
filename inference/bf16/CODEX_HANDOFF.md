# CODEX_HANDOFF.md

## 当前目标

BF16＋现有guided RTC独立服务、PC启停/回退、历史服务验收已完成本机离线开发。下一步是4090恢复后按README上传与真实验收；本轮禁止在线，未上传/加载模型/机器人运行。

## 项目关键约束

* 新remote root：`/opt/cobot-workbench/lab-packages/2026-10-21-10-59-BF16-RTC-Service`，loopback18960；PC默认lab-4090/既有独立SSH config。
* 旧BF16依赖13-15目录只读，H50/7D绝对rad-m/F32输出/10steps/RTC12-35-beta5/18Hz保持。不是V2，不接QP/APPLY。
* 原FP32保持；原GPU owner占用则拒绝启动。prepare-handover只读，原FP32停止是显式手工原controller命令。回退恢复捕获step，不硬编码5000。
* 默认dry-run，任何在线操作均需ExecuteOnline。源码/hash/host/boot/SSD/解释器/nonce/lease守卫不能放宽。
* 测试：README的scripts/run_local_tests.py，绝对PC Python3.12，禁止外连/模型导入。无需安装依赖。
* 本轮服务验收只读4090已有历史数据，非真实机器人数据；真实机器人原始流/诊断仍须当次核验USB。

## 已修改文件

* service/contract.py、backend.py、server.py：独立BF16协议、真实加载入口与服务。
* service/control.py、pc/bf16_control.py、PC .ps1：独立启停、上传、归属检查、回退。
* service/acceptance.py：严格16历史数据+黄金数组的3+100 HTTP验收。
* tests、scripts、README/reports/source_manifest：CPU验证、冻结、执行说明。旧目录/正式源码不改。

## 已完成验证

* 69统一tests通过，零失败/错误/跳过；148 loopback连接，外连/模型导入尝试0。
* 实际HTTP联测103合成请求，结果只能SYNTHETIC_PROTOCOL_TEST_ONLY；错owner与signedzero mismatch拒绝。
* PowerShell AST零错误，inspect/start5000 dry-run已执行；无真实SSH。
* 最终2545正式文件/冻结输入/hash/源码ZIP回读见receipts/2026-10-21-11-35-offline-freeze.json和release.json。

## 当前问题

* 真实Linux/GPU构造、IR、暖机、新服务延时和原黄金数组重放尚未执行。
* 5k历史黄金不能代其他checkpoint验收；原BF16相对FP32最坏0.722°/2.668mm未获任务精度准入。
* 原FP32网页/机器人NO_SEND仍保持原精度合同，本包不是直接给原网页换模型的开关。
* 部分构造/关停不能证实结束时拒绝自动恢复；保存run/owner/closed/source日志，不能盲目重复执行。

## 下一步

4090可用且Tiger恢复在线范围后，按README：upload→inspect→prepare-handover→手工原controller停止→start5000→status实际READY→accept→rollback原capturedstep。原网络、主机、盘、owner和source状态都需刷新。不要在当前离线阶段调用ExecuteOnline。

## 不要重复做

* 不自动暂停/抢占FP32，不改变原配置，不重写SDK/ROS/CAN，不添加V2/QP。
* 不将native response previous_valid_steps35误当实际已知prefix35；native请求仍是0。
* config_sha256统一原JSON文件字节SHA，不能与canonical JSON hash混用。
* 不用wrapper exit、process alive、synthetic tests或历史服务PASS冒充真实机器人验收。
