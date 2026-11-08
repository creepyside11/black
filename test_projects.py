"""Проекты: реестры, списки серверов, разбор чужого формата.

Второй проект (Arizona RP) отличается от Black Russia всем, что можно
отличить: свой файл аккаунтов, свой кэш списка серверов, другой формат
ответа API, другой первый пакет входа. Эти тесты держат границы между
проектами — если они поедут, ферма одного проекта начнёт работать по
данным другого, а это тихая потеря аккаунтов, а не громкая ошибка.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import br_servers
import projects as projects_mod


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._file = os.path.join(self._dir, "projects.json")
        self._old_file = os.environ.get("BR_PROJECTS_FILE")
        self._old_project = os.environ.get("BR_PROJECT")
        os.environ["BR_PROJECTS_FILE"] = self._file
        os.environ.pop("BR_PROJECT", None)
        # Путь берётся из константы на импорте, поэтому переменной окружения
        # мало: подменяем саму константу, иначе тест писал бы в боевой
        # projects.json рядом с ботом.
        self._patch = mock.patch.object(projects_mod, "CONFIG_FILE", self._file)
        self._patch.start()
        projects_mod.reset_cache()

    def tearDown(self):
        self._patch.stop()
        for name, old in (("BR_PROJECTS_FILE", self._old_file),
                          ("BR_PROJECT", self._old_project)):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        projects_mod.reset_cache()

    def test_defaults_have_both_projects(self):
        keys = projects_mod.project_keys()
        self.assertIn("br", keys)
        self.assertIn("arz", keys)

    def test_each_project_has_own_registry(self):
        br = projects_mod.get_project("br")
        arz = projects_mod.get_project("arz")
        self.assertEqual(os.path.basename(br.accounts_path), "accounts.json")
        self.assertEqual(os.path.basename(arz.accounts_path), "accounts_arz.json")
        self.assertNotEqual(br.accounts_path, arz.accounts_path)
        self.assertNotEqual(br.servers_cache_path, arz.servers_cache_path)

    def test_handshake_differs(self):
        """У BR свой первый пакет, у Arizona — стандартный RakNet."""
        self.assertEqual(projects_mod.get_project("br").handshake, "br")
        self.assertEqual(projects_mod.get_project("arz").handshake, "raknet")

    def test_broken_handshake_falls_back_to_br(self):
        with open(self._file, "w", encoding="utf-8") as fh:
            json.dump({"projects": {"xx": {"title": "X", "handshake": "рактнет"}}}, fh)
        projects_mod.reset_cache()
        self.assertEqual(projects_mod.get_project("xx").handshake, "br")

    def test_aliases_resolve(self):
        for alias in ("br", "BR", "бр", "blackrussia", "блекраша"):
            self.assertEqual(projects_mod.resolve_key(alias), "br", alias)
        for alias in ("arz", "ARZ", "arizona", "Аризона", "аризона рп"):
            self.assertEqual(projects_mod.resolve_key(alias), "arz", alias)
        self.assertIsNone(projects_mod.resolve_key("такогонет"))

    def test_active_switch_persists(self):
        self.assertEqual(projects_mod.active_key(), "br")
        chosen = projects_mod.set_active("arizona")
        self.assertEqual(chosen.key, "arz")
        self.assertEqual(projects_mod.active_key(), "arz")
        # Выбор обязан пережить сброс кэша и перезапуск процесса, иначе
        # переключение в панели работало бы до первого рестарта.
        projects_mod.reset_cache()
        self.assertEqual(projects_mod.active_key(), "arz")
        with open(self._file, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["active"], "arz")

    def test_env_project_wins_over_file(self):
        projects_mod.set_active("br")
        os.environ["BR_PROJECT"] = "arz"
        projects_mod.reset_cache()
        self.assertEqual(projects_mod.active_key(), "arz")

    def test_clean_server_name_strips_prefix(self):
        arz = projects_mod.get_project("arz")
        self.assertEqual(
            arz.clean_server_name("ARIZONA RP | MOBILE I"), "MOBILE I")
        self.assertEqual(arz.clean_server_name("Mobile II"), "Mobile II")
        br = projects_mod.get_project("br")
        self.assertEqual(br.clean_server_name("BLACK RUSSIA | RED"), "RED")

    def test_unknown_project_falls_back_to_active(self):
        """Опечатка в команде не должна ронять запуск: берём активный."""
        self.assertIsNone(projects_mod.resolve_key("nope"))
        self.assertEqual(projects_mod.get_project("nope").key,
                         projects_mod.active_key())


class ProfileTests(unittest.TestCase):
    """Профиль проекта: id применяются и откатываются при переключении."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._old = os.environ.get("BR_PROJECTS_FILE")
        os.environ["BR_PROJECTS_FILE"] = os.path.join(self._dir, "projects.json")
        self._arz = os.path.join(self._dir, "arz.profile.json")
        self._br = os.path.join(self._dir, "br.profile.json")
        with open(self._arz, "w", encoding="utf-8") as fh:
            json.dump({"packets": {"AUTH_KEY": 12},
                       "rpc": {"CHAT": 25},
                       "raknet": {"version": 7, "magic": True}}, fh)
        with open(self._br, "w", encoding="utf-8") as fh:
            json.dump({}, fh)
        cfg = os.path.join(self._dir, "projects.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"active": "br", "projects": {
                "arz": {"rpc_profile": self._arz, "handshake": "raknet"},
                "br": {"rpc_profile": self._br},
            }}, fh)
        self._patch = mock.patch.object(projects_mod, "CONFIG_FILE", cfg)
        self._patch.start()
        projects_mod.reset_cache()

    def tearDown(self):
        self._patch.stop()
        if self._old is None:
            os.environ.pop("BR_PROJECTS_FILE", None)
        else:
            os.environ["BR_PROJECTS_FILE"] = self._old
        # Профиль правит общие константы пакета: возвращаем их на место,
        # иначе остальные тесты побегут на id от Arizona.
        projects_mod.apply_rpc_profile(projects_mod.get_project("br"))
        projects_mod.reset_cache()

    def test_profile_applies_ids(self):
        from samp import constants

        original_auth = constants.PacketID.AUTH_KEY
        original_chat = constants.RPC.CHAT
        report = projects_mod.apply_rpc_profile(projects_mod.get_project("arz"))
        self.assertEqual(report["applied"]["PacketID.AUTH_KEY"], 12)
        self.assertEqual(report["applied"]["RPC.CHAT"], 25)
        self.assertEqual(constants.PacketID.AUTH_KEY, 12)
        self.assertEqual(constants.RPC.CHAT, 25)

        # Возврат на BR обязан откатить чужой профиль: у BR профиль пустой,
        # и без отката id Arizona остались бы в силе на Black Russia.
        projects_mod.apply_rpc_profile(projects_mod.get_project("br"))
        self.assertEqual(constants.PacketID.AUTH_KEY, original_auth)
        self.assertEqual(constants.RPC.CHAT, original_chat)

    def test_raknet_options_from_profile(self):
        projects_mod.apply_rpc_profile(projects_mod.get_project("arz"))
        self.assertEqual(projects_mod.raknet_options(),
                         {"version": 7, "magic": True})
        projects_mod.apply_rpc_profile(projects_mod.get_project("br"))
        self.assertEqual(projects_mod.raknet_options(), {})

    def test_unknown_names_are_ignored(self):
        path = self._arz
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"rpc": {"ТАКОГО_НЕТ": 1}}, fh)
        projects_mod.reset_cache()
        report = projects_mod.apply_rpc_profile(projects_mod.get_project("arz"))
        self.assertEqual(report["applied"], {})


ARIZONA_PAYLOAD = {
    "arizona": [{"number": 7, "name": "PC One", "ip": "1.1.1.1", "port": 7777}],
    "arizonaMobile": [
        {"number": 3, "name": "Mobile III", "ip": "mobile3.arizona-rp.com",
         "port": 5125, "online": 612, "maxplayers": 1000, "password": 0},
        {"number": 2, "name": "Mobile II", "ip": "mobile2.arizona-rp.com",
         "port": 5125, "online": 515, "maxplayers": 1000, "password": 0},
    ],
}

BR_PAYLOAD = [
    {"id": 4, "firstname": "RED", "name": "BLACK RUSSIA | RED",
     "ip": "80.66.82.20", "port": 5125, "online": 300, "maxonline": 1000},
]


class NormalizeTests(unittest.TestCase):
    def test_walk_uses_path(self):
        self.assertEqual(br_servers._walk(ARIZONA_PAYLOAD, "arizonaMobile"),
                         ARIZONA_PAYLOAD["arizonaMobile"])
        self.assertIsNone(br_servers._walk(ARIZONA_PAYLOAD, "missing"))
        self.assertEqual(br_servers._walk(ARIZONA_PAYLOAD, ""), ARIZONA_PAYLOAD)

    def test_arizona_section_only(self):
        items = br_servers._normalize(ARIZONA_PAYLOAD, "arizonaMobile")
        names = [item["name"] for item in items]
        self.assertEqual(names, ["Mobile III", "Mobile II"])
        self.assertNotIn("PC One", names)

    def test_adapt_arizona_fields(self):
        items = br_servers._normalize(ARIZONA_PAYLOAD, "arizonaMobile")
        first = items[0]
        # number -> id: в списке Arizona нет поля id, а боту он нужен.
        self.assertEqual(int(first["id"]), 3)
        self.assertEqual(first["ip"], "mobile3.arizona-rp.com")
        self.assertEqual(first["port"], 5125)
        self.assertEqual(first["online"], 612)
        # maxplayers -> maxonline: панель и Telegram читают maxonline.
        self.assertEqual(first["maxonline"], 1000)
        # name -> firstname: по нему строится короткое имя сервера.
        self.assertEqual(first["firstname"], "Mobile III")

    def test_adapt_br_unchanged(self):
        items = br_servers._normalize(BR_PAYLOAD)
        self.assertEqual(int(items[0]["id"]), 4)
        self.assertEqual(items[0]["firstname"], "RED")
        self.assertEqual(items[0]["maxonline"], 1000)

    def test_disk_cache_is_per_project(self):
        self.assertNotEqual(os.path.basename(br_servers.cache_path("br")),
                            os.path.basename(br_servers.cache_path("arz")))


class IsolationTests(unittest.TestCase):
    """Данные одного проекта не должны подменять другой."""

    def test_api_urls_per_project(self):
        br_urls = " ".join(br_servers.api_urls("br"))
        self.assertIn("blackrussia", br_urls)
        # У Arizona источника API больше нет. Лончер отдавал mobile1/2/3 с
        # портом 5125, а игровой порт — 7777, и на 5125 хендшейка никто не
        # ждёт. Список теперь статический, из projects.py.
        self.assertEqual(br_servers.api_urls("arz"), [])
        self.assertTrue(br_servers.api_urls("br"))

    def test_static_servers_per_project(self):
        servers = br_servers.get_servers("arz")
        self.assertEqual(len(servers), 32)
        self.assertEqual(int(servers[0]["port"]), 7777)
        self.assertEqual(servers[0]["ip"], "185.169.134.3")
        self.assertEqual(servers[-1]["name"], "SPACE")
        # Номера из списка должны находиться: в панели их выбирают цифрой.
        self.assertEqual(br_servers.find_server("3", "arz")["name"], "SCOTTDALE")
        self.assertEqual(br_servers.find_server("scottdale", "arz")["id"], 3)

    def test_not_configured_message_points_to_projects_json(self):
        project = projects_mod.get_project("arz")
        text = str(br_servers._not_configured_error(project))
        self.assertIn("projects.json", text)
        self.assertIn("Arizona", text)

    def test_find_server_by_number_and_name(self):
        def fake_servers(project=None):
            return br_servers._normalize(ARIZONA_PAYLOAD, "arizonaMobile")

        with mock.patch.object(br_servers, "get_servers", fake_servers):
            by_number = br_servers.find_server("3", "arz")
            by_name = br_servers.find_server("mobile i", "arz")
            by_full = br_servers.find_server("Mobile III", "arz")
            missing = br_servers.find_server("такого нет", "arz")

        self.assertIsNotNone(by_number)
        self.assertEqual(by_number["ip"], "mobile3.arizona-rp.com")
        # "mobile i" — подстрока "Mobile III": поиск по части имени.
        self.assertIsNotNone(by_name)
        self.assertIsNotNone(by_full)
        self.assertIsNone(missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
