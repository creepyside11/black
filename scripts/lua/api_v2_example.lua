-- Пример дополнения: не отправляет в игровой чат и не двигает бота.
-- Сначала запускайте через test_lua_scripts и в режиме --dry.
function on_load()
  br.log('API v' .. tostring(br.api_version) .. ': пример запущен')
  local prev = br.storage_get('runs', 0)
  if type(prev) ~= 'number' then prev = 0 end
  br.storage_set('runs', prev + 1)
  br.timer_start('status', 1, 'on_status_timer', {limit=3}, 2)
end

function on_status_timer(e)
  local a=br.account()
  local name=a and a.nickname or 'тестовый аккаунт'
  br.log('['..tostring(name)..'] проверка '..e.count..', связность='..tostring(br.connected()))
  if e.count >= e.data.limit then br.timer_cancel(e.timer_id) end
end

function on_checkpoint(e)
  local distance=br.distance(br.pos(), br.checkpoint())
  if distance then br.log(('До метки %.1f'):format(distance)) else br.log('Метка получена; позиция недоступна') end
end

function on_dialog(e)
  local index=br.find_dialog_row(e.text or '', 'Статистика', e.style, true)
  if index ~= nil then br.log('Строка статистики: '..index..' (без ответа)') end
end

function on_action_result(e)
  for _,r in ipairs(e.results or {}) do
    if r.status=='error' then br.log('Ошибка действия: '..tostring(r.kind)) end
  end
end
