# 完整单行命令：Windows PC PowerShell；无需预定义变量，复制时去掉行首 #。
# 按需选择一条命令，不要把所有示例一起执行。
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' --help
# # 显式同步新源码，不启动模型或运动：
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' sync
# # 同步后读取真实状态：
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' status
# # AgileX 本机或已 SSH 的 Linux 终端使用此命令显示帮助：
# bash "/media/robot/RECORDING_USB/2026-10-06-14-05-AGILEX-UNIFIED-CONTROL/2026-10-06-14-05-run.sh" --help
# 本轮仅补充命令注释；未运行测试、SSH、模型、相机或机械臂。
"""Shared PC/Linux entry. Importing this file does not touch robot hardware."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))  # Explicit project import under isolated python -I.

if __name__ == "__main__":
    cfg = json.loads((ROOT / "2026-10-06-14-05-config.json").read_text(encoding="utf-8"))
    if os.name == "nt":
        from agilex_control.pc import main
    else:
        from agilex_control.cli import main
    raise SystemExit(main(cfg, ROOT))
