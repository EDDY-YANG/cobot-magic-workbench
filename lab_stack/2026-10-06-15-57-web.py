# PC PowerShell: & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-15-57-web.py'
# Starts a PC-only website. Opening it does not connect SSH or move the robot.
# Linux --agent is selected only by the PC bridge; do not paste it as a motion command.
import argparse
import json
import os
from pathlib import Path
import sys
import webbrowser

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description="AgileX 四页网页工作台；默认仅 PC 本地页面")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument('--semantic-config', type=Path, help='独立语义监督配置；仅载入明确审阅的历史录像')
    parser.add_argument('--presence-config',type=Path,help='人员/手暂停逻辑的只读本地视频回放配置')
    parser.add_argument("--agent", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    cfg = json.loads((ROOT / "2026-10-06-14-05-config.json").read_text(encoding="utf-8"))
    if args.agent:
        if os.name != "posix":
            raise RuntimeError("SSH agent only runs on the verified AgileX Linux host")
        from agilex_control.web_agent import run_agent
        run_agent(cfg, ROOT)
        return
    from agilex_control.web_backend import WebApp, make_server
    workspace = None
    if args.semantic_config:
        from agilex_control.semantic_workspace import SemanticWorkspace
        workspace = SemanticWorkspace(json.loads(args.semantic_config.read_text(encoding='utf-8')))
    presence=None
    if args.presence_config:
        from agilex_control.presence_presentation import PresencePresentation
        presence=PresencePresentation(json.loads(args.presence_config.read_text(encoding='utf-8')))
    app = WebApp(cfg, ROOT, semantic_workspace=workspace,presence_workspace=presence)
    try:
        server = make_server(app, args.port)
    except OSError as exc:
        print(f"端口 {args.port} 不可用：{exc}。不会关闭或接管旧网页。可显式 --port 8768。", file=sys.stderr)
        app.close()
        return 2
    url = f"http://127.0.0.1:{server.server_port}/"
    print("AgileX 四页工作台：" + url, flush=True)
    print("页面加载不发机械臂指令；点击‘连接’建立只读 SSH。Ctrl+C 关闭网页服务。", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            app.close()
        except Exception as exc:
            print("远端关闭未确认：" + str(exc), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
