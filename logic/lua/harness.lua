-- logic/lua/harness.lua
-- Lua-загрузчик для моста logic/lua_engine.py.
--
-- Запуск:    lua54 logic/lua/harness.lua <путь-к-скрипту.lua>
-- Протокол:  по одной JSON-строке в каждую сторону, через stdin/stdout.
--
--   Python -> Lua:  {"id":1,"op":"load","path":"...","armed":false,"seed":7,
--                    "max_actions":32}
--                   {"id":2,"op":"event","name":"on_chat","args":{...},
--                    "snapshot":{"pos":{"x":..,"y":..,"z":..},"now":..,"tail":[...]}}
--                   {"id":3,"op":"ping"}   {"id":4,"op":"shutdown"}
--
--   Lua -> Python:  {"op":"ready","lua":"Lua 5.4"}          (один раз при старте)
--                   {"id":1,"ok":true,"logs":[...],"actions":[...],"dropped":0}
--                   {"id":1,"ok":false,"error":"..."}
--
-- Песочница. Скрипт грузится в СВОЁ окружение (_ENV), собранное здесь, а не в
-- глобальную таблицу Lua. В окружение не попадают вовсе: io, debug, package,
-- require, dofile, loadfile, load, collectgarbage и весь os. Поэтому
-- os.execute у скрипта — это индексация nil, часы ему даёт br.now().
-- Харнесс свои полноценные io/os/debug держит в локалах и скрипту их не отдаёт;
-- io.write скрипту недоступен, печатать в stdout он не может — только br.log,
-- строки которого уезжают в ответе.
--
-- Всё, что скрипт делает через br, делится на две части: мгновенные запросы
-- (br.log/pos/now/server_tail — считаются тут же, из снимка, приложенного к
-- запросу) и отложенные действия (br.walk/tp/pickup/send_rpc/send_json/sleep —
-- копятся в actions и исполняются Питоном после ответа). Ни одного обратного
-- запроса во время вызова: одна строка туда, одна обратно.

local real_load = load
local real_io = io
local real_debug = debug
local real_tostring = tostring
local real_tonumber = tonumber
local real_type = type
local real_pairs = pairs
local real_ipairs = ipairs
local real_pcall = pcall
local real_xpcall = xpcall
local real_select = select
local real_error = error
local real_floor = math.floor
local real_format = string.format
local real_concat = table.concat
local real_exit = os.exit

local SCRIPT_PATH = (arg and arg[1]) or nil
local MAX_SCRIPT_BYTES = 512 * 1024
local MAX_LOG = 200
local MAX_LOG_LEN = 2000

-- ============================ JSON =========================================

local json = {}
-- Keep empty arrays and null values distinct across the JSON bridge.
local array_marks = setmetatable({}, { __mode = 'k' })
local json_null = {}
local function make_array(t)
  local result = t or {}
  if real_type(result) ~= 'table' then real_error('array expects a table', 0) end
  array_marks[result] = true
  return result
end

local JSON_ESCAPES = {
  ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
  ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function encode_string(text)
  local body = text:gsub('[%z\1-\31\\"]', function(ch)
    return JSON_ESCAPES[ch] or real_format('\\u%04x', ch:byte())
  end)
  return '"' .. body .. '"'
end

local function encode_number(value)
  if value ~= value or value == math.huge or value == -math.huge then
    return 'null'
  end
  if math.type(value) == 'integer' then
    return real_format('%d', value)
  end
  return real_format('%.14g', value)
end

local function is_array(value)
  if array_marks[value] then return true end
  local count, max_index = 0, 0
  for key in real_pairs(value) do
    if real_type(key) ~= 'number' or key < 1 or key % 1 ~= 0 then return false end
    count = count + 1
    if key > max_index then max_index = key end
  end
  return count > 0 and count == max_index
end

local encode_value

encode_value = function(value, depth)
  local kind = real_type(value)
  if value == nil or value == json_null then
    return 'null'
  elseif kind == 'boolean' then
    return value and 'true' or 'false'
  elseif kind == 'number' then
    return encode_number(value)
  elseif kind == 'string' then
    return encode_string(value)
  elseif kind == 'table' then
    if depth > 12 then
      return 'null'
    end
    if is_array(value) then
      local items = {}
      for index = 1, #value do
        items[index] = encode_value(value[index], depth + 1)
      end
      return '[' .. real_concat(items, ',') .. ']'
    end
    local fields = {}
    for key, item in real_pairs(value) do
      local name = real_type(key) == 'string' and key or real_tostring(key)
      fields[#fields + 1] = encode_string(name) .. ':' .. encode_value(item, depth + 1)
    end
    return '{' .. real_concat(fields, ',') .. '}'
  end
  return encode_string(real_tostring(value))
end

function json.encode(value)
  return encode_value(value, 1)
end

local function skip_space(text, index)
  local _, stop = text:find('^[ \t\r\n]*', index)
  return stop + 1
end

local function decode_string(text, index)
  local out = {}
  local cursor = index + 1
  while true do
    local ch = text:sub(cursor, cursor)
    if ch == '' then
      real_error('незакрытая строка', 0)
    elseif ch == '"' then
      return real_concat(out), cursor + 1
    elseif ch == '\\' then
      local esc = text:sub(cursor + 1, cursor + 1)
      if esc == 'u' then
        local hex = text:sub(cursor + 2, cursor + 5)
        if #hex < 4 or hex:find('[^0-9a-fA-F]') then
          real_error('плохая \\u-последовательность', 0)
        end
        local code = real_tonumber(hex, 16)
        cursor = cursor + 6
        if code >= 0xD800 and code <= 0xDBFF and text:sub(cursor, cursor + 1) == '\\u' then
          local low = real_tonumber(text:sub(cursor + 2, cursor + 5), 16)
          if low and low >= 0xDC00 and low <= 0xDFFF then
            code = 0x10000 + (code - 0xD800) * 0x400 + (low - 0xDC00)
            cursor = cursor + 6
          end
        end
        if code >= 0xD800 and code <= 0xDFFF then
          code = 0xFFFD
        end
        out[#out + 1] = utf8.char(code)
      else
        local map = {
          ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b',
          f = '\f', n = '\n', r = '\r', t = '\t',
        }
        local plain = map[esc]
        if not plain then
          real_error('неизвестная escape-последовательность \\' .. esc, 0)
        end
        out[#out + 1] = plain
        cursor = cursor + 2
      end
    else
      out[#out + 1] = ch
      cursor = cursor + 1
    end
  end
end

local decode_value

decode_value = function(text, index)
  index = skip_space(text, index)
  local ch = text:sub(index, index)
  if ch == '{' then
    local obj = {}
    index = skip_space(text, index + 1)
    if text:sub(index, index) == '}' then
      return obj, index + 1
    end
    while true do
      index = skip_space(text, index)
      if text:sub(index, index) ~= '"' then
        real_error('в объекте ожидался ключ-строка', 0)
      end
      local key
      key, index = decode_string(text, index)
      index = skip_space(text, index)
      if text:sub(index, index) ~= ':' then
        real_error('после ключа ожидалось ":"', 0)
      end
      local value
      value, index = decode_value(text, index + 1)
      obj[key] = value
      index = skip_space(text, index)
      local delim = text:sub(index, index)
      if delim == ',' then
        index = index + 1
      elseif delim == '}' then
        return obj, index + 1
      else
        real_error('в объекте ожидалось "," или "}"', 0)
      end
    end
  elseif ch == '[' then
    local arr = make_array()
    index = skip_space(text, index + 1)
    if text:sub(index, index) == ']' then
      return arr, index + 1
    end
    while true do
      local value
      value, index = decode_value(text, index)
      arr[#arr + 1] = value
      index = skip_space(text, index)
      local delim = text:sub(index, index)
      if delim == ',' then
        index = index + 1
      elseif delim == ']' then
        return arr, index + 1
      else
        real_error('в массиве ожидалось "," или "]"', 0)
      end
    end
  elseif ch == '"' then
    return decode_string(text, index)
  elseif ch == 't' and text:sub(index, index + 3) == 'true' then
    return true, index + 4
  elseif ch == 'f' and text:sub(index, index + 4) == 'false' then
    return false, index + 5
  elseif ch == 'n' and text:sub(index, index + 3) == 'null' then
    return json_null, index + 4
  end
  local number = text:match('^[-+0-9.eE]+', index)
  if number then
    local parsed = real_tonumber(number)
    if parsed then
      return parsed, index + #number
    end
  end
  real_error('не понял значение на позиции ' .. index, 0)
end

function json.decode(text)
  local ok, value, stop = real_pcall(function()
    return decode_value(text, 1)
  end)
  if not ok then
    return nil, real_tostring(value)
  end
  return value, stop
end

-- ======================= состояние одного вызова ============================

local logs = {}
local actions = {}
local dropped = 0
local max_actions = 32
local armed = false
local snapshot = { pos = nil, now = 0, tail = {} }
local handlers = {}
local loaded = false

local function push_log(text)
  if #logs >= MAX_LOG then
    return
  end
  local line = real_tostring(text)
  if #line > MAX_LOG_LEN then
    line = line:sub(1, MAX_LOG_LEN) .. '…'
  end
  logs[#logs + 1] = line
end

local function as_number(value)
  if real_type(value) == 'number' then
    return value
  end
  return nil
end

local function emit_action(action)
  if real_type(action) ~= 'table' then
    push_log('emit: нужно таблицу действия')
    return false
  end
  if #actions >= max_actions then
    dropped = dropped + 1
    return false
  end
  actions[#actions + 1] = action
  return true
end

-- ============================== br =========================================

local br = {}
br.null = json_null
br.array = make_array
br.version = '1'
br.armed = false
br.script = SCRIPT_PATH

br.log = function(text)
  push_log(text)
  return true
end

br.pos = function()
  local point = snapshot.pos
  if point == json_null or real_type(point) ~= 'table' then
    return nil
  end
  return { x = point.x, y = point.y, z = point.z }
end

br.now = function()
  return snapshot.now
end

br.server_tail = function(count)
  local out = {}
  local tail = snapshot.tail
  if real_type(tail) ~= 'table' then
    return out
  end
  local want = as_number(count)
  if want == nil then
    want = #tail
  end
  want = real_floor(want)
  if want < 0 then
    want = 0
  end
  local from = #tail - want + 1
  if from < 1 then
    from = 1
  end
  for index = from, #tail do
    out[#out + 1] = tail[index]
  end
  return out
end

local function emit_move(kind, x, y, z)
  local nx, ny, nz = as_number(x), as_number(y), as_number(z)
  if not (nx and ny and nz) then
    push_log(kind .. ': нужны три числа (x, y, z)')
    return false
  end
  return emit_action({ kind = kind, x = nx, y = ny, z = nz })
end

br.walk = function(x, y, z)
  return emit_move('walk', x, y, z)
end

br.tp = function(x, y, z)
  return emit_move('tp', x, y, z)
end

br.pickup = function(id)
  if id ~= nil and as_number(id) == nil then
    push_log('pickup: id, если задан, должен быть числом')
    return false
  end
  return emit_action({ kind = 'pickup', id = as_number(id) })
end

br.send_rpc = function(rpc_id, payload_hex)
  local rid = as_number(rpc_id)
  if not rid then
    push_log('send_rpc: rpc_id должен быть числом')
    return false
  end
  local hex = payload_hex
  if hex == nil then
    hex = ''
  end
  if real_type(hex) ~= 'string' then
    push_log('send_rpc: payload_hex должен быть строкой')
    return false
  end
  if #hex % 2 ~= 0 or hex:find('[^0-9a-fA-F]') then
    push_log('send_rpc: payload_hex — чётное число hex-символов')
    return false
  end
  return emit_action({ kind = 'send_rpc', rpc_id = rid, payload_hex = hex:lower() })
end

br.send_packet = function(packet_id, payload_hex)
  local id = as_number(packet_id)
  local hex = payload_hex or ''
  if not id or real_type(hex) ~= 'string' or #hex % 2 ~= 0 or hex:find('[^0-9a-fA-F]') then
    push_log('send_packet: нужен номер и hex-строка с чётной длиной'); return false
  end
  return emit_action({kind='send_packet', packet_id=id, payload_hex=hex:lower()})
end

br.send_json = function(interface_id, payload, packet_name)
  local iid = as_number(interface_id)
  if not iid then
    push_log('send_json: interface_id должен быть числом')
    return false
  end
  if real_type(payload) ~= 'table' then
    push_log('send_json: payload должен быть таблицей')
    return false
  end
  if packet_name ~= nil and real_type(packet_name) ~= 'string' then
    push_log('send_json: packet_name должен быть строкой'); return false
  end
  return emit_action({ kind = 'send_json', interface_id = iid, payload = payload,
                       packet_name = packet_name or 'interface_sync' })
end

br.sleep = function(seconds)
  local sec = as_number(seconds)
  if not sec or sec < 0 then
    push_log('sleep: нужно неотрицательное число секунд')
    return false
  end
  if sec > 3600 then
    sec = 3600
  end
  return emit_action({ kind = 'sleep', sec = sec })
end

-- Функции отправляют действие мосту. Они не ждут его выполнения.
br.drive = function(x, y, z) return emit_move('drive', x, y, z) end
br.stop_movement = function() return emit_action({kind='stop_movement'}) end
br.immortality = function(enabled) return emit_action({kind='immortality', enabled=enabled and true or false}) end
br.chat = function(text) return emit_action({kind='chat', text=real_tostring(text or '')}) end
br.keys = function(keys, lr, ud) return emit_action({kind='keys', keys=keys or 0, lr=lr or 0, ud=ud or 0}) end
br.vehicle = function(id, seat) return emit_action({kind='vehicle', id=id, seat=seat or 0}) end
br.spawn_select = function(index) return emit_action({kind='spawn_select', index=index}) end
br.dialog = function(id, response, item, text)
  return emit_action({kind='dialog', id=id, response=response or 1, item=item or 0, text=text or ''})
end
br.solve_captcha = function(data) return emit_action({kind='solve_captcha', data=data}) end
br.after = function(seconds, name, data)
  if real_type(seconds) ~= 'number' or seconds < 0 or seconds > 3600 or real_type(name) ~= 'string' then
    push_log('after: нужны секунды от 0 до 3600 и имя события'); return false
  end
  return emit_action({kind='after', sec=seconds, name=name, data=data or {}})
end
-- Асинхронный контроллер маршрутов; события приходят отдельно.
br.route_start = function(name, mode, options)
  if real_type(name) ~= 'string' or (options ~= nil and real_type(options) ~= 'table') then
    push_log('route_start: нужны имя маршрута, режим и необязательная таблица настроек'); return false
  end
  return emit_action({kind='route_start', route=name, mode=mode or 'walk', options=options or {}})
end
br.route_save = function(name, points)
  if real_type(name) ~= 'string' or real_type(points) ~= 'table' then
    push_log('route_save: нужны имя и массив точек'); return false
  end
  return emit_action({kind='route_save', route=name, points=make_array(points)})
end
br.route_pause = function() return emit_action({kind='route_pause'}) end
br.route_resume = function() return emit_action({kind='route_resume'}) end
br.route_stop = function() return emit_action({kind='route_stop'}) end
br.route_status = function() return snapshot.route end

br.interact = function(id) return br.send_rpc(436, br.u32(id)) end
br.select_textdraw = function(id) return br.send_rpc(437, br.u16(id)) end
local function ashex(bytes)
  return (bytes:gsub('.', function(c) return string.format('%02x', string.byte(c)) end))
end
br.u16 = function(n) return ashex(string.pack('<I2', n)) end
br.u32 = function(n) return ashex(string.pack('<I4', n)) end
br.npc = function(button) return br.send_json(63, {bk=button or 0}, 'interface_npc_dialog_response') end
-- Settings are non-secret values configured for this script in the VPS panel.
br.settings = function() return snapshot.settings or {} end
br.setting = function(key, default)
  local settings = snapshot.settings or {}
  local value = settings[key]
  if value == nil or value == json_null then return default end
  return value
end
br.connected = function() return snapshot.connected == true end
local function optional(value)
  if value == json_null then return nil end
  return value
end
br.vehicle_id = function() return optional(snapshot.vehicle_id) end
br.health = function() return optional(snapshot.health) end
br.money = function() return optional(snapshot.money) end
br.interior = function() return optional(snapshot.interior) end
br.mode = function() return optional(snapshot.mode) end
br.bot_id = function() return optional(snapshot.bot_id) end
br.event = function(name, fn)
  if real_type(name) ~= 'string' or real_type(fn) ~= 'function' then return false end
  handlers[name] = fn
  return true
end

br.emit = emit_action

br.action_count = function()
  return #actions
end

-- ============================ песочница ====================================

local SAFE_GLOBALS = {
  'assert', 'error', 'ipairs', 'next', 'pairs', 'pcall', 'xpcall', 'select',
  'tonumber', 'tostring', 'type', 'rawget', 'rawset', 'rawequal', 'rawlen',
  'setmetatable', 'getmetatable', 'unpack',
}

local function build_env()
  local env = {}
  for _, name in real_ipairs(SAFE_GLOBALS) do
    env[name] = _G[name]
  end
  env.table = table
  env.string = string
  env.math = math
  env.utf8 = utf8
  env.coroutine = coroutine
  env._VERSION = _VERSION
  -- os, io, debug, package, require, dofile, loadfile, load сюда не попадают
  -- вовсе: у скрипта они nil. Часы скрипту даёт br.now(), а не os.clock.
  env.print = function(...)
    local parts = {}
    for index = 1, real_select('#', ...) do
      parts[#parts + 1] = real_tostring((real_select(index, ...)))
    end
    push_log(real_concat(parts, '\t'))
  end
  env.br = br
  env.emit = emit_action
  return env
end

-- ============================ загрузка =====================================

local chunk_env = nil

local function collect_handlers(env)
  local found = {}
  for key, value in real_pairs(env) do
    if real_type(key) == 'string' and key:sub(1, 3) == 'on_'
        and real_type(value) == 'function' then
      found[key] = value
    end
  end
  return found
end

local function do_load(msg)
  local path = msg.path or SCRIPT_PATH
  if not path then
    return false, 'не передан путь к скрипту'
  end
  local handle, open_error = real_io.open(path, 'r')
  if not handle then
    return false, 'не открыл ' .. real_tostring(path) .. ': ' .. real_tostring(open_error)
  end
  local source = handle:read('a')
  handle:close()
  if real_type(source) ~= 'string' then
    return false, 'не прочитал ' .. real_tostring(path)
  end
  if #source > MAX_SCRIPT_BYTES then
    return false, 'скрипт больше ' .. MAX_SCRIPT_BYTES .. ' байт'
  end

  armed = msg.armed and true or false
  br.armed = armed
  br.script = path
  if real_type(msg.max_actions) == 'number' and msg.max_actions >= 1 then
    max_actions = real_floor(msg.max_actions)
  end
  if real_type(msg.seed) == 'number' then
    math.randomseed(real_floor(msg.seed))
  end

  chunk_env = build_env()
  -- mode 't': только текст, никакого предкомпилированного байткода.
  local chunk, load_error = real_load(source, '@' .. real_tostring(path), 't', chunk_env)
  if not chunk then
    return false, 'скрипт не компилируется: ' .. real_tostring(load_error)
  end
  handlers = {}
  local ok, body_error = real_pcall(chunk)
  if not ok then
    return false, 'ошибка при выполнении тела скрипта: ' .. real_tostring(body_error)
  end
  for name, fn in real_pairs(collect_handlers(chunk_env)) do handlers[name] = fn end
  loaded = true
  return true
end

local function call_handler(name, args)
  local fn = handlers[name]
  if not fn and name ~= 'on_load' then
    fn = handlers['on_event']
  end
  if not fn then
    return true
  end
  if real_type(args) ~= 'table' then
    args = {}
  end
  args.name = name
  local ok, trace = real_xpcall(fn, function(err)
    return real_debug.traceback(real_tostring(err), 2)
  end, args)
  if not ok then
    return false, real_tostring(trace)
  end
  return true
end

-- ============================== цикл =======================================

local function write_line(payload)
  real_io.stdout:write(json.encode(payload))
  real_io.stdout:write('\n')
  real_io.stdout:flush()
end

local function respond(id, body)
  body.id = id
  if body.ok == nil then
    body.ok = true
  end
  body.logs = logs
  body.actions = actions
  body.dropped = dropped
  write_line(body)
end

local function handle(msg)
  local id = msg.id
  local op = msg.op
  if real_type(msg.snapshot) == 'table' then
    snapshot = msg.snapshot
  end
  if op == 'shutdown' then
    respond(id, {})
    real_io.stdout:flush()
    real_exit(0)
  elseif op == 'ping' then
    respond(id, { pong = true })
  elseif op == 'load' then
    local ok, err = do_load(msg)
    if not ok then
      respond(id, { ok = false, error = err })
      return
    end
    local called, call_error = call_handler('on_load', { armed = armed, script = msg.path or SCRIPT_PATH })
    respond(id, { ok = called, error = call_error })
  elseif op == 'event' then
    if not loaded then
      respond(id, { ok = false, error = 'скрипт ещё не загружен' })
      return
    end
    local ok, err = call_handler(msg.name or 'on_event', msg.args)
    respond(id, { ok = ok, error = err })
  else
    respond(id, { ok = false, error = 'неизвестная операция: ' .. real_tostring(op) })
  end
end

write_line({ op = 'ready', lua = _VERSION, harness = '1' })

while true do
  local line = real_io.stdin:read('*l')
  if not line then
    break
  end
  if line ~= '' then
    local msg = json.decode(line)
    if real_type(msg) ~= 'table' then
      real_io.stderr:write('harness: не разобрал строку от питона\n')
    else
      logs, actions, dropped = {}, {}, 0
      local ok, err = real_pcall(handle, msg)
      if not ok then
        respond(msg.id, { ok = false, error = 'сбой моста: ' .. real_tostring(err) })
      end
      logs, actions, dropped = {}, {}, 0
    end
  end
end
