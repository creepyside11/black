-- Дополнение: запустите только на подключённом аккаунте.
-- Начните с --dry: маршрут будет проверен, но движения не будет.
-- example.json содержит ВЫМЫШЛЕННЫЕ координаты: замените их перед реальным запуском.
function on_load(e)
  br.log('Пример маршрута загружен')
  br.route_start('example', 'walk', {timeout=120, stuck_timeout=30})
end

function on_route_start_point(e)
  br.log(('Началась точка %d из %d'):format(e.point, e.total))
end

function on_route_point(e)
  br.log(('Точка %d из %d достигнута'):format(e.point, e.total))
end

function on_route_end(e)
  br.log('Маршрут завершён контроллером движения')
end

function on_route_error(e)
  br.log('Маршрут остановлен: ' .. tostring(e.reason))
end

function on_route_dry(e)
  br.log('Холостая проверка маршрута: ' .. tostring(e.total) .. ' точек')
end
