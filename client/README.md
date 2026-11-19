# AgileX SSH薄客户端

这是完整连接客户端源码，不包含网页副本、模型权重、机器人SDK或控制器。所有业务页面、状态和动作接口来自授权4090服务器。同一服务器发布新网页后，所有客户端下次请求使用服务器版本。

需要Python3.10+和OpenSSH。本版尚非无需Python的签名安装程序；不自动下载解释器、安装驱动或模型。

1. 由服务器管理员给你独立SSH访问权限，并提供主机密钥指纹。先通过标准SSH配置完成身份校验；不要关闭StrictHostKeyChecking，也不要分享别人的私钥。
2. 复制client.example.json为client.local.json，填写已授权的SSH别名及自己的SSH配置路径。密码/私钥内容不写入JSON、不放进GitHub。
3. Windows双击Start-Workstation.cmd；macOS/Linux运行`python3 gateway.py --config client.local.json`。Windows也可直接执行该Python命令。
4. 打开显示的127.0.0.1地址。默认端口8875若已被旧客户端占用，请先选另一个local_port，例如8880；程序不会接管已有服务。

SSH必须允许对服务器127.0.0.1:8910的本地转发；8910应运行兼容平台服务，健康协议为agilex-platform-health-v1。没有兼容服务器或授权密钥时，本客户端会明确显示未连接，不能代替GPU服务器或设备代理。

客户端仅创建自己的SSH隧道，不在服务器执行shell启动命令，不选择模型、不自动恢复动作。连接恢复仅恢复网页访问；POST不自动重发。当前设备任务仍受服务器现有独占锁、STOP、USB、反馈与模型准入约束。

第一版可使用已有SSH账户；细分查看者/操作者权限和跨用户控制租约仍需服务器升级，不能把共享一个SSH私钥称为多用户权限管理。初次分发只给经过授权的使用者。
