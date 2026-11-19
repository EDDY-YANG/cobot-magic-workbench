"""Portable source loader. SHA check precedes every frozen source import."""
from pathlib import Path
import ast
import hashlib
import importlib.util
import json
import math
import sys
import numpy as np

ROOT = Path(__file__).resolve().parent

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def checked(name):
    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf8'))
    path = ROOT / name
    if sha(path) != manifest['sha256'][name]:
        raise RuntimeError('Frozen source changed: ' + name)
    return path

def verify_all():
    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf8'))
    for name in manifest['sha256']:
        checked(name)
    return manifest

def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def official_temporal():
    return load_file('v2_portable_official_temporal', checked('frozen/optimizer.py')).TimeParameterizationMPC

def original_rtc_validator():
    return load_file('v2_portable_rtc_schema', checked('frozen/frozen_rtc_core.py'))

def command_limiter(bounds):
    return load_file('v2_portable_command_codec', checked('frozen/command_limits.py')).CommandLimiter(bounds)

def official_queue_functions():
    path = checked('frozen/executor.py')
    tree = ast.parse(path.read_text(encoding='utf8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'OnDeviceMpcExecutor')
    names = ('_updated_future_queue', '_predict_steps_with_history')
    nodes = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    progress = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ProgressManager')
    namespace = {'np': np, 'math': math}
    exec(compile(ast.Module(body=[progress, *nodes], type_ignores=[]), str(path), 'exec'), namespace)
    return {name: namespace[name] for name in (*names, 'ProgressManager')}
