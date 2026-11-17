-- Диагностика дополнением: текст диалогов и чата намеренно не записывается.
function on_load()
  br.log('Arizona: наблюдение за событиями; ответы не отправляются')
end
function on_spawn_menu() br.log('Arizona: получено событие меню спавна') end
function on_dialog(e)
  br.log('Arizona: диалог id=' .. tostring(e.id) .. ', тип=' .. tostring(e.style))
end
function on_checkpoint(e)
  br.log('Arizona: получена метка')
end
function on_checkpoint_clear() br.log('Arizona: метка удалена') end
