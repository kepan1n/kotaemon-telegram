# Kotaemon Telegram Bridge

[![English](https://img.shields.io/badge/README-English-blue)](README.md)
[![Русский](https://img.shields.io/badge/README-Русский-brightgreen)](README_RU.md)

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)](https://www.python.org/)
[![Vibecoded with Codex 5.3](https://img.shields.io/badge/Vibecoded%20with-Codex%205.3-7c3aed)](https://openai.com)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)
[![Stars](https://img.shields.io/github/stars/kepan1n/kotaemon-telegram?style=flat)](https://github.com/kepan1n/kotaemon-telegram/stargazers)
[![Issues](https://img.shields.io/github/issues/kepan1n/kotaemon-telegram)](https://github.com/kepan1n/kotaemon-telegram/issues)
[![Last commit](https://img.shields.io/github/last-commit/kepan1n/kotaemon-telegram)](https://github.com/kepan1n/kotaemon-telegram/commits/main)

Telegram bot bridge for **Kotaemon** with document-aware Q&A, citations, PDF source pages, and mindmap rendering.

## Features

- Inline file picker (`/files`) with multi-select
- Ask questions (`/ask` or plain text)
- Clean citations output (`/citations`)
- Source pages as high-quality PDF renders (`/sources`)
- Disk cache for rendered PDF pages (faster repeated sends)
- Pre-warm all found source pages in advance (`/prepdf`)
- "Citation + PDF" flow with full quote and matching page (`/citsrc`)
- Mindmap image rendering (`/mindmap`) with pretty renderer + fallback
- Admin ACL commands: `/adduser`, `/deluser`, `/users`
- Stable restart helper to avoid duplicate polling processes

## Commands

- `/start` — greeting + immediate file picker
- `/cmd` — full command help
- `/files` — inline file picker
- `/use <name|id>` — select file by name or id
- `/clearuse` — clear selected files
- `/selected` — show selected file ids
- `/ask <question>` — ask Kotaemon
- `/citations` — cleaned citations
- `/sources` — "PDF where info comes from"
- `/prepdf <pdf_url_or_path>` — admin-only pre-render of all pages for one PDF
- `/citsrc` — full citation + matched PDF page
- `/mindmap` — mindmap image
- `/relogin` — relogin to Kotaemon

Admin:
- `/adduser <telegram_id>`
- `/deluser <telegram_id>`
- `/users`

## Quick start (local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env
python bot.py
```

## Ubuntu Server deploy (systemd)

```bash
chmod +x deploy.sh
./deploy.sh <linux-user>
```

The deploy script installs OS deps for Playwright, creates venv, installs Chromium, writes systemd unit, enables and restarts service.

Logs:

```bash
journalctl -u kotaemon-telegram-bridge -f
```

## Security

- Keep `.env` private
- Use strict whitelist
- Prefer dedicated Kotaemon account instead of `admin`

## License

Apache-2.0
