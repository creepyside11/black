-- Самостоятельный бот: стандартная авторизация проекта остаётся включённой.
-- Запуск: /lua bot ID spawn_bot.lua
function on_load(e)
  br.log('Жду меню выбора спавна; авторизацию выполняет программа')
end
function on_spawn_menu(e)
  local options = e.data and e.data.m
  if type(options) ~= 'table' or type(options[1]) ~= 'number' then return end
  br.log('Получено меню, выбираю первый доступный ID: ' .. tostring(options[1]))
  br.after(math.random(16, 34) / 10, 'on_choose_spawn', {id=options[1]})
end
function on_choose_spawn(e) br.spawn_select(e.id) end
function on_spawn(e) br.log('Событие спавна пришло от клиента') end
function on_json(e)
  if e.interface_id == 76 and e.data and e.data.o == 1 then
    br.log('Закрываю заставку при входе')
    br.send_json(76, {t=1}, 'interface_cinematic_stop')
  elseif e.interface_id == 26 and e.data and e.data.o == 1 then
    br.log('Получена капча; запускаю подключённый решатель')
    br.solve_captcha(e.data)
  end
end
