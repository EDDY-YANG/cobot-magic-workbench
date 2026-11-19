"""Reproducibly extract reviewed pure lifecycle helpers, never run a legacy main."""
from pathlib import Path
import ast,hashlib,json
ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT.parent/'2026-10-21-00-05-离线传输与停止时序优化/hardware_adapter'
NAMES=('require','file_sha','verify_sources','import_canonical','current_loaded_model',
       'owned_model_scope','watch_stdin_eof','loaded_runtime_factory','stable_anchor','math_finite',
       'bounded_counts','close_publisher_after_audit','completed_window_status','result_exit_code',
       'admission_summary','identity_from_proc_fields','process_identity')
def main():
    source=(SOURCE/'experiment_runner.py').read_text(encoding='utf8')
    tree=ast.parse(source);nodes={n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    header='''"""Reviewed pure helpers extracted from prior NO_SEND runner; no I/O on import."""
from contextlib import ExitStack,contextmanager
from copy import deepcopy
import hashlib,importlib,json,os,socket,sys,threading,time
from pathlib import Path
OWNER_NAMES=("h50-deployment","fp32-shadow-run","writer-can_right")
def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf8')).hexdigest()
'''
    pieces=[]
    for name in NAMES:
        node=nodes[name];piece=ast.get_source_segment(source,node)
        if node.decorator_list:piece='\n'.join('@'+ast.get_source_segment(source,d) for d in node.decorator_list)+'\n'+piece
        pieces.append(piece)
    (ROOT/'runtime/legacy_support.py').write_text(header+'\n\n'+'\n\n\n'.join(pieces)+'\n',encoding='utf8')
    frozen=ROOT/'runtime/frozen_legacy';frozen.mkdir(exist_ok=True)
    (ROOT/'runtime/__init__.py').write_text('',encoding='utf8')
    (frozen/'__init__.py').write_text('',encoding='utf8')
    (frozen/'live_bridge.py').write_bytes((SOURCE/'live_bridge.py').read_bytes())
    manifest={'schema':'reviewed-nosend-helper-extraction-v1','source_directory':str(SOURCE),
      'files':{n:hashlib.sha256((SOURCE/n).read_bytes()).hexdigest() for n in ('experiment_runner.py','live_bridge.py')},
      'extracted_functions':NAMES,'legacy_main_executed':False,'planner_client_or_service_reused':False}
    (ROOT/'runtime/legacy-source-binding.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
if __name__=='__main__':main()
