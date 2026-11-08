import os
import sys
import time
import math
import re
import threading
from collections import deque

# Добавляем родительский каталог в sys.path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import br_servers
from core.network import Network
from core import security
from core.protocol import RakNetProtocol
from samp.api import BotAPI
from samp.session import SampSession
from samp.sync import PlayerSyncScheduler
from samp.packets import PacketBuilder
from logic.registration import RegistrationConfig
from logic.json_ui import JsonUIConfig
from logic.events import emitter

class InteractiveBot:
    def __init__(self, server_query: str, nickname: str, password: str, proxy: str = "", on_update_cb=None):
        self.server_query = server_query
        self.nickname = nickname.strip()
        self.password = password.strip()
        self.proxy = proxy.strip() if proxy else ""
        self.on_update_cb = on_update_cb

        # Поиск сервера
        server_info = br_servers.find_server(server_query)
        if not server_info:
            raise ValueError(f"Сервер '{server_query}' не найден!")

        self.server_name = server_info.get("name", server_query).upper()
        self.host = server_info.get("ip")
        self.port = int(server_info.get("port", 7777))

        self.status = "Остановлен"
        self.level_str = "-"
        self.money = 0
        self.is_spawned = False
        self.is_connected = False
        self.stopping = False

        self.chat_history = deque(maxlen=200)
        self.active_dialog = None  # {id, style, title, info, b1, b2, items: []}
        self.active_checkpoint = None  # (x, y, z, radius)
        self.target_checkpoint = None
        self.is_moving = False

        self.thread = None
        self.session = None
        self.api = None
        self.network = None
        self.protocol = None
        self.sync_scheduler = None
        self._lock = threading.Lock()

    def add_chat_line(self, line: str):
        with self._lock:
            self.chat_history.append(line)
        if self.on_update_cb:
            self.on_update_cb("chat", line)

    def log(self, line: str):
        self.add_chat_line(f"[*] {line}")

    def start(self):
        self.stopping = False
        self.status = "Подключение..."
        self.setup_session()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True

    def setup_session(self):
        self.network = Network(self.host, self.port)
        self.protocol = RakNetProtocol(self.network, security)
        self.sync_scheduler = PlayerSyncScheduler()

        reg_config = None
        if self.password:
            reg_config = RegistrationConfig(
                enabled=True,
                auto_login=True,
                password=self.password,
                gender=0,
                skin=78,
                step_delay=0.6,
                ack_timeout=15.0,
            )

        json_ui = JsonUIConfig(
            console_output=False,
            auto_close_cinematic=True,
        )

        self.session = SampSession(
            self.protocol,
            self.host,
            self.port,
            self.nickname,
            sync_scheduler=self.sync_scheduler,
            fps=60.0,
            pings_ips_continuous=False,
            server_password=None,
            http_pre_ping=True,
            http_pre_ping_port=80,
            http_pre_ping_timeout=5.0,
            http_pre_ping_host_suffix="blackrussia.online",
            registration=reg_config,
            json_ui=json_ui,
        )

        self.api = BotAPI(self.session)
        self.network.set_proxy_manager(self.session.proxy)

        if self.proxy:
            try:
                self.session.proxy.connect(self.proxy, timeout=5.0)
                self.log(f"Настроен SOCKS5 прокси: {self.proxy}")
            except Exception as e:
                self.log(f"Ошибка настройки прокси {self.proxy}: {e}")

        self._bind_events()

    def _bind_events(self):
        sess = self.session

        def on_connect(*args, **kwargs):
            self.is_connected = True
            self.status = "Авторизация..."
            self.log("Соединение с сервером установлено, авторизация...")

        def on_spawn(*args, **kwargs):
            self.is_spawned = True
            self.status = "В игре"
            self.log("Успешный спавн в игре!")
            # Запрашиваем статистику
            threading.Thread(target=self._initial_stats, daemon=True).start()

        def on_chat(bot_obj, message, *args, **kwargs):
            clean_msg = re.sub(r'\{[0-9a-fA-F]{6}\}', '', str(message)).strip()
            self.add_chat_line(f"[ЧАТ] {clean_msg}")

            clean_lower = clean_msg.lower()
            if "баланс" in clean_lower or "на руках" in clean_lower:
                m = re.findall(r'(?:баланс|деньги|наличные|на руках)[^\d\n\r]{0,15}?([\d\s,]+)', clean_msg, re.IGNORECASE)
                if m:
                    for val in m:
                        raw = re.sub(r'[^\d]', '', val)
                        if raw:
                            self.money = int(raw)
                            break

        def on_dialog(bot_obj, d_id, style, title, info, b1, b2, *args, **kwargs):
            clean_title = re.sub(r'\{[0-9a-fA-F]{6}\}', '', str(title or "")).strip()
            clean_info = re.sub(r'\{[0-9a-fA-F]{6}\}', '', str(info or "")).strip()
            b1 = str(b1 or "").strip()
            b2 = str(b2 or "").strip()

            items = []
            lines = [ln.strip() for ln in clean_info.split("\n") if ln.strip()]
            for idx, line in enumerate(lines):
                col_text = re.sub(r'\t+', ' | ', line)
                items.append((idx, col_text))

            with self._lock:
                self.active_dialog = {
                    "id": int(d_id),
                    "style": int(style),
                    "title": clean_title,
                    "info": clean_info,
                    "b1": b1,
                    "b2": b2,
                    "items": items
                }

            self.log(f"[ДИАЛОГ #{d_id}] {clean_title} | Кнопки: [{b1}] [{b2}]")

            diag_text = f"{clean_title} {clean_info}".lower()
            if "статистика" in diag_text or "уровень" in diag_text or "exp" in diag_text:
                lvl_m = re.search(r'(?:уровень|level|lvl)[:\s]+(\d+)', clean_info, re.IGNORECASE)
                exp_m = re.search(r'(?:exp|опыт|очки опыта|очков)[:\s]+(\d+)\s*[\/из]\s*(\d+)', clean_info, re.IGNORECASE)
                if lvl_m:
                    found_lvl = lvl_m.group(1)
                    if exp_m:
                        self.level_str = f"{found_lvl} ({exp_m.group(1)}/{exp_m.group(2)})"
                    else:
                        self.level_str = f"{found_lvl} lvl"
                    if self.on_update_cb:
                        self.on_update_cb("stats", None)

                money_matches = re.findall(r'(?:баланс|деньги|наличные|рублей|на руках|основной счет|банк(?:\s+счет)?)[^\d\n\r]{0,15}?([\d\s,]+)', clean_info, re.IGNORECASE)
                if money_matches:
                    for mm in money_matches:
                        raw_m = re.sub(r'[^\d]', '', mm)
                        if raw_m and int(raw_m) > 0:
                            self.money = int(raw_m)
                            if self.on_update_cb:
                                self.on_update_cb("stats", None)
                            break

            if self.on_update_cb:
                self.on_update_cb("dialog", self.active_dialog)

        def on_set_checkpoint(bot_obj, x, y, z, radius, *args, **kwargs):
            self.active_checkpoint = (float(x), float(y), float(z), float(radius))
            self.log(f"[GPS/МЕТКА] Сервер выставил метку: X={x:.1f}, Y={y:.1f}, Z={z:.1f} (радиус: {radius})")
            if self.on_update_cb:
                self.on_update_cb("checkpoint", self.active_checkpoint)

        def on_disable_checkpoint(*args, **kwargs):
            self.active_checkpoint = None
            self.log("[GPS/МЕТКА] Метка снята сервером.")
            if self.on_update_cb:
                self.on_update_cb("checkpoint", None)

        def on_set_money(bot_obj, amount, *args, **kwargs):
            try:
                self.money = int(amount)
                if self.on_update_cb:
                    self.on_update_cb("stats", None)
            except Exception:
                pass

        sess._bind_callback("onconnect", on_connect)
        sess._bind_callback("onSpawn", on_spawn)
        emitter.on("onShowChat")(on_chat)
        emitter.on("onShowDialog")(on_dialog)
        emitter.on("OnSetCheckpoint")(on_set_checkpoint)
        emitter.on("OnDisableCheckpoint")(on_disable_checkpoint)
        emitter.on("OnSetMoney")(on_set_money)

    def _run(self):
        try:
            self.log(f"Подключение к {self.server_name} ({self.host}:{self.port}) ник: {self.nickname}...")
            connected = self.api.connect()
            if not connected and not self.stopping:
                self.log("Сервер не ответил на подключение.")
                self.status = "Ошибка подключения"
        except Exception as e:
            if not self.stopping:
                self.status = f"Ошибка: {e}"
                self.log(f"Критическая ошибка сессии: {e}")

    def stop(self):
        self.stopping = True
        self.status = "Остановлен"
        try:
            if self.session:
                self.session.disconnect()
        except Exception:
            pass
        try:
            if self.network:
                self.network.close()
        except Exception:
            pass

    def _initial_stats(self):
        time.sleep(3.0)
        if self.is_spawned:
            self.send_command("/mm")

    def send_command(self, cmd: str):
        if not self.session or not self.session.connected:
            self.log("Бот не в игре!")
            return False
        clean_cmd = cmd.strip()
        self.log(f"Отправка команды: {clean_cmd}")
        self.api.sendchat(clean_cmd)
        return True

    def send_dialog_response(self, response: int, list_item: int = 0, input_text: str = ""):
        if not self.session or not self.session.connected or not self.active_dialog:
            return False

        d_id = self.active_dialog.get("id", 0)
        self.log(f"Ответ диалогу #{d_id}: кнопка={response}, пункт={list_item}, текст='{input_text}'")
        try:
            self.session._sender.send(
                PacketBuilder.rpc_build("dialog_response", int(d_id), int(response), int(list_item), str(input_text)),
                rpc_name="dialog_response",
            )
            with self._lock:
                self.active_dialog = None
            if self.on_update_cb:
                self.on_update_cb("dialog", None)
            return True
        except Exception as e:
            self.log(f"Ошибка отправки ответа диалогу: {e}")
            return False

    def close_dialog(self):
        return self.send_dialog_response(response=0, list_item=0, input_text="")

    def get_position(self):
        if self.session and hasattr(self.session, "players") and hasattr(self.session.players, "bot"):
            return self.session.players.bot.get_position()
        return (0.0, 0.0, 0.0)

    def is_in_vehicle(self):
        if self.session and hasattr(self.session, "players") and hasattr(self.session.players, "bot"):
            return bool(self.session.players.bot.is_in_vehicle())
        return False

    def get_nearby_pickups(self, radius: float = 60.0):
        if not self.session or not hasattr(self.session, "stream"):
            return []
        pickups = self.session.stream.get_all_pickups()
        if not pickups:
            return []

        cur_x, cur_y, cur_z = self.get_position()
        result = []
        for p in pickups:
            px = float(p.get("x", 0.0))
            py = float(p.get("y", 0.0))
            pz = float(p.get("z", 0.0))
            pid = int(p.get("pickup_id", -1))
            dist = math.sqrt((cur_x - px) ** 2 + (cur_y - py) ** 2 + (cur_z - pz) ** 2)
            if dist <= radius:
                result.append({
                    "id": pid,
                    "model": int(p.get("model_id", 0)),
                    "x": px,
                    "y": py,
                    "z": pz,
                    "dist": round(dist, 1)
                })
        result.sort(key=lambda x: x["dist"])
        return result

    def navigate_to(self, target_x: float, target_y: float, target_z: float, radius: float = 1.5, on_finish=None):
        if not self.session or not self.is_spawned:
            self.log("Бот не в игре для перемещения!")
            return False

        if self.is_moving:
            self.stop_movement()

        self.is_moving = True
        self.target_checkpoint = (target_x, target_y, target_z, radius)

        def _worker():
            try:
                is_veh = self.is_in_vehicle()
                mode = "vehicle" if is_veh else "walk"
                self.log(f"Начало движения к ({target_x:.1f}, {target_y:.1f}, {target_z:.1f}) [режим: {mode}]...")

                self.api.MoveToCoord(
                    target_x, target_y, target_z,
                    mode=mode,
                    coord_delay=0.035 if not is_veh else 0.05,
                    accel_time=1.0,
                    brake_time=1.0,
                    completion_distance=max(1.0, radius * 0.8)
                )

                while self.is_moving and not self.stopping:
                    cur_x, cur_y, cur_z = self.get_position()
                    dist = math.sqrt((cur_x - target_x)**2 + (cur_y - target_y)**2 + (cur_z - target_z)**2)
                    if dist <= max(1.5, radius):
                        self.log(f"Прибыли к цели ({target_x:.1f}, {target_y:.1f}, {target_z:.1f})!")
                        break
                    time.sleep(0.5)

                if on_finish:
                    on_finish()

            except Exception as e:
                self.log(f"Ошибка навигации: {e}")
            finally:
                self.is_moving = False
                if self.on_update_cb:
                    self.on_update_cb("nav_finish", None)

        threading.Thread(target=_worker, daemon=True).start()
        return True

    def stop_movement(self):
        self.is_moving = False
        try:
            self.api.StopMoveToCoord()
        except Exception:
            pass

    def interact_with_pickup(self, pickup_id: int):
        pickups = self.get_nearby_pickups(radius=100.0)
        target = None
        for p in pickups:
            if p["id"] == pickup_id:
                target = p
                break
        if not target:
            self.log(f"Пикап #{pickup_id} не найден поблизости.")
            return False

        def _on_arrive():
            time.sleep(0.5)
            self.log(f"Подбор пикапа #{pickup_id}...")
            self.api.SendPickup(pickup_id)
            time.sleep(0.3)
            self.api.SetKey(keys=1)
            time.sleep(0.3)
            self.api.SetKey(keys=0)

        self.log(f"Идем к пикапу #{pickup_id} (дистанция {target['dist']}м)...")
        return self.navigate_to(target["x"], target["y"], target["z"], radius=1.0, on_finish=_on_arrive)
