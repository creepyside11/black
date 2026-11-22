-- level_bot.lua: бот для прокачки уровня (режим bot)
--
-- Что делает:
--   1. Надёжно спавнится: выбирает точку спавна, ждёт on_spawn, проверяет позицию
--      через br.pos(), при неудаче повторяет выбор.
--   2. Стоит на спавне и чуть-чуть двигается (шаг на 0.6-1.5 м и обратно).
--   3. После каждого PayDay (и по таймеру на всякий случай) смотрит свой уровень
--      командой /stats и пишет его в лог.
--
-- Запуск (в консоли программы):
--   /stop 1
--   /lua bot 1 level_bot.lua --dry    -- проверка без действий
--   /lua bot 1 level_bot.lua          -- боевой запуск
--   /lua status 1                     -- состояние
--   /lua stop 1                       -- остановить Lua

------------------------------------------------------------
-- НАСТРОЙКИ
------------------------------------------------------------
local CFG = {
  -- спавн
  PREFERRED_SPAWN_ID  = nil,  -- nil = первый ID из меню сервера
  FALLBACK_SPAWN_ID   = 0,    -- если меню не удалось разобрать (смотрите лог [SPAWN])
  SPAWN_CONFIRM_DELAY = 2.0,  -- сек после on_spawn до проверки позиции
  SPAWN_TIMEOUT       = 15,   -- сек ждать спавн после выбора, потом повторить
  SPAWN_MAX_TRIES     = 6,    -- после стольких неудач пауза SPAWN_COOLDOWN
  SPAWN_COOLDOWN      = 60,

  -- движение на месте
  MOVE_RADIUS_MIN = 0.6,      -- метры от точки спавна
  MOVE_RADIUS_MAX = 1.5,
  MOVE_PAUSE_MIN  = 25,       -- пауза между шагами, сек
  MOVE_PAUSE_MAX  = 70,
  LOG_MOVES       = false,

  -- проверка уровня
  CHECK_COMMAND            = '/stats',
  CHECK_DELAY_AFTER_PAYDAY = 4,    -- сек после PayDay до проверки
  CHECK_FALLBACK_MIN       = 30,   -- проверять и без PayDay раз в N минут
  CHECK_TIMEOUT            = 10,   -- сек ждать ответ сервера
  CHECK_RETRIES            = 3,
  LEVEL_FROM_CHAT          = false, -- искать уровень в чате (если сервер пишет статистику туда)
  STATUS_EVERY_MIN         = 10,   -- сводка в лог

  -- Бессмертие от голода. Выключено намеренно: HP, застывшее на сотне
  -- круглосуточно, — ровная константа, которой у живого игрока не бывает
  -- (голод, урон, восстановление дают колебания). Сервер такие признаки
  -- копит и банит не сразу, а через часы, у каждого аккаунта в свой момент.
  ANTI_STARVE = false,
  DEBUG_CHAT  = false,        -- писать весь чат в лог (чтобы подобрать PAYDAY_PATTERNS)
}

-- Что считать PayDay (простой поиск подстроки; регистр важен).
-- Если PayDay не ловится: включите DEBUG_CHAT, найдите строку и добавьте её сюда.
local PAYDAY_PATTERNS = {
  'PayDay', 'Payday', 'PAYDAY', 'Pay Day',
  'Банковский чек', 'банковский чек',
  'Зарплата', 'зарплата',
}

-- Как достать уровень из текста (Lua-шаблоны, один захват).
-- "Уровень розыска: 0" не совпадёт: после слова допустимы только пробел : - =
local LEVEL_PATTERNS = {
  'Уровень[%s:%-=]*(%d+)',
  'уровень[%s:%-=]*(%d+)',
  'Level[%s:%-=]*(%d+)',
  'LVL[%s:%-=]*(%d+)',
  'Lvl[%s:%-=]*(%d+)',
}
local EXP_PATTERNS = {
  'Опыт[%s:%-=]*(%d+)%s*/%s*(%d+)',
  'опыт[%s:%-=]*(%d+)%s*/%s*(%d+)',
  'EXP[%s:%-=]*(%d+)%s*/%s*(%d+)',
}

------------------------------------------------------------
-- СОСТОЯНИЕ
------------------------------------------------------------
local S = {
  state = 'wait_connect',  -- wait_connect | wait_menu | spawning | ingame
  t0 = nil,
  spawn_id = nil, spawn_tries = 0, spawn_sent = nil, spawn_event = nil, select_at = nil,
  home = nil, away = false, moving = false, move_started = 0, next_move = 0,
  awaiting = false, await_since = 0, check_at = nil, check_reason = '', check_tries = 0,
  fallback_at = nil,
  level = nil, start_level = nil, exp_cur = nil, exp_need = nil,
  paydays = 0, last_payday = nil,
  status_at = nil, last_err = 0,
}

------------------------------------------------------------
-- УТИЛИТЫ
------------------------------------------------------------
local function log(s) br.log(s) end

local function fmt_time(sec)
  sec = math.floor(sec)
  return string.format('%02d:%02d:%02d', sec // 3600, (sec % 3600) // 60, sec % 60)
end

local function uptime()
  if not S.t0 then return '00:00:00' end
  return fmt_time(br.now() - S.t0)
end

local function strip(text)
  return (tostring(text or ''):gsub('{%x%x%x%x%x%x}', ''))
end

local function rand(a, b) return a + math.random() * (b - a) end

local function dump(v, depth)
  depth = depth or 0
  if type(v) ~= 'table' then return tostring(v) end
  if depth >= 3 then return '{...}' end
  local parts, n = {}, 0
  for k, x in pairs(v) do
    n = n + 1
    if n > 12 then parts[#parts + 1] = '...' break end
    parts[#parts + 1] = tostring(k) .. '=' .. dump(x, depth + 1)
  end
  return '{' .. table.concat(parts, ', ') .. '}'
end

local function contains_any(text, list)
  for _, p in ipairs(list) do
    if text:find(p, 1, true) then return true end
  end
  return false
end

local function parse_level(text)
  local t = strip(text)
  for _, pat in ipairs(LEVEL_PATTERNS) do
    local n = t:match(pat)
    if n then
      local cur, need
      for _, ep in ipairs(EXP_PATTERNS) do
        local a, b = t:match(ep)
        if a then cur, need = tonumber(a), tonumber(b) break end
      end
      return tonumber(n), cur, need
    end
  end
  return nil
end

-- Достаём ID точек спавна из меню (формат зависит от сервера, поэтому "мягко").
local ID_KEYS = { 'id', 'spawn_id', 'spawn', 'index', 'i', 'point', 'p' }
local LIST_KEYS = { 'list', 'points', 'spawns', 'items', 'spawn_list', 'locations', 'data', 'l', 'a', 's' }

local function collect_ids(data, out, depth)
  if type(data) ~= 'table' or depth > 4 then return end
  if #data > 0 then
    for _, v in ipairs(data) do
      if type(v) == 'number' then
        out[#out + 1] = v
      elseif type(v) == 'table' then
        local found
        for _, k in ipairs(ID_KEYS) do
          if type(v[k]) == 'number' then found = v[k] break end
        end
        if found then out[#out + 1] = found else collect_ids(v, out, depth + 1) end
      end
    end
    return
  end
  for _, k in ipairs(LIST_KEYS) do
    if type(data[k]) == 'table' then collect_ids(data[k], out, depth + 1) end
    if #out > 0 then return end
  end
end

local function pick_spawn_id(data)
  local ids = {}
  collect_ids(data, ids, 0)
  if #ids == 0 then return nil, ids end
  if CFG.PREFERRED_SPAWN_ID ~= nil then
    for _, id in ipairs(ids) do
      if id == CFG.PREFERRED_SPAWN_ID then return id, ids end
    end
  end
  return ids[1], ids
end

------------------------------------------------------------
-- СПАВН
------------------------------------------------------------
local function select_spawn()
  local id = S.spawn_id
  if id == nil then id = CFG.FALLBACK_SPAWN_ID end
  br.spawn_select(id)
  S.state = 'spawning'
  S.spawn_sent = br.now()
  S.spawn_event = nil
  S.spawn_tries = S.spawn_tries + 1
  log(string.format('[SPAWN] выбираю точку %s (попытка %d/%d)', tostring(id), S.spawn_tries, CFG.SPAWN_MAX_TRIES))
end

local function confirm_spawn(p, now)
  S.home = { x = p.x, y = p.y, z = p.z }
  S.state = 'ingame'
  S.spawn_tries = 0
  S.spawn_sent = nil
  S.away = false
  S.moving = false
  S.next_move = now + rand(5, 12)
  S.check_at = now + 6
  S.check_reason = 'после спавна'
  S.fallback_at = now + CFG.CHECK_FALLBACK_MIN * 60
  log(string.format('[SPAWN] OK, в мире: x=%.1f y=%.1f z=%.1f | онлайн %s', p.x, p.y, p.z, uptime()))
  if CFG.ANTI_STARVE then br.immortality(true) end
end

------------------------------------------------------------
-- УРОВЕНЬ
------------------------------------------------------------
local function record_level(lvl, exp_cur, exp_need, src)
  if S.start_level == nil then S.start_level = lvl end
  local change = ''
  if S.level and lvl > S.level then change = string.format('  <<< НОВЫЙ УРОВЕНЬ (+%d)', lvl - S.level) end
  S.level = lvl
  if exp_cur then S.exp_cur, S.exp_need = exp_cur, exp_need end
  local exp = ''
  if exp_cur then exp = string.format(' | опыт %d/%d', exp_cur, exp_need) end
  log(string.format('[LVL] Уровень: %d (за сессию %+d)%s | PayDay: %d | онлайн %s%s',
    lvl, lvl - S.start_level, exp, S.paydays, uptime(), change))
  S.awaiting = false
  S.check_tries = 0
  S.fallback_at = br.now() + CFG.CHECK_FALLBACK_MIN * 60
end

local function start_check(now)
  S.awaiting = true
  S.await_since = now
  S.check_at = nil
  log(string.format('[LVL] проверяю уровень (%s): %s', S.check_reason, CFG.CHECK_COMMAND))
  br.chat(CFG.CHECK_COMMAND)
end

local function register_payday(src)
  local now = br.now()
  if S.last_payday and now - S.last_payday < 60 then return end
  S.last_payday = now
  S.paydays = S.paydays + 1
  log(string.format('[PAYDAY] #%d (%s) | онлайн %s', S.paydays, src, uptime()))
  if not S.awaiting then
    S.check_at = now + CFG.CHECK_DELAY_AFTER_PAYDAY
    S.check_reason = 'после PayDay'
  end
end

local function close_dialog(e)
  -- у диалога с одной кнопкой нажатие = response 1, у двух кнопок закрываем второй
  local resp = (e.button2 == nil or e.button2 == '') and 1 or 0
  br.dialog(e.id, resp, -1, '')
end

------------------------------------------------------------
-- ДВИЖЕНИЕ НА МЕСТЕ
------------------------------------------------------------
local function do_move(now)
  local p = br.pos()
  if not p or not S.home then return end
  local tx, ty = S.home.x, S.home.y
  local dx, dy = p.x - S.home.x, p.y - S.home.y
  local far = (dx * dx + dy * dy) > 16  -- унесло дальше 4 м: сразу домой
  if not S.away and not far then
    local ang = math.random() * 2 * math.pi
    local r = rand(CFG.MOVE_RADIUS_MIN, CFG.MOVE_RADIUS_MAX)
    tx = S.home.x + math.cos(ang) * r
    ty = S.home.y + math.sin(ang) * r
    S.away = true
  else
    S.away = false
  end
  br.walk(tx, ty, S.home.z)
  S.moving = true
  S.move_started = now
  if CFG.LOG_MOVES then log(string.format('[MOVE] -> %.1f %.1f', tx, ty)) end
end

------------------------------------------------------------
-- ГЛАВНЫЙ ЦИКЛ (on_tick ~ каждые 0.25 с)
------------------------------------------------------------
local function tick()
  local now = br.now()
  if not S.t0 then
    S.t0 = now
    S.status_at = now + CFG.STATUS_EVERY_MIN * 60
  end

  -- отложенный выбор спавна
  if S.select_at and now >= S.select_at then
    S.select_at = nil
    select_spawn()
  end

  -- подтверждение спавна и повторы
  if S.state == 'spawning' then
    if S.spawn_event and now - S.spawn_event >= CFG.SPAWN_CONFIRM_DELAY then
      local p = br.pos()
      if p and br.connected() then confirm_spawn(p, now) end
    end
    if S.state == 'spawning' and S.spawn_sent and now - S.spawn_sent > CFG.SPAWN_TIMEOUT then
      S.spawn_sent = nil
      if S.spawn_tries >= CFG.SPAWN_MAX_TRIES then
        log(string.format('[SPAWN] %d неудачных попыток, пауза %d сек', S.spawn_tries, CFG.SPAWN_COOLDOWN))
        S.spawn_tries = 0
        S.select_at = now + CFG.SPAWN_COOLDOWN
      else
        log('[SPAWN] спавн не подтверждён, повторяю выбор')
        S.select_at = now + 1
      end
    end
    return
  end

  if S.state ~= 'ingame' then return end

  -- таймаут ответа на /stats
  if S.awaiting and now - S.await_since > CFG.CHECK_TIMEOUT then
    S.awaiting = false
    S.check_tries = S.check_tries + 1
    if S.check_tries < CFG.CHECK_RETRIES then
      log(string.format('[LVL] нет ответа на %s, повтор через 15 сек (%d/%d)', CFG.CHECK_COMMAND, S.check_tries, CFG.CHECK_RETRIES))
      S.check_at = now + 15
      S.check_reason = 'повтор'
    else
      log('[LVL] уровень получить не удалось: проверьте CHECK_COMMAND и LEVEL_PATTERNS')
      S.check_tries = 0
      S.fallback_at = now + CFG.CHECK_FALLBACK_MIN * 60
    end
  end

  -- плановая проверка уровня
  if not S.awaiting and S.check_at and now >= S.check_at then
    start_check(now)
  end
  if not S.awaiting and not S.check_at and S.fallback_at and now >= S.fallback_at then
    S.check_at = now
    S.check_reason = 'по таймеру'
  end

  -- шаги на месте
  if S.moving and now - S.move_started > 12 then
    S.moving = false
    br.stop_movement()
    S.next_move = now + rand(CFG.MOVE_PAUSE_MIN, CFG.MOVE_PAUSE_MAX)
  end
  if not S.moving and not S.awaiting and now >= S.next_move then
    do_move(now)
  end

  -- сводка
  if S.status_at and now >= S.status_at then
    S.status_at = now + CFG.STATUS_EVERY_MIN * 60
    log(string.format('[STAT] онлайн %s | уровень %s (старт %s) | PayDay: %d',
      uptime(), tostring(S.level), tostring(S.start_level), S.paydays))
  end
end

------------------------------------------------------------
-- ОБРАБОТЧИКИ СОБЫТИЙ
------------------------------------------------------------
function on_load(e)
  log('[BOT] level_bot загружен, armed=' .. tostring(e and e.armed))
  if e and e.armed == false then
    log('[BOT] действия отключены (--dry?): бот ничего не отправит')
  end
end

function on_tick(e)
  local ok, err = pcall(tick)
  if not ok then
    local now = br.now()
    if now - S.last_err > 10 then
      S.last_err = now
      log('[ERR] tick: ' .. tostring(err))
    end
  end
end

function on_auth(e)
  log('[AUTH] ' .. tostring(e.state))
end

function on_connect(e)
  S.state = 'wait_menu'
  log('[NET] подключено, жду меню спавна')
end

function on_disconnect(e)
  log(string.format('[NET] разрыв соединения | онлайн %s', uptime()))
  S.state = 'wait_connect'
  S.awaiting = false
  S.moving = false
  S.check_at = nil
  S.select_at = nil
  S.spawn_sent = nil
  S.spawn_event = nil
  S.spawn_tries = 0
  S.home = nil
end

function on_spawn_menu(e)
  local id, ids = pick_spawn_id(e.data)
  if id == nil then
    log('[SPAWN] не смог разобрать меню, данные: ' .. dump(e.data))
    S.spawn_id = nil
  else
    S.spawn_id = id
    log(string.format('[SPAWN] меню получено, доступно ID: %d, беру %s', #ids, tostring(id)))
  end
  S.state = 'wait_menu'
  S.select_at = br.now() + rand(1.0, 2.0)
end

function on_spawn(e)
  local now = br.now()
  S.spawn_event = now
  if S.state ~= 'spawning' then
    -- сервер заспавнил без меню (или после смерти): подтверждаем по позиции
    S.state = 'spawning'
    S.spawn_sent = now
    S.select_at = nil
  end
  log('[SPAWN] событие spawn, проверяю позицию...')
end

function on_json(e)
  if e.interface_id == 26 and e.data and e.data.o == 1 then
    log('[CAPTCHA] запрос капчи, решаю')
    br.solve_captcha(e.data)
  end
end

function on_dialog(e)
  if S.state ~= 'ingame' then return end
  local text = strip((e.title or '') .. '\n' .. (e.text or ''))
  if S.awaiting then
    local lvl, ec, en = parse_level(text)
    if lvl then
      record_level(lvl, ec, en, 'dialog')
      close_dialog(e)
      return
    end
  end
  if contains_any(text, PAYDAY_PATTERNS) then
    register_payday('диалог')
    local lvl, ec, en = parse_level(text)
    if lvl then record_level(lvl, ec, en, 'payday') end
    close_dialog(e)
  end
end

function on_chat(e)
  local msg = strip(e.message)
  if CFG.DEBUG_CHAT then log('[CHAT] ' .. msg) end
  if S.state ~= 'ingame' then return end
  if contains_any(msg, PAYDAY_PATTERNS) then register_payday('чат') end
  if CFG.LEVEL_FROM_CHAT and S.awaiting then
    local lvl, ec, en = parse_level(msg)
    if lvl then record_level(lvl, ec, en, 'chat') end
  end
end

function on_notification(e)
  if S.state ~= 'ingame' then return end
  if contains_any(strip(e.text), PAYDAY_PATTERNS) then register_payday('уведомление') end
end

function on_move_end(e)
  S.moving = false
  S.next_move = br.now() + rand(CFG.MOVE_PAUSE_MIN, CFG.MOVE_PAUSE_MAX)
end
