<div align="center">

# 🚀 BLACK RUSSIA BOTNET & AI CONTROLLER

<p align="center">
  <b>Высокопроизводительный автономный клиент и ботнет для мобильного криминального мультиплеера BLACK RUSSIA (CRMP)</b>
</p>

[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.14-blue?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Termux%20%7C%20Linux%20%7C%20Android%20%7C%20macOS-orange?style=for-the-badge&logo=linux&logoColor=white)](https://termux.dev/)
[![Black Russia](https://img.shields.io/badge/Black%20Russia-CRMP%20RakNet-red?style=for-the-badge)](https://blackrussia.online/)
[![News Channel](https://img.shields.io/badge/Telegram_Канал-@raknetbrnews1-0088cc?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/raknetbrnews1)

<br>

<p align="center">
  <a href="https://t.me/raknetbrnews1">
    <img src="https://img.shields.io/badge/📢_Наш_Канал_Обновлений-@raknetbrnews1-0088cc?style=for-the-badge&logo=telegram&logoColor=white" alt="Наш Telegram Канал"/>
  </a>
  <a href="https://blackrussia.online/">
    <img src="https://img.shields.io/badge/🌐_Официальный_Сайт-blackrussia.online-E0584B?style=for-the-badge&logo=googlechrome&logoColor=white" alt="Официальный Сайт"/>
  </a>
  <a href="https://play.google.com/store/apps/details?id=com.launcher.brgame">
    <img src="https://img.shields.io/badge/Google_Play-Скачать_Игру-34A853?style=for-the-badge&logo=googleplay&logoColor=white" alt="Google Play"/>
  </a>
  <a href="https://apps.apple.com/ru/app/black-russia/id1644102824">
    <img src="https://img.shields.io/badge/App_Store-iOS_Версия-000000?style=for-the-badge&logo=apple&logoColor=white" alt="App Store"/>
  </a>
</p>

</div>

---

## 📢 Канал обновлений проекта
Все самые свежие новости разработки, патчи протокола RakNet, обходы античитов и готовые базы публикуются в нашем официальном Telegram-канале:
👉 **[@raknetbrnews1](https://t.me/raknetbrnews1)** — обязательно подписывайся!

---

## 🛠 Подробная инструкция по установке

### 📱 Вариант 1: Установка в Termux (Android)

1. **Обновите пакеты Termux:**
   ```bash
   pkg update -y && pkg upgrade -y
   ```

2. **Установите Git и Python:**
   ```bash
   pkg install git python -y
   ```

3. **Склонируйте репозиторий проекта:**
   ```bash
   git clone https://github.com/creepyside11/black.git
   cd black
   ```

4. **Установите необходимые зависимости:**
   ```bash
   pip install pyTelegramBotAPI requests urllib3 certifi
   ```

5. **Создайте глобальную команду быстрого запуска `br`:**
   ```bash
   cat << 'EOF' > $PREFIX/bin/br
   #!/data/data/com.termux/files/usr/bin/bash
   cd ~/black || cd /data/data/com.termux/files/home/blackrussia || exit 1
   exec python3 bot.py "$@"
   EOF
   chmod +x $PREFIX/bin/br
   ```

6. **Готово! Запустите бота любой удобной командой:**
   ```bash
   br
   ```

---

### 💻 Вариант 2: Установка на Linux / VPS (Ubuntu, Debian)

1. **Обновите систему и установите Python + Git:**
   ```bash
   sudo apt update && sudo apt install -y git python3 python3-pip
   ```

2. **Склонируйте репозиторий:**
   ```bash
   git clone https://github.com/creepyside11/black.git
   cd black
   ```

3. **Установите зависимости:**
   ```bash
   pip3 install pyTelegramBotAPI requests urllib3 certifi
   ```

4. **Создайте алиас `br` для всей системы:**
   ```bash
   sudo ln -sf $(pwd)/br /usr/local/bin/br 2>/dev/null || alias br="python3 $(pwd)/bot.py"
   ```

5. **Запуск:**
   ```bash
   python3 bot.py
   ```

---

## ✨ Полный обзор всех функций ботнета

### 🧠 1. Автономный Нейроконтроллер (ИИ Командир)
- **Любые модели LLM**: Поддержка OpenAI (`gpt-4o-mini`, `gpt-4o`), DeepSeek (`deepseek-chat`), Anthropic Claude, Qwen, Ollama, OpenRouter, vLLM.
- **Интерактивная настройка через Telegram**: Изменение `Base URL`, `API Token` и `Model ID` в несколько нажатий прямо из меню бота.
- **Естественный язык**: Командир понимает свободную русскую речь и мгновенно выполняет действия:
  - *"Запусти 10 ботов на сервер Воронеж качать лвл"*
  - *"Останови всех ботов кроме 12 и 15"*
  - *"Раскидай аккаунты по случайным серверам и включи мониторинг"*
  - *"Проверь аккаунты на баны и выдай статистику"*
- **Автономный мониторинг 24/7**: При активации команды `monitor_24_7` ИИ непрерывно сканирует состояние фермы, поднимает упавшие инстансы и переподключает их после рестартов серверов.

---

### 🛡 2. Детект банов и мгновенная автозамена аккаунтов
- **Глубокий анализ причин блокировок**:
  - Распознавание киков и банов античита: `читы`, `постороннее ПО`, `кликеры`, `ботоводство`, `ППВ`, пакет `CONNECTION_BANNED (0x19)`.
  - Отсеивание временных блокировок за частые подключения (15–20 минут) от перманентных банов.
- **Мгновенная автозамена забаненного бота**:
  - Если сервер блокирует бота, система не останавливает ферму, а **автоматически регистрирует нового бота с реалистичным ником на том же сервере**, сохраняет в `accounts.json` и сразу запускает выполнять ту же задачу!
- **Анализ через ИИ**: Подробный отчет с причинами бана и советами отправляется владельцу в Telegram.

---

### 📈 3. Умная качка LVL (`LevelGrinderTask`)
- **Спавн на реальном городском вокзале**:
  - Метод `_ensure_spawn_in_real_world` гарантирует появление персонажа на открытом спавне с реальными игроками, исключая зависание под текстурами в спектейт-зоне.
- **Человеческая рандомизация простоя**:
  - Минимальное время покоя: **20 секунд**.
  - Максимальное время покоя: **9 минут** (540 секунд, строгий запас до серверного AFK-кика в 15 минут).
  - Точность рандомизации вплоть до миллисекунд (`random.uniform(20.0, 540.0)`).
- **Микро-поведение человека**:
  - Случайные повороты головы и угол камеры.
  - Микро-шаги на 1-2 метра вперед/назад, случайные приседания и прыжки.
  - Детект игроков рядом: если в радиусе 9 метров подходит живой человек, бот замирает и прекращает дергаться.
- **Автоответчик администраторам и игрокам**:
  - Автоматически ловит вопросы в `/pm`, личные сообщения и обращения в общем чате (*"ты тут?"*, *"вы тут?"*, *"бот?"*, *"ку"*).
  - Выдерживает реалистичную человеческую задержку печати (2-4 секунды) и отвечает в чат (`"Да, я тут"`, `"тут я, не афк"`, `"/b тут"`).
- **Бессмертие от голода**: Для аккаунтов 1 уровня поддерживается 100 HP, предотвращая смерть от голода до первого PayDay.
- **Авто-сбор бонусов**: Каждые 30 минут автоматически забирает бесплатные награды из донат-меню и ежедневную рулетку.

---

### ⛏ 4. Квесты Дяди Славы & Шахта (`QuestMasterTask`)
- **Полная сюжетная линия новичка**:
  - Автоматический скип заставки и диалогов со Славой на вокзале.
  - Покупка еды в уличном киоске, посадка в автомобиль, поездка в 24/7 за аптечкой.
  - Стайлинг-центр, АЗС, ремкомплект, прием звонков от Дяди Славы.
  - Поездка на шахту, переодевание и мини-игра добычи руды (попадание в зеленую зону).
  - Возврат на вокзал, забор кейса новичка в донате и финальный диалог с получением прав категории B и купона x2.

---

### 🌐 5. Прокси и мульти-серверность
- Поддержка индивидуальных и общих прокси `SOCKS5` / `HTTP(S)`.
- Поддержка всех официальных серверов Black Russia (более 70 серверов в актуальной базе `br_servers.py`).
- Массовая генерация аккаунтов под выбранный сервер в одну команду.

---

## 🎮 Консольные команды

| Команда | Описание |
|---|---|
| `br` | Запустить ботнет из любого места терминала |
| `/tg <token> [admin_id]` | Запустить Telegram-контроллер на лету |
| `/startall` | Запустить всех ботов из базы |
| `/stopall` | Остановить всех активных ботов |
| `/start <id>` | Запустить бота по ID (например `/start 12` или `/start 1,2,5`) |
| `/stop <id>` | Остановить бота по ID |
| `/batch <сервер> <кол-во> <пароль>` | Массовая генерация новых аккаунтов |
| `/task <lvl\|quest\|mine\|stop> [id]` | Назначить задачу боту |
| `/list` | Показать список ботов, уровни, баланс и статусы |
| `/menu` | Интерактивное меню с нумерацией пунктов |
| `/quit` | Завершить работу всех ботов и выйти |

---

## 📱 Официальные ссылки игры Black Russia

- 🌐 [Официальный сайт проекта](https://blackrussia.online/)
- 📱 [Скачать лаунчер для Android (Google Play)](https://play.google.com/store/apps/details?id=com.launcher.brgame)
- 🍏 [Скачать клиент для iOS (App Store)](https://apps.apple.com/ru/app/black-russia/id1644102824)
- 💬 [Официальный Telegram-канал разработчиков игры](https://t.me/blackrussia.online)

---

<div align="center">
  <b>Канал обновлений RakNet: <a href="https://t.me/raknetbrnews1">@raknetbrnews1</a></b><br>
  <sub>Разработано для образовательных целей и тестирования сетевых протоколов CRMP.</sub>
</div>
