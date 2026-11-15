-- Пример для записи шахты. Тайминги требуют проверки на текущем сервере.
local current_key = nil
local generation = 0
function on_json(e)
  local d = e.data
  if e.interface_id == 26 and d and d.o == 1 then
    br.solve_captcha(d)
  elseif e.interface_id == 31 and d then
    if d.c == 1 then
      current_key = nil
      generation = generation + 1
    elseif d.o == 1 and d.g == 2 then
      generation = generation + 1
    elseif d.o == 1 and d.g == 1 and d.p_title
        and string.find(d.p_title, 'Раскопка', 1, true) then
      local key = tostring(d.p_start) .. ':' .. tostring(d.p_max)
      if current_key == key then return end
      current_key = key
      generation = generation + 1
      br.after(6.83, 'on_answer_mine_slider', {version=generation})
    end
  end
end
function on_answer_mine_slider(e)
  if e.version ~= generation then return end
  br.send_json(31, {t=1})
  br.send_json(31, {s=1})
end
