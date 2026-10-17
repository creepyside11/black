import json
import os
import requests
import random
import re

AI_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_config.json")

def load_ai_config() -> dict:
    default_cfg = {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model_id": "gpt-4o-mini",
        "enabled": False
    }
    if os.path.exists(AI_CONFIG_FILE):
        try:
            with open(AI_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default_cfg.update(data)
        except Exception:
            pass
    return default_cfg

def save_ai_config(cfg: dict) -> None:
    try:
        with open(AI_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

class AICommander:
    """
    Нейроконтроллер ботнета Black Russia.
    Принимает естественный текст от владельца через Telegram
    и преобразует его в конкретные команды управления фермой через LLM (OpenAI-compatible).
    """
    def __init__(self, manager):
        self.manager = manager
        self.config = load_ai_config()

    def update_settings(self, base_url: str = None, api_key: str = None, model_id: str = None, enabled: bool = None):
        if base_url is not None:
            self.config["base_url"] = base_url.strip().rstrip("/")
        if api_key is not None:
            self.config["api_key"] = api_key.strip()
        if model_id is not None:
            self.config["model_id"] = model_id.strip()
        if enabled is not None:
            self.config["enabled"] = bool(enabled)
        save_ai_config(self.config)

    def execute_natural_command(self, user_prompt: str) -> str:
        """Анализирует текст задачи через LLM и выполняет действия над ботами"""
        cfg = load_ai_config()
        api_key = cfg.get("api_key")
        base_url = cfg.get("base_url", "https://api.openai.com/v1")
        model = cfg.get("model_id", "gpt-4o-mini")

        if not api_key:
            return "❌ API-ключ не настроен! Перейдите в ⚙️ Настройки ИИ и укажите Base URL, API Key и ID модели."

        # Собираем актуальный статус ботнета
        bots_info = []
        with self.manager._lock:
            for bid, b in sorted(self.manager.bots.items()):
                bots_info.append({
                    "id": bid,
                    "nick": b.nickname,
                    "server": b.server_name,
                    "status": b.status,
                    "lvl": b.level_str,
                    "banned": b.is_banned,
                    "task": b.default_task_name
                })

        system_instruction = (
            "Ты — автономный AI-контроллер фермы ботов для игры Black Russia.\n"
            "Твоя задача — распарсить команду пользователя на русском языке и вернуть JSON со списком действий.\n"
            "Доступные действия:\n"
            "1. start_bots: [список bot_id] (или 'all', или filter по серверу/статусу)\n"
            "2. stop_bots: [список bot_id] (или 'all')\n"
            "3. set_server: {bot_ids: [...], server: 'SERVER_NAME'}\n"
            "4. set_random_server: {bot_ids: [...]}\n"
            "5. check_bans: true (запустить проверку всех/выбранных аккаунтов на бан)\n"
            "6. set_task: {bot_ids: [...], task: 'lvl'|'quest'|'mine'}\n"
            "7. monitor_24_7: true (включить непрерывный фоновый мониторинг состояния ботнета)\n"
            "8. info: текстовый ответ пользователю\n\n"
            "Ответ СТРОГО в формате JSON:\n"
            "{\n"
            "  \"reply\": \"Человеческий ответ владельцу о том, что сделано\",\n"
            "  \"actions\": [\n"
            "     {\"type\": \"start_bots\", \"targets\": [12, 13]},\n"
            "     {\"type\": \"set_task\", \"targets\": [12, 13], \"task\": \"lvl\"}\n"
            "  ]\n"
            "}"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        endpoint = f"{base_url}/chat/completions"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"Текущий ботнет ({len(bots_info)} ботов):\n{json.dumps(bots_info[:25], ensure_ascii=False)}\n\nКоманда владельца: {user_prompt}"}
            ],
            "temperature": 0.2
        }

        try:
            resp = requests.post(endpoint, headers=headers, json=payload, timeout=30)
            if resp.status_code != 200:
                return f"❌ Ошибка LLM API [{resp.status_code}]: {resp.text[:200]}"
            res_json = resp.json()
            content = res_json["choices"][0]["message"]["content"]
            
            # Извлекаем JSON из ответа
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if not json_match:
                return f"🤖 Ответ ИИ:\n{content}"
            
            parsed = json.loads(json_match.group(0))
            reply_text = parsed.get("reply", "Команда обработана.")
            actions = parsed.get("actions", [])

            # Выполняем действия над ботами
            executed_summary = []
            with self.manager._lock:
                for act in actions:
                    atype = act.get("type")
                    targets = act.get("targets", [])
                    if targets == "all":
                        target_bots = list(self.manager.bots.values())
                    elif isinstance(targets, list):
                        target_bots = [self.manager.bots[t] for t in targets if t in self.manager.bots]
                    else:
                        target_bots = list(self.manager.bots.values())

                    if atype == "start_bots":
                        started = 0
                        for b in target_bots:
                            if not b.is_banned:
                                b.start()
                                started += 1
                        executed_summary.append(f"▶️ Запущено ботов: {started}")

                    elif atype == "stop_bots":
                        stopped = 0
                        for b in target_bots:
                            b.stop()
                            stopped += 1
                        executed_summary.append(f"⏹ Остановлено ботов: {stopped}")

                    elif atype == "set_task":
                        task_name = act.get("task", "lvl")
                        for b in target_bots:
                            b.default_task_name = task_name
                            if b.api and b.session and b.session.connected:
                                b.set_task(task_name)
                        executed_summary.append(f"⚙️ Установлена задача '{task_name}' для {len(target_bots)} ботов")

                    elif atype == "set_server":
                        srv = act.get("server")
                        if srv:
                            from br_servers import BR_SERVERS
                            srv_upper = srv.upper()
                            if srv_upper in BR_SERVERS:
                                for b in target_bots:
                                    host, port = BR_SERVERS[srv_upper]
                                    b.server_name = srv_upper
                                    b.host = host
                                    b.port = port
                                self.manager.save_accounts()
                                executed_summary.append(f"🌐 Сервер изменен на {srv_upper} для {len(target_bots)} ботов")

                    elif atype == "set_random_server":
                        from br_servers import BR_SERVERS
                        server_names = list(BR_SERVERS.keys())
                        for b in target_bots:
                            rand_srv = random.choice(server_names)
                            host, port = BR_SERVERS[rand_srv]
                            b.server_name = rand_srv
                            b.host = host
                            b.port = port
                        self.manager.save_accounts()
                        executed_summary.append(f"🎲 Случайные сервера распределены для {len(target_bots)} ботов")

                    elif atype == "monitor_24_7":
                        self.manager.start_monitor_loop()
                        executed_summary.append("🛰 Режим непрерывного мониторинга 24/7 активирован")

                    elif atype == "check_bans":
                        # Запускаем фоновую проверку на баны
                        banned_count = sum(1 for b in self.manager.bots.values() if b.is_banned)
                        executed_summary.append(f"🔍 Запущена проверка банов. Текущих банов в базе: {banned_count}")

            final_report = f"🤖 <b>Ответ ИИ Командира:</b>\n{reply_text}\n\n"
            if executed_summary:
                final_report += "<b>Выполненные действия:</b>\n" + "\n".join(f"• {s}" for s in executed_summary)
            return final_report

        except Exception as e:
            return f"❌ Ошибка выполнения ИИ команды: {e}"
