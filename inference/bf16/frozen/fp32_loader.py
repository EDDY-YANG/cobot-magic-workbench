"""Isolated FP32 Pi0.5 inference. No robot, camera, CAN or training code.

Keep pinned checkpoint storage unchanged. Floating parameters must already be
FP32; integer model state stays integer. GPU forward IR is audited before use.
"""
from pathlib import Path
import argparse, collections, datetime, hashlib, json, os, re, socket, subprocess, sys, time

JOB = Path('/opt/cobot-workbench/lab-packages/2026-10-19-15-38-Pi05-FP32-Deployment')
ARCHIVE = Path('/data/4090-1tb/RobotData/02_Models/2026-10-18_Pi05_FP32_FullFineTune/Complete_Archive_Package')
EXPECTED_PARAMETERS = 3353433872
COMMIT = '215abfb217dbac7d5f1273282331b9b1866c0479'
INPUT_MANIFEST_SHA = '1fa6ae01906c22f7ad0a5bacb8540b03eec5bdf3c2712ca14ee2366e8b9826e9'

def now(): return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(8*1024**2),b''): h.update(block)
    return h.hexdigest()
def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.writing-'+str(os.getpid()))
    with tmp.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
def event(status,**extra): print(json.dumps({'at':now(),'status':status,**extra},ensure_ascii=True),flush=True)

def forbidden_hlo_types(text):
    # The x in tensor<1x50x32xbf16> is a shape separator, not an identifier.
    return re.findall(r'(?:(?<![A-Za-z0-9_])|(?<=x))(bf16|f16|f64)(?![A-Za-z0-9_])',text)

def precision_is_highest(value):
    return (isinstance(value,(tuple,list)) and len(value)==2
            and all(getattr(x,'name',None)=='HIGHEST' for x in value))

def guard():
    if socket.gethostname()!='user-MS-7E06': raise RuntimeError('Wrong host')
    m=json.loads(subprocess.check_output(['findmnt','-J','-T',str(JOB),'-o','TARGET,UUID'],text=True))['filesystems'][0]
    if m['target']!='/data/4090-1tb' or m['uuid']!='e76043b6-4ab6-42a5-8b06-4963ee92a567': raise RuntimeError('Wrong target filesystem')
    locked=JOB/'source/uv.lock'
    if sha(locked)!='793488b5a55bb87200db90a61fd0af51922b686d94e1da4f4c587ab119b37d74':raise RuntimeError('Wrong frozen source lock')

def bootstrap():
    guard()
    # Set these before importing either JAX or any module which imports JAX.
    os.environ['JAX_DEFAULT_MATMUL_PRECISION']='highest'
    os.environ['NVIDIA_TF32_OVERRIDE']='0'
    os.environ['XLA_PYTHON_CLIENT_PREALLOCATE']='false'
    os.environ['JAX_PLATFORMS']='cuda'
    os.environ['JAX_ENABLE_X64']='false'
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    sys.path.insert(0,str(JOB/'source/packages/openpi-client/src'))
    sys.path.insert(0,str(JOB/'source/src'))
    import jax
    jax.config.update('jax_default_matmul_precision','highest')
    if jax.default_backend()!='gpu':raise RuntimeError('GPU backend required; no CPU fallback')
    return jax

class FP32Policy:
    def __init__(self,step=2000,num_steps=10,seed=42):
        if step not in (2000,5000,10000,15000,20000): raise ValueError('Unknown candidate')
        if num_steps!=10:raise ValueError('This acceptance contract freezes sampling steps at 10')
        self.jax=bootstrap()
        import numpy as np
        import flax.nnx as nnx
        import jax.numpy as jnp
        import sentencepiece
        from openpi import transforms as t
        from openpi.models import model as model_api,pi0_config,tokenizer
        from openpi.shared import normalize
        self.np=np;self.jnp=jnp;self.model_api=model_api;self.nnx=nnx
        self.step=step;self.num_steps=num_steps;self.seed=seed
        self.checkpoint=(ARCHIVE/'models'/('update-'+str(step).zfill(8))).resolve(strict=True)
        self.checkpoint.relative_to(ARCHIVE.resolve(strict=True))
        self.manifest=json.loads((ARCHIVE/'download-manifest.json').read_text(encoding='utf-8'))
        expected={row['relative']:row for row in self.manifest['files']}
        for name in ('candidate.json','assets/tiger_piper_right_strict95/norm_stats.json'):
            p=self.checkpoint/name;rel=str(p.relative_to(ARCHIVE.resolve(strict=True)))
            if sha(p)!=expected[rel]['sha256']:raise RuntimeError('Checkpoint metadata/norm mismatch')
        candidate=json.loads((self.checkpoint/'candidate.json').read_text())
        if candidate['completed_updates']!=step or candidate['parameter_dtype']!='float32':raise RuntimeError('FP32 candidate identity mismatch')
        self.norm_path=self.checkpoint/'assets/tiger_piper_right_strict95/norm_stats.json'
        self.norm=normalize.load(self.norm_path.parent)
        for key in ('state','actions'):
            if self.norm[key].mean.shape!=(7,) or self.norm[key].q01.shape!=(7,) or self.norm[key].q99.shape!=(7,): raise RuntimeError('Wrong physical normalization dimensions')
        token_path=ARCHIVE/'reading-support/assets/big_vision/paligemma_tokenizer.model'
        support=json.loads((ARCHIVE/'SUPPORT-READY.json').read_text())
        token_row=next(r for r in support['files'] if r['path'].endswith('paligemma_tokenizer.model'))
        if sha(token_path)!=token_row['sha256']:raise RuntimeError('Tokenizer identity mismatch')
        # Reuse the exact upstream tokenize method, with the archived local model.
        class LocalTokenizer(tokenizer.PaligemmaTokenizer):
            def __init__(self,path):
                self._max_len=200
                self._tokenizer=sentencepiece.SentencePieceProcessor(model_proto=path.read_bytes())
        self.transforms=t.compose([t.Normalize(self.norm,use_quantiles=True),t.ResizeImages(224,224),
                                  t.TokenizePrompt(LocalTokenizer(token_path),discrete_state_input=True),t.PadStatesAndActions(32)])
        self.unnormalize=t.Unnormalize(self.norm,use_quantiles=True)
        self.config=pi0_config.Pi0Config(pi05=True,dtype='float32',action_horizon=50,action_dim=32,max_token_len=200)
        start=time.perf_counter();event('RESTORING_ORIGINAL_FP32',checkpoint=str(self.checkpoint))
        # Preserve int counters and reject reduced-precision floating leaves.
        # This deliberately bypasses create_trained_policy's BF16 conversion.
        params=model_api.restore_params(self.checkpoint/'params',dtype=None)
        before=self.parameter_summary(params)
        if before['floating_elements']!=EXPECTED_PARAMETERS:raise RuntimeError('Wrong restored FP32 parameter count before model merge')
        model=self.config.load(params,remove_extra_params=False)
        del params
        self.graph,self.state=nnx.split(model)
        after=self.parameter_summary(nnx.state(model,nnx.Param).to_pure_dict())
        del model
        self.load_seconds=time.perf_counter()-start
        if after['floating_elements']!=EXPECTED_PARAMETERS:raise RuntimeError('Wrong restored trainable parameter count')
        self.param_before=before;self.param_after=after
        graph=self.graph
        def forward(state,rng,batch):
            model=nnx.merge(graph,state)
            # Construct the typed dataclass from actual tracers inside forward.
            # Passing it as an outer argument makes JAX reconstruct it with
            # ArgInfo metadata, which the pinned runtime checker correctly rejects.
            obs=model_api.Observation.from_dict(batch)
            return model.sample_actions(rng,obs,num_steps=num_steps)
        self.forward=forward;self.jit=self.jax.jit(forward)
        self.rng=self.jax.random.key(seed)
        self.compiled=None;self.audit=None
        self.metadata={'schema':'tiger-pi05-fp32-local-v1','checkpoint_step':step,'checkpoint':str(self.checkpoint),
                       'precision':'float32','model_compute_dtype':'float32','matmul_precision':'highest',
                       'tf32_override':'0','horizon':50,'internal_action_dim':32,'physical_action_dim':7,
                       'units':['rad']*6+['m'],'action_basis':'absolute','sampling_steps':10,
                       'seed':seed,'batch_size':1,'robot_commands':0,'source_commit':COMMIT,
                       'normalization_sha256':sha(self.norm_path),'tokenizer_sha256':sha(token_path)}
        event('MODEL_RESTORED_FP32',load_seconds=self.load_seconds,parameters=after)

    def parameter_summary(self,tree):
        counts=collections.Counter();elements=collections.Counter()
        for x in self.jax.tree.leaves(tree):
            if not hasattr(x,'dtype'):continue
            dtype=str(x.dtype);counts[dtype]+=1;elements[dtype]+=int(x.size)
            if ('float' in dtype or dtype in ('f16','bf16')) and dtype!='float32':raise RuntimeError('Non-FP32 floating parameter: '+dtype)
        return {'leaf_dtypes':dict(counts),'elements_by_dtype':dict(elements),'floating_elements':elements['float32']}

    def prepare(self,data):
        np=self.np
        if set(data)!={'state','front','right','prompt'}:raise ValueError('Exactly state/front/right/prompt inputs are required')
        state=np.asarray(data['state'])
        if state.shape!=(7,) or state.dtype!=np.float32 or not np.isfinite(state).all():raise ValueError('State must be finite float32[7], six rad and one m')
        for k in ('front','right'):
            a=data[k]
            if not isinstance(a,np.ndarray) or a.dtype!=np.uint8 or a.ndim!=3 or a.shape[-1]!=3 or min(a.shape[:2])<32:raise ValueError('Real RGB uint8 HWC image required: '+k)
        if not isinstance(data['prompt'],str) or not data['prompt'].strip():raise ValueError('Real task prompt is required')
        inputs={'state':state.copy(),'prompt':data['prompt'],
                'image':{'base_0_rgb':data['front'],'right_wrist_0_rgb':data['right'],
                         'left_wrist_0_rgb':np.zeros_like(data['front'])},
                'image_mask':{'base_0_rgb':np.True_,'right_wrist_0_rgb':np.True_,'left_wrist_0_rgb':np.False_}}
        # The zero left-wrist array declares an absent sensor, attention mask False.
        inputs=self.transforms(inputs)
        batch=self.jax.tree.map(lambda x:self.jnp.asarray(x)[None,...],inputs)
        return batch

    def audit_and_compile(self,data,receipt_dir):
        batch=self.prepare(data);key=self.jax.random.key(self.seed)
        start=time.perf_counter();closed=self.jax.make_jaxpr(self.forward)(self.state,key,batch)
        dtypes=collections.Counter();primitives=collections.Counter();precisions=collections.Counter();seen=set()
        def variable(v):
            dt=getattr(getattr(v,'aval',None),'dtype',None)
            if dt is not None:dtypes[str(dt)]+=1
        def walk(value):
            if isinstance(value,dict):
                for x in value.values():walk(x)
            elif isinstance(value,(list,tuple)):
                for x in value:walk(x)
            elif hasattr(value,'jaxpr'):walk(value.jaxpr)
            elif hasattr(value,'eqns') and id(value) not in seen:
                seen.add(id(value))
                for v in list(value.invars)+list(value.outvars)+list(value.constvars):variable(v)
                for eq in value.eqns:
                    name=eq.primitive.name;primitives[name]+=1
                    for v in list(eq.invars)+list(eq.outvars):variable(v)
                    if name in ('dot_general','conv_general_dilated'):
                        raw_precision=eq.params.get('precision');precision=repr(raw_precision);precisions[precision]+=1
                        if not precision_is_highest(raw_precision):raise RuntimeError('Dot/conv not explicitly highest precision: '+precision)
                    walk(eq.params)
        walk(closed)
        bad={k:v for k,v in dtypes.items() if ('float' in k or k in ('f16','bf16')) and k!='float32'}
        if bad:raise RuntimeError('Forward JAXPR contains non-FP32 floating dtype: '+str(bad))
        lowered=self.jit.lower(self.state,key,batch)
        hlo=str(lowered.compiler_ir(dialect='stablehlo'))
        if forbidden_hlo_types(hlo):raise RuntimeError('StableHLO contains reduced/other floating precision')
        self.audit={'status':'FP32_FORWARD_IR_VERIFIED','at':now(),'jaxpr_dtype_counts':dict(dtypes),
                    'dot_conv_precision_counts':dict(precisions),'primitives':dict(primitives),
                    'stablehlo_sha256':hashlib.sha256(hlo.encode()).hexdigest(),'stablehlo_bytes':len(hlo.encode()),
                    'fp16_bf16_nodes':0,'source_commit':COMMIT,'trace_lower_seconds':time.perf_counter()-start,
                    'note':'IR/config validation, not a hardware instruction trace'}
        write_json(Path(receipt_dir)/'precision-audit.json',self.audit)
        event('FP32_IR_VERIFIED',dtype_counts=dict(dtypes),trace_seconds=self.audit['trace_lower_seconds'])
        start=time.perf_counter();self.compiled=lowered.compile();self.compile_seconds=time.perf_counter()-start
        self.metadata['precision_audit_passed']=True
        event('FP32_COMPILE_COMPLETE',seconds=self.compile_seconds)

    def infer(self,data):
        if self.compiled is None:raise RuntimeError('FP32 graph must be audited and compiled first')
        start=time.perf_counter();batch=self.prepare(data)
        self.rng,key=self.jax.random.split(self.rng)
        t0=time.perf_counter();raw=self.compiled(self.state,key,batch)
        raw.block_until_ready();device_ms=(time.perf_counter()-t0)*1000
        if str(raw.dtype)!='float32' or raw.shape!=(1,50,32):raise RuntimeError('Unexpected raw model dtype/shape')
        raw_host=self.np.asarray(raw[0]);state=self.np.asarray(batch['state'][0])
        output=self.unnormalize({'actions':raw_host,'state':state})
        postprocess_dtype=str(output['actions'].dtype)
        actions=self.np.asarray(output['actions'][:,:7],dtype=self.np.float32)
        if not self.np.isfinite(actions).all():raise RuntimeError('Model produced non-finite physical actions; not clipped')
        return {'actions':actions,'timing':{'device_synchronized_ms':device_ms,'total_host_output_ms':(time.perf_counter()-start)*1000},
                'raw_dtype':str(raw.dtype),'raw_shape':list(raw.shape),'cpu_unnormalize_dtype':postprocess_dtype,
                'physical_wire_dtype':str(actions.dtype),'action_basis':'absolute_rad_m','robot_commands':0}

def real_samples(limit=5):
    import numpy as np
    setup=json.loads((JOB/'receipts/2026-10-19-15-38-setup.json').read_text())
    root=Path(setup['input_root']);manifest=Path(setup['input_manifest'])
    if sha(manifest)!=INPUT_MANIFEST_SHA:raise RuntimeError('Historical input manifest changed')
    data=json.loads(manifest.read_text(encoding='utf-8'))
    # Deterministic, interleaved samples from existing train/validation episodes.
    groups=[[r for r in data['samples'] if r['ordinal']==ep] for ep in (2,7)]
    selected=[]
    for i in range(max(map(len,groups))):
        for g in groups:
            if i<len(g):selected.append(g[i])
    result=[]
    for row in selected[:limit]:
        p=(root/row['input_npz_path']).resolve(strict=True);p.relative_to(root.resolve())
        if sha(p)!=row['input_npz_sha256']:raise RuntimeError('Real input NPZ changed')
        with np.load(p,allow_pickle=False) as z:
            if set(z.files)!={'state','front','right','prompt'}:raise RuntimeError('Unexpected historical input fields')
            item={k:z[k].copy() for k in z.files};item['prompt']=str(item['prompt'].item())
        if item['prompt']!=data['prompt']:raise RuntimeError('Historical prompt changed')
        result.append(({'sample_id':row['sample_id'],'ordinal':row['ordinal'],'split':row['split'],'sha256':row['input_npz_sha256']},item))
    return result

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--step',type=int,default=2000)
    parser.add_argument('--mode',choices=('validate','serve'),default='validate')
    parser.add_argument('--port',type=int,default=18882)
    args=parser.parse_args()
    guard()
    import fcntl
    lock=(JOB/'FP32_GPU_OWNER.lock').open('a+')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    rows=subprocess.check_output(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits'],text=True).strip()
    if rows:raise RuntimeError('Another GPU compute owner is active; preserve it: '+rows)
    run_id=datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')+'-'+args.mode+'-'+str(os.getpid())
    receipts=JOB/'receipts'/run_id;receipts.mkdir()
    write_json(JOB/'receipts/current-run.json',{'status':'STARTING','at':now(),'run_id':run_id,'pid':os.getpid(),'receipts':str(receipts),'mode':args.mode,'step':args.step})
    try:
        start=time.perf_counter();policy=FP32Policy(args.step)
        restored={'status':'PARAMETERS_LOADED_FP32','at':now(),'run_id':run_id,'pid':os.getpid(),
                  'metadata':policy.metadata,'parameter_summary':policy.param_after,'load_seconds':policy.load_seconds,
                  'model_compute_not_yet_validated':True,'robot_commands':0}
        write_json(receipts/'parameter-restoration.json',restored)
        write_json(JOB/'receipts/current-run.json',{**restored,'receipts':str(receipts),'mode':args.mode,'step':args.step})
        samples=real_samples(5)
        policy.audit_and_compile(samples[0][1],receipts)
        rows=[]
        for cycle in range(3):
            for meta,data in samples:
                result=policy.infer(data)
                out=JOB/'outputs'/(run_id+'-'+str(cycle)+'-'+meta['sample_id']+'.npy')
                policy.np.save(out,result['actions'],allow_pickle=False)
                rows.append({'cycle':cycle,**meta,**{k:v for k,v in result.items() if k!='actions'},
                             'actions_file':str(out),'actions_sha256':sha(out),
                             'actions_min':result['actions'].min(axis=0).tolist(),'actions_max':result['actions'].max(axis=0).tolist()})
                event('REAL_INPUT_INFERRED',cycle=cycle,sample=meta['sample_id'],timing=result['timing'])
        warm=policy.np.array([r['timing']['total_host_output_ms'] for r in rows[1:]])
        report={'status':'FP32_LOAD_AND_REAL_INPUT_INFERENCE_VERIFIED','at':now(),'run_id':run_id,'pid':os.getpid(),
                'metadata':policy.metadata,'load_seconds':policy.load_seconds,'compile_seconds':policy.compile_seconds,
                'startup_to_validation_seconds':time.perf_counter()-start,'parameter_summary':policy.param_after,
                'precision_audit':policy.audit,'real_inputs':5,'inferences':len(rows),'rows':rows,
                'warm_host_ms':{'n':len(warm),'p50':float(policy.np.percentile(warm,50)),'p95':float(policy.np.percentile(warm,95)),
                                'min':float(warm.min()),'max':float(warm.max())},
                'jax_allocator_memory':policy.jax.devices()[0].memory_stats(),'robot_commands':0,
                'candidate_selection':'2000 is a technical acceptance candidate, not production selection',
                'limitations':'15 inference calls on five historical inputs; no robot performance, no sustained scheduling guarantee'}
        write_json(receipts/'validation.json',report)
        write_json(JOB/'receipts/current-run.json',{'status':report['status'],'at':now(),'run_id':run_id,'pid':os.getpid(),'receipts':str(receipts),'mode':args.mode,'step':args.step})
        if args.mode=='serve':
            sys.path.insert(0,str(Path(__file__).resolve().parent))
            from fp32_http import serve
            serve(policy,args.port,ready_file=receipts/'http-ready.json')
        else:event('VALIDATION_COMPLETE',warm_host_ms=report['warm_host_ms'],receipt=str(receipts/'validation.json'))
    except BaseException as exc:
        import traceback
        failure={'status':'FAILED_NO_PRECISION_FALLBACK','at':now(),'run_id':run_id,'pid':os.getpid(),'error':repr(exc),'traceback':traceback.format_exc(),'robot_commands':0}
        write_json(receipts/'failure.json',failure);write_json(JOB/'receipts/current-run.json',failure)
        raise

if __name__=='__main__':main()
