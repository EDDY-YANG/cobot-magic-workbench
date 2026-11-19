"""Compatibility infrastructure recovery; no model selection, inference or motion."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import http.client
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import time

from .main import LegacyClient, strict_json

SCHEMA = "agilex-reconcile-v1"
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
LAUNCHER_FILES = {"local_backend.py", "boot_binding_startup.py",
                  "boot_rebind_remote.py", "boot_rebind_inspector.py"}


class RecoveryBlocked(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise RecoveryBlocked(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def finished_job(job):
    if not isinstance(job, dict):
        return False
    if job.get("state") in TERMINAL:
        return True
    if job.get("state") != "blocked":
        return False
    # Legacy web_backend._run records a ValidationError as blocked and adds
    # its timezone-aware finished_at in the same locked update. A bare blocked
    # status is not sufficient evidence that its operation finished.
    try:
        finished = datetime.fromisoformat(job.get("finished_at", ""))
        return finished.tzinfo is not None
    except (TypeError, ValueError):
        return False


def idle_reason(state):
    if not isinstance(state, dict) or not isinstance(state.get("jobs"), list):
        return "JOB_STATE_UNKNOWN"
    if any(not finished_job(j) for j in state["jobs"]):
        return "TASK_ACTIVE_OR_UNKNOWN"
    rec = state.get("hub", {}).get("recording", {})
    if rec.get("active") is not False or rec.get("saving") is not False:
        return "RECORDING_ACTIVE_OR_UNKNOWN"
    p = state.get("precision_workbench", {})
    models = p.get("models", {})
    if p.get("fresh") is not True or p.get("error"):
        return "MODEL_STATE_STALE"
    for name in ("FP32", "BF16"):
        row = models.get(name, {})
        if (row.get("busy") is not False or row.get("lease_active") is not False
                or row.get("transitioning") is True
                or row.get("status") not in ("READY", "STOPPED", "OFF")):
            return "MODEL_LEASE_ACTIVE_OR_UNKNOWN"
    return None


def boot_needed(state):
    p = state.get("precision_workbench", {})
    errors = str(p.get("robot_profile", {}).get("error", "")) + " " + str(p.get("v2_profile", {}).get("error", ""))
    return ("robot host/boot changed" in errors or "V2 host rebooted" in errors
            or state.get("connected") is not True
            or state.get("hub", {}).get("api_reachable") is not True)


class Journal:
    """Small management intent ledger; never raw robot/model telemetry."""
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "reconcile-intents.local.json"
        self.rows = json.loads(self.path.read_text()) if self.path.exists() else {}

    def put(self, key, value):
        if self.rows.get(key) == value:
            return
        self.rows[key] = value
        raw = json.dumps(self.rows, sort_keys=True, indent=2).encode()
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)


class Reconciler:
    def __init__(self, adapter, journal):
        self.adapter, self.journal = adapter, journal

    def cycle(self, repair=False):
        result = {"schema": SCHEMA, "mode": "REPAIR" if repair else "READ_ONLY",
                  "status": "UNKNOWN", "actions": [], "model_loads": 0,
                  "motion_commands": 0, "automatic_task_replay": False}
        try:
            observation = self.adapter.observe()
            state = observation["state"]
            reason = idle_reason(state)
            if reason:
                return {**result, "status": "DEFERRED", "reason_code": reason}
            generation = observation["generation"]
            plan = []
            if boot_needed(state):
                plan.append(("prepare_session", None))
            else:
                for name, endpoint in observation["paths"].items():
                    if endpoint == "BLOCKED":
                        return {**result, "status": "BLOCKED", "reason_code": "UNKNOWN_OR_MISMATCHED_ENDPOINT", "resource": name}
                    if endpoint == "ABSENT":
                        plan.append(("timeqp" if name == "timeqp" else "model_path", None if name == "timeqp" else name))
            result["actions"] = [{"kind": kind, "precision": precision} for kind, precision in plan]
            if not plan:
                # Unknown outcomes are resolved only by observed postconditions.
                for key, value in list(self.journal.rows.items()):
                    if repair and key.startswith(generation + ":") and value.get("state") in ("PENDING", "COMPLETE"):
                        self.journal.put(key, {"state": "OBSERVED_COMPLETE"})
                return {**result, "status": "READY"}
            if not repair:
                return {**result, "status": "PLAN_ONLY"}
            kind, precision = plan[0]  # One bounded transition, then observe again.
            key = generation + ":" + kind + ":" + (precision or "")
            prior = self.journal.rows.get(key, {})
            if prior.get("state") == "PENDING" and kind != "timeqp":
                return {**result, "status": "BLOCKED", "reason_code": "PREVIOUS_OUTCOME_UNKNOWN_NO_REPLAY"}
            if prior.get("state") == "FAILED" or (kind == "prepare_session" and prior.get("state") == "COMPLETE"):
                return {**result, "status": "DEFERRED", "reason_code": "AWAITING_POSTCONDITION_OR_OPERATOR_REVIEW"}
            self.adapter.verify_environment()
            self.journal.put(key, {"state": "PENDING", "kind": kind, "precision": precision})
            if kind == "prepare_session":
                reply = self.adapter.prepare_session()
                require(reply.get("status") in ("LOCAL_WEB_AND_EXISTING_SESSION_READY", "EXISTING_TASK_PRESERVED"),
                        "LEGACY_PREPARATION_NOT_CONFIRMED")
            elif kind == "timeqp":
                self.adapter.ensure_timeqp()
            else:
                self.adapter.ensure_model_path(precision)
            self.journal.put(key, {"state": "COMPLETE", "kind": kind, "precision": precision})
            return {**result, "status": "TRANSITION_COMPLETED_REOBSERVE"}
        except RecoveryBlocked as exc:
            return {**result, "status": "BLOCKED", "reason_code": str(exc)}
        except Exception:
            # Pending intent stays pending. Neither POSTs nor ambiguous launches
            # are blindly retried across process restarts.
            return {**result, "status": "UNKNOWN", "reason_code": "OPERATION_OUTCOME_UNKNOWN_NO_REPLAY"}


REMOTE_PROBE = """
import http.client,json,socket,sys
from pathlib import Path
v=json.load(sys.stdin);rows={}
for port in v['ports']:
 with socket.socket() as s:
  s.settimeout(.3);opened=s.connect_ex(('127.0.0.1',port))==0
 row={'open':opened,'health':None}
 if opened:
  c=http.client.HTTPConnection('127.0.0.1',port,timeout=1.0)
  try:
   c.request('GET','/health',headers={'Connection':'close'});r=c.getresponse();raw=r.read(262145)
   if r.status==200 and len(raw)<=262144 and r.getheader('Content-Type','').split(';')[0]=='application/json':
    row['health']=json.loads(raw)
  except Exception:pass
  finally:c.close()
 rows[str(port)]=row
print(json.dumps({'host':socket.gethostname(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'ports':rows}))
"""


class LegacyAdapter:
    def __init__(self, config, state_dir):
        self.cfg, self.state_dir = config, Path(state_dir)
        self.client = LegacyClient(config["legacy_origin"], timeout=3)
        self.legacy = None
        self.precision = None

    def run(self, argv, timeout=10, data=None, allow_not_found=False):
        value = subprocess.run(argv, input=data, capture_output=True, text=True, timeout=timeout)
        require(value.returncode == 0 or (allow_not_found and "LoadState=not-found" in value.stdout), "REGISTERED_COMMAND_FAILED")
        return value.stdout

    def verify_environment(self):
        require(socket.gethostname() == self.cfg["expected_host"], "WRONG_MANAGEMENT_HOST")
        roots = {"launcher": Path(self.cfg["legacy_launcher_root"]),
                 "legacy": Path(self.cfg["legacy_root"]), "timeqp": Path(self.cfg["timeqp_root"])}
        for group, root in roots.items():
            expected = self.cfg["approved_sources"].get(group)
            require(isinstance(expected, dict) and bool(expected), "SOURCE_MANIFEST_REQUIRED")
            if group == "launcher":
                actual = LAUNCHER_FILES
            else:
                scopes = ("agilex_control",) if group == "legacy" else ("src", "vendor", "scripts")
                actual = {p.relative_to(root).as_posix() for scope in scopes for p in (root / scope).rglob("*")
                          if p.is_file() and p.suffix in (".py", ".json") and "__pycache__" not in p.parts}
            require(set(expected) == actual, "SOURCE_SET_CHANGED")
            for relative, sha in expected.items():
                require(not Path(relative).is_absolute() and ".." not in Path(relative).parts, "UNSAFE_SOURCE_MEMBER")
                path = root / relative
                require(not path.is_symlink() and path.resolve().is_relative_to(root.resolve()), "SOURCE_PATH_ESCAPE")
                require(digest(path.read_bytes()) == sha, "APPROVED_SOURCE_CHANGED")
        expected = self.cfg["expected_timeqp_implementation"]
        require(isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{64}", expected), "TIMEQP_FINGERPRINT_REQUIRED")

    def _imports(self):
        self.verify_environment()
        if self.legacy is None:
            root = Path(self.cfg["legacy_launcher_root"]).resolve()
            sys.path.insert(0, str(root))
            for name in ("local_backend", "boot_binding_startup"):
                prior = sys.modules.get(name)
                require(prior is None or Path(prior.__file__).resolve().parent == root, "FOREIGN_LEGACY_IMPORT")
            self.legacy = importlib.import_module("local_backend")
        if self.precision is None:
            root = Path(self.cfg["legacy_root"]).resolve()
            sys.path.insert(0, str(root))
            prior = sys.modules.get("agilex_control")
            require(prior is None or Path(prior.__file__).resolve().parent == root / "agilex_control", "FOREIGN_CONTROL_IMPORT")
            module = importlib.import_module("agilex_control.precision_services")
            require(Path(module.__file__).resolve() == root / "agilex_control/precision_services.py", "FOREIGN_PRECISION_IMPORT")
            self.precision = module.RealPrecisionIO()
        return self.legacy, self.precision

    def _robot_probe(self, ports):
        alias = self.cfg.get("robot_ssh_alias", "tiger-agilex")
        require(re.fullmatch(r"[A-Za-z0-9_.-]+", alias) is not None, "INVALID_SSH_ALIAS")
        argv = ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=4", alias,
                shlex.join(["/usr/bin/python3", "-I", "-B", "-c", REMOTE_PROBE])]
        value = json.loads(self.run(argv, timeout=8, data=json.dumps({"ports": ports})))
        require(value.get("host") == self.cfg["expected_robot_host"], "WRONG_ROBOT_HOST")
        require(isinstance(value.get("boot_id"), str), "ROBOT_BOOT_UNKNOWN")
        return value

    def _local_health(self, port):
        with socket.socket() as probe:
            probe.settimeout(.3)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return {"open": False, "health": None}
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        value = None
        try:
            conn.request("GET", "/health", headers={"Connection": "close"})
            response = conn.getresponse()
            raw = response.read(262145)
            if response.status == 200 and len(raw) <= 262144 and response.getheader("Content-Type", "").split(";")[0] == "application/json":
                value = strict_json(raw)
        except Exception:
            pass
        finally:
            conn.close()
        row = {"open": True, "health": value}
        if port == 18970:
            row["owner_verified"] = self._timeqp_process_owner()
        return row

    def _timeqp_process_owner(self):
        try:
            output = self.run(["ss", "-H", "-ltnp", "sport = :18970"], timeout=3)
            pids = set(re.findall(r"pid=(\d+)", output))
            if len(pids) != 1:
                return False
            proc = Path("/proc") / next(iter(pids))
            args = [a.decode() for a in (proc / "cmdline").read_bytes().rstrip(b"\x00").split(b"\x00")]
            root = Path(self.cfg["timeqp_root"]).resolve()
            if not args or Path(args[0]).absolute() != root / "environment/bin/python" or "--serve" not in args:
                return False
            scripts = [a for a in args[1:] if a.endswith("timeqp_server.py")]
            if len(scripts) != 1:
                return False
            script = Path(scripts[0])
            if not script.is_absolute():
                script = (proc / "cwd").resolve() / script
            return script.resolve() == root / "scripts/timeqp_server.py"
        except Exception:
            return False

    def _timeqp_valid(self, row):
        h = row.get("health") or {}
        return (h.get("ready") is True and isinstance(h.get("instance_id"), str)
                and h.get("implementation_sha256") == self.cfg["expected_timeqp_implementation"])

    def observe(self):
        require(socket.gethostname() == self.cfg["expected_host"], "WRONG_MANAGEMENT_HOST")
        state = self.client.state()
        if idle_reason(state):
            return {"state": state, "generation": "unknown", "paths": {}}
        models = state["precision_workbench"]["models"]
        ports = [18970] + [18883 if name == "FP32" else 18961 for name, row in models.items()
                           if name in ("FP32", "BF16") and row.get("ready") is True]
        robot = self._robot_probe(ports)
        generation = Path("/proc/sys/kernel/random/boot_id").read_text().strip() + "/" + robot["boot_id"]
        local, remote = self._local_health(18970), robot["ports"]["18970"]
        if local["open"] and (not self._timeqp_valid(local) or local.get("owner_verified") is not True):
            timeqp = "BLOCKED"
        elif remote["open"] and (not self._timeqp_valid(remote) or not self._timeqp_valid(local)
                or remote["health"]["instance_id"] != local["health"]["instance_id"]):
            timeqp = "BLOCKED"
        else:
            timeqp = "READY" if local["open"] and remote["open"] else "ABSENT"
        paths = {"timeqp": timeqp}
        for name in ("FP32", "BF16"):
            if models[name].get("ready") is not True:
                continue
            source, dest = (18882, 18883) if name == "FP32" else (18960, 18961)
            current, target = self._local_health(source), robot["ports"][str(dest)]
            if not target["open"]:
                paths[name] = "ABSENT"
                continue
            a, b = current.get("health") or {}, target.get("health") or {}
            same = (a.get("contract_fingerprint") is not None
                    and a.get("contract_fingerprint") == b.get("contract_fingerprint")
                    and a.get("policy", {}).get("checkpoint_step") == b.get("policy", {}).get("checkpoint_step"))
            if name == "FP32":
                same = same and a.get("manager", {}).get("worker") is not None and a.get("manager", {}).get("worker") == b.get("manager", {}).get("worker")
            else:
                same = same and a.get("service_nonce") is not None and a.get("service_nonce") == b.get("service_nonce")
            paths[name] = "READY" if same else "BLOCKED"
        return {"state": state, "generation": generation, "paths": paths}

    def _assert_idle(self):
        reason = idle_reason(self.client.state())
        require(reason is None, reason or "NOT_IDLE")

    def prepare_session(self):
        legacy, _ = self._imports()
        self._assert_idle()
        return legacy.prepare_session(self.cfg["legacy_origin"])

    def _start_owned(self, kind, argv, working_directory=None):
        unit = "agilex-platform-" + kind + ".service"
        path = self.state_dir / ("owned-" + kind + ".local.json")
        expected = {"unit": unit, "argv": argv, "working_directory": working_directory}
        output = self.run(["systemctl", "--user", "show", unit, "-p", "LoadState", "-p", "ActiveState", "-p", "MainPID"], allow_not_found=True)
        properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        registered = json.loads(path.read_text()) if path.exists() else None
        require(registered is None or registered == expected, "OWNED_UNIT_REGISTRY_MISMATCH")
        if properties.get("LoadState") != "not-found":
            require(registered == expected, "UNIT_OCCUPIED_WITHOUT_REGISTRY")
            pid = int(properties.get("MainPID", "0"))
            require(pid > 0 and properties.get("ActiveState") == "active", "OWNED_UNIT_PENDING_OR_FAILED")
            actual = (Path("/proc") / str(pid) / "cmdline").read_bytes().rstrip(b"\x00").split(b"\x00")
            require(actual == [a.encode() for a in argv], "OWNED_UNIT_PROCESS_MISMATCH")
            return
        if registered is None:
            with path.open("x") as output:
                json.dump(expected, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
        command = ["systemd-run", "--user", "--quiet", "--collect", "--unit=" + unit,
                   "--property=Type=exec", "--property=Restart=no", "--setenv=PYTHONDONTWRITEBYTECODE=1"]
        if working_directory:
            command.append("--property=WorkingDirectory=" + working_directory)
        self.run(command + ["--", *argv], timeout=12)

    def ensure_timeqp(self):
        _, precision = self._imports()
        self._assert_idle()
        with precision.switch_guard() as guard:
            guard()
            root = Path(self.cfg["timeqp_root"]).resolve()
            local = self._local_health(18970)
            require(not local["open"] or (self._timeqp_valid(local) and local.get("owner_verified") is True), "UNKNOWN_LOCAL_TIMEQP_PORT")
            if not local["open"]:
                self._start_owned("timeqp", [str(root / "environment/bin/python"), "-I", "-B",
                                           str(root / "scripts/timeqp_server.py"), "--serve"], str(root))
            for _ in range(20):
                guard()
                local = self._local_health(18970)
                if self._timeqp_valid(local) and local.get("owner_verified") is True:
                    break
                time.sleep(.2)
            require(self._timeqp_valid(local) and local.get("owner_verified") is True, "TIMEQP_START_NOT_CONFIRMED")
            remote = self._robot_probe([18970])["ports"]["18970"]
            if not remote["open"]:
                alias = self.cfg.get("robot_ssh_alias", "tiger-agilex")
                self._start_owned("timeqp-forward", ["/usr/bin/ssh", "-NT", "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
                    "-o", "ConnectTimeout=4", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                    "-R", "127.0.0.1:18970:127.0.0.1:18970", alias])
                for _ in range(8):
                    guard()
                    remote = self._robot_probe([18970])["ports"]["18970"]
                    if remote["open"]:
                        break
                    time.sleep(.2)
            require(self._timeqp_valid(remote)
                    and remote["health"]["instance_id"] == local["health"]["instance_id"],
                    "TIMEQP_FORWARD_NOT_CONFIRMED")
            guard()

    def ensure_model_path(self, precision_name):
        _, precision = self._imports()
        self._assert_idle()
        row = precision.inspect_models()[precision_name]
        require(row.get("ready") is True and row.get("busy") is False
                and row.get("lease_active") is False, "MODEL_NOT_ALREADY_READY_AND_IDLE")
        precision.prepare_model_path(precision_name, row["step"], lambda *args, **kwargs: None)


@contextmanager
def exclusive(directory):
    import fcntl
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "reconcile.lock").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--repair", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=7)
    args = parser.parse_args()
    require(5 <= args.interval <= 10, "POLL_INTERVAL_MUST_BE_5_TO_10_SECONDS")
    require(args.config.name.endswith(".local.json"), "INSTALLATION_CONFIG_MUST_BE_LOCAL")
    config = strict_json(args.config.read_bytes())
    with exclusive(args.state_dir):
        engine = Reconciler(LegacyAdapter(config, args.state_dir), Journal(args.state_dir))
        previous = None
        while True:
            result = engine.cycle(repair=args.repair)
            raw = json.dumps(result, sort_keys=True)
            if raw != previous:
                print(raw, flush=True)
                temporary = Path(args.state_dir) / "reconcile-status.tmp"
                temporary.write_text(raw + "\n")
                os.replace(temporary, Path(args.state_dir) / "reconcile-status.local.json")
                previous = raw
            if not args.watch:
                return 0 if result["status"] in ("READY", "PLAN_ONLY", "TRANSITION_COMPLETED_REOBSERVE") else 2
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
