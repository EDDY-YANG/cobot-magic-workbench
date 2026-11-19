"""Pure, allowlisted projection of legacy state; never grants admission."""
import math
import re

STATUS_SCHEMA = "agilex-platform-status-v1"
RELEASE_SCHEMA = "agilex-software-release-v1"
ATOM = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
BOOT = re.compile(r"^[a-fA-F0-9-]{36}$")


def atom(value):
    return value if isinstance(value, str) and ATOM.fullmatch(value) else None


def object_or_empty(value):
    return value if isinstance(value, dict) else {}


def classify_error(value):
    if not value:
        return None
    text = str(value).lower()
    if "boot" in text or "reboot" in text:
        return "BOOT_SESSION_CHANGED"
    if "source" in text or "sha" in text or "fingerprint" in text:
        return "SOURCE_IDENTITY_MISMATCH"
    if "usb" in text or "mount" in text:
        return "USB_IDENTITY_UNCONFIRMED"
    if "timeqp" in text:
        return "TIMEQP_UNAVAILABLE"
    if "model" in text:
        return "MODEL_UNCONFIRMED"
    return "LEGACY_GUARD_BLOCKED"


def component(ready, error=None, fresh=True):
    if not fresh:
        return {"status": "UNKNOWN", "reason_code": "COMPONENT_STALE"}
    return {"status": "READY" if ready is True and not error else "BLOCKED",
            "reason_code": classify_error(error) if error else
            (None if ready is True else "NOT_READY")}


def project_status(state, *, release_id, server_instance_id, observed_at,
                   upstream_status="ONLINE", age_s=0.0, error_code=None):
    """Only copied fields below can escape. Config, CSRF, raw errors never do."""
    online = upstream_status == "ONLINE" and isinstance(state, dict)
    unknown = {"status": "UNKNOWN", "reason_code": "UPSTREAM_UNAVAILABLE"}
    components = {name: dict(unknown) for name in
                  ("robot_link", "hub", "shared_runner", "v2_runner")}
    components["storage"] = {"status": "UNKNOWN", "uuid": None, "mountpoint": None,
                             "free_bytes": None, "verified": False, "reason_code": "USB_IDENTITY_UNCONFIRMED"}
    models = {name: {"status": "UNKNOWN", "step": None}
              for name in ("FP32", "BF16")}
    ownership = {"status": "UNKNOWN", "active_jobs": []}
    boot = {"status": "UNKNOWN", "robot_boot_id": None}
    capabilities = []
    validation = "UNKNOWN"
    current_failure = None if online else {
        "code": error_code or "UPSTREAM_UNAVAILABLE", "component": "legacy_backend"}
    historical_failure = None
    if online:
        precision = object_or_empty(state.get("precision_workbench"))
        hub = object_or_empty(state.get("hub"))
        profile = object_or_empty(precision.get("robot_profile"))
        v2 = object_or_empty(precision.get("v2_profile"))
        precision_fresh = precision.get("fresh") is True and not precision.get("error")
        components["robot_link"] = component(state.get("connected"))
        components["hub"] = component(hub.get("api_reachable"),
                                      fresh=state.get("connected") is True)
        components["shared_runner"] = component(profile.get("ready"), profile.get("error"),
                                                 fresh=precision_fresh)
        components["v2_runner"] = component(v2.get("ready"), v2.get("error"),
                                             fresh=v2.get("fresh") is True)
        rows = object_or_empty(precision.get("models"))
        for name in models:
            row = object_or_empty(rows.get(name))
            # STOPPED/OFF is a healthy intentional resource state, not a failure.
            status = str(row.get("status", row.get("state", ""))).upper()
            known_idle = row.get("busy") is False and row.get("lease_active") is False
            if precision_fresh and row:
                if row.get("busy") is True or row.get("lease_active") is True:
                    model_status = "ACTIVE"
                elif row.get("ready") is True and known_idle:
                    model_status = "READY"
                elif status in ("STOPPED", "OFF", "NOT_RUNNING", "ABSENT"):
                    model_status = "OFF"
                else:
                    model_status = "UNKNOWN"
                step = row.get("step")
                models[name] = {"status": model_status,
                                "step": step if type(step) is int and step >= 0 else None}
        usb = object_or_empty(state.get("usb"))
        selected = object_or_empty(object_or_empty(state.get("usb_storage")).get("selected"))
        usb_uuid, mount = atom(selected.get("uuid")), selected.get("mountpoint")
        valid_mount = isinstance(mount, str) and mount.startswith("/media/") and len(mount) <= 512 and not any(ord(c) < 32 for c in mount)
        verified = state.get("connected") is True and usb.get("verified") is True and usb_uuid is not None and valid_mount
        free = selected.get("free_bytes", selected.get("available_bytes"))
        components["storage"] = {"status": "READY" if verified else "UNKNOWN",
                                 "uuid": usb_uuid if verified else None,
                                 "mountpoint": mount if verified else None,
                                 "free_bytes": free if verified and type(free) is int and free >= 0 else None,
                                 "verified": verified,
                                 "reason_code": None if verified else "USB_IDENTITY_UNCONFIRMED"}
        jobs = state.get("jobs")
        jobs = jobs if isinstance(jobs, list) else []
        active = []
        for raw in jobs[-32:]:
            job = object_or_empty(raw)
            entry = {"id": atom(job.get("id")), "action": atom(job.get("action")),
                     "state": atom(job.get("state"))}
            if job.get("state") in ("queued", "running", "unknown"):
                active.append(entry)
            elif job.get("state") in ("failed", "cancelled", "interrupted"):
                historical_failure = {**entry,
                    "context": "HISTORICAL_JOB_NOT_CURRENT_CONNECTION"}
        recording = object_or_empty(hub.get("recording"))
        model_busy = any(m["status"] == "ACTIVE" for m in models.values())
        if active or model_busy or recording.get("active") is True or recording.get("saving") is True:
            owner_status = "ACTIVE"
        elif isinstance(state.get("jobs"), list) and recording.get("active") is False and recording.get("saving") is False:
            owner_status = "IDLE"
        else:
            owner_status = "UNKNOWN"
        ownership = {"status": owner_status, "active_jobs": active}
        value = profile.get("boot_id")
        if precision_fresh and isinstance(value, str) and BOOT.fullmatch(value):
            boot = {"status": "OBSERVED", "robot_boot_id": value}
        for action, raw in object_or_empty(state.get("capabilities")).items():
            if not atom(action):
                continue
            cap = object_or_empty(raw)
            available = cap.get("available")
            capabilities.append({"action": action,
                                 "available": available if type(available) is bool else None,
                                 "reason_code": classify_error(cap.get("reason"))})
        acceptance = object_or_empty(v2.get("acceptance"))
        if v2.get("fresh") is True and components["v2_runner"]["status"] == "READY":
            validation = "VALID" if acceptance.get("validated") is True else "NOT_VALIDATED"
        for name, value in components.items():
            if value["status"] != "READY":
                current_failure = {"code": value["reason_code"], "component": name}
                break
    components["models"] = models
    if type(age_s) not in (int, float) or not math.isfinite(age_s) or age_s < 0:
        age_s = None
    return {
        "schema": STATUS_SCHEMA, "protocol_version": 1, "observed_at": observed_at,
        "server_instance_id": server_instance_id,
        "upstream": {"status": upstream_status, "fresh": online, "age_s": age_s,
                     "error_code": error_code},
        "software_release": {"schema": RELEASE_SCHEMA, "release_id": release_id,
                             "integrity": "UNVERIFIED", "scope": "management_facade"},
        "runtime_session": {"schema": "agilex-runtime-session-v1",
                            "status": "OBSERVED" if online else "UNKNOWN",
                            "components": components, "ownership": ownership, "boot": boot,
                            "network": {"status": "READY" if online else "UNKNOWN"}},
        "validation_record": {"schema": "agilex-validation-summary-v1", "status": validation,
                              "source": "legacy_guard", "motion_verified": False},
        "legacy_guard": {"authoritative": True, "execution_admission_granted": False,
                         "capabilities": capabilities},
        "current_failure": current_failure, "historical_failure": historical_failure}
