function on_load(e)
  br.log('Полигон запущен; жду тестовые события')
end
function on_spawn(e)
  br.log('Получен тестовый спавн')
  br.after(2, 'on_test_ready', {message='таймер сработал'})
end
function on_test_ready(e)
  br.log(e.message)
  br.walk(100, 200, 15) -- имитируется, никуда не двигается
end
function on_chat(e)
  if e.message == 'Тестовое сообщение' then br.log('Чат обработан') end
end
