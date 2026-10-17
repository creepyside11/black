<div align="center">

# 🚀 BLACK RUSSIA BOTNET & AI CONTROLLER

<p align="center">
  <b>Высокопроизводительный автономный клиент и ботнет для мобильного криминального мультиплеера BLACK RUSSIA (CRMP)</b>
</p>

[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.14-blue?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Termux%20%7C%20Linux%20%7C%20Android%20%7C%20macOS-orange?style=for-the-badge&logo=linux&logoColor=white)](https://termux.dev/)
[![Black Russia](https://img.shields.io/badge/Black%20Russia-CRMP%20RakNet-red?style=for-the-badge)](https://blackrussia.online/)
[![Telegram](https://img.shields.io/badge/Telegram%20Bot-Controller-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/)

<br>

<p align="center">
  <a href="https://blackrussia.online/">
    <img src="https://img.shields.io/badge/🌐_Официальный_Сайт-blackrussia.online-E0584B?style=for-the-badge&logo=googlechrome&logoColor=white" alt="Официальный Сайт"/>
  </a>
  <a href="https://play.google.com/store/apps/details?id=com.launcher.brgame">
    <img src="https://img.shields.io/badge/Google_Play-Скачать_Игру-34A853?style=for-the-badge&logo=googleplay&logoColor=white" alt="Google Play"/>
  </a>
  <a href="https://apps.apple.com/ru/app/black-russia/id1644102824">
    <img src="https://img.shields.io/badge/App_Store-iOS_Версия-000000?style=for-the-badge&logo=apple&logoColor=white" alt="App Store"/>
  </a>
  <a href="https://t.me/blackrussia.online">
    <img src="https://img.shields.io/badge/Telegram-Канал_Проекта-229ED9?style=for-the-badge&logo=telegram&logoColor=white" alt="Telegram Канал"/>
  </a>
</p>

</div>

---

## ✨ Ключевые возможности

- 🧠 **Автономный Нейроконтроллер (ИИ Командир)**:
  - Подключение любой модели LLM (OpenAI, DeepSeek, OpenRouter, Ollama, vLLM).
  - Настройка `Base URL`, `API Token`, `Model ID` прямо через интерфейс Telegram.
  - Понимание команд на естественном русском языке: *"Запусти 5 ботов на Воронеж качать лвл"*, *"Проверь баны"*, *"Распредели случайные сервера"*.
  - Круглосуточный режим автономного мониторинга ботнета `24/7`.

- ⚡️ **Быстрый запуск одной командой**:
  - Установлен глобальный алиас `br`: запуск ботнета из любой точки терминала без лишних команд.
  - Консольная команда `/tg <ТОКЕН>` для мгновенной привязки Telegram-бота управления без перезапуска.

- 🛡 **Умная система распознавания банов & автозамена**:
  - Распознавание киков и блокировок античита (читы, ботоводство, кликеры, стороннее ПО).
  - При бане за бота система автоматически генерирует новый реалистичный аккаунт на том же сервере, сохраняет его и запускает взамен забаненного.
  - Отправка подробного отчета и анализа причин бана в Telegram и ИИ.

- 📈 **Качка LVL (Anti-AFK EXP Grinder)**:
  - Гарантированный спавн на реальном городском вокзале среди живых игроков (обход спект-боксов).
  - Непредсказуемая рандомизация простоя: от **20 секунд** до **9 минут** с точностью до долей секунды.
  - Микро-шаги, осмотры, приседания и повороты головы для полной иллюзии живого игрока.
  - Интеллектуальный автоответчик на проверки администрации в `/pm` и чат рядом (*"тут"*, *"да, играю"*, *"/b тут"*).
  - Сбор бесплатных ежедневных рулеток и бонусов PayDay раз в 30 минут.

- ⛏ **Шахта & Квесты Дяди Славы**:
  - Полная автоматизация стартовой сюжетной линии Дяди Славы (`QuestMasterTask`).
  - Поездки, заправки, прохождение мини-игры добычи руды на шахте.

---

## 🚀 Быстрый старт

### 1. Запуск одной командой
В любом месте терминала Termux / Linux просто напишите:
```bash
br
```

### 2. Запуск через Python с Telegram-ботом
```bash
python3 bot.py --tg ВАШ_ТОКЕН_ТЕЛЕГРАМ --admin ВАШ_ТЕЛЕГРАМ_ID
```
Либо внутри работающей консоли:
```text
/tg ВАШ_ТОКЕН_ТЕЛЕГРАМ
```

---

## 🎮 Доступные консольные команды

| Команда | Описание |
|---|---|
| `br` | Глобальный быстрый запуск бота |
| `/tg <token> [admin_id]` | Запустить Telegram-контроллер на лету |
| `/startall` | Запустить всех ботов из базы |
| `/stopall` | Остановить всех активных ботов |
| `/start <id>` | Запустить конкретного бота по ID |
| `/stop <id>` | Остановить бота по ID |
| `/batch <сервер> <кол-во> <пароль>` | Массовая генерация аккаунтов |
| `/list` | Показать список ботов, уровни и статусы |
| `/menu` | Интерактивное консольное меню |
| `/quit` | Корректно завершить работу и выйти |

---

## 📲 Официальные ссылки игры Black Russia

- 🌐 [Официальный сайт проекта](https://blackrussia.online/)
- 📱 [Скачать лаунчер для Android (Google Play)](https://play.google.com/store/apps/details?id=com.launcher.brgame)
- 🍏 [Скачать клиент для iOS (App Store)](https://apps.apple.com/ru/app/black-russia/id1644102824)
- 💬 [Официальный Telegram-канал разработчиков](https://t.me/blackrussia.online)
- 👥 [Официальная группа ВКонтакте](https://vk.com/blackrussia.online)

---

<div align="center">
  <sub>Разработано для образовательных целей и тестирования сетевых протоколов CRMP.</sub>
</div>
