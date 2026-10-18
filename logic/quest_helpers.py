"""Сборка помощников для сюжетки: диалоги НПС, ларёк, звонки, покраска.

Один диспетчер на все интерфейсы, чтобы в bot.py не расползались
обработчики по каждому iid. Всё, что приходит, проходит через on_json и
уходит тому помощнику, который это узнал.
"""

from __future__ import annotations

from . import calls as calls_mod
from . import npc_dialog as npc_mod
from . import paint as paint_mod
from . import shop as shop_mod

NPC_DIALOG_IID = 63
SHOP_IID = shop_mod.SHOP_INTERFACE
CALL_IID = calls_mod.CALL_INTERFACE


class QuestHelpers:
    def __init__(
        self,
        bot,
        *,
        log=None,
        answer_calls=True,
        story_calls_only=True,
        paint_iid=None,
    ):
        self.bot = bot
        self.log = log or (lambda msg: None)

        self.npc = npc_mod.NpcDialogFlow(bot, log=self.log)
        self.shop = shop_mod.ShopRunner(bot, log=self.log)
        self.calls = calls_mod.CallAnswerer(
            bot, log=self.log, story_only=story_calls_only
        )
        self.paint = paint_mod.PaintPicker(bot, log=self.log)
        if paint_iid is not None:
            self.paint.interface_id = int(paint_iid)

        self.answer_calls = bool(answer_calls)
        self.last_state = None
        self.seen_interfaces = {}

    # ---- приём -------------------------------------------------------

    def on_json(self, interface_id, payload):
        """Вернуть имя обработавшего помощника или None."""
        try:
            iid = int(interface_id)
        except (TypeError, ValueError):
            return None

        self.seen_interfaces[iid] = self.seen_interfaces.get(iid, 0) + 1

        if iid == NPC_DIALOG_IID:
            if self.npc.on_payload(payload):
                self.last_state = "npc"
                return "npc"
            if isinstance(payload, dict) and payload.get("c") == 1:
                self.npc.on_close()
                self.last_state = "npc_closed"
                return "npc"
            return None

        if iid == CALL_IID:
            if self.answer_calls and self.calls.on_payload(payload):
                self.last_state = "call"
                return "call"
            return None

        if iid == SHOP_IID:
            if self.shop.on_payload(payload):
                self.last_state = "shop"
                return "shop"
            return None

        # Палитра покраски: iid заранее неизвестен, узнаём по содержимому.
        if iid != NPC_DIALOG_IID and iid != CALL_IID:
            if self.paint.on_payload(payload):
                self.paint.interface_id = iid
                self.last_state = "paint"
                return "paint"

        return None

    def on_npc_dialog(self, name, text, buttons, model=None):
        """Событие уже разобранного диалога НПС (onNpcDialog)."""
        payload = {"o": 1, "n": name, "d": text, "m": model, "b": []}
        for index, button in enumerate(buttons or []):
            if not isinstance(button, dict):
                continue
            payload["b"].append(
                {
                    "bn": button.get("text") or button.get("name") or "",
                    "bk": button.get("key", index),
                    "bt": button.get("type"),
                }
            )
        if self.npc.on_payload(payload):
            self.last_state = "npc"
            return True
        return False

    # ---- действия ----------------------------------------------------

    def next(self):
        """Пролистать реплику НПС."""
        return self.npc.advance()

    def buy_food(self, name=None):
        """Купить еду в ларьке."""
        return self.shop.buy(name=name)

    def pick_black(self):
        """Выбрать чёрный цвет в покраске."""
        return self.paint.pick_black()

    def status(self):
        return {
            "npc": self.npc.status(),
            "shop": self.shop.status(),
            "calls": self.calls.status(),
            "paint": self.paint.status(),
            "last": self.last_state,
            "interfaces": dict(self.seen_interfaces),
        }
