-- Дополнение: выводит позицию и сообщения о появлении персонажа.
function on_load(e)
  br.log('Lua-дополнение запущено. bot=' .. tostring(br.bot_id()))
  br.after(2, 'on_show_position', {})
end
function on_show_position(e)
  local p = br.pos()
  if p then br.log(('Позиция %.1f %.1f %.1f'):format(p.x, p.y, p.z))
  else br.log('Позиция пока недоступна') end
  br.after(15, 'on_show_position', {})
end
function on_spawn(e) br.log('Получено событие спавна') end
