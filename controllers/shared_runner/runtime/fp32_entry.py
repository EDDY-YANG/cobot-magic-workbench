"""Original FP32 controller/parameters; bounded streaming post-run audit only.

The original entry remains available untouched. This private web entry uses the
original Python and actual config, and only extends finished-file reading for
long runs. It never substitutes an inference or motion implementation.
"""
from pathlib import Path
import json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from runtime.legacy_support import verify_sources,require
reviewed=json.loads((ROOT/'bindings/reviewed-production.json').read_text())
canonical=Path(reviewed['canonical_root'])
cfg,_=verify_sources(canonical,ROOT/'receipts/live-source-snapshot.json')
require(Path(sys.executable).absolute()==Path(cfg['hardware_python']),'exact original FP32 interpreter required')
require(len(sys.argv)>1 and sys.argv[1] in ('h50-acceptance','deploy-h50'),'only original acceptance/deploy entry supported')
sys.path.insert(0,str(canonical))
from agilex_control.cli import main
from runtime.long_run_audit import audit_scope
with audit_scope():raise SystemExit(main(cfg,canonical))
