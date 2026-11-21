// app.js — 前端只负责画与转发；所有状态都来自 Python 侧
const $ = (s) => document.querySelector(s);
const api = () => window.pywebview?.api;

const HIST = 240;                  // 折线保留点数（4Hz × 60 秒）
const hist = { t: [], l: [], r: [] };
const shown = { t: 0, l: 0, r: 0, w: 0, ms: 0 };
const target = { t: 0, l: 0, r: 0, w: 0, ms: 0 };
const BUDGET = 12.5;               // 服务端写入上限（k5_min_iv = 0.08s）

let muted = false, canvas, ctx, dpr = 1;

// ---------- 参数表：界面由数据生成，加一个旋钮只改这里 ----------
const SPEC = {
  motor: [
    { k: 'threshold', label: '噪声门限', min: 0, max: 20, step: 0.5, dec: 1,
      desc: '低于它的声音不算震动' },
    { k: 'strength', label: '震动强度', min: 0, max: 200, step: 1,
      desc: '整体比例，100 = 原始' },
    { k: 'boost_x', label: '转子增益', min: 0.5, max: 4, step: 0.1, dec: 1,
      desc: 'k5 是偏心转子，比音圈更需要过驱才起转' },
    { k: 'boost_add', label: '起转地板', min: 0, max: 200, step: 1,
      desc: '非零电平直接抬到这里，补转子的启动死区（旧两级增益用）' },
    { k: 'k5_rotor_gain', type: 'switch', label: '合并增益',
      desc: '开=单级转子曲线（D1，推荐）；关=旧两级增益（回滚）' },
    { k: 'k5_floor', label: '曲线地板', min: 0, max: 254, step: 1,
      desc: '合并曲线的起转高度，低于它转子不转' },
    { k: 'k5_gamma', label: '曲线指数', min: 0.1, max: 2, step: 0.05, dec: 2,
      desc: '<1 中段更响，>1 中段更轻' },
    { k: 'k5_knee', label: '软限幅点', min: 50, max: 254, step: 1,
      desc: '超过它 tanh 压缩渐近 255，不再硬裁' },
    { k: 'hold_ms', label: '节拍保持', min: 0, max: 400, step: 10,
      desc: '鼓点间隙不归零，避免一跳一跳' },
    { k: 'pcm_gain', label: '音频增益', min: 100, max: 200, step: 1,
      desc: '软膝曲线的斜率' },
  ],
  throttle: [
    { k: 'k5_min_iv', label: '写入间隔', min: 0.05, max: 0.2, step: 0.005, ms: true,
      desc: '越小越跟手，但接收器每次写会卡内核约 20ms' },
    { k: 'k5_hard_iv', label: '最小硬间隔', min: 0.02, max: 0.08, step: 0.002, ms: true,
      desc: '两帧之间的绝对下限，防背靠背把驱动写挂' },
    { k: 'k5_jump_bypass', label: '抢发跳变', min: 0, max: 255, step: 5,
      desc: '电平跳变超过它时立刻发一帧（打击起手），欠账随后偿还（旧两级增益用）' },
    { k: 'k5_jump_bypass_merged', label: '抢发跳变(合并)', min: 10, max: 255, step: 5,
      desc: '合并曲线的起手电平更低，阈值相应下调（默认 40）' },
  ],
  impact: [
    { k: 'tr_p2_max', label: '冲击强度', min: 60, max: 255, step: 1 },
    { k: 'tr_head_ms', label: '起振时长', min: 5, max: 80, step: 1,
      desc: '越短越"点"，越长越"闷"' },
    { k: 'tr_lockout_ms', label: '连打抑制', min: 20, max: 250, step: 5,
      desc: '这段时间内不再起新的头' },
    { k: 'tr_body_hold_ms', label: '余震保持', min: 0, max: 400, step: 10 },
    { k: 'tr_release_ms', label: '释放时间', min: 60, max: 500, step: 10,
      desc: '越大越"黏"' },
    { k: 'tr_p3_min', label: '频率下限', min: 20, max: 120, step: 5 },
    { k: 'tr_p3_max', label: '频率上限', min: 20, max: 120, step: 5 },
    { k: 'tr_head_floor', label: '起振门限', min: 0, max: 120, step: 5,
      desc: '低于它不判为打击' },
    { k: 'tr_enable', type: 'switch', label: '冲击层',
      desc: '关闭后扳机只剩阻尼' },
  ],
  damp: [
    { k: 'tf_enable', type: 'switch', label: '阻尼层',
      desc: '手指按在行程内才摸得到' },
    { k: 'tf_max', label: '峰值力度', min: 20, max: 255, step: 1 },
    { k: 'tf_base', label: '基线力度', min: 1, max: 60, step: 1,
      desc: '静默时的常态阻力，1 = 最轻' },
    { k: 'tf_attack_ms', label: '上升时间', min: 1, max: 80, step: 1 },
    { k: 'tf_release_ms', label: '下降时间', min: 20, max: 600, step: 10,
      desc: '比上升慢才有"惯性"' },
  ],
  legacy: [
    { k: 'trig_burst', type: 'switch', label: '旧爆发',
      desc: '固定时长的 mode 2，冲击层会接班' },
    { k: 'trig_threshold', label: '触发门限', min: 0, max: 80, step: 1 },
    { k: 'trig_burst_ms', label: '爆发时长', min: 60, max: 400, step: 10 },
  ],
};

// ---------- 画布 ----------
function sizeCanvas() {
  canvas = canvas || $('#wave');
  if (!canvas) return;
  dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function drawGrid(w, h) {
  const top = 6, bottom = h - 6, span = bottom - top;
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = Math.round(bottom - (span * i) / 4) + 0.5;
    ctx.strokeStyle = i === 0 ? 'rgba(255,255,255,.09)' : 'rgba(255,255,255,.055)';
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
}

function curve(key, color) {
  const a = hist[key];
  if (a.length < 2) return;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const step = w / (HIST - 1);
  const x0 = w - (a.length - 1) * step;
  ctx.beginPath();
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.lineJoin = 'round';
  for (let i = 0; i < a.length; i++) {
    const y = h - 6 - (a[i] / 255) * (h - 12);
    const x = x0 + i * step;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
}

function draw() {
  if (!ctx) return;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  ctx.clearRect(0, 0, w, h);
  drawGrid(w, h);
  const peak = hist.t.length ? Math.max(...hist.t, ...hist.l, ...hist.r) : 0;
  if (peak <= 0) {
    ctx.fillStyle = 'rgba(255,255,255,.18)';
    ctx.font = '11px "Segoe UI","Microsoft YaHei UI",system-ui';
    ctx.textAlign = 'center';
    ctx.fillText('等待信号 · 播放任意声音', w / 2, h / 2 - 14);
    ctx.textAlign = 'left';
    return;
  }
  curve('l', 'rgba(125,140,248,.85)');
  curve('r', 'rgba(90,209,200,.85)');
  curve('t', 'rgba(232,232,236,.95)');
}

// ---------- 数字补间 ----------
function tween(dt) {
  const k = 1 - Math.pow(0.0015, dt);
  for (const key of ['t', 'l', 'r', 'w', 'ms']) {
    shown[key] += (target[key] - shown[key]) * k;
    if (Math.abs(target[key] - shown[key]) < .05) shown[key] = target[key];
  }
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('#v-t', Math.round(shown.t));
  set('#v-l', Math.round(shown.l));
  set('#v-r', Math.round(shown.r));
  set('#v-w', shown.w.toFixed(1));
  set('#v-ms', shown.ms.toFixed(2));
}

let lastT = performance.now();
function frame(now) {
  // 位图尺寸必须跟着 CSS 尺寸走：首帧时布局可能还没完成，之后窗口缩放也会变
  if (canvas && (canvas.width !== Math.round(canvas.clientWidth * dpr) ||
                 canvas.height !== Math.round(canvas.clientHeight * dpr))) {
    sizeCanvas();
  }
  tween(Math.min(.1, (now - lastT) / 1000));
  draw();
  lastT = now;
  requestAnimationFrame(frame);
}

// ---------- 参数面板 ----------
function fmt(spec, v) {
  if (spec.ms) return Math.round(Number(v) * 1000);
  if (spec.dec) return Number(v).toFixed(spec.dec);
  return Math.round(Number(v));
}

function buildParam(spec) {
  const box = document.createElement('div');
  if (spec.type === 'switch') {
    box.className = 'switch';
    box.dataset.key = spec.k;
    box.innerHTML =
      '<div><label>' + spec.label + '</label>' +
      (spec.desc ? '<span class="sub">' + spec.desc + '</span>' : '') + '</div>' +
      '<button class="tg" aria-pressed="false"><i></i></button>';
    return box;
  }
  box.className = 'slider';
  box.dataset.key = spec.k;
  box.innerHTML =
    '<label>' + spec.label + '<span class="val">—</span>' +
    (spec.ms ? '<span class="ru"> ms</span>' : '') + '</label>' +
    '<input type="range" min="' + spec.min + '" max="' + spec.max +
    '" step="' + spec.step + '" value="' + spec.min + '">' +
    (spec.desc ? '<span class="desc">' + spec.desc + '</span>' : '');
  return box;
}

function buildAll() {
  const map = { motor: '#params-motor', throttle: '#params-throttle',
                impact: '#params-impact', damp: '#params-damp',
                legacy: '#params-legacy' };
  for (const [group, sel] of Object.entries(map)) {
    const host = $(sel);
    if (!host) continue;
    host.textContent = '';
    for (const spec of SPEC[group]) host.appendChild(buildParam(spec));
  }
}

function bindParams() {
  document.querySelectorAll('.params .slider').forEach((box) => {
    const spec = findSpec(box.dataset.key);
    const input = box.querySelector('input');
    const val = box.querySelector('.val');
    let timer = null;
    const paint = () => { val.textContent = fmt(spec, input.value); };
    paint();
    input.addEventListener('input', () => {
      paint();
      clearTimeout(timer);
      // 拖的时候只更新数字，停手 150ms 才写配置：一次拖动只落一版
      timer = setTimeout(() => api()?.set_cfg(spec.k, Number(input.value)), 150);
    });
  });

  document.querySelectorAll('.params .switch').forEach((box) => {
    const spec = findSpec(box.dataset.key);
    const tg = box.querySelector('.tg');
    tg.addEventListener('click', async () => {
      const next = tg.getAttribute('aria-pressed') !== 'true';
      tg.setAttribute('aria-pressed', String(next));
      await api()?.set_cfg(spec.k, next ? 1 : 0);
      flash(spec.label + (next ? ' 已开启' : ' 已关闭'));
    });
  });
}

function findSpec(key) {
  for (const list of Object.values(SPEC)) {
    for (const s of list) if (s.k === key) return s;
  }
  return { k: key, dec: 0 };
}

function applyCfg(cfg) {
  for (const list of Object.values(SPEC)) {
    for (const spec of list) {
      if (cfg[spec.k] === undefined) continue;
      const box = document.querySelector('.params [data-key="' + spec.k + '"]')
               || document.querySelector('[data-key="' + spec.k + '"]');
      if (!box) continue;
      const v = Number(cfg[spec.k]);
      if (spec.type === 'switch') {
        box.querySelector('.tg').setAttribute('aria-pressed', String(v === 1));
      } else {
        const input = box.querySelector('input');
        input.value = String(v);
        box.querySelector('.val').textContent = fmt(spec, v);
      }
    }
  }
}

// ---------- 状态 ----------
function setLink(state, text) {
  $('#link').className = 'pill ' + (state || '');
  $('#link-text').textContent = text;
}

async function poll() {
  if (!api()) return;
  try {
    const s = await api().state();
    if (!s) return;
    target.t = s.channels?.[0] || 0;
    target.l = s.channels?.[1] || 0;
    target.r = s.channels?.[2] || 0;
    target.w = s.writes_per_s || 0;
    target.ms = s.avg_ms || 0;
    // 只有探针真的出了新样本才入图：探针 10 次/秒、界面 4 次/秒轮询，
    // 不去重会把同一帧记成好几个点，曲线看着像"卡住不动"。
    const seq = s.seq ?? 0;
    if (seq !== lastSeq) {
      lastSeq = seq;
      hist.t.push(target.t); hist.l.push(target.l); hist.r.push(target.r);
      for (const k of ['t', 'l', 'r']) if (hist[k].length > HIST) hist[k].shift();
    }

    $('#v-f').textContent = s.fails ?? 0;
    setLink(s.link_state, s.link_text || '未知');

    const tm = $('#trig-mode');
    tm.textContent = s.trig_mode || '常态';
    tm.className = 'hint' + (s.trig_mode === '冲击' ? ' hot' : '');
    if (s.trig_mode === '冲击') hit($('#v-t'));
    const tstate = $('#trig-state');
    if (tstate) tstate.textContent = s.trig_mode || '—';

    $('#st-line').textContent = s.status_line || '';
    $('#k-pid').textContent = s.engine_pid || '未运行';
    $('#k-link').textContent = s.handshake || '—';

    const rate = s.writes_per_s || 0;
    $('#k-w').textContent = rate.toFixed(1);
    const fill = $('#m-fill');
    fill.style.width = Math.min(100, (rate / BUDGET) * 100) + '%';
    fill.classList.toggle('hi', rate > BUDGET * 0.9);

    if (s.log) {
      const el = $('#log');
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 26;
      el.textContent = s.log.join('\n');
      if (atBottom) el.scrollTop = el.scrollHeight;
    }
    applyMute(s.muted);
  } catch (e) {
    setLink('bad', '界面失联');
  }
}

function applyMute(on) {
  muted = !!on;
  $('#btn-mute').classList.toggle('armed', muted);
  $('#mute-label').textContent = muted ? '恢复震动' : '暂停震动';
}

// ---------- 交互 ----------
function bindActions() {
  document.querySelectorAll('[data-act]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const act = btn.dataset.act, label = btn.textContent;
      btn.disabled = true;
      if (act === 'recycle') btn.textContent = '重连中';
      if (act === 'reload') btn.textContent = '重载中';
      try {
        const r = await api()?.action(act);
        flash(r?.message || '完成');
      } catch (e) {
        flash('操作失败');
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    });
  });

  $('#btn-mute').addEventListener('click', async () => {
    const next = !muted;
    applyMute(next);
    const r = await api()?.set_mute(next);
    flash(r?.message || (next ? '已暂停' : '已恢复'));
  });

  $('#btn-clear').addEventListener('click', () => { $('#log').textContent = ''; });
}

function bindTabs() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      const view = tab.dataset.view;
      document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t === tab));
      document.querySelectorAll('.view').forEach((v) => {
        v.classList.toggle('on', v.dataset.view === view);
      });
      // 画布在隐藏视图里尺寸是 0，切回来必须重新量一次
      if (view === 'monitor') { sizeCanvas(); }
    });
  });
}

const hitTimers = new WeakMap();
function hit(el) {
  el.classList.add('hit');
  clearTimeout(hitTimers.get(el));
  hitTimers.set(el, setTimeout(() => el.classList.remove('hit'), 200));
}

let flashTimer = null;
let lastSeq = -1;
function flash(msg) {
  const n = $('#note');
  if (!n) return;
  n.textContent = msg;
  n.style.color = 'var(--text)';
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => {
    n.textContent = '参数热加载，约 1 秒生效。';
    n.style.color = '';
  }, 2800);
}

// ---------- 启动 ----------
window.addEventListener('pywebviewready', async () => {
  buildAll();
  bindParams();
  bindActions();
  bindTabs();
  sizeCanvas();
  const cfg = await api()?.cfg();
  if (cfg) applyCfg(cfg);
  setInterval(poll, 250);
  poll();
  requestAnimationFrame(frame);
});

window.addEventListener('resize', () => { sizeCanvas(); });
document.addEventListener('contextmenu', (e) => e.preventDefault());
