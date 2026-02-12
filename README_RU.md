# Kotaemon Telegram Bridge

[![English](https://img.shields.io/badge/README-English-blue)](README.md)
[![Русский](https://img.shields.io/badge/README-Русский-brightgreen)](README_RU.md)

Telegram-бот для работы с **Kotaemon**: вопросы по документам, цитаты, страницы PDF-источников и mindmap.

## Возможности

- Inline-выбор документов (`/files`) с галочками
- Вопросы к документам (`/ask` или обычный текст)
- Очищенный вывод цитат (`/citations`)
- Источники как страницы PDF хорошего качества (`/sources`)
- Режим "полная цитата + страница PDF" (`/citsrc`)
- Mindmap картинкой (`/mindmap`), красивый рендер + fallback
- ACL для пользователей: `/adduser`, `/deluser`, `/users`
- Скрипт безопасного перезапуска без дублей процессов

## Команды

- `/start` — приветствие + сразу выбор файлов
- `/cmd` — список команд
- `/files` — inline-пикер файлов
- `/use <имя|id>` — выбрать файл по имени/id
- `/clearuse` — очистить выбор
- `/selected` — показать выбранные file_id
- `/ask <вопрос>` — задать вопрос
- `/citations` — очищенные цитаты
- `/sources` — «PDF откуда Инфо»
- `/citsrc` — полная цитата + страница PDF
- `/mindmap` — mindmap
- `/relogin` — перелогин в Kotaemon

Админ:
- `/adduser <telegram_id>`
- `/deluser <telegram_id>`
- `/users`

## Локальный запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполните .env
python bot.py
```

## Деплой на Ubuntu Server (systemd)

```bash
chmod +x deploy.sh
./deploy.sh <linux-user>
```

Логи:

```bash
journalctl -u kotaemon-telegram-bridge -f
```

## Безопасность

- Не храните секреты в git
- Держите whitelist строгим
- Лучше использовать отдельного пользователя Kotaemon
