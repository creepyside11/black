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

let state = { bots: [], vpn: {}, live: false };
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
    state = await api("state");
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
  renderVpn(vpn);
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

function renderVpn(vpn) {
  const nodes = vpn.nodes || [];
  const summary = vpn.summary || {};
  $("#vpn-count").textContent = `${summary.usable || 0} рабочих из ${nodes.length}`;

  $("#vpn-stats").innerHTML = [
    ["Всего узлов", nodes.length, ""],
    ["Рабочих", summary.usable || 0, "ok"],
    ["Живых", summary.alive || 0, ""],
    ["Мёртвых", summary.dead || 0, "bad"],
  ].map(([k, v, cls]) =>
    `<div class="stat glass"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`
  ).join("");

  if (!nodes.length) {
    $("#nodes").innerHTML = `<div class="empty">${
      vpn.available === false ? "Пул прокси не запущен" : "Узлов нет"
    }</div>`;
    return;
  }

  $("#nodes").innerHTML = nodes.map((n) => {
    const kind = n.usable ? "ok" : n.alive ? "warn" : "bad";
    const label = n.usable ? "рабочий" : n.alive ? "нужен клиент" : "мёртвый";
    const dis = n.usable ? "" : "disabled";
    return `
      <div class="node">
        <div class="top">
          <span class="dot ${kind === "ok" ? "on" : kind === "warn" ? "wait" : "bad"}"></span>
          <span class="name">${escapeHtml(n.name || "без имени")}</span>
        </div>
        <div class="addr">${escapeHtml(n.address || "")} · ${escapeHtml(n.protocol || "")}</div>
        <div class="row">
          <span class="badge ${kind}">${label}</span>
          <button class="btn btn-sm" data-cluster="${escapeHtml(n.key)}" ${dis}>Раздать</button>
        </div>
      </div>`;
  }).join("");
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

    bulkBtn.disabled = true;
    try {
      // Старт/стоп/удаление идут пачкой, статистика и вход в мир —
      // своими командами: они работают в фоне и не блокируют мост.
      const cmd = act === "stats" ? "stats"
        : act === "real_world" ? "real_world"
        : act + "_many";
      const res = await post(cmd, { ids });
      const done = (res.result || {}).count ?? ids.length;
      toast(
        act === "delete" ? `Удаляю: ${done}`
          : act === "stats" ? `Запросил статистику: ${done}`
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
  btn.disabled = true;
  btn.textContent = "Создаю…";
  try {
    const res = await post("add", {
      server: $("#c-server").value,
      nickname: $("#c-nick").value.trim(),
      password: $("#c-pass").value.trim(),
      task: $("#c-task").value,
    });
    const r = res.result || {};
    toast(`Создан ${r.nickname || ""} (#${r.id ?? "?"})`, "ok");
    $("#c-nick").value = "";
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
