-- Диагностика дополнением: не выполняет вход и не отправляет игровые команды.
function on_load()
  br.log('Arizona: диагностика запущена; вход на сервер этим скриптом не реализован')
  br.timer_start('status', 10, 'report_status', nil, 60)
end
function report_status()
  br.log('Arizona: состояние соединения=' .. tostring(br.connected()))
end
function on_connect() br.log('Arizona: событие соединения') end
function on_disconnect() br.log('Arizona: событие отключения') end
function on_spawn() br.log('Arizona: получено событие спавна') end
