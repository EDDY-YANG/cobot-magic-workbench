const el = id => document.getElementById('dg-' + id);
let session = null;
let busy = false;
let lastPhase = null;
function message(value) { el('error').textContent = value; el('error').hidden = !value; }
export function displayDagger(state) {
  session = state;
  if (!state) {
    for (const id of ['enter','finish']) el(id).disabled = true;
    el('phase').textContent = '状态未更新';
    el('policy').textContent = '待核对';
    el('record').textContent = '待核对';
    el('time').textContent = '—';
    for (const side of ['left','right']) for (const key of ['q','g','s']) el(side+'-'+key).textContent = '—';
    el('note').textContent = '等待读取 4090 工作台状态。';
    return;
  }
  if (lastPhase !== state.phase) {message('');lastPhase=state.phase;}
  for (const key of ['phase','policy','record','note']) el(key).textContent = state[key];
  el('time').textContent = state.elapsed_s == null ? '—' : state.elapsed_s.toFixed(1)+' s';
  for (const side of ['left','right']) {
    const arm = state.arms[side];
    el(side+'-q').textContent = arm.q == null ? '—' : arm.q.toFixed(2)+'°';
    el(side+'-g').textContent = arm.g == null ? '—' : arm.g.toFixed(1)+' mm';
    el(side+'-s').textContent = arm.status;
  }
  el('enter').disabled = busy || !state.can_enter;
  el('finish').disabled = busy || !state.can_finish;
}
export function bindDagger(api) {
  for (const [id,action] of [['enter','dagger_intervene'],['finish','dagger_finish']]) {
    el(id).addEventListener('click', async () => {
      if (busy || !session) return;
      const binding=session.binding;
      busy=true;displayDagger(session);message('');
      try {
        const reply=await api('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,params:{binding}})});
        displayDagger(reply.job);
      } catch(error) {message(error.message);}
      finally {busy=false;displayDagger(session);}
    });
  }
}
