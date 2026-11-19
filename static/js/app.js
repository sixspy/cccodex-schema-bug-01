const msg = document.getElementById('msg');
const view = document.getElementById('view');
let contracts = [];
let lastCheck = null;

async function api(path, opts={}) {
  const r = await fetch(path, {
    headers: {'Content-Type': 'application/json', ...(opts.headers||{})},
    ...opts
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}

function setMsg(t, isErr=false) {
  msg.textContent = t || '';
  msg.className = isErr ? 'err' : 'muted';
}

async function loadContracts() {
  const data = await api('/api/contracts');
  contracts = data.items || [];
}

function healthChip(health) {
  const bad = health !== '针齐';
  return `<span class="chip ${bad ? 'warn' : ''}">${health}</span>`;
}

function renderContracts() {
  view.innerHTML = `<div class="grid">${contracts.map(c => `
    <article class="card">
      <div class="row">
        <h3>${c.table_name} · v${c.version_no}</h3>
        <span class="chip">${c.status}</span>
      </div>
      <p class="muted">${c.note || ''} · 字段 ${c.field_count}</p>
    </article>`).join('')}</div>`;
}

function draftOptions() {
  return contracts.filter(c => c.status === '草稿')
    .map(c => `<option value="${c.id}">${c.table_name} v${c.version_no}</option>`).join('');
}

function renderCheck() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-check" type="button">运行破坏性检查</button>
      <pre id="out" class="muted"></pre>
    </div>`;
  document.getElementById('btn-check').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/breaking-check`, {method:'POST', body:'{}'});
      lastCheck = j.check;
      document.getElementById('out').textContent = JSON.stringify(j.check, null, 2);
      setMsg(`检查结论：${j.check.verdict}（现行读针 v${j.check.from_version}，与升版同源）`);
    } catch (e) { setMsg(e.message, true); }
  };
}

function renderApply() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-apply" type="button">换版应用</button>
      <p class="muted">升版读针与检查读针同根：脏针 / 缺针会被白话拦下，双现行写库即被唯一索引拒绝。</p>
    </div>`;
  document.getElementById('btn-apply').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/apply`, {method:'POST', body:'{}'});
      setMsg(`已应用 ${j.contract.table_name} v${j.contract.version_no}，读针已随事务切换`);
      await loadContracts();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function renderPointers() {
  const [p, runs] = await Promise.all([
    api('/api/pointers'), api('/api/repair-runs')
  ]);
  const items = p.items || [];
  view.innerHTML = `
    <div class="pointer-grid">
      ${items.map(it => `
        <article class="card">
          <div class="row">
            <h3>${it.table_name}</h3>
            ${healthChip(it.health)}
          </div>
          <p class="muted">${it.detail}</p>
          <div class="versions">
            读针版号：<b>v${it.pointer_version ?? '—'}</b><br/>
            现行契约：v${it.contract_version ?? '—'}　目录入库：v${it.catalog_version ?? '—'}
          </div>
          <div class="row" style="margin-top:0.6rem">
            <button class="primary" data-repair="${it.table_name}" type="button">值班纠偏</button>
          </div>
        </article>`).join('') || '<p class="muted">暂无工程</p>'}
    </div>
    <h3 style="margin-top:1.2rem">纠偏编排记录（入档）</h3>
    <table class="runs">
      <thead><tr><th>时间</th><th>工程</th><th>旧针</th><th>新针</th><th>原因</th><th>结果</th></tr></thead>
      <tbody>${(runs.items || []).map(r => `
        <tr><td>${r.created_at}</td><td>${r.table_name}</td>
        <td>v${r.old_version ?? '—'}</td><td>v${r.new_version}</td>
        <td>${r.reason}</td><td>${r.result}</td></tr>`).join('')
        || '<tr><td colspan="6" class="muted">暂无纠偏记录</td></tr>'}
      </tbody>
    </table>`;
  view.querySelectorAll('[data-repair]').forEach(btn => {
    btn.onclick = async () => {
      try {
        const j = await api('/api/pointers/repair', {
          method: 'POST', body: JSON.stringify({table_name: btn.dataset.repair})
        });
        setMsg(`纠偏完成：${j.pointer.table_name} 读针切换至 v${j.pointer.pointer_version}（${j.pointer.health}）`);
        await renderPointers();
      } catch (e) { setMsg(e.message, true); }
    };
  });
}

async function renderReconcile() {
  const reports = (await api('/api/reconcile-reports')).items || [];
  const pointers = (await api('/api/pointers')).items || [];
  const bad = pointers.filter(p => p.health !== '针齐');
  const watermark = bad.length ? bad[0].health : '针齐';
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <button class="primary" id="btn-rec" type="button">跑一轮对账</button>
        <span class="watermark">对账水线：${healthChip(watermark)}（与指针卡片同一标记）</span>
      </div>
      <div class="grid" id="reps">${reports.map(r => `
        <article class="card"><strong>${r.as_of}</strong>
        ${r.pointer_health ? `<p>水线【${r.pointer_health}】</p>` : ''}
        <p>${r.summary}</p></article>`).join('') || '<p class="muted">暂无对账</p>'}</div>
    </div>`;
  document.getElementById('btn-rec').onclick = async () => {
    try {
      const j = await api('/api/reconcile-reports', {
        method: 'POST', body: JSON.stringify({as_of: '2026-11-18'})
      });
      setMsg(j.report.summary);
      await renderReconcile();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function show(tab) {
  setMsg('');
  await loadContracts();
  if (tab === 'contracts') renderContracts();
  if (tab === 'check') renderCheck();
  if (tab === 'apply') renderApply();
  if (tab === 'pointers') await renderPointers();
  if (tab === 'reconcile') await renderReconcile();
}

document.querySelectorAll('.pills button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.pills button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    show(btn.dataset.tab);
  };
});
document.getElementById('btn-logout').onclick = async () => {
  await api('/api/logout', {method: 'POST', body:'{}'});
  location.href = '/login';
};
show('contracts');
