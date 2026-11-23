"""Apply published presentation assets to the existing operator HTTP services.

Installation paths and source pins live in an external deployment configuration.
The original process entry, model/robot clients and POST handlers own all actions.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ASSET_PATHS = {
    '/api/appearance/workbench-layout.js': ('workbench-layout.js', 'text/javascript; charset=utf-8'),
    '/api/device-view/panel.mjs': ('device-mode-panel.mjs', 'text/javascript; charset=utf-8'),
    '/api/device-view/panel.css': ('device-mode-panel.css', 'text/css; charset=utf-8'),
    '/device-mode-panel.mjs': ('device-mode-panel.mjs', 'text/javascript; charset=utf-8'),
    '/device-mode-panel.css': ('device-mode-panel.css', 'text/css; charset=utf-8'),
    '/api/appearance/operator-theme.css': ('operator-theme.css', 'text/css; charset=utf-8'),
    '/api/appearance/operator-theme.js': ('operator-theme.js', 'text/javascript; charset=utf-8'),
}


def inject_html(raw):
    text = raw.decode('utf-8')
    if text.count('</body>') != 1:
        raise ValueError('Expected one workbench HTML body')
    if 'src="/api/appearance/operator-theme.js"' in text:
        return raw
    tags = ('<link rel="stylesheet" href="/api/appearance/operator-theme.css">'
            '<script defer src="/api/appearance/operator-theme.js"></script>')
    return text.replace('</body>', tags+'</body>', 1).encode('utf-8')


def load_installation(path):
    config = json.loads(path.read_text())
    if config['kind'] not in ('workbench', 'review'):
        raise ValueError('Unknown UI service')
    for filename, expected in config['pins'].items():
        if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != expected:
            raise ValueError('Installation source mismatch: '+filename)
    entry = Path(config['entry']).resolve(strict=True)
    if str(entry) not in config['pins']:
        raise ValueError('Unpinned entry point')
    # Resolve only the published theme bundled next to this tool.
    assets = Path(__file__).resolve().parents[1]/'lab_stack'/'web'
    static = {route: ((assets/name).read_bytes(), mime)
              for route, (name, mime) in ASSET_PATHS.items()}
    image_path = config.get("device_view_asset")
    if image_path:
        image_file = Path(image_path).resolve(strict=True)
        if str(image_file) not in config["pins"]:
            raise ValueError("Unpinned device overview image")
        static["/api/device-view/cobot-top.png"] = (image_file.read_bytes(), "image/png")
    sys.path.insert(0, str(entry.parent))
    spec = importlib.util.spec_from_file_location('_original_operator_entry', entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return config, module, static


def decorate_right_feedback(raw):
    """Prefer an explicit Hub integrity failure over a fresh-looking CAN snapshot."""
    source = raw.decode("utf-8")

    def replace_once(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise ValueError("Unrecognized operator app structure: " + old.splitlines()[0])
        source = source.replace(old, new, 1)

    marker = "function capability(action) {"
    replace_once(marker, """let rightCanErrors = null;
function observeRightCanErrors(state, now = performance.now()) {
  const hub = state?.hub;
  const arm = hub?.arms?.right;
  const owner = hub?.producer;
  if (state?.connected !== true || hub?.api_reachable !== true ||
      !Number.isInteger(owner?.pid) || typeof owner.proc_start_ticks !== "string" ||
      !Number.isSafeInteger(arm?.error_frames) || arm.error_frames < 0) {
    rightCanErrors = null;
    return;
  }
  const identity = owner.pid + ":" + owner.proc_start_ticks;
  if (rightCanErrors?.identity !== identity || arm.error_frames < rightCanErrors.count) {
    rightCanErrors = { identity, count: arm.error_frames, lastIncreaseMs: -Infinity };
    return;
  }
  if (arm.error_frames > rightCanErrors.count) rightCanErrors.lastIncreaseMs = now;
  rightCanErrors.count = arm.error_frames;
}
function rightSharedFeedbackBlock(state, now = performance.now()) {
  const hub = state?.hub;
  const arm = hub?.arms?.right;
  if (state?.connected !== true || hub?.api_reachable !== true || !arm) return null;
  if (rightCanErrors && now - rightCanErrors.lastIncreaseMs <= 1500)
    return "右侧 CAN 正在新增错误帧。右臂姿态及新运动目标已暂停，请检查总线。";
  if (arm.fresh !== false) return null;
  const count = Number.isInteger(arm.timestamp_failures) && arm.timestamp_failures > 0
    ? "（时间戳异常 " + arm.timestamp_failures + " 次）" : "";
  return "右臂共享反馈无效" + count + "，暂不能确认单臂姿态。请核对右侧 CAN 反馈来源。";
}
""" + marker)
    marker = '  if (action === "move_target" && !targetInitialized)'
    replace_once(marker, """  const feedbackBlock = rightSharedFeedbackBlock(lastState);
  if (feedbackBlock && (motionActions.has(action) || action === "mode_takeover" || action === "mode_high_follow"))
    return { available: false, reason: feedbackBlock };
""" + marker)
    replace_once("  renderDualFollowerTelemetry(state);", "  observeRightCanErrors(state);\n  renderDualFollowerTelemetry(state);")
    marker = "    const sourceReady = connected && arm.observer_running === true &&"
    replace_once(marker, """    const sharedBlock = role === "right" && rightSharedFeedbackBlock(state);
    const sourceReady = connected && !sharedBlock && arm.observer_running === true &&""")
    replace_once('!connected ? "未连接" : !sourceReady ? "观察通道未就绪" : "关节过期／缺失",',
                 '!connected ? "未连接" : sharedBlock ? "右臂共享反馈无效" : !sourceReady ? "观察通道未就绪" : "关节过期／缺失",')
    replace_once("    right = object(arms.right),", """    rightSource = object(arms.right),
    feedbackBlock = rightSharedFeedbackBlock(state),
    right = feedbackBlock ? { ...rightSource, fresh: false, q_deg: null, joints_deg: null,
      grip_m: null, gripper_m: null, gripper_feedback: null } : rightSource,""")
    replace_once('"device-can",\n    right.fresh === true', '"device-can",\n    feedbackBlock ? "右臂共享反馈无效" : right.fresh === true')
    replace_once('"device-can-detail",\n    "反馈年龄 "', '"device-can-detail",\n    feedbackBlock || "反馈年龄 "')
    replace_once('setPill("motion-feedback-state", displayFeedback.label, displayFeedback.kind);',
                 'setPill("motion-feedback-state", feedbackBlock ? "右臂反馈未通过" : displayFeedback.label, feedbackBlock ? "warn" : displayFeedback.kind);')
    replace_once('"robot-source",\n    actual', '"robot-source",\n    feedbackBlock || (actual')
    replace_once(': "实际姿态：未接收到真实反馈",\n  );', ': "实际姿态：未接收到真实反馈"),\n  );')
    marker = '  if (error.source === "precondition" && available)'
    replace_once(marker, """  if (online && currentCapability?.available === false && currentCapability.reason)
    current = "当前阻塞：" + currentCapability.reason;
""" + marker)
    return source.encode("utf-8")


def decorate_workbench(module, static):
    original_assets = module.assets
    def assets(root):
        result = original_assets(root)
        for path in ('/', '/index.html'):
            raw, mime = result[path]
            raw = raw.replace(b'src="/workbench-layout.js', b'src="/api/appearance/workbench-layout.js')
            result[path] = (inject_html(raw), mime)
        app, mime = result["/app.js"]
        result["/app.js"] = (decorate_right_feedback(app), mime)
        result.update(static)
        return result
    module.assets = assets


def decorate_review(module, static):
    original_handler = module.Handler
    original_index = (module.HERE/'web'/'index.html').read_bytes()
    index = inject_html(original_index)
    class Handler(original_handler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if self.valid_host() and (path in static or path in ('/', '/index.html')):
                raw, mime = static.get(path, (index, 'text/html; charset=utf-8'))
                self.send_headers(200, mime, len(raw))
                self.wfile.write(raw)
                return
            return super().do_GET()
    module.Handler = Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installation', type=Path, required=True)
    args = parser.parse_args()
    config, module, static = load_installation(args.installation)
    if config['kind'] == 'workbench':
        # A pinned server-only extension; robot source/handlers stay unchanged.
        root = Path(config['argv'][config['argv'].index('--root') + 1])
        sys.path.insert(0, str(root))
        from agilex_control import web_backend
        recovery_path = Path(__file__).with_name('workbench_usb_recovery.py')
        for helper in (recovery_path, recovery_path.with_name('usb_reconnect_probe.py')):
            if str(helper.resolve()) not in config['pins']:
                raise ValueError('Unpinned USB reconnect helper: ' + str(helper))
        recovery_spec = importlib.util.spec_from_file_location('_usb_recovery', recovery_path)
        recovery = importlib.util.module_from_spec(recovery_spec)
        recovery_spec.loader.exec_module(recovery)
        recovery.install(web_backend)
        decorate_workbench(module, static)
    else:
        decorate_review(module, static)
    sys.argv = [config['entry'], *config['argv']]
    module.main()


if __name__ == '__main__':
    main()
