/* RakCRMP — логика панели. Без сборки и без зависимостей. */

const $ = (sel) => document.querySelector(sel);

const SERVERS = [
  "ARKHANGELSK", "ASTRAKHAN", "BATYREVO", "BELGOROD", "BRATSK", "BRYANSK",
  "CHELYABINSK", "CHITA", "EKATERINBURG", "IRKUTSK", "IZHEVSK", "KALUGA",
  "KAZAN", "KEMEROVO", "KHABAROVSK", "KIROV", "KOSTROMA", "KRASNODAR",
  "KRASNOYARSK", "KURGAN", "KURSK", "LIPETSK", "MAGNITOGORSK", "MOSCOW",
  "MURMANSK", "NABEREZHNYE", "NIZHNY", "NORILSK", "NOVGOROD", "NOVOSIBIRSK",
  "OMSK", "OREL", "ORENBURG", "PENZA", "PERM", "PETROZAVODSK", "PSKOV",
  "ROSTOV", "RYAZAN", "SAMARA", "SARATOV", "SMOLENSK", "SOCHI",
  "STAVROPOL", "SURGUT", "TAMBOV", "TOMSK", "TULA", "TVER", "TYUMEN",
  "UFA", "ULYANOVSK", "VLADIVOSTOK", "VLADIMIR", "VOLGOGRAD", "VOLOGDA",
  "VORONEZH", "YAKUTSK", "YAROSLAVL", "YOSHKAR", "ZABAYKALSK",
];

const TASKS = {
  lvl: "Прокачка", quest: "Квесты", mine: "Шахта",
  factory: "Завод", bus: "Автобус", stop: "Без задачи",
};

/* ---------- состояние ---------- */

let state = { bots: [], vpn: {}, live: false, vpnPage: 1,
              pickBulk: "", pickCreate: "spread", picker: null };
let filter = "";
let pollTimer = null;
let busy = new Set();
// Выделенные аккаунты. Живут между опросами: панель перерисовывается
// каждые 4 секунды, и сбрасывать галочки на каждой перерисовке нельзя.
let selected = new Set();

/* ---------- сеть ---------- */

async function api(path, opts = {}) {
  const res = await fetch("/api/" + path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = {};
  try { data = await res.json(); } catch { data = {}; }
  if (res.status === 401) {
    showLogin();
    throw new Error("нужен вход");
  }
  if (!res.ok) throw new Error(data.error || `ошибка ${res.status}`);
  return data;
}

const post = (path, body) =>
  api(path, { method: "POST", body: JSON.stringify(body || {}) });

/* ---------- тосты ---------- */

function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity .3s";
    setTimeout(() => el.remove(), 320);
  }, 3600);
}

/* ---------- вход ---------- */

function showLogin() {
  stopPolling();
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#login-name").focus();
}

function showApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  startPolling();
  loadState();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#login-btn");
  const err = $("#login-err");
  err.textContent = "";
  btn.disabled = true;
  btn.textContent = "Проверяю…";
  try {
    await post("login", {
      login: $("#login-name").value.trim(),
      password: $("#login-pass").value,
    });
    $("#login-pass").value = "";
    showApp();
  } catch (exc) {
    err.textContent = exc.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Войти";
  }
});

$("#logout").addEventListener("click", async () => {
  try { await post("logout"); } catch { /* всё равно уходим */ }
  showLogin();
});

/* ---------- опрос ---------- */

function startPolling() {
  stopPolling();
  // Живо, но не в лоб: 4 секунды — боту хватает, сети тоже.
  pollTimer = setInterval(() => {
    if (document.visibilityState === "visible") loadState(true);
  }, 4000);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !$("#app").classList.contains("hidden")) {
    loadState(true);
  }
});

async function loadState(quiet = false) {
  try {
    const fresh = await api("state");
    // Состояние интерфейса живёт между опросами. Раньше state
    // перезаписывался целиком, и всё, что выбрал руками — прокси,
    // открытая страница узлов — слетало на следующем опросе.
    state = {
      ...fresh,
      vpnPage: state.vpnPage,
      pickBulk: state.pickBulk,
      pickCreate: state.pickCreate,
      pickLabels: state.pickLabels,
      picker: state.picker,
    };
    render();
    if (!quiet && state.live === false) {
      toast("Бот не отвечает — показан последний снимок", "bad");
    }
  } catch (exc) {
    if (exc.message !== "нужен вход") toast(exc.message, "bad");
  }
}

/* ---------- отрисовка ---------- */

const STATUS = {
  on: ["В игре", "заспавн", "авторизован"],
  wait: ["Ожидание", "Подключение", "очередь", "Очередь"],
  bad: ["ЗАБАНЕН", "Забанен", "бан"],
};

function statusKind(bot) {
  if (bot.banned) return "bad";
  const s = String(bot.status || "");
  if (STATUS.bad.some((k) => s.includes(k))) return "bad";
  if (STATUS.on.some((k) => s.includes(k))) return "on";
  if (STATUS.wait.some((k) => s.includes(k))) return "wait";
  return "off";
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function render() {
  const bots = state.bots || [];
  const online = bots.filter((b) => statusKind(b) === "on").length;
  const banned = bots.filter((b) => b.banned).length;
  const vpn = state.vpn || {};
  const usable = (vpn.summary && vpn.summary.usable) || 0;

  $("#stats").innerHTML = [
    ["Всего", bots.length, ""],
    ["Онлайн", online, "ok"],
    ["В бане", banned, banned ? "bad" : ""],
    ["Узлов VPN", usable, usable ? "ok" : "warn"],
  ].map(([k, v, cls]) =>
    `<div class="stat glass"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`
  ).join("");

  renderBots(bots);
  // Опрос приходит раз в 4с и всегда с первой страницей. Если открыта
  // другая — не сбиваем её, иначе пагинация сама себя закрывает.
  if ((state.vpnPage || 1) <= 1) {
    renderVpn(vpn);
  } else {
    renderVpnStatsOnly(vpn);
  }
  renderBulkProxy(vpn);
  renderCreateProxy(vpn);
  renderSettings();
  pruneSelection();
  renderBulk();
}

function renderBots(bots) {
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? bots.filter((b) =>
        String(b.nickname).toLowerCase().includes(needle) ||
        String(b.server).toLowerCase().includes(needle) ||
        String(b.id) === needle)
    : bots;

  $("#bots-count").textContent = `${shown.length} из ${bots.length}`;

  if (!shown.length) {
    $("#bots").innerHTML = `<div class="empty">${
      bots.length ? "Ничего не найдено" : "Аккаунтов пока нет"
    }</div>`;
    return;
  }

  $("#bots").innerHTML = shown.map((b) => {
    const kind = statusKind(b);
    const running = kind === "on" || kind === "wait";
    const id = Number(b.id);
    const dis = busy.has(id) ? "disabled" : "";
    const proxy = b.proxy ? shortProxy(b.proxy) : "свой IP";
    const picked = selected.has(id) ? "checked" : "";

    // Уровень и опыт. Опыт «до следующего» считается на сервере, здесь
    // только показываем: цифры приходят из меню статистики, а не из
    // реестра, поэтому всегда свежие.
    const hasExp = b.exp_cur !== null && b.exp_cur !== undefined && b.exp_next;
    const lvl = b.level_num ? `lvl ${b.level_num}` : "—";
    const pct = hasExp ? Math.max(0, Math.min(100, Number(b.exp_percent) || 0)) : 0;
    const expTitle = hasExp
      ? `Опыт ${b.exp_cur} из ${b.exp_next}, до следующего уровня ${b.exp_left}`
      : "Статистика ещё не запрашивалась";

    return `
      <div class="bot ${b.banned ? "banned" : ""} ${picked ? "picked" : ""}">
        <label class="pick"><input type="checkbox" data-pick="${id}" ${picked} ${dis}></label>
        <div class="id">${id}</div>
        <div class="nick">${escapeHtml(b.nickname)}
          <small>${escapeHtml(b.server || "")}</small></div>
        <div class="cell"><span class="dot ${kind}"></span>${escapeHtml(b.status || "—")}</div>
        <div class="cell lvl" title="${escapeHtml(expTitle)}">
          <b>${escapeHtml(lvl)}</b>
          ${hasExp ? `<small>${b.exp_cur}/${b.exp_next} · осталось ${b.exp_left}</small>
          <i class="xp"><s style="width:${pct}%"></s></i>` : ""}
        </div>
        <div class="cell" title="${escapeHtml(lvlTitle(b))}">
          ${escapeHtml(TASKS[b.task] || b.task || "—")}
          ${lvlLine(b)}
        </div>
        <div class="cell" title="${escapeHtml(b.proxy || "")}">${escapeHtml(proxy)}</div>
        <div class="acts">
          ${b.banned
            ? `<button class="icon-btn green" data-act="unban" data-id="${id}" ${dis} title="Снять бан">↺</button>`
            : running
              ? `<button class="icon-btn red" data-act="stop" data-id="${id}" ${dis} title="Стоп">■</button>`
              : `<button class="icon-btn green" data-act="start" data-id="${id}" ${dis} title="Запустить">▶</button>`}
          <button class="icon-btn" data-act="restart" data-id="${id}" ${dis} title="Перезапустить">⟳</button>
          <button class="icon-btn red" data-act="delete" data-id="${id}" ${dis} title="Удалить">✕</button>
        </div>
      </div>`;
  }).join("");
}

/* ---------- выделение ---------- */

// Куда движок качки поставил бота. Показываем вокзал и точку, чтобы по
// одной строке было видно, кто где стоит, а не просто «качается».
function lvlPoint(b) {
  const lvl = b.lvl || {};
  const point = lvl.target;
  if (!Array.isArray(point) || point.length < 2) return null;
  return {
    station: lvl.station || "",
    index: Number.isInteger(lvl.target_index) ? lvl.target_index + 1 : null,
    coords: `${Number(point[0]).toFixed(1)}, ${Number(point[1]).toFixed(1)}`,
  };
}

function lvlLine(b) {
  const p = lvlPoint(b);
  if (!p) return "";
  const num = p.index ? `точка ${p.index} · ` : "";
  return `<small class="lvlpoint">${escapeHtml(num + p.coords)}</small>`;
}

function lvlTitle(b) {
  const p = lvlPoint(b);
  if (!p) return "";
  return `${p.station}${p.index ? `, точка ${p.index}` : ""} (${p.coords})`;
}

function visibleBots() {
  const needle = filter.trim().toLowerCase();
  const bots = state.bots || [];
  return needle
    ? bots.filter((b) =>
        String(b.nickname).toLowerCase().includes(needle) ||
        String(b.server).toLowerCase().includes(needle) ||
        String(b.id) === needle)
    : bots;
}

function renderBulk() {
  const bar = $("#bulk");
  const count = selected.size;
  bar.classList.toggle("hidden", count === 0);
  $("#bulk-count").textContent = `Выбрано: ${count}`;
  $("#pick-all").checked = count > 0 && count === visibleBots().length;
  $("#pick-all").indeterminate = count > 0 && count < visibleBots().length;
}

function pruneSelection() {
  const alive = new Set((state.bots || []).map((b) => Number(b.id)));
  for (const id of Array.from(selected)) {
    if (!alive.has(id)) selected.delete(id);
  }
}

function shortProxy(url) {
  const m = String(url).match(/@?([\d.]+):(\d+)/);
  return m ? `${m[1]}:${m[2]}` : String(url).slice(0, 24);
}

// Выпадашка «прокси для выбранных»: сохранённые вручную плюс узлы пула.
// Узлы с мостом тоже годятся — сервер сам поднимет sing-box и отдаст
// локальный socks5.
// Выбор прокси: один модальный список на оба места (массовая полоса и
// форма создания). Раньше это были <select>, и на большом пуле они
// превращались в простыню без поиска — а после пагинации узлов вообще
// показывали только открытую страницу. Теперь список тянется с сервера
// страницами, с поиском по имени, адресу и протоколу.
const PICKER_LABELS = {
  "": "— прокси не выбран —",
  spread: "🎲 Рабочие из пула (вразброс)",
  none: "🚫 Своё IP (сбросить)",
};

function pickerValue(target) {
  return target === "create"
    ? (state.pickCreate ?? "spread")
    : (state.pickBulk ?? "");
}

function pickerLabel(value) {
  if (value in PICKER_LABELS) return PICKER_LABELS[value];
  if (value.startsWith("px:")) return `⚡️ ${value.slice(3)}`;
  if (value.startsWith("node:")) {
    const known = (state.pickLabels || {})[value];
    if (known) return known;
    const node = ((state.vpn || {}).nodes || [])
      .find((n) => n.key === value.slice(5));
    return node ? `🟢 ${node.name}` : "🟢 узел пула";
  }
  return value;
}

function rememberLabel(value, label) {
  state.pickLabels = { ...(state.pickLabels || {}), [value]: label };
}

function syncProxyButtons() {
  const bulk = $("#bulk-proxy-btn");
  if (bulk) {
    const v = pickerValue("bulk");
    bulk.textContent = v ? pickerLabel(v) : "🌐 Прокси";
    bulk.classList.toggle("active", Boolean(v));
  }
  const create = $("#c-proxy-btn");
  if (create) create.textContent = pickerLabel(pickerValue("create"));
}

// Заглушки под старые вызовы: узлы теперь живут в выборщике, а не в
// выпадашке, но шапка панели по-прежнему обновляет счётчики.
function renderBulkProxy() {
  syncProxyButtons();
}

function renderCreateProxy() {
  syncProxyButtons();
}

function openPicker(target) {
  state.picker = { target, page: 1, search: "" };
  $("#picker").classList.remove("hidden");
  $("#picker-title").textContent = target === "create"
    ? "Прокси для новых аккаунтов"
    : "Прокси для выбранных";
  $("#picker-search").value = "";
  loadPicker();
}

function closePicker() {
  $("#picker").classList.add("hidden");
  state.picker = null;
}

async function loadPicker() {
  const pick = state.picker;
  if (!pick) return;
  const list = $("#picker-list");
  list.innerHTML = `<div class="empty">Загружаю…</div>`;
  try {
    const res = await post("vpn_status", {
      page: pick.page,
      per_page: 12,
      search: pick.search,
    });
    renderPicker(res.result || {});
  } catch (exc) {
    list.innerHTML = `<div class="empty">${escapeHtml(exc.message)}</div>`;
  }
}

function renderPicker(vpn) {
  const pick = state.picker;
  if (!pick) return;
  const nodes = vpn.nodes || [];
  const saved = vpn.saved || [];
  const current = pickerValue(pick.target);
  const parts = [];

  // Базовые варианты — только на первой странице без поиска: в поиске
  // они мешают и не относятся к запросу.
  if (pick.page === 1 && !pick.search) {
    const base = pick.target === "create"
      ? ["spread", "none"]
      : ["none"];
    for (const value of base) {
      parts.push(pickerRow(value, PICKER_LABELS[value], "", current));
    }
    for (const item of saved) {
      parts.push(pickerRow(`px:${item.name}`, `⚡️ ${item.name}`,
        "сохранённый", current));
    }
  }

  for (const node of nodes) {
    const banned = node.status === "banned";
    const label = node.usable ? "рабочий" : banned ? "забанен"
      : node.alive ? "нужен клиент" : "мёртвый";
    const kind = node.usable ? "ok" : node.alive && !banned ? "warn" : "bad";
    const dis = node.usable && !banned ? "" : "disabled";
    parts.push(`
      <button class="pick-row" type="button" data-pick-node="${escapeHtml(node.key)}"
              ${dis} ${current === `node:${node.key}` ? 'data-on="1"' : ""}>
        <span class="dot ${kind === "ok" ? "on" : kind === "warn" ? "wait" : "bad"}"></span>
        <span class="pick-main">
          <span class="pick-name">${escapeHtml(node.name || "без имени")}</span>
          <span class="pick-sub">${escapeHtml(node.address || "")}${
            node.latency ? ` · ${node.latency}мс` : ""
          }</span>
        </span>
        <span class="badge ${kind}">${label}</span>
      </button>`);
  }

  if (!parts.length) {
    parts.push(`<div class="empty">${
      pick.search ? "Ничего не нашлось" : "Узлов нет"
    }</div>`);
  }
  $("#picker-list").innerHTML = parts.join("");
  renderPickerPager(vpn);
}

function pickerRow(value, label, hint, current) {
  return `
    <button class="pick-row" type="button" data-pick-value="${escapeHtml(value)}"
            ${current === value ? 'data-on="1"' : ""}>
      <span class="pick-main">
        <span class="pick-name">${escapeHtml(label)}</span>
        ${hint ? `<span class="pick-sub">${escapeHtml(hint)}</span>` : ""}
      </span>
      <span class="pick-check">✓</span>
    </button>`;
}

function renderPickerPager(vpn) {
  const box = $("#picker-pager");
  const page = vpn.page || 1;
  const pages = vpn.pages || 1;
  const total = vpn.total ?? 0;
  if (pages <= 1) {
    box.innerHTML = total ? `<span class="pager-info">Узлов: ${total}</span>` : "";
    return;
  }
  const btn = (p, label, dis) =>
    `<button class="btn btn-sm" type="button" data-pickpage="${p}" ${
      dis ? "disabled" : ""
    }>${label}</button>`;
  box.innerHTML = [
    btn(1, "«", page === 1),
    btn(page - 1, "‹", page === 1),
    `<span class="pager-info">${page} / ${pages} · узлов ${total}</span>`,
    btn(page + 1, "›", page === pages),
    btn(pages, "»", page === pages),
  ].join("");
}

function choosePickerValue(value, label) {
  const pick = state.picker;
  if (!pick) return;
  if (label) rememberLabel(value, label);
  if (pick.target === "create") state.pickCreate = value;
  else state.pickBulk = value;
  syncProxyButtons();
  closePicker();
}

function renderSettings() {
  if (!$("#s-pass").value && state.settings) {
    $("#s-pass").value = state.settings.base_password || "";
  }
  if (state.settings && $("#s-task").dataset.init !== "1") {
    $("#s-task").value = state.settings.default_task || "lvl";
    $("#s-task").dataset.init = "1";
  }
}

/* ---------- действия ---------- */

/* Галочки выделения. Отдельным обработчиком: change не всплывает так,
   как click, и вешать его на документ надёжнее, чем на каждую строку. */
document.addEventListener("change", (e) => {
  const pick = e.target.closest("[data-pick]");
  if (pick) {
    const id = Number(pick.dataset.pick);
    if (pick.checked) selected.add(id); else selected.delete(id);
    const row = pick.closest(".bot");
    if (row) row.classList.toggle("picked", pick.checked);
    renderBulk();
    return;
  }
  if (e.target.id === "pick-all") {
    const all = e.target.checked;
    visibleBots().forEach((b) => {
      const id = Number(b.id);
      if (all) selected.add(id); else selected.delete(id);
    });
    render();
  }
});

$("#bulk-clear").addEventListener("click", () => {
  selected.clear();
  render();
});

document.addEventListener("click", async (e) => {
  const bulkBtn = e.target.closest("[data-bulk]");
  if (bulkBtn) {
    const act = bulkBtn.dataset.bulk;
    const ids = Array.from(selected);
    if (!ids.length) return;
    if (act === "delete" && !confirm(`Удалить ${ids.length} аккаунт(ов)?`)) return;

    // Прокси для выбранных берём из выпадашки: имя сохранённого или
    // ключ узла пула. Пусто — ничего не делаем, чтобы не сбросить
    // случайно настроенные прокси.
    let payload = { ids };
    let cmd;
    if (act === "proxy") {
      const value = state.pickBulk || "";
      if (!value) {
        toast("Выбери прокси в списке", "bad");
        return;
      }
      cmd = "proxy_many";
      if (value === "none") payload.proxy = "none";
      else if (value.startsWith("node:")) payload.node = value.slice(5);
      else if (value.startsWith("px:")) {
        const item = ((state.vpn || {}).saved || [])
          .find((p) => p.name === value.slice(3));
        if (!item) {
          toast("Сохранённый прокси не найден", "bad");
          return;
        }
        payload.proxy = item.proxy;
      }
    } else if (act === "goal") {
      const raw = (($("#bulk-goal") || {}).value || "").trim();
      if (raw === "") {
        toast("Введи целевой уровень (0 — снять)", "bad");
        return;
      }
      cmd = "goal_many";
      payload.level = Number(raw) || 0;
    } else {
      // Старт/стоп/удаление идут пачкой, статистика и вход в мир —
      // своими командами: они работают в фоне и не блокируют мост.
      cmd = act === "stats" ? "stats"
        : act === "real_world" ? "real_world"
        : act + "_many";
    }

    bulkBtn.disabled = true;
    try {
      const res = await post(cmd, payload);
      const done = (res.result || {}).count ?? ids.length;
      toast(
        act === "delete" ? `Удаляю: ${done}`
          : act === "stats" ? `Запросил статистику: ${done}`
          : act === "proxy" ? `Прокси применяю: ${done}`
          : act === "goal" ? `Цель ${payload.level || 0} lvl: ${done}`
          : `В очереди: ${done}`,
        "ok",
      );
      if (act === "delete") selected.clear();
      await loadState(true);
    } catch (exc) {
      toast(exc.message, "bad");
    } finally {
      bulkBtn.disabled = false;
    }
    return;
  }

  const botBtn = e.target.closest("[data-act]");
  if (botBtn) {
    const id = Number(botBtn.dataset.id);
    const act = botBtn.dataset.act;
    if (act === "delete" && !confirm(`Удалить аккаунт #${id}?`)) return;
    busy.add(id);
    render();
    try {
      await post(act, { id });
      toast("Готово", "ok");
      await loadState(true);
    } catch (exc) {
      toast(exc.message, "bad");
    } finally {
      busy.delete(id);
      render();
    }
    return;
  }

  const pageBtn = e.target.closest("[data-nodepage]");
  if (pageBtn) {
    pageBtn.disabled = true;
    await loadVpnPage(Number(pageBtn.dataset.nodepage) || 1);
    pageBtn.disabled = false;
    return;
  }

  const banBtn = e.target.closest("[data-ban]");
  if (banBtn) {
    banBtn.disabled = true;
    try {
      await post("vpn_ban", { key: banBtn.dataset.ban, reason: "вручную" });
      toast("Адрес забанен и больше не выдаётся", "ok");
      await loadVpnPage(state.vpnPage || 1);
    } catch (exc) {
      toast(exc.message, "bad");
    } finally {
      banBtn.disabled = false;
    }
    return;
  }

  const unbanBtn = e.target.closest("[data-unban]");
  if (unbanBtn) {
    unbanBtn.disabled = true;
    try {
      await post("vpn_unban", { key: unbanBtn.dataset.unban });
      toast("Бан снят", "ok");
      await loadVpnPage(state.vpnPage || 1);
    } catch (exc) {
      toast(exc.message, "bad");
    } finally {
      unbanBtn.disabled = false;
    }
    return;
  }

  const nodeBtn = e.target.closest("[data-cluster]");
  if (nodeBtn) {
    nodeBtn.disabled = true;
    try {
      const res = await post("vpn_use", { key: nodeBtn.dataset.cluster });
      toast("Роздано ботам: " + ((res.result || {}).applied || []).length, "ok");
      await loadState(true);
    } catch (exc) {
      toast(exc.message, "bad");
    } finally {
      nodeBtn.disabled = false;
    }
  }
});

$("#refresh").addEventListener("click", () => loadState());

/* ---------- выборщик прокси ---------- */

document.addEventListener("click", (e) => {
  const open = e.target.closest("[data-pick]");
  if (open) {
    e.preventDefault();
    openPicker(open.dataset.pick);
    return;
  }
  if (e.target.id === "picker-close" || e.target.id === "picker") {
    closePicker();
    return;
  }
  const val = e.target.closest("[data-pick-value]");
  if (val) {
    choosePickerValue(val.dataset.pickValue);
    return;
  }
  const node = e.target.closest("[data-pick-node]");
  if (node) {
    const name = (node.querySelector(".pick-name") || {}).textContent || "";
    choosePickerValue(`node:${node.dataset.pickNode}`,
      `🟢 ${name.trim()}`.trim());
    return;
  }
  const page = e.target.closest("[data-pickpage]");
  if (page && state.picker) {
    state.picker.page = Number(page.dataset.pickpage) || 1;
    loadPicker();
  }
});

let pickerSearchTimer = null;
document.addEventListener("input", (e) => {
  if (e.target.id !== "picker-search" || !state.picker) return;
  clearTimeout(pickerSearchTimer);
  const text = e.target.value;
  pickerSearchTimer = setTimeout(() => {
    if (!state.picker) return;
    state.picker.search = text;
    state.picker.page = 1;
    loadPicker();
  }, 260);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.picker) closePicker();
});

$("#vpn-refresh").addEventListener("click", async (e) => {
  const btn = e.target;
  btn.disabled = true;
  btn.textContent = "Проверяю…";
  try {
    await post("vpn_refresh");
    toast("Проверка запущена", "ok");
    await loadState(true);
  } catch (exc) {
    toast(exc.message, "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Перепроверить";
  }
});

$("#bots-search").addEventListener("input", (e) => {
  filter = e.target.value;
  renderBots(state.bots || []);
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    ["accounts", "vpn", "create", "settings"].forEach((name) => {
      $("#tab-" + name).classList.toggle("hidden", name !== tab.dataset.tab);
    });
  });
});

/* ---------- создание ---------- */

function fillServers() {
  const sel = $("#c-server");
  sel.innerHTML = `<option value="">— выбери сервер —</option>` +
    SERVERS.map((s) => `<option value="${s}">${s}</option>`).join("");
}

$("#create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#c-submit");
  const count = Math.max(1, Number($("#c-count").value) || 1);
  const choice = state.pickCreate || "spread";
  // Значение из выпадашки переводим в то, что понимает сервер:
  // px:<имя> -> строка сохранённого, node:<ключ> -> узел пула.
  const payloadProxy = { proxy: choice };
  if (choice.startsWith("px:")) {
    const item = ((state.vpn || {}).saved || [])
      .find((p) => p.name === choice.slice(3));
    payloadProxy.proxy = item ? item.proxy : "none";
  } else if (choice.startsWith("node:")) {
    payloadProxy.proxy = "";
    payloadProxy.node = choice.slice(5);
  }
  btn.disabled = true;
  btn.textContent = "Создаю…";
  try {
    if (count > 1) {
      // Партия: ник не задаётся — при десятке одинаковых имён сервер
      // всё равно перегенерирует занятые.
      const res = await post("add_many", {
        server: $("#c-server").value,
        count,
        password: $("#c-pass").value.trim(),
        task: $("#c-task").value,
        ...payloadProxy,
      });
      const r = res.result || {};
      toast(
        `Создано ${r.count ?? 0} · с прокси ${r.with_proxy ?? 0}`,
        "ok",
      );
    } else {
      const res = await post("add", {
        server: $("#c-server").value,
        nickname: $("#c-nick").value.trim(),
        password: $("#c-pass").value.trim(),
        task: $("#c-task").value,
        ...payloadProxy,
      });
      const r = res.result || {};
      toast(`Создан ${r.nickname || ""} (#${r.id ?? "?"})`, "ok");
      $("#c-nick").value = "";
    }
    await loadState(true);
  } catch (exc) {
    toast(exc.message, "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Создать";
  }
});

/* ---------- настройки ---------- */

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#s-submit");
  btn.disabled = true;
  try {
    await post("settings", {
      base_password: $("#s-pass").value.trim(),
      default_task: $("#s-task").value,
    });
    toast("Сохранено", "ok");
  } catch (exc) {
    toast(exc.message, "bad");
  } finally {
    btn.disabled = false;
  }
});

/* ---------- старт ---------- */

(async () => {
  fillServers();
  try {
    const s = await api("session");
    if (s.authed) showApp(); else showLogin();
  } catch {
    showLogin();
  }
})();
