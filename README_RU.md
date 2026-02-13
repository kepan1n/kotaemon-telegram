# Kotaemon Telegram Bridge

[![English](https://img.shields.io/badge/README-English-blue)](README.md)
[![Русский](https://img.shields.io/badge/README-Русский-brightgreen)](README_RU.md)

Telegram-бот для работы с **Kotaemon**: вопросы по документам, цитаты, страницы PDF-источников и mindmap.

## Возможности

- Inline-выбор документов (`/files`) с галочками
- Вопросы к документам (`/ask` или обычный текст)
- Очищенный вывод цитат (`/citations`)
- Источники как страницы PDF хорошего качества (`/sources`)
- Дисковый кэш отрисованных PDF-страниц (повторная выдача быстрее)
- Прогрев всех найденных страниц заранее (`/prepdf`)
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
- `/prepdf <pdf_url_or_path>` — админ-команда: заранее отрисовать все страницы одного PDF
- `/prepdfid <file_id>` — админ-команда: прогреть PDF по file_id
- `/ingest [file_id|name|path]` — админ-команда: локальная предобработка (или всего `storage/incoming`)
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

## Локальное хранилище (самый быстрый режим)

Клади PDF в:
- `storage/incoming/<file_id>.pdf` или `storage/incoming/<name>.pdf`

Далее (админ):
- `/ingest <file_id|name|path>` — обработать один файл
- `/ingest` — обработать все PDF в `storage/incoming`

Будут созданы:
- `storage/rendered/pdf_pages/<key>/page-XXXX.pdf`
- `storage/rendered/png_pages/<key>/page-XXXX.png`
- `storage/rendered/bundles/<key>.pdf`

Если для выбранного файла есть локальный bundle, кнопка PDF отправляет его сразу.

## Безопасность

- Не храните секреты в git
- Держите whitelist строгим
- Лучше использовать отдельного пользователя Kotaemon
