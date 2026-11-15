// app.js — 前端只负责画与转发；所有状态都来自 Python 侧
const $ = (s) => document.querySelector(s);
const api = () => window.pywebview?.api;

const HIST = 240;                  // 折线保留点数（2Hz × 120 秒）
const hist = { t: [], l: [], r: [] };
const shown = { t: 0, l: 0, r: 0, w: 0, ms: 0 };
const target = { t: 0, l: 0, r: 0, w: 0, ms: 0 };
const BUDGET = 12.5;               // 服务端写入上限（k5_min_iv = 0.08s）

let muted = false, canvas, ctx, dpr = 1;

// ---------- 画布 ----------
function sizeCanvas() {
  canvas = canvas || $('#wave');
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
  $('#v-t').textContent = Math.round(shown.t);
  $('#v-l').textContent = Math.round(shown.l);
  $('#v-r').textContent = Math.round(shown.r);
  $('#v-w').textContent = shown.w.toFixed(1);
  $('#v-ms').textContent = shown.ms.toFixed(2);
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
    hist.t.push(target.t); hist.l.push(target.l); hist.r.push(target.r);
    for (const k of ['t', 'l', 'r']) if (hist[k].length > HIST) hist[k].shift();

    $('#v-f').textContent = s.fails ?? 0;
    setLink(s.link_state, s.link_text || '未知');

    const tm = $('#trig-mode');
    tm.textContent = s.trig_mode || '常态';
    tm.className = 'hint' + (s.trig_mode === '冲击' ? ' hot' : '');
    if (s.trig_mode === '冲击') hit($('#v-t'));

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
function bindSliders() {
  document.querySelectorAll('.slider').forEach((box) => {
    const key = box.dataset.key;
    const input = box.querySelector('input');
    const val = box.querySelector('.val');
    let timer = null;
    input.addEventListener('input', () => {
      val.textContent = input.value;
      clearTimeout(timer);
      timer = setTimeout(() => api()?.set_cfg(key, Number(input.value)), 150);
    });
  });
}

function bindSwitch() {
  document.querySelectorAll('.switch').forEach((box) => {
    const key = box.dataset.key;
    const tg = box.querySelector('.tg');
    tg.addEventListener('click', async () => {
      const next = tg.getAttribute('aria-pressed') !== 'true';
      tg.setAttribute('aria-pressed', String(next));
      await api()?.set_cfg(key, next ? 1 : 0);
      flash(next ? '阻尼层已开启：手指按在行程内可感到顶手' : '阻尼层已关闭');
    });
  });
}

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

const hitTimers = new WeakMap();
function hit(el) {
  el.classList.add('hit');
  clearTimeout(hitTimers.get(el));
  hitTimers.set(el, setTimeout(() => el.classList.remove('hit'), 200));
}

let flashTimer = null;
function flash(msg) {
  const n = $('#note');
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
  sizeCanvas();
  const cfg = await api()?.cfg();
  if (cfg) {
    document.querySelectorAll('.slider').forEach((box) => {
      const k = box.dataset.key;
      if (cfg[k] === undefined) return;
      const input = box.querySelector('input');
      input.value = cfg[k];
      box.querySelector('.val').textContent = cfg[k];
    });
    const tg = document.querySelector('.switch[data-key=tf_enable] .tg');
    if (tg) tg.setAttribute('aria-pressed', String(Number(cfg.tf_enable) === 1));
  }
  bindSliders(); bindSwitch(); bindActions();
  setInterval(poll, 500);
  poll();
  requestAnimationFrame(frame);
});

window.addEventListener('resize', sizeCanvas);
document.addEventListener('contextmenu', (e) => e.preventDefault());
