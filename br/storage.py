import os
import json
import threading

DEFAULT_ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")

class AccountStorage:
    def __init__(self, filepath=DEFAULT_ACCOUNTS_FILE):
        self.filepath = filepath
        self._lock = threading.RLock()
        self.accounts = []
        self.load()

    def load(self):
        with self._lock:
            if not os.path.exists(self.filepath):
                self.accounts = []
                return
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.accounts = json.load(f)
                if not isinstance(self.accounts, list):
                    self.accounts = []
            except Exception:
                self.accounts = []

    def save(self):
        with self._lock:
            try:
                with open(self.filepath, "w", encoding="utf-8") as f:
                    json.dump(self.accounts, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[!] Ошибка сохранения аккаунтов: {e}")

    def list_accounts(self):
        with self._lock:
            return list(self.accounts)

    def get_account(self, index: int):
        with self._lock:
            if 0 <= index < len(self.accounts):
                return dict(self.accounts[index])
            return None

    def add_account(self, server: str, nickname: str, password: str):
        with self._lock:
            # Проверяем, существует ли уже
            for acc in self.accounts:
                if acc.get("nickname", "").lower() == nickname.lower() and acc.get("server", "").lower() == server.lower():
                    acc["password"] = password
                    self.save()
                    return acc
            new_acc = {
                "server": server.upper(),
                "nickname": nickname,
                "password": password,
                "lvl": "-",
                "money": 0,
                "created_at": None
            }
            self.accounts.append(new_acc)
            self.save()
            return new_acc

    def update_password(self, index: int, new_password: str):
        with self._lock:
            if 0 <= index < len(self.accounts):
                self.accounts[index]["password"] = new_password
                self.save()
                return True
            return False

    def update_stats(self, nickname: str, server: str, lvl: str = None, money: int = None):
        with self._lock:
            updated = False
            for acc in self.accounts:
                if acc.get("nickname", "").lower() == nickname.lower() and acc.get("server", "").lower() == server.lower():
                    if lvl is not None:
                        acc["lvl"] = str(lvl)
                    if money is not None:
                        acc["money"] = int(money)
                    updated = True
            if updated:
                self.save()

    def delete_account(self, index: int):
        with self._lock:
            if 0 <= index < len(self.accounts):
                removed = self.accounts.pop(index)
                self.save()
                return removed
            return None
