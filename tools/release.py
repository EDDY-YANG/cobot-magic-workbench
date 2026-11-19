"""Deterministic, standard-library source/client packages. No deployment actions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

SCHEMA = "agilex-release-v1"
MANIFEST = "release-manifest.json"
PREFIXES = {
    "source": ("client/", "server/", "web/", "site/", "tools/", "tests/", "docs/", ".github/workflows/"),
    "client": ("client/",),
}
TOP_LEVEL = {"README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", ".gitignore", "pyproject.toml"}
EXTENSIONS = {".py", ".md", ".html", ".css", ".js", ".mjs", ".toml", ".yaml",
              ".yml", ".txt", ".ps1", ".cmd", ".bat", ".sh", ".svg", ".png"}
EXCLUDED = {".git", "__pycache__", ".venv", "venv", "node_modules", "receipts",
            "runs", "logs", "secrets", "weights", "checkpoints", "raw", "data",
            "dist", "build", "releases", "staging", ".pytest_cache"}
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_FILES = 2000


class ReleaseError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(name):
    require(isinstance(name, str) and name and "\\" not in name and ":" not in name,
            "Unsafe member path")
    path = PurePosixPath(name)
    require(not path.is_absolute() and path.as_posix() == name
            and all(p not in ("", ".", "..") for p in name.split("/")),
            "Traversal or noncanonical member path: " + name)
    require(not any(ord(c) < 32 for c in name), "Control character in path")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10))}
    require(all(p == p.rstrip(" .") and p.split(".")[0].lower() not in reserved
                for p in path.parts), "Nonportable Windows member path")
    return path


def allowed(name, package):
    path = safe_path(name)
    lower = name.lower()
    if any(p.lower() in EXCLUDED for p in path.parts):
        return False
    if path.name.startswith(".env") or lower.endswith((".local.json", ".log")):
        return False
    scope = any(name.startswith(prefix) for prefix in PREFIXES[package])
    if package == "source" and name in TOP_LEVEL:
        return True
    if not scope:
        return False
    if path.name == "LICENSE":
        return True
    if path.suffix.lower() == ".json":
        return lower.endswith((".example.json", ".schema.json"))
    return path.suffix.lower() in EXTENSIONS


def no_symlinks(path):
    absolute = Path(os.path.abspath(path))
    for member in (absolute, *absolute.parents):
        require(not member.is_symlink(), "Symlink refused: " + str(member))
    return absolute


def walk_files(root, exclude_runtime=False):
    root = no_symlinks(root)
    require(root.is_dir(), "Directory missing: " + str(root))
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            require(not (Path(current) / name).is_symlink(),
                    "Symlink refused: " + str(Path(current) / name))
        if exclude_runtime:
            dirs[:] = [name for name in dirs if name.lower() not in EXCLUDED]
        for name in files:
            path = Path(current) / name
            require(stat.S_ISREG(path.stat().st_mode), "Nonregular file refused")
            yield path.relative_to(root).as_posix(), path


def check_content(name, raw):
    require(len(raw) <= MAX_FILE, "File exceeds package limit: " + name)
    require(not re.search(rb"-----BEGIN (?:OPENSSH|RSA|EC|DSA|ENCRYPTED)? ?PRIVATE KEY-----", raw),
            "Private key material refused: " + name)
    require(not re.search(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})", raw),
            "Credential-shaped content refused: " + name)


def source_payload(root, package):
    root = no_symlinks(root)
    require(root.is_dir(), "Source directory missing")
    payload = {}
    # Walk only explicit scopes: never traverse model/data/history directories.
    for prefix in PREFIXES[package]:
        folder = root / prefix.rstrip("/")
        require(not folder.is_symlink(), "Symlink scope refused")
        if folder.exists():
            for relative, path in walk_files(folder, exclude_runtime=True):
                name = prefix + relative
                if allowed(name, package):
                    payload[name] = path.read_bytes()
    if package == "source":
        for name in TOP_LEVEL:
            path = root / name
            require(not path.is_symlink(), "Symlink member refused: " + name)
            if path.is_file():
                payload[name] = path.read_bytes()
    require(payload, "No allowed source files")
    for name, raw in payload.items():
        check_content(name, raw)
    require(len(payload) <= MAX_FILES and sum(map(len, payload.values())) <= MAX_TOTAL,
            "Package exceeds file-count or size limit")
    return payload


def role_for(name):
    first = name.split("/", 1)[0]
    return {"client": "client", "web": "web", "server": "server_facade",
            "tools": "release_tools"}.get(first, "source_support")


def role_hashes(rows):
    roles = {}
    for row in rows:
        roles.setdefault(role_for(row["path"]), []).append(row)
    return {role: digest(canonical(members)) for role, members in sorted(roles.items())}


def git_revision(root):
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != Path(root).resolve():
            return None
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                                capture_output=True, text=True, timeout=5)
        value = result.stdout.strip()
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=5)
        return value if (result.returncode == 0 and status.returncode == 0
                         and not status.stdout.strip()
                         and re.fullmatch(r"[0-9a-f]{40,64}", value)) else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def make_manifest(payload, package, version, revision):
    require(re.fullmatch(r"[0-9][A-Za-z0-9._-]{0,63}", version) is not None
            and ".." not in version, "Unsafe version")
    require(len({name.casefold() for name in payload}) == len(payload),
            "Case-colliding paths are not portable")
    rows = [{"path": name, "size": len(raw), "sha256": digest(raw)}
            for name, raw in sorted(payload.items())]
    body = {"schema": SCHEMA, "package": package, "version": version,
            "source_revision": revision, "source_tree_sha256": digest(canonical(rows)),
            "roles": role_hashes(rows), "files": rows}
    body["release_id"] = package + "-" + version + "-" + digest(canonical(body))[:16]
    return body


def write_zip(path, manifest, payload):
    path = no_symlinks(path)
    require(path.parent.is_dir(), "Output parent must already exist")
    with path.open("xb") as target:
        try:
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
                for name, raw in sorted({**payload, MANIFEST: canonical(manifest)}.items()):
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    archive.writestr(info, raw)
        except BaseException:
            target.close()
            path.unlink()
            raise


def build(root, output, package="source", version="0.1.0-alpha.1"):
    require(package in PREFIXES, "Unknown package")
    payload = source_payload(root, package)
    manifest = make_manifest(payload, package, version, git_revision(root))
    write_zip(output, manifest, payload)
    verify(output)
    return manifest


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def read_package(path):
    path = no_symlinks(path)
    if path.is_dir():
        members = {}
        for name, file in walk_files(path):
            require(file.stat().st_size <= MAX_FILE, "Oversize member")
            members[name] = file.read_bytes()
            require(len(members) <= MAX_FILES + 1
                    and sum(map(len, members.values())) <= MAX_TOTAL + MAX_FILE, "Package too large")
        return members
    require(path.is_file(), "Package not found")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        require(len(infos) <= MAX_FILES + 1, "Too many members")
        require(sum(info.file_size for info in infos) <= MAX_TOTAL + MAX_FILE, "Package too large")
        members = {}
        for info in infos:
            safe_path(info.filename)
            mode = info.external_attr >> 16
            require(not info.is_dir() and stat.S_ISREG(mode), "Nonregular ZIP member")
            require(info.filename not in members, "Duplicate ZIP member")
            require(info.file_size <= MAX_FILE, "Oversize member")
            members[info.filename] = archive.read(info)
        return members


def verify(path, expected_manifest_sha256=None):
    members = read_package(path)
    require(MANIFEST in members, "Release manifest missing")
    raw = members.pop(MANIFEST)
    if expected_manifest_sha256:
        require(digest(raw) == expected_manifest_sha256, "Trusted manifest SHA differs")
    manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    require(isinstance(manifest, dict) and manifest.get("schema") == SCHEMA, "Unsupported schema")
    require(manifest.get("package") in PREFIXES, "Unknown package")
    require(isinstance(manifest.get("files"), list), "File list missing")
    require(raw == canonical(manifest), "Manifest is not canonical")
    expected = {}
    for row in manifest["files"]:
        require(isinstance(row, dict) and set(row) == {"path", "size", "sha256"}, "Invalid file record")
        name = row["path"]
        require(allowed(name, manifest["package"]), "Member outside package allowlist: " + str(name))
        require(name not in expected and isinstance(row["size"], int)
                and not isinstance(row["size"], bool) and row["size"] >= 0, "Invalid duplicate/size")
        expected[name] = row
    require(set(members) == set(expected), "Missing or unexpected package files")
    require(members, "Empty release")
    for name, content in members.items():
        check_content(name, content)
        require(len(content) == expected[name]["size"]
                and digest(content) == expected[name]["sha256"], "File SHA/size differs: " + name)
    revision = manifest.get("source_revision")
    require(revision is None or isinstance(revision, str)
            and re.fullmatch(r"[0-9a-f]{40,64}", revision), "Invalid source revision")
    calculated = make_manifest(members, manifest["package"], manifest.get("version", ""), revision)
    require(manifest == calculated, "Manifest fields, role hashes or release ID differ")
    return manifest


def stage(archive, destination, expected_manifest_sha256=None):
    manifest = verify(archive, expected_manifest_sha256)
    parent = no_symlinks(destination)
    require(parent.is_dir(), "Stage parent must already exist")
    final = parent / manifest["release_id"]
    lock = parent / (".stage-lock-" + manifest["release_id"])
    # Exclusive per-release gate serializes this tool, not other writable accounts.
    with lock.open("xb") as gate:
        gate.write(b"Staging in progress; inspect interrupted work before clearing this lock.\n")
        gate.flush()
        temporary = None
        try:
            if final.exists() or final.is_symlink():
                require(verify(final, expected_manifest_sha256) == manifest, "Existing stage differs")
                return final
            members = read_package(archive)
            require(digest(members[MANIFEST]) == digest(canonical(manifest)), "Archive changed during stage")
            for row in manifest["files"]:
                require(digest(members.get(row["path"], b"")) == row["sha256"], "Archive changed during stage")
            require(set(members) == {MANIFEST, *(row["path"] for row in manifest["files"])},
                    "Archive membership changed during stage")
            temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=parent))
            for name, raw in members.items():
                target = temporary.joinpath(*safe_path(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as out:
                    out.write(raw)
            require(verify(temporary, expected_manifest_sha256) == manifest, "Stage readback differs")
            require(not final.exists() and not final.is_symlink(), "Concurrent stage target appeared")
            temporary.rename(final)
        finally:
            if temporary is not None and temporary.exists():
                require(not temporary.is_symlink() and temporary.resolve().parent == parent.resolve(),
                        "Refusing cleanup outside owned staging parent")
                shutil.rmtree(temporary)
            gate.close()
            lock.unlink()
    return final


def summary(path, manifest):
    return {"status": "VERIFIED", "path": str(Path(path).absolute()),
            "release_id": manifest["release_id"], "package": manifest["package"],
            "version": manifest["version"], "source_revision": manifest["source_revision"],
            "file_count": len(manifest["files"]), "roles": manifest["roles"],
            "manifest_sha256": digest(canonical(manifest)),
            "activation_implemented": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build")
    create.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--package", choices=sorted(PREFIXES), default="source")
    create.add_argument("--version", default="0.1.0-alpha.1")
    for name in ("verify", "inspect", "stage"):
        command = commands.add_parser(name)
        command.add_argument("package", type=Path)
        command.add_argument("--expected-manifest-sha256")
        if name == "stage":
            command.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = build(args.root, args.output, args.package, args.version)
            print(json.dumps(summary(args.output, result), ensure_ascii=False))
        elif args.command == "stage":
            target = stage(args.package, args.destination, args.expected_manifest_sha256)
            print(json.dumps(summary(target, verify(target, args.expected_manifest_sha256)), ensure_ascii=False))
        else:
            result = verify(args.package, args.expected_manifest_sha256)
            print(json.dumps(result if args.command == "inspect" else summary(args.package, result),
                             ensure_ascii=False, indent=2))
    except (ReleaseError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        parser.exit(2, "Release refused: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
