"""Кейсы и рулетка: разбор протокола и пробник доверия сервера.

Что известно из кода клиента (samp/interface_builders.py, samp/interface_parsers.py):

    Интерфейс 73 (CASES)
      сервер -> клиент  {"t":1,"cc":[{"id":<case>,"cot":<сколько>}]}   список кейсов
      клиент -> сервер  {"t":2,"cs":<case>,"type":0|1}                  открыть
      сервер -> клиент  {"t":2,"pr":[<prize_id>, ...]}                  что выпало
      клиент -> сервер  {"t":3,"bt1":[<prize_id>, ...]}                 ЗАБРАТЬ ЭТИ id
      клиент -> сервер  {"t":4,"d":1}                                   закрыть

    Интерфейс 74 (REWARD)
      сервер -> клиент  {"pr":[{"id":..,"n":..}, ...]}                  доступные награды
      клиент -> сервер  {"t":4,"id":<reward_id>,"s":1}                  ЗАБРАТЬ ЭТОТ id

    Интерфейс 22 (DONATE_MENU)
      клиент -> сервер  {"t":2}                                         бесплатная рулетка

Вывод: сам бросок делает сервер. Но в двух местах id приза приходит ОТ КЛИЕНТА:
`bt1` при сборе из кейса и `id` при получении награды. Если сервер не сверяет
этот список с тем, что реально выдал, — можно забрать чужой приз.

Этот модуль ничего не предполагает. Он делает ровно один запрос с подменённым
id и записывает, что ответил сервер. По ответу видно, есть ли проверка.
"""

from __future__ import annotations

import time

CASES_INTERFACE = 73
REWARD_INTERFACE = 74
DONATE_INTERFACE = 22


class CaseSession:
    """Состояние одного захода в кейсы, собранное из трафика сервера."""

    def __init__(self):
        self.cases = []          # [{'case_id': int, 'count': int}]
        self.last_prizes = []    # id, которые сервер реально выдал
        self.last_raw = None
        self.opened_case = None

    def __repr__(self):
        return (f"CaseSession(cases={len(self.cases)}, "
                f"last_prizes={self.last_prizes})")


class CaseProbe:
    """Слушает интерфейс кейсов и умеет отправить подменённый сбор приза.

    Ничего не делает сам по себе: только реагирует на вызовы. Так его можно
    подключить к живой сессии и не мешать остальной логике.
    """

    def __init__(self, bot, *, log=None):
        self.bot = bot
        self.state = CaseSession()
        self._log = log or (lambda m: None)
        self.attempts = []

    # ---------- приём трафика ----------

    def on_payload(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get('type') != 'interface_sync':
            return

        iid = payload.get('interface_id')
        data = payload.get('json') or {}

        if iid == CASES_INTERFACE:
            self._on_cases(payload, data)
        elif iid == REWARD_INTERFACE:
            self._on_reward(payload, data)

    def _on_cases(self, payload, data):
        state = payload.get('state')
        if state == 'case_menu_open':
            self.state.cases = list(payload.get('available_cases') or [])
            self._log(f"[CASES] доступно кейсов: {len(self.state.cases)}")
            for c in self.state.cases[:10]:
                self._log(f"    id={c['case_id']} остаток={c['count']}")
        elif state == 'case_reward_open':
            self.state.last_prizes = list(payload.get('prize_ids') or [])
            self.state.last_raw = data
            self._log(f"[CASES] сервер выдал призы: {self.state.last_prizes}")

    def _on_reward(self, payload, data):
        rewards = payload.get('rewards')
        if isinstance(rewards, list) and rewards:
            self._log(f"[REWARD] доступно наград: {len(rewards)}")
            for r in rewards[:10]:
                self._log(f"    id={r.get('id')} name={r.get('name')!r}")

    # ---------- действия ----------

    def open_case(self, case_id: int, *, skip_animation: bool = True) -> bool:
        """Обычное открытие. Результат всё равно решает сервер."""
        self.state.opened_case = int(case_id)
        ok = self.bot.api.CaseOpen(int(case_id), skip_animation=skip_animation)
        self._log(f"[CASES] открываю кейс {case_id} -> {ok}")
        return bool(ok)

    def collect_honest(self) -> bool:
        """Забрать ровно то, что выдал сервер. Контрольный случай."""
        if not self.state.last_prizes:
            self._log("[CASES] нечего забирать: сервер ещё не выдал призы")
            return False
        return self._send_collect(self.state.last_prizes, label="honest")

    def collect_tampered(self, prize_ids) -> bool:
        """Забрать произвольные id. Вот здесь и проверяется доверие сервера."""
        return self._send_collect([int(x) for x in prize_ids], label="tampered")

    def _send_collect(self, prize_ids, *, label):
        payload = {'t': 3, 'bt1': [int(x) for x in prize_ids]}
        attempt = {
            'when': time.time(),
            'label': label,
            'sent': list(payload['bt1']),
            'server_gave': list(self.state.last_prizes),
            'accepted': None,
        }
        self.attempts.append(attempt)
        self._log(f"[CASES] отправляю t=3 ({label}): {payload['bt1']}")
        ok = self.bot.api.send_json(CASES_INTERFACE, payload)
        attempt['accepted'] = bool(ok)
        if not ok:
            self._log("[CASES] отправка не удалась")
        return bool(ok)

    # ---------- отчёт ----------

    def report(self) -> str:
        if not self.attempts:
            return "попыток не было"
        lines = []
        for a in self.attempts:
            sent = a['sent']
            gave = a['server_gave']
            same = sent == gave
            lines.append(
                f"  {a['label']:9} отправлено={sent} сервер_выдал={gave} "
                f"{'совпадает' if same else 'ПОДМЕНА'}"
            )
        return "\n".join(lines)


def plan_attack(case_session: CaseSession, *, target_id: int | None = None) -> list[dict]:
    """Готовит последовательность проб, не отправляя их.

    Идея: сравнить, как сервер реагирует на честный сбор и на подменённый.
    Если на подмену приходит тот же ответ, что на честный, — проверки нет.
    """
    if not case_session.last_prizes:
        return []

    honest = list(case_session.last_prizes)
    plan = [{'t': 3, 'bt1': honest, 'note': 'контроль: как выдал сервер'}]

    if target_id is not None:
        plan.append({'t': 3, 'bt1': [int(target_id)],
                     'note': 'один чужой id вместо выданного'})

    doubled = honest + honest
    plan.append({'t': 3, 'bt1': doubled, 'note': 'дубликат: приз дважды'})

    if len(honest) > 1:
        plan.append({'t': 3, 'bt1': honest[:-1], 'note': 'частичный сбор'})

    return plan
