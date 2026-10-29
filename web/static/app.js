/* RakCRMP Mini App.
   Вход — подписанный initData от Telegram. Пароль остаётся запасным
   входом для обычного браузера: панель открывают и с компьютера. */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const TG = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

const S = {
  token: null,
  user: null,
  config: null,
  bots: [],
  vpn: { nodes: [], summary: {}, saved: [] },
  // Список узлов живёт отдельно от общего опроса: опрос каждые 4 секунды
  // приносит первую страницу, и раньше он стирал ту, на которую перелистнул.
  vpnList: { nodes: [], page: 1, pages: 1, total: 0 },
  vpnLoaded: false,
  filter: 'all',
  query: '',
  selectMode: false,
  picked: new Set(),
  vpnQuery: '',
  vpnUsable: false,
  vpnPage: 1,
  timer: null,
};

/* ---------- сеть ---------- */

async function api(path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (S.token) headers['X-Rak-Token'] = S.token;
  let res;
  try {
    res = await fetch('/api/' + path, {
      method: 'POST',
      headers,
      body: JSON.stringify(body || {}),
      credentials: 'same-origin',
    });
  } catch (e) {
    throw new Error('сеть недоступна');
  }
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  if (!res.ok) throw new Error(data.error || ('ошибка ' + res.status));
  return data;
}

async function getJSON(path) {
  const headers = {};
  if (S.token) headers['X-Rak-Token'] = S.token;
  const res = await fetch(path, { headers, credentials: 'same-origin' });
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  if (!res.ok) throw new Error(data.error || ('ошибка ' + res.status));
  return data;
}

/* ---------- тосты ---------- */

function toast(text, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = text;
  $('#toasts').appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s, transform .25s';
    el.style.opacity = '0';
    el.style.transform = 'translateY(-8px)';
    setTimeout(() => el.remove(), 260);
  }, 2600);
}

const fail = (e) => toast(e && e.message ? e.message : 'не вышло', 'bad');
const good = (t) => toast(t, 'ok');

/* ---------- модалка ---------- */

function closeSheet() {
  $('#sheet-wrap').hidden = true;
  $('#sheet').innerHTML = '';
}

function openSheet(html) {
  const sheet = $('#sheet');
  sheet.innerHTML = html;
  $('#sheet-wrap').hidden = false;
  return sheet;
}

function choose(title, options) {
  return new Promise((resolve) => {
    const sheet = openSheet(`<h3>${esc(title)}</h3>`);
    options.forEach((opt) => {
      const el = document.createElement('div');
      el.className = 'opt';
      el.innerHTML = `${esc(opt.label)}${opt.note ? `<small>${esc(opt.note)}</small>` : ''}`;
      el.onclick = () => { closeSheet(); resolve(opt.value); };
      sheet.appendChild(el);
    });
    const cancel = document.createElement('div');
    cancel.className = 'opt';
    cancel.style.textAlign = 'center';
    cancel.style.color = 'var(--ink-mute)';
    cancel.textContent = 'Отмена';
    cancel.onclick = () => { closeSheet(); resolve(null); };
    sheet.appendChild(cancel);
  });
}

function askForm(title, fields) {
  return new Promise((resolve) => {
    const sheet = openSheet(`<h3>${esc(title)}</h3><div class="form" id="ask-form"></div>`);
    const form = $('#ask-form', sheet);
    fields.forEach((f) => {
      const wrap = document.createElement('label');
      wrap.className = 'fld';
      wrap.innerHTML = `<span>${esc(f.label)}</span>
        <input id="ask-${f.name}" type="${f.type || 'text'}"
               value="${f.value != null ? esc(String(f.value)) : ''}"
               placeholder="${f.placeholder ? esc(f.placeholder) : ''}">`;
      form.appendChild(wrap);
    });
    const ok = document.createElement('button');
    ok.className = 'btn btn-solid btn-block';
    ok.textContent = 'Готово';
    ok.type = 'button';
    ok.onclick = () => {
      const out = {};
      fields.forEach((f) => { out[f.name] = $('#ask-' + f.name, sheet).value.trim(); });
      closeSheet();
      resolve(out);
    };
    form.appendChild(ok);
    const cancel = document.createElement('button');
    cancel.className = 'btn btn-block';
    cancel.textContent = 'Отмена';
    cancel.type = 'button';
    cancel.onclick = () => { closeSheet(); resolve(null); };
    form.appendChild(cancel);
    setTimeout(() => {
      const first = $('input', form);
      if (first && !TG) first.focus();
    }, 60);
  });
}

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ---------- вход ---------- */

async function boot() {
  if (TG) {
    TG.ready();
    TG.expand();
    try { TG.setHeaderColor('#08090b'); } catch (e) {}
  }

  const initData = TG && TG.initData ? TG.initData : '';

  if (initData) {
    $('#gate-sub').textContent = 'Проверяю вход через Telegram…';
    try {
      const r = await api('tg', { initData });
      S.token = r.token;
      S.user = r.user;
      return enter();
    } catch (e) {
      $('#gate-sub').textContent = 'Telegram не подтвердил вход';
      $('#gate-err').textContent = e.message;
      return showPassword();
    }
  }

  try {
    const s = await getJSON('/api/session');
    if (s.authed) return enter();
  } catch (e) {}

  $('#gate-sub').textContent = 'Вход в панель';
  showPassword();
}

function showPassword() {
  $('#login-form').hidden = false;
}

async function doPassword(ev) {
  ev.preventDefault();
  $('#gate-err').textContent = '';
  const btn = $('#in-submit');
  btn.disabled = true;
  try {
    await api('login', {
      login: $('#in-login').value,
      password: $('#in-pass').value,
    });
    return enter();
  } catch (e) {
    $('#gate-err').textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function enter() {
  $('#gate').hidden = true;
  $('#app').hidden = false;

  try {
    S.config = await getJSON('/api/config');
  } catch (e) {
    S.config = { servers: [], tasks: [], tg: false };
  }
  fillForms();
  renderWho();
  await refresh();
  startPolling();
}

function renderWho() {
  const u = S.user;
  const name = u ? (u.first_name || u.username || '') : (S.config && S.config.login) || '';
  $('#who').textContent = name;
  $('#m-login').textContent = (S.config && S.config.login) || '—';
  $('#m-tg').textContent = u ? (u.username ? '@' + u.username : String(u.id)) : 'пароль';
  $('#m-servers').textContent = String((S.config && S.config.servers || []).length);
}

/* ---------- формы ---------- */

function fillForms() {
  const servers = (S.config && S.config.servers) || [];
  const tasks = (S.config && S.config.tasks) || ['lvl', 'quest', 'mine', 'factory', 'bus', 'farm', 'stop'];
  const taskNames = {
    lvl: 'Прокачка', quest: 'Квесты', mine: 'Шахта', factory: 'Завод',
    bus: 'Автобус', farm: 'Ферма', stop: 'Без задачи',
  };

  const serverSel = $('#c-server');
  serverSel.innerHTML = servers.length
    ? servers.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join('')
    : '<option value="">нет списка серверов</option>';

  const taskOpts = tasks.map(
    (t) => `<option value="${esc(t)}">${esc(taskNames[t] || t)}</option>`
  ).join('');
  $('#c-task').innerHTML = taskOpts;
  $('#s-task').innerHTML = taskOpts;

  fillProxySelect();
}

function fillProxySelect() {
  // Опрос перерисовывает список каждые 4 секунды. Без сохранения выбора
  // форма создания теряла бы выбранный прокси на каждом тике.
  const keep = ($('#c-proxy') || {}).value || '';
  const saved = (S.vpn && S.vpn.saved) || [];
  const nodes = (S.vpnList && S.vpnList.nodes) || [];
  const opts = ['<option value="">Своё IP — без прокси</option>'];
  saved.forEach((p) => {
    opts.push(`<option value="${esc(p.proxy)}">${esc(p.name || p.proxy)}</option>`);
  });
  nodes.filter((n) => n.usable).forEach((n) => {
    opts.push(`<option value="node:${esc(n.key)}">${esc(n.name)}</option>`);
  });
  $('#c-proxy').innerHTML = opts.join('');
  if (keep) {
    const sel = $('#c-proxy');
    if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
  }
}

/* ---------- обновление ---------- */

async function refresh() {
  try {
    const st = await getJSON('/api/state');
    S.bots = st.bots || [];
    if (st.vpn && st.vpn.nodes) {
      S.vpn = st.vpn;
      if (!S.vpnLoaded) {
        S.vpnList = st.vpn;
        S.vpnLoaded = true;
      }
    }
    setLive(true, st.live);
    renderBots();
    renderStats();
    renderVpn();
  } catch (e) {
    setLive(false, false);
  }
}

function setLive(on, live) {
  const el = $('#live');
  el.classList.toggle('dead', !on);
  $('em', el).textContent = on ? (live ? 'живой' : 'снимок') : 'нет связи';
}

function startPolling() {
  if (S.timer) clearInterval(S.timer);
  S.timer = setInterval(() => {
    if (document.hidden) return;
    refresh();
  }, 4000);
}

/* ---------- отрисовка аккаунтов ---------- */

/* Статусы бота — русские строки: «В игре (заспавнен)», «Очередь #3»,
   «Подключение…», «Остановлен», «ЗАБАНЕН». Проверяем по подстрокам,
   а не по точному равенству: строку с таймером целиком не сравнить. */
function statusKind(bot) {
  if (bot.banned) return 'bad';
  const s = String(bot.status || '').toLowerCase();
  if (s.includes('бан')) return 'bad';
  if (s.includes('в игре') || s.includes('игр')) return 'work';
  if (s.includes('очеред') || s.includes('подключ') || s.includes('ожидан')) return 'online';
  if (s.includes('ошибк') || s.includes('прокси не')) return 'bad';
  return '';
}

function isRunning(bot) {
  const s = String(bot.status || '').toLowerCase();
  if (!s) return false;
  return !(s.includes('остановлен') || s.includes('отключен') || s.includes('бан'));
}

function visibleBots() {
  const q = S.query.toLowerCase();
  return S.bots.filter((b) => {
    if (S.filter === 'online' && !isRunning(b)) return false;
    if (S.filter === 'offline' && isRunning(b)) return false;
    if (S.filter === 'banned' && !b.banned) return false;
    if (!q) return true;
    return String(b.nickname || '').toLowerCase().includes(q)
        || String(b.server || '').toLowerCase().includes(q);
  });
}

function renderStats() {
  const all = S.bots.length;
  const online = S.bots.filter((b) => isRunning(b)).length;
  const banned = S.bots.filter((b) => b.banned).length;
  $('#stat-row').innerHTML = `
    <div class="stat"><b>${all}</b><span>всего</span></div>
    <div class="stat"><b>${online}</b><span>в игре</span></div>
    <div class="stat"><b>${banned}</b><span>бан</span></div>`;
}

function renderBots() {
  const list = $('#bots');
  const bots = visibleBots();
  $('#bots-empty').hidden = bots.length > 0;
  list.innerHTML = '';

  bots.forEach((bot) => {
    const kind = statusKind(bot);
    const picked = S.picked.has(bot.id);
    const el = document.createElement('div');
    el.className = 'bot' + (picked ? ' picked' : '');

    const pos = Array.isArray(bot.position)
      ? bot.position.map((v) => Number(v).toFixed(1)).join(', ')
      : '';
    const lvl = bot.lvl || {};
    const line = [lvl.station, lvl.phase, lvl.report].filter(Boolean).join(' · ') || pos;

    el.innerHTML = `
      <div class="bot-top">
        <span class="dot ${kind}"></span>
        <span class="bot-name">${esc(bot.nickname || '—')}</span>
        <span class="bot-id">#${bot.id}</span>
        <span class="bot-srv">${esc(bot.server || '')}</span>
      </div>
      <div class="bot-meta">
        <span>ур. <b>${bot.level_num != null ? esc(bot.level_num) : '—'}</b>${
          bot.exp_percent != null ? ` · ${esc(bot.exp_percent)}%` : ''}</span>
        <span><b>${fmtMoney(bot.money)}</b></span>
        <span>${esc(bot.task || '—')}</span>
      </div>
      ${line ? `<div class="bot-line">${esc(line)}</div>` : ''}
      <div class="bot-acts">
        <button class="btn" data-act="toggle">${isRunning(bot) ? '■ Стоп' : '▶ Старт'}</button>
        <button class="btn" data-act="task">Задача</button>
        <button class="btn" data-act="world">В мир</button>
        <button class="btn" data-act="more">⋯</button>
      </div>`;

    if (S.selectMode) {
      el.onclick = (ev) => {
        if (ev.target.closest('button')) return;
        togglePick(bot.id);
      };
    }
    $$('button', el).forEach((btn) => {
      btn.onclick = (ev) => {
        ev.stopPropagation();
        if (S.selectMode && btn.dataset.act !== 'more') {
          togglePick(bot.id);
          return;
        }
        onBotAction(bot, btn.dataset.act);
      };
    });

    list.appendChild(el);
  });
}

function fmtMoney(v) {
  const n = Number(v || 0);
  if (!n) return '0';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(Math.round(n));
}

/* ---------- действия ---------- */

async function onBotAction(bot, act) {
  try {
    if (act === 'toggle') {
      const running = isRunning(bot);
      await api(running ? 'stop' : 'start', { id: bot.id });
      good(running ? 'Остановлен' : 'Запущен');
      return refresh();
    }
    if (act === 'task') {
      const tasks = (S.config && S.config.tasks) || [];
      const names = {
        lvl: 'Прокачка', quest: 'Квесты', mine: 'Шахта', factory: 'Завод',
        bus: 'Автобус', farm: 'Ферма', stop: 'Без задачи',
      };
      const t = await choose('Задача для ' + (bot.nickname || '#' + bot.id),
        tasks.map((v) => ({ label: names[v] || v, value: v })));
      if (!t) return;
      await api('task', { id: bot.id, task: t });
      good('Задача: ' + (names[t] || t));
      return refresh();
    }
    if (act === 'world') {
      await api('real_world', { id: bot.id });
      good('Отправляю в мир');
      return refresh();
    }
    if (act === 'more') {
      const pick = await choose('Ещё — ' + (bot.nickname || '#' + bot.id), [
        { label: 'Перезапустить', value: 'restart' },
        { label: 'Сменить прокси', value: 'proxy' },
        { label: 'Цель по уровню', value: 'goal' },
        { label: 'Телепорт по координатам', value: 'goto' },
        { label: 'Снять бан', value: 'unban' },
        { label: 'Удалить аккаунт', value: 'delete' },
      ]);
      if (!pick) return;
      return onBotAction(bot, pick);
    }
    if (act === 'restart') {
      await api('restart', { id: bot.id });
      good('Перезапущен');
      return refresh();
    }
    if (act === 'proxy') {
      const saved = S.vpn.saved || [];
      const opts = [{ label: 'Своё IP — без прокси', value: 'none' }];
      saved.forEach((p) => opts.push({ label: p.name || p.proxy, value: p.proxy }));
      (S.vpn.nodes || []).filter((n) => n.usable).forEach(
        (n) => opts.push({ label: n.name, note: n.address, value: 'node:' + n.key }));
      const v = await choose('Прокси', opts);
      if (!v) return;
      if (v.startsWith('node:')) {
        await api('proxy_bot', { id: bot.id, node: v.slice(5) });
      } else {
        await api('proxy_bot', { id: bot.id, proxy: v });
      }
      good('Прокси применён');
      return refresh();
    }
    if (act === 'goal') {
      const f = await askForm('Цель по уровню', [
        { name: 'level', label: 'Уровень, 0 — снять', type: 'number', value: 0 },
      ]);
      if (!f) return;
      await api('goal_many', { ids: [bot.id], level: Number(f.level) });
      good('Цель задана');
      return refresh();
    }
    if (act === 'goto') {
      const f = await askForm('Телепорт', [
        { name: 'x', label: 'X', type: 'number' },
        { name: 'y', label: 'Y', type: 'number' },
        { name: 'z', label: 'Z', type: 'number' },
      ]);
      if (!f) return;
      await api('goto', { id: bot.id, x: Number(f.x), y: Number(f.y), z: Number(f.z) });
      good('Телепорт отправлен');
      return refresh();
    }
    if (act === 'unban') {
      await api('unban', { id: bot.id });
      good('Разбан отправлен');
      return refresh();
    }
    if (act === 'delete') {
      const sure = await choose('Удалить ' + (bot.nickname || '#' + bot.id) + '?', [
        { label: 'Да, удалить', value: true },
      ]);
      if (!sure) return;
      await api('delete', { id: bot.id });
      S.picked.delete(bot.id);
      good('Удалён');
      return refresh();
    }
  } catch (e) {
    fail(e);
  }
}

/* ---------- выделение и массовые ---------- */

function togglePick(id) {
  if (S.picked.has(id)) S.picked.delete(id);
  else S.picked.add(id);
  renderBots();
  renderBulk();
}

function renderBulk() {
  const bar = $('#bulkbar');
  const n = S.picked.size;
  bar.hidden = !(S.selectMode && n > 0);
  $('#bulk-count').textContent = 'Выбрано: ' + n;
}

async function bulk(action) {
  const ids = [...S.picked];
  if (!ids.length) return toast('Никого не выбрала', 'bad');
  try {
    if (action === 'goal_many') {
      const f = await askForm('Цель по уровню', [
        { name: 'level', label: 'Уровень, 0 — снять', type: 'number', value: 0 },
      ]);
      if (!f) return;
      await api('goal_many', { ids, level: Number(f.level) });
    } else if (action === 'delete_many') {
      const sure = await choose(`Удалить ${ids.length} аккаунтов?`, [
        { label: 'Да, удалить', value: true },
      ]);
      if (!sure) return;
      await api('delete_many', { ids });
      S.picked.clear();
    } else {
      await api(action, { ids });
    }
    good('Готово');
    renderBulk();
    refresh();
  } catch (e) {
    fail(e);
  }
}

/* ---------- прокси ---------- */

function pingText(n) {
  // Пинг показываем только там, где он что-то значит. У мёртвого и у
  // забаненного узла любое число — это время локального сокета либо
  // остаток от прошлой проверки. Врать «1 мс» хуже, чем прочерк.
  const st = String(n.status || '');
  if (st === 'banned' || st === 'dead') return '';
  if (!n.alive && !n.usable) return '';
  if (n.latency == null || n.latency <= 0) return '';
  return n.latency + ' мс';
}

/* Что значит каждый статус — прямо в интерфейсе, без догадок:
     рабочий       бот может через него ходить прямо сейчас
     нужен клиент  протокол (vless/vmess/trojan) бот сам не умеет, нужен мост
     не пускает    узел отвечает, но трафик через него не проходит
     мёртвый       не отвечает вообще
     забанен       помечен вручную, бот его не берёт */
function nodeStatus(n) {
  const st = String(n.status || '');
  if (n.usable || st === 'ok') return { dot: 'online', text: 'рабочий' };
  if (st === 'banned') return { dot: 'bad', text: 'забанен' };
  if (st === 'needs_client') return { dot: 'work', text: 'нужен клиент' };
  if (st === 'no_udp') return { dot: 'work', text: 'не пускает' };
  if (st === 'no_game') return { dot: 'work', text: 'игра не отвечает' };
  if (n.alive) return { dot: 'work', text: 'не пускает' };
  return { dot: 'bad', text: 'мёртвый' };
}

function renderVpn() {
  const sum = (S.vpn && S.vpn.summary) || {};
  const list = S.vpnList || {};
  const saved = (S.vpn && S.vpn.saved) || [];
  $('#vpn-stats').innerHTML = `
    <div class="stat"><b>${sum.total != null ? sum.total : (list.total || 0)}</b><span>узлов</span></div>
    <div class="stat"><b>${sum.usable != null ? sum.usable : 0}</b><span>рабочих</span></div>
    <div class="stat"><b>${saved.length}</b><span>своих</span></div>`;

  const box = $('#nodes');
  box.innerHTML = '';
  const nodes = list.nodes || [];
  if (!nodes.length) {
    box.innerHTML = '<div class="empty">Узлов нет. Добавь подписку.</div>';
  }
  nodes.forEach((n) => {
    const st = nodeStatus(n);
    const el = document.createElement('div');
    el.className = 'node';
    el.innerHTML = `
      <span class="dot ${st.dot}"></span>
      <div class="node-main">
        <div class="node-name">${esc(n.name)}</div>
        <div class="node-sub">${esc(n.protocol || '')} · ${esc(n.address || '')} · ${st.text}</div>
      </div>
      <span class="node-lat">${pingText(n)}</span>
      ${n.usable ? '<button class="icon" data-act="use">✓</button>' : ''}`;
    $$('button', el).forEach((b) => {
      b.onclick = async () => {
        try {
          await api('vpn_use', { key: n.key });
          good('Узел применён');
        } catch (e) { fail(e); }
      };
    });
    box.appendChild(el);
  });

  const pages = list.pages || 1;
  const page = list.page || 1;
  const pager = $('#vpn-pager');
  pager.innerHTML = '';
  if (pages > 1) {
    pager.innerHTML = `<button id="pv">←</button><span>${page} / ${pages}</span><button id="nx">→</button>`;
    const go = (target) => {
      const next = Math.max(1, Math.min(pages, target));
      if (next === page) return;
      S.vpnPage = next;
      loadVpn().then(() => {
        const top = $('#nodes');
        if (top) top.scrollIntoView({ block: 'start', behavior: 'smooth' });
      });
    };
    $('#pv', pager).onclick = () => go(page - 1);
    $('#nx', pager).onclick = () => go(page + 1);
  }
  fillProxySelect();
}

async function loadVpn() {
  try {
    const r = await api('vpn_status', {
      page: S.vpnPage,
      per_page: 24,
      search: S.vpnQuery,
      only_usable: S.vpnUsable,
    });
    const res = r.result || {};
    if (res.nodes) {
      S.vpnList = res;
      S.vpnLoaded = true;
    }
    renderVpn();
  } catch (e) {
    fail(e);
  }
}

/* ---------- создание ---------- */

async function onCreate(ev) {
  ev.preventDefault();
  const server = $('#c-server').value;
  if (!server) return toast('Нет сервера', 'bad');
  const count = Math.max(1, Math.min(50, Number($('#c-count').value) || 1));
  const proxy = $('#c-proxy').value;
  const body = {
    server,
    count,
    password: $('#c-pass').value.trim(),
    task: $('#c-task').value,
  };
  if (proxy.startsWith('node:')) body.node = proxy.slice(5);
  else if (proxy) body.proxy = proxy;

  const btn = $('#c-go');
  btn.disabled = true;
  btn.textContent = 'Создаю…';
  try {
    if (count > 1) await api('add_many', { ...body, count });
    else await api('add', { ...body, nickname: $('#c-nick').value.trim() });
    good(count > 1 ? `Создаю ${count}` : 'Аккаунт создан');
    $('#c-nick').value = '';
    refresh();
  } catch (e) {
    fail(e);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Создать';
  }
}

/* ---------- настройки ---------- */

async function onSettings(ev) {
  ev.preventDefault();
  try {
    const r = await api('settings', {
      base_password: $('#s-pass').value.trim(),
      default_task: $('#s-task').value,
    });
    const res = r.result || {};
    if (res.base_password) $('#s-pass').value = res.base_password;
    good('Сохранено');
  } catch (e) {
    fail(e);
  }
}

async function loadSettings() {
  try {
    const r = await api('settings', {});
    const res = r.result || {};
    if (res.base_password) $('#s-pass').value = res.base_password;
    if (res.default_task) $('#s-task').value = res.default_task;
  } catch (e) {}
}

/* ---------- навигация ---------- */

function showView(name) {
  $$('.view').forEach((v) => { v.hidden = v.dataset.view !== name; });
  $$('.dock-btn').forEach((b) => b.classList.toggle('on', b.dataset.view === name));
  if (name === 'proxy') loadVpn();
  if (name === 'more') loadSettings();
}

/* ---------- привязки ---------- */

function wire() {
  $('#login-form').onsubmit = doPassword;

  $$('.dock-btn').forEach((b) => {
    b.onclick = () => showView(b.dataset.view);
  });

  $('#btn-refresh').onclick = () => { refresh(); toast('Обновлено'); };

  $('#q').oninput = (e) => { S.query = e.target.value; renderBots(); };

  $$('#filters .chip').forEach((c) => {
    c.onclick = () => {
      S.filter = c.dataset.filter;
      $$('#filters .chip').forEach((x) => x.classList.toggle('on', x === c));
      renderBots();
    };
  });

  $('#btn-select').onclick = () => {
    S.selectMode = !S.selectMode;
    if (!S.selectMode) S.picked.clear();
    $('#btn-select').classList.toggle('on', S.selectMode);
    $('#btn-select').textContent = S.selectMode ? 'Готово' : 'Выбрать';
    renderBots();
    renderBulk();
  };

  $('#bulk-x').onclick = () => {
    S.picked.clear();
    renderBots();
    renderBulk();
  };

  $$('[data-bulk]').forEach((b) => {
    b.onclick = () => bulk(b.dataset.bulk);
  });

  $('#vpn-q').oninput = (e) => { S.vpnQuery = e.target.value; S.vpnPage = 1; };
  $('#vpn-q').onchange = () => loadVpn();
  $('#vpn-refresh').onclick = async () => {
    // Проверка идёт в фоне: подъём моста под каждый узел занимает минуты.
    // Поэтому не ждём ответа с готовым списком, а подтягиваем его пару раз.
    const btn = $('#vpn-refresh');
    btn.disabled = true;
    try {
      await api('vpn_refresh', {});
      good('Проверяю узлы — это займёт минуту');
      loadVpn();
      setTimeout(() => { loadVpn(); refresh(); }, 12000);
      setTimeout(() => { loadVpn(); refresh(); btn.disabled = false; }, 35000);
    } catch (e) {
      btn.disabled = false;
      fail(e);
    }
  };
  $('#vpn-usable').onclick = (e) => {
    S.vpnUsable = !S.vpnUsable;
    e.target.classList.toggle('on', S.vpnUsable);
    S.vpnPage = 1;
    loadVpn();
  };
  $('#vpn-sub').onclick = async () => {
    const f = await askForm('Новая подписка', [
      { name: 'name', label: 'Название', value: 'подписка' },
      { name: 'url', label: 'Ссылка на подписку', placeholder: 'https://…' },
    ]);
    if (!f || !f.url) return;
    try {
      await api('vpn_sub_add', { name: f.name, url: f.url });
      good('Подписка добавлена');
      loadVpn();
    } catch (e) { fail(e); }
  };

  $('#create-form').onsubmit = onCreate;
  $('#settings-form').onsubmit = onSettings;

  $('#btn-logout').onclick = async () => {
    try { await api('logout', {}); } catch (e) {}
    location.reload();
  };

  $('#sheet-wrap').onclick = (e) => {
    if (e.target.id === 'sheet-wrap') closeSheet();
  };

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh();
  });
}

wire();
boot();
