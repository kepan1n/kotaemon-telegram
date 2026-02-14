import base64
import io
import json
import hashlib
import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from gradio_client import Client
from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputMediaPhoto
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kotaemon-bridge")


@dataclass
class Settings:
    token: str
    kotaemon_url: str
    kotaemon_user: str
    kotaemon_pass: str
    whitelist: set[int]
    state_db: str


class StateDB:
    def __init__(self, path: str, bootstrap_admins: set[int] | None = None):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_state (
              tg_user_id INTEGER PRIMARY KEY,
              selected_files_json TEXT NOT NULL DEFAULT '[]',
              last_chat_history_json TEXT NOT NULL DEFAULT '[]',
              last_retrieval_html TEXT NOT NULL DEFAULT '',
              last_mindmap_json TEXT NOT NULL DEFAULT '{}',
              last_pdf_targets_json TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS acl_users (
              tg_user_id INTEGER PRIMARY KEY,
              is_admin INTEGER NOT NULL DEFAULT 0,
              enabled INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        self.conn.commit()

        # lightweight migration for older DBs
        try:
            self.conn.execute("ALTER TABLE user_state ADD COLUMN last_pdf_targets_json TEXT NOT NULL DEFAULT '[]'")
            self.conn.commit()
        except Exception:
            pass

        for uid in (bootstrap_admins or set()):
            self.conn.execute(
                "INSERT OR IGNORE INTO acl_users(tg_user_id, is_admin, enabled) VALUES(?,1,1)",
                (uid,),
            )
        self.conn.commit()

    def get_user(self, user_id: int) -> dict[str, Any]:
        cur = self.conn.execute(
            "SELECT selected_files_json,last_chat_history_json,last_retrieval_html,last_mindmap_json,last_pdf_targets_json FROM user_state WHERE tg_user_id=?",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            self.conn.execute("INSERT INTO user_state(tg_user_id) VALUES(?)", (user_id,))
            self.conn.commit()
            return {
                "selected_files": [],
                "last_chat_history": [],
                "last_retrieval_html": "",
                "last_mindmap": {},
                "last_pdf_targets": [],
            }
        return {
            "selected_files": json.loads(row[0] or "[]"),
            "last_chat_history": json.loads(row[1] or "[]"),
            "last_retrieval_html": row[2] or "",
            "last_mindmap": json.loads(row[3] or "{}"),
            "last_pdf_targets": json.loads(row[4] or "[]"),
        }

    def save_user(self, user_id: int, **kwargs):
        current = self.get_user(user_id)
        current.update(kwargs)
        self.conn.execute(
            """
            UPDATE user_state
            SET selected_files_json=?, last_chat_history_json=?, last_retrieval_html=?, last_mindmap_json=?, last_pdf_targets_json=?
            WHERE tg_user_id=?
            """,
            (
                json.dumps(current["selected_files"], ensure_ascii=False),
                json.dumps(current["last_chat_history"], ensure_ascii=False),
                current["last_retrieval_html"],
                json.dumps(current["last_mindmap"], ensure_ascii=False),
                json.dumps(current.get("last_pdf_targets", []), ensure_ascii=False),
                user_id,
            ),
        )
        self.conn.commit()

    def is_allowed(self, user_id: int, bootstrap_whitelist: set[int]) -> bool:
        cur = self.conn.execute("SELECT enabled FROM acl_users WHERE tg_user_id=?", (user_id,))
        row = cur.fetchone()
        if row is None:
            return user_id in bootstrap_whitelist
        return bool(row[0])

    def is_admin(self, user_id: int, bootstrap_whitelist: set[int]) -> bool:
        cur = self.conn.execute("SELECT is_admin, enabled FROM acl_users WHERE tg_user_id=?", (user_id,))
        row = cur.fetchone()
        if row is None:
            return user_id in bootstrap_whitelist
        return bool(row[0]) and bool(row[1])

    def add_user(self, user_id: int, admin: bool = False):
        self.conn.execute(
            "INSERT INTO acl_users(tg_user_id,is_admin,enabled) VALUES(?,?,1) "
            "ON CONFLICT(tg_user_id) DO UPDATE SET enabled=1, is_admin=excluded.is_admin",
            (user_id, 1 if admin else 0),
        )
        self.conn.commit()

    def disable_user(self, user_id: int):
        self.conn.execute(
            "INSERT INTO acl_users(tg_user_id,is_admin,enabled) VALUES(?,?,0) "
            "ON CONFLICT(tg_user_id) DO UPDATE SET enabled=0",
            (user_id, 0),
        )
        self.conn.commit()

    def list_acl(self) -> list[tuple[int, int, int]]:
        cur = self.conn.execute("SELECT tg_user_id,is_admin,enabled FROM acl_users ORDER BY is_admin DESC, tg_user_id ASC")
        return [(int(a), int(b), int(c)) for a, b, c in cur.fetchall()]


class KotaemonBridge:
    def __init__(self, url: str, username: str, password: str):
        self.client = Client(url)
        self.username = username
        self.password = password
        self.login()

    def login(self):
        self.client.predict(self.username, self.password, api_name="/login")
        log.info("kotaemon login called")

    def safe_login(self):
        try:
            self.login()
            return True
        except Exception as e:
            log.exception("kotaemon login failed: %s", e)
            return False

    def list_files(self) -> list[dict[str, str]]:
        files = self._list_files_once()
        if files:
            return files
        # auto-fallback: refresh session once and retry
        log.info("list_files empty -> attempting relogin and retry")
        if self.safe_login():
            files = self._list_files_once()
        return files

    def _list_files_once(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []

        def _valid(name: str, fid: str) -> bool:
            n = (name or "").strip()
            f = (fid or "").strip()
            bad = {"", "-", "none", "null"}
            return n.lower() not in bad and f.lower() not in bad

        # Primary source: dataframe endpoint with stable [id, name, ...]
        try:
            df = self.client.predict(api_name="/list_file")
            if isinstance(df, dict):
                rows = df.get("data") or []
                for row in rows:
                    if isinstance(row, (list, tuple)) and len(row) >= 2:
                        fid, name = str(row[0]), str(row[1])
                        if _valid(name, fid):
                            item = {"id": fid, "name": name}
                            for extra in row[2:]:
                                s = str(extra or "").strip()
                                if not s:
                                    continue
                                if s.lower().endswith(".pdf") or "/files/" in s or s.startswith("http"):
                                    item["url"] = s
                                    break
                            result.append(item)
        except Exception:
            pass

        if result:
            return result

        # Fallback: dropdown endpoint
        out = self.client.predict(api_name="/list_file_names")
        if isinstance(out, dict):
            choices = out.get("choices") or []
            for item in choices:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    name, fid = str(item[0]), str(item[1])
                    if _valid(name, fid):
                        rec = {"name": name, "id": fid}
                        for extra in item[2:]:
                            s = str(extra or "").strip()
                            if s.lower().endswith(".pdf") or "/files/" in s or s.startswith("http"):
                                rec["url"] = s
                                break
                        result.append(rec)
                elif isinstance(item, str):
                    if _valid(item, item):
                        result.append({"name": item, "id": item})
            return result
        if isinstance(out, list):
            for item in out:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    name, fid = str(item[0]), str(item[1])
                    if _valid(name, fid):
                        result.append({"name": name, "id": fid})
                else:
                    s = str(item)
                    if _valid(s, s):
                        result.append({"name": s, "id": s})
        return result

    def ask(self, text: str, selected_files: list[str], history: list):
        def _run_once():
            submit = self.client.predict({"text": text, "files": []}, history, "tg-bridge", [], api_name="/submit_msg")
            chat_history = submit[1]
            file_mode = "select" if selected_files else "all"
            new_history, retrieval_html, mindmap = self.client.predict(
                chat_history,
                "",
                "highlight",
                "ru",
                file_mode,
                selected_files,
                "all",
                [],
                "all",
                [],
                api_name="/chat_fn",
            )
            answer = ""
            if new_history and isinstance(new_history[-1], list) and len(new_history[-1]) >= 2:
                answer = str(new_history[-1][1] or "")
            return answer, new_history, retrieval_html or "", mindmap or {}

        try:
            return _run_once()
        except Exception:
            # one recovery attempt after re-login
            self.safe_login()
            return _run_once()


def _clean_snippet(snippet: str) -> str:
    snippet = re.sub(r"Relevance score\s*:\s*[0-9.]+", "", snippet, flags=re.I)
    snippet = re.sub(r"Vectorstore score:\s*\(full-text search\)", "", snippet, flags=re.I)
    snippet = re.sub(r"LLM relevant score:\s*[0-9.]+", "", snippet, flags=re.I)
    snippet = re.sub(r"Reranking score:\s*[0-9.]+", "", snippet, flags=re.I)

    # remove repeating footer/header junk from manuals
    snippet = re.sub(r"\b\d{3}\s+БИТ\.СТРОИТЕЛЬСТВО\s*/\s*снабжение\s*и\s*склад\b", "", snippet, flags=re.I)
    snippet = re.sub(r"www\.1bit\.ru", "", snippet, flags=re.I)
    snippet = re.sub(r"\+\s*7\s*495\s*748\s*[–-]\s*09\s*[–-]\s*99", "", snippet, flags=re.I)
    snippet = re.sub(r"РУКОВОДСТВО\s+ПОЛЬЗОВАТЕЛЯ\s+\d+", "", snippet, flags=re.I)
    snippet = re.sub(r"bitstroitelstvo\s*@\s*1cbit\.ru", "", snippet, flags=re.I)

    return re.sub(r"\s+", " ", snippet).strip(" -–")


def extract_citations_data(html: str, top_n: int = 5) -> list[dict[str, Any]]:
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []

    for det in soup.select("details.evidence"):
        summary = det.find("summary")
        if not summary:
            continue
        s_txt = summary.get_text(" ", strip=True)
        if not s_txt or "Mindmap" in s_txt:
            continue

        m_page = re.search(r"\[Page\s*(\d+)\]", s_txt, re.I)
        page = int(m_page.group(1)) if m_page else -1

        m_score = re.search(r"\[score:\s*([0-9.]+)\]", s_txt, re.I)
        score = float(m_score.group(1)) if m_score else -1.0

        doc = s_txt
        if m_page:
            doc = s_txt[m_page.end():]
        doc = re.sub(r"\[score:.*?\]", "", doc, flags=re.I).replace("[Preview]", "").strip(" -") or "Документ"

        link = det.select_one("a.pdf-link")
        data_src = (link.get("data-src") or "").strip() if link else ""
        data_page = int((link.get("data-page") or page or 1)) if link else (page if page > 0 else 1)

        content = det.find(class_="evidence-content")
        snippet = content.get_text(" ", strip=True) if content else det.get_text(" ", strip=True)
        snippet = _clean_snippet(snippet)

        rows.append(
            {
                "score": score,
                "page": data_page,
                "doc": doc,
                "snippet": snippet,
                "data_src": data_src,
            }
        )

    rows.sort(key=lambda x: x.get("score", -1), reverse=True)
    return rows[:top_n]


def parse_citations_text(html: str) -> str:
    rows = extract_citations_data(html, top_n=5)
    if not rows:
        if not html:
            return "Цитаты не найдены."
        text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        return (text[:1200] + "\n...") if len(text) > 1200 else (text or "Цитаты не найдены.")

    out = ["Цитаты (очищено):"]
    for i, r in enumerate(rows, 1):
        snippet = r["snippet"]
        if len(snippet) > 420:
            snippet = snippet[:420].rstrip() + "…"
        out.append(f"{i}) стр. {r['page']} | score {r['score']:g} | {r['doc']}")
        out.append(f"   {snippet}")

    return "\n".join(out)[:3900]


def parse_source_links(html: str) -> list[tuple[str, str]]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []

    # Generic links
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href == "#":
            continue
        title = (a.get_text(" ", strip=True) or href).strip()
        out.append((title[:120], href))

    # Kotaemon PDF preview links in citations panel
    for a in soup.select("a.pdf-link"):
        data_src = (a.get("data-src") or "").strip()
        page = (a.get("data-page") or "").strip()
        search = (a.get("data-search") or "").strip()
        if not data_src:
            continue
        title = f"PDF preview page {page}" if page else "PDF preview"
        if search:
            title += f" | {search[:80]}"
        out.append((title, data_src))

    uniq = []
    seen = set()
    for t, h in out:
        if h in seen:
            continue
        seen.add(h)
        uniq.append((t, h))
    return uniq


def extract_mindmap_markdown(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for s in soup.find_all("script"):
        t = (s.get("type") or "").strip().lower()
        if t == "text/template":
            body = s.get_text("\n", strip=False)
            if "markmap" in body.lower() or body.strip().startswith("#"):
                return body.strip()
    return ""


def extract_embedded_images(html: str, limit: int = 6) -> list[bytes]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    images: list[bytes] = []

    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src.startswith("data:image"):
            continue
        m = re.match(r"data:image/[^;]+;base64,(.+)$", src, re.DOTALL)
        if not m:
            continue
        try:
            blob = base64.b64decode(m.group(1), validate=False)
            if blob:
                images.append(blob)
        except Exception:
            continue
        if len(images) >= limit:
            break
    return images


def parse_pdf_preview_targets(html: str, base_url: str) -> list[tuple[str, int]]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, int]] = []
    for a in soup.select("a.pdf-link"):
        src = (a.get("data-src") or "").strip()
        page_raw = (a.get("data-page") or "1").strip()
        if not src:
            continue
        page = 1
        try:
            page = max(1, int(page_raw))
        except Exception:
            page = 1
        if src.startswith("/"):
            src = base_url.rstrip("/") + src
        out.append((src, page))
    return out


def prepare_pdf_targets(html: str, base_url: str) -> list[tuple[str, int]]:
    """Normalize, dedupe and sort PDF targets by page asc (then url)."""
    raw = parse_pdf_preview_targets(html, base_url)
    seen: set[tuple[str, int]] = set()
    uniq: list[tuple[str, int]] = []
    for src, page in raw:
        key = ((src or "").strip(), int(page or 1))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        uniq.append(key)
    uniq.sort(key=lambda x: (x[1], x[0]))
    return uniq


def format_pages_brief(targets: list[tuple[str, int]]) -> str:
    pages = sorted({p for _, p in targets})
    if not pages:
        return ""
    return ", ".join(str(p) for p in pages)


def cited_pages_from_html(html: str, max_pages: int = 10) -> list[int]:
    rows = extract_citations_data(html or "", top_n=50)
    pages = sorted({int(r.get("page") or 0) for r in rows if int(r.get("page") or 0) > 0})
    return pages[:max_pages]


def cited_pages_for_item(st: dict[str, Any], target_item: dict[str, Any], base_url: str, max_pages: int = 10) -> list[int]:
    pages = cited_pages_from_html(st.get("last_retrieval_html", ""), max_pages=max_pages)
    if pages:
        return pages

    # fallback parser from saved targets (src,page)
    aliases = [str(x).strip().lower() for x in target_item.get("aliases", [])]
    from_targets: list[int] = []
    for src, pg in targets_from_state(st, base_url):
        ssrc = str(src).lower()
        if any(a and a in ssrc for a in aliases):
            from_targets.append(int(pg))
    return sorted(set(from_targets))[:max_pages]


def targets_from_state(st: dict[str, Any], base_url: str) -> list[tuple[str, int]]:
    raw = st.get("last_pdf_targets") or []
    out: list[tuple[str, int]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            src = normalize_pdf_url(base_url, str(item[0]))
            try:
                page = max(1, int(item[1]))
            except Exception:
                page = 1
            if src:
                out.append((src, page))
    if out:
        out = sorted(set(out), key=lambda x: (x[1], x[0]))
    return out


def build_pdf_from_local_pages_dir(pages_dir: Path, out_pdf: Path, max_pages: int = 10) -> tuple[bool, int]:
    try:
        files = sorted(pages_dir.glob("page-*.pdf"))[:max_pages]
        if not files:
            return False, 0
        import fitz  # PyMuPDF

        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        doc = fitz.open()
        for p in files:
            src = fitz.open(str(p))
            doc.insert_pdf(src)
            src.close()
        doc.save(str(out_pdf))
        doc.close()
        return True, len(files)
    except Exception as e:
        log.warning("build local limited pdf failed: %s", e)
        return False, 0


def build_pdf_from_local_pages(pages_dir: Path, out_pdf: Path, pages: list[int], max_pages: int = 10) -> tuple[bool, int]:
    try:
        uniq_pages = sorted({int(p) for p in pages if int(p) > 0})[:max_pages]
        if not uniq_pages:
            return False, 0
        import fitz  # PyMuPDF

        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        doc = fitz.open()
        added = 0
        png_dir = Path(str(pages_dir).replace("/pdf_pages/", "/png_pages/"))

        for p in uniq_pages:
            fp = pages_dir / f"page-{p:04d}.pdf"
            if fp.exists():
                src = fitz.open(str(fp))
                doc.insert_pdf(src)
                src.close()
                added += 1
                continue

            # fallback: build page from pre-rendered png
            ip = png_dir / f"page-{p:04d}.png"
            if ip.exists():
                img = fitz.Pixmap(str(ip))
                page = doc.new_page(width=img.width, height=img.height)
                page.insert_image(page.rect, filename=str(ip))
                added += 1

        if added == 0:
            doc.close()
            log.warning("build local citation-pages pdf: no source pages found in %s", pages_dir)
            return False, 0

        # save optimized (smaller upload, fewer telegram timeouts)
        doc.save(str(out_pdf), garbage=4, deflate=True, use_objstms=1)
        doc.close()
        return True, added
    except Exception as e:
        log.exception("build local citation-pages pdf failed pages_dir=%s pages=%s err=%s", pages_dir, pages, e)
        return False, 0


def build_combined_pdf_from_targets(
    targets: list[tuple[str, int]],
    out_pdf: Path,
    zoom: float = 2.6,
    cookies: dict[str, str] | None = None,
) -> tuple[bool, int]:
    """Build one PDF from rendered target pages; returns (ok, page_count)."""
    try:
        images: list[Image.Image] = []
        for src, page in targets:
            p = get_or_render_pdf_page_png(src, page, zoom=zoom, cookies=cookies)
            if not p or not p.exists():
                continue
            img = Image.open(p).convert("RGB")
            images.append(img)

        if not images:
            return False, 0

        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        first, rest = images[0], images[1:]
        first.save(out_pdf, format="PDF", save_all=True, append_images=rest, resolution=220.0)
        return True, len(images)
    except Exception as e:
        log.warning("combined pdf build failed: %s", e)
        return False, 0


def build_png_collage(images: list[Path], out_file: Path, cols: int = 3, max_images: int = 10, tile_w: int = 620) -> tuple[bool, int]:
    try:
        items = [p for p in images if p.exists()][:max_images]
        if not items:
            return False, 0

        pil_images = []
        for p in items:
            im = Image.open(p).convert("RGB")
            ratio = tile_w / max(1, im.width)
            tile_h = int(im.height * ratio)
            pil_images.append(im.resize((tile_w, tile_h)))

        rows = (len(pil_images) + cols - 1) // cols
        row_heights = []
        for r in range(rows):
            row = pil_images[r * cols : (r + 1) * cols]
            row_heights.append(max(im.height for im in row) if row else 0)

        pad = 12
        width = cols * tile_w + (cols + 1) * pad
        height = sum(row_heights) + (rows + 1) * pad
        canvas = Image.new("RGB", (width, height), (245, 245, 245))

        y = pad
        idx = 0
        for r in range(rows):
            x = pad
            for _ in range(cols):
                if idx >= len(pil_images):
                    break
                im = pil_images[idx]
                canvas.paste(im, (x, y))
                x += tile_w + pad
                idx += 1
            y += row_heights[r] + pad

        out_file.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_file, format="PNG")
        return True, len(items)
    except Exception as e:
        log.warning("build png collage failed: %s", e)
        return False, 0


def split_text_chunks(text: str, limit: int = 3500) -> list[str]:
    s = (text or "").strip()
    if not s:
        return []
    chunks: list[str] = []
    while len(s) > limit:
        cut = s.rfind("\n", 0, limit)
        if cut < int(limit * 0.6):
            cut = s.rfind(" ", 0, limit)
        if cut < int(limit * 0.6):
            cut = limit
        chunks.append(s[:cut].strip())
        s = s[cut:].strip()
    if s:
        chunks.append(s)
    return chunks


async def send_document_retry(msg, path: Path, caption: str | None = None) -> bool:
    for attempt in range(1, 3):
        try:
            size = path.stat().st_size if path.exists() else -1
            log.info("reply_document attempt=%s path=%s size=%s", attempt, path, size)

            async def _send_once():
                with path.open("rb") as f:
                    return await msg.reply_document(
                        document=f,
                        caption=caption,
                        read_timeout=None,
                        write_timeout=None,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

            await asyncio.wait_for(_send_once(), timeout=65)
            log.info("reply_document success attempt=%s path=%s", attempt, path)
            return True
        except asyncio.TimeoutError:
            log.error("reply_document local-timeout attempt=%s path=%s", attempt, path)
        except Exception as e:
            log.exception("reply_document failed attempt=%s path=%s err=%s", attempt, path, e)
    return False


async def send_media_group_retry(msg, media: list[InputMediaPhoto]) -> bool:
    for attempt in range(1, 3):
        try:
            log.info("reply_media_group attempt=%s count=%s", attempt, len(media))
            await asyncio.wait_for(
                msg.reply_media_group(
                    media=media,
                    read_timeout=None,
                    write_timeout=None,
                    connect_timeout=30,
                    pool_timeout=30,
                ),
                timeout=65,
            )
            log.info("reply_media_group success attempt=%s", attempt)
            return True
        except asyncio.TimeoutError:
            log.error("reply_media_group local-timeout attempt=%s", attempt)
        except Exception as e:
            log.exception("reply_media_group failed attempt=%s err=%s", attempt, e)
    return False


def schedule_background_pdf_send(msg, path: Path, caption: str):
    async def _job():
        ok = await send_document_retry(msg, path, caption=caption)
        if ok:
            try:
                await msg.reply_text("Единый PDF отправлен фоном ✅")
            except Exception:
                pass
        else:
            try:
                await msg.reply_text("Не удалось отправить единый PDF даже в фоне (таймаут Telegram).")
            except Exception:
                pass

    asyncio.create_task(_job())


async def send_citsrc_item(msg, photo_path: Path | None, header: str, snippet: str):
    snippet = (snippet or "").strip()
    caption = f"{header}\n\n{snippet}".strip()
    if len(caption) > 1024:
        caption = caption[:1021].rstrip() + "..."

    if photo_path and photo_path.exists():
        with photo_path.open("rb") as f:
            await msg.reply_photo(photo=f, caption=caption)
    else:
        await msg.reply_text(caption)


def normalize_pdf_url(base_url: str, value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    # keep local absolute path as-is if file exists on host
    if v.startswith("/") and Path(v).exists():
        return v
    if v.startswith("/"):
        return base_url.rstrip("/") + v
    return v


def try_resolve_pdf_url_by_id(s: Settings, b: KotaemonBridge, file_id: str) -> str:
    fid = (file_id or "").strip()
    if not fid:
        return ""

    # direct path/url provided instead of id
    direct = normalize_pdf_url(s.kotaemon_url, fid)
    if direct.lower().startswith(("http://", "https://")) and direct.lower().endswith(".pdf"):
        return direct

    files = b.list_files()
    for f in files:
        if (f.get("id") or "").strip() != fid:
            continue
        u = normalize_pdf_url(s.kotaemon_url, f.get("url", ""))
        if u.lower().startswith(("http://", "https://")):
            return u
        # best-effort fallback by filename
        n = (f.get("name") or "").strip()
        if n.lower().endswith(".pdf"):
            cand = normalize_pdf_url(s.kotaemon_url, f"/files/{n}")
            return cand

    # last-resort common patterns
    for pat in (f"/files/{fid}", f"/file/{fid}", f"/api/files/{fid}", f"/api/file/{fid}"):
        cand = normalize_pdf_url(s.kotaemon_url, pat)
        try:
            r = requests.get(cand, timeout=10)
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code == 200 and ("application/pdf" in ct or cand.lower().endswith(".pdf")):
                return cand
        except Exception:
            continue

    return ""


def pdf_cache_file(pdf_url: str, page_num: int, zoom: float = 2.6, cache_dir: Path = Path("./out/pdf_cache")) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{pdf_url}|{page_num}|{zoom:.2f}".encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"p{page_num}_{key}.png"


def get_or_render_pdf_page_png(pdf_url: str, page_num: int, zoom: float = 2.6, cookies: dict[str, str] | None = None) -> Path | None:
    out_file = pdf_cache_file(pdf_url, page_num, zoom=zoom)
    if out_file.exists() and out_file.stat().st_size > 0:
        return out_file
    if render_pdf_page_png(pdf_url, page_num, out_file, zoom=zoom, cookies=cookies):
        return out_file
    return None


def prewarm_pdf_all_pages(pdf_url: str, zoom: float = 2.6, cookies: dict[str, str] | None = None) -> tuple[int, int, int]:
    """Return (total_pages, rendered_new, already_cached)."""
    try:
        import fitz  # PyMuPDF

        if pdf_url.startswith("/") and Path(pdf_url).exists():
            doc = fitz.open(pdf_url)
        else:
            r = requests.get(pdf_url, timeout=60, cookies=(cookies or {}))
            r.raise_for_status()
            doc = fitz.open(stream=r.content, filetype="pdf")

        total = int(doc.page_count)
        rendered = 0
        cached = 0
        for idx in range(total):
            page_num = idx + 1
            out_file = pdf_cache_file(pdf_url, page_num, zoom=zoom)
            if out_file.exists() and out_file.stat().st_size > 0:
                cached += 1
                continue
            page = doc.load_page(idx)
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            pix.save(str(out_file))
            rendered += 1

        doc.close()
        return total, rendered, cached
    except Exception as e:
        log.warning("prewarm all pages failed: %s", e)
        return 0, 0, 0


def render_pdf_page_png(pdf_url: str, page_num: int, out_file: Path, zoom: float = 2.2, cookies: dict[str, str] | None = None) -> bool:
    try:
        import fitz  # PyMuPDF

        if pdf_url.startswith("/") and Path(pdf_url).exists():
            doc = fitz.open(pdf_url)
        else:
            r = requests.get(pdf_url, timeout=45, cookies=(cookies or {}))
            r.raise_for_status()
            doc = fitz.open(stream=r.content, filetype="pdf")
        idx = min(max(page_num - 1, 0), max(doc.page_count - 1, 0))
        page = doc.load_page(idx)
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(out_file))
        doc.close()
        return True
    except Exception as e:
        log.warning("pdf page render failed: %s", e)
        return False


def load_cyrillic_font(size: int = 16):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


async def render_mindmap_markdown_png_pretty(md: str, out_file: Path) -> bool:
    """Render via real markmap in headless browser (best visual quality)."""
    if not md:
        return False
    try:
        from playwright.async_api import async_playwright

        md_js = json.dumps(md)
        html = f"""
<!doctype html>
<html>
<head>
  <meta charset='utf-8'/>
  <style>
    html,body {{ margin:0; padding:0; background:#ffffff; }}
    #mindmap {{ width: 1800px; height: 1200px; }}
  </style>
  <script src='https://cdn.jsdelivr.net/npm/markmap-autoloader@0.18.10/dist/index.min.js'></script>
</head>
<body>
  <div class='markmap' id='mindmap'><script type='text/template' id='mdtpl'></script></div>
  <script>
    document.getElementById('mdtpl').textContent = {md_js};
  </script>
</body>
</html>
"""

        out_file.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 2200, "height": 1600}, device_scale_factor=2)
            await page.set_content(html, wait_until="networkidle")
            await page.wait_for_timeout(1600)

            # capture rendered SVG at higher scale
            svg = page.locator("#mindmap svg")
            if await svg.count() > 0:
                await page.evaluate("""
                    const s = document.querySelector('#mindmap svg');
                    if (s) {
                      s.style.transformOrigin = 'top left';
                      s.style.transform = 'scale(2)';
                    }
                """)
                await page.wait_for_timeout(200)
                await svg.first.screenshot(path=str(out_file))
            else:
                locator = page.locator("#mindmap")
                await locator.screenshot(path=str(out_file))

            await browser.close()
        return out_file.exists() and out_file.stat().st_size > 0
    except Exception as e:
        log.warning("pretty markmap render failed: %s", e)
        return False


def render_mindmap_markdown_png(md: str, out_file: Path) -> bool:
    # Fallback renderer: heading hierarchy as graphical tree
    if not md:
        return False

    nodes: list[tuple[int, str]] = []
    in_frontmatter = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.strip() == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if not line.strip().startswith("#"):
            continue
        lvl = len(line) - len(line.lstrip("#"))
        text = line.lstrip("#").strip()
        if text:
            nodes.append((lvl, text[:80]))

    if not nodes:
        return False

    nodes = nodes[:140]
    font = load_cyrillic_font(18)

    margin_x, margin_y = 30, 30
    level_w = 280
    row_h = 42
    box_h = 28
    box_w = 250

    width = margin_x * 2 + level_w * (max(l for l, _ in nodes) + 1)
    height = margin_y * 2 + row_h * (len(nodes) + 2)

    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)

    coords = []
    stack_idx = {}
    for i, (lvl, text) in enumerate(nodes):
        x = margin_x + (lvl - 1) * level_w
        y = margin_y + i * row_h
        coords.append((x, y, lvl, text))
        stack_idx[lvl] = i

        # parent lookup: nearest previous smaller level
        parent = None
        for pl in range(lvl - 1, 0, -1):
            if pl in stack_idx:
                parent = stack_idx[pl]
                break
        if parent is not None:
            px, py, _, _ = coords[parent]
            d.line((px + box_w, py + box_h // 2, x, y + box_h // 2), fill=(120, 120, 120), width=2)

    # draw nodes on top of lines
    palette = [(235,245,255), (230,255,240), (255,245,230), (245,235,255), (255,235,240)]
    for x, y, lvl, text in coords:
        fill = palette[(lvl - 1) % len(palette)]
        d.rounded_rectangle((x, y, x + box_w, y + box_h), radius=8, outline=(80, 80, 80), fill=fill, width=2)
        d.text((x + 8, y + 8), text, fill="black", font=font)

    out_file.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_file)
    return True


def render_mindmap_png(mindmap: dict, out_file: Path) -> bool:
    try:
        import plotly.io as pio

        if not isinstance(mindmap, dict):
            return False
        if mindmap.get("type") != "plotly":
            return False
        plot_json = mindmap.get("plot")
        if not plot_json:
            return False
        fig = pio.from_json(plot_json)
        fig.write_image(str(out_file), width=1400, height=900, scale=1)
        return True
    except Exception as e:
        log.warning("mindmap render failed: %s", e)
        return False


def get_settings() -> Settings:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    wl = {int(x.strip()) for x in os.getenv("TELEGRAM_WHITELIST", "").split(",") if x.strip()}
    return Settings(
        token=token,
        kotaemon_url=os.getenv("KOTAEMON_URL", "https://kotaemon.example.com").strip(),
        kotaemon_user=os.getenv("KOTAEMON_USERNAME", "admin").strip(),
        kotaemon_pass=os.getenv("KOTAEMON_PASSWORD", "admin").strip(),
        whitelist=wl,
        state_db=os.getenv("STATE_DB", "./state.db").strip(),
    )


def auth_ok(update: Update, settings: Settings, db: StateDB | None = None) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if db is None:
        return uid in settings.whitelist
    return db.is_allowed(uid, settings.whitelist)


def admin_ok(update: Update, settings: Settings, db: StateDB) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return db.is_admin(uid, settings.whitelist)


def action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📄 PDF откуда Инфо", callback_data="act:src")],
            [InlineKeyboardButton("🧩 PNG альбом", callback_data="act:collage")],
            [InlineKeyboardButton("📎 Цитаты + PDF", callback_data="act:citsrc")],
            [InlineKeyboardButton("🧠 Mindmap", callback_data="act:mm")],
        ]
    )


async def safe_edit_text(msg, text: str, reply_markup=None):
    try:
        return await msg.edit_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return None
        raise


def storage_root() -> Path:
    p = Path("./storage")
    p.mkdir(parents=True, exist_ok=True)
    return p


def storage_paths() -> dict[str, Path]:
    root = storage_root()
    paths = {
        "root": root,
        "incoming": root / "incoming",
        "pdf_pages": root / "rendered" / "pdf_pages",
        "png_pages": root / "rendered" / "png_pages",
        "bundles": root / "rendered" / "bundles",
        "index": root / "index.json",
    }
    for k, v in paths.items():
        if k != "index":
            v.mkdir(parents=True, exist_ok=True)
    return paths


def load_storage_index() -> dict[str, Any]:
    p = storage_paths()["index"]
    if not p.exists():
        return {"items": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"items": {}}


def save_storage_index(data: dict[str, Any]):
    p = storage_paths()["index"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def storage_key(s: str) -> str:
    x = re.sub(r"[^0-9a-zA-Z._-]+", "_", (s or "").strip())
    return x.strip("_")[:120] or hashlib.sha1((s or "x").encode("utf-8")).hexdigest()[:16]


def find_bundle_by_alias(alias: str) -> Path | None:
    idx = load_storage_index().get("items", {})
    needle = (alias or "").strip().lower()
    for _, item in idx.items():
        aliases = [str(x).strip().lower() for x in item.get("aliases", [])]
        if needle in aliases:
            b = Path(item.get("bundle", ""))
            if b.exists():
                return b
    return None


def ingest_local_pdf(pdf_path: Path, key: str, aliases: list[str] | None = None) -> tuple[bool, int]:
    try:
        import fitz  # PyMuPDF

        sp = storage_paths()
        key = storage_key(key)
        pdf_dir = sp["pdf_pages"] / key
        png_dir = sp["png_pages"] / key
        pdf_dir.mkdir(parents=True, exist_ok=True)
        png_dir.mkdir(parents=True, exist_ok=True)

        doc = fitz.open(str(pdf_path))
        total = int(doc.page_count)
        for i in range(total):
            page_num = i + 1
            ppdf = pdf_dir / f"page-{page_num:04d}.pdf"
            ppng = png_dir / f"page-{page_num:04d}.png"

            if not ppdf.exists():
                nd = fitz.open()
                nd.insert_pdf(doc, from_page=i, to_page=i)
                nd.save(str(ppdf))
                nd.close()

            if not ppng.exists():
                page = doc.load_page(i)
                pix = page.get_pixmap(matrix=fitz.Matrix(2.4, 2.4), alpha=False)
                pix.save(str(ppng))

        bundle = sp["bundles"] / f"{key}.pdf"
        if not bundle.exists():
            doc.save(str(bundle))
        doc.close()

        idx = load_storage_index()
        items = idx.setdefault("items", {})
        cur_aliases = set(items.get(key, {}).get("aliases", []))
        for a in (aliases or []):
            if a:
                cur_aliases.add(str(a))
        cur_aliases.add(key)
        cur_aliases.add(pdf_path.name)
        cur_aliases.add(pdf_path.stem)

        items[key] = {
            "source": str(pdf_path),
            "bundle": str(bundle),
            "pdf_pages_dir": str(pdf_dir),
            "png_pages_dir": str(png_dir),
            "pages": total,
            "aliases": sorted(cur_aliases),
        }
        save_storage_index(idx)
        return True, total
    except Exception as e:
        log.warning("ingest local pdf failed: %s", e)
        return False, 0


def files_keyboard(files: list[dict[str, str]], selected_ids: list[str], page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    total = len(files)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    chunk = files[page * per_page : (page + 1) * per_page]

    rows = []
    for f in chunk:
        checked = "✅ " if f["id"] in selected_ids else "▫️ "
        label = (checked + f["name"])[:40]
        rows.append([InlineKeyboardButton(label, callback_data=f"file:toggle:{f['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"file:page:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"file:page:{page+1}"))

    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🧹 Очистить выбор", callback_data="file:clear")])
    return InlineKeyboardMarkup(rows)


async def deny(update: Update):
    if update.message:
        await update.message.reply_text("Доступ запрещен (whitelist).")
    elif update.callback_query:
        await update.callback_query.answer("Доступ запрещен", show_alert=True)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)

    await update.message.reply_text(
        "Привет! 👋\n"
        "Я подключен к чату с документами Легенда\n"
        "Отметь документы ниже, по которым есть вопросы, галкой ✅, затем просто пиши вопрос."
    )

    # show file picker immediately (same UX as /files)
    b: KotaemonBridge = context.application.bot_data["bridge"]
    files = b.list_files()
    if not files:
        return await update.message.reply_text("Файлы не найдены (API вернул пусто).")

    st = db.get_user(update.effective_user.id)
    kb = files_keyboard(files, st["selected_files"], page=0)
    await update.message.reply_text(
        f"Файлы: {len(files)}. Выбрано: {len(st['selected_files'])}.\n"
        "Нажимай на файл, чтобы добавить/убрать из контекста.",
        reply_markup=kb,
    )


async def cmd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)

    base = [
        "/start — приветствие + выбор файлов",
        "/files — выбор файлов (inline)",
        "/use <имя|id> — добавить файл в контекст",
        "/clearuse — очистить выбор",
        "/selected — показать выбранные file_id",
        "/ask <вопрос> — задать вопрос",
        "/citations — очищенные цитаты",
        "/sources — страницы PDF-источников",
        "/citsrc — полная цитата + страница источника",
        "/mindmap — mindmap",
        "/relogin — переподключить Kotaemon",
        "/cmd — список команд",
    ]
    if admin_ok(update, s, db):
        base += [
            "",
            "Админ:",
            "/adduser <id> — добавить пользователя",
            "/deluser <id> — отключить пользователя",
            "/users — ACL список",
            "/ingest [file_id|name|path] — обработать PDF(ы) из storage/incoming",
            "/prepdf <pdf_url_or_path> — прогреть все страницы PDF в кэш",
            "/prepdfid <file_id> — прогреть PDF по file_id",
        ]
    await update.message.reply_text("\n".join(base))


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    b: KotaemonBridge = context.application.bot_data["bridge"]
    db: StateDB = context.application.bot_data["db"]
    files = b.list_files()
    if not files:
        return await update.message.reply_text("Файлы не найдены (API вернул пусто).")

    st = db.get_user(update.effective_user.id)
    kb = files_keyboard(files, st["selected_files"], page=0)
    await update.message.reply_text(
        f"Файлы: {len(files)}. Выбрано: {len(st['selected_files'])}.\n"
        "Нажимай на файл, чтобы добавить/убрать из контекста.",
        reply_markup=kb,
    )


async def use_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    if not context.args:
        return await update.message.reply_text("Использование: /use <имя файла или id>")

    needle = " ".join(context.args).strip()
    b: KotaemonBridge = context.application.bot_data["bridge"]
    files = b.list_files()
    chosen = None
    for f in files:
        if needle == f["id"] or needle.lower() == f["name"].lower():
            chosen = f
            break

    if not chosen:
        return await update.message.reply_text("Не нашёл файл. Смотри /files и копируй имя или id точно.")

    db: StateDB = context.application.bot_data["db"]
    uid = update.effective_user.id
    st = db.get_user(uid)
    sel = st["selected_files"]
    if chosen["id"] not in sel:
        sel.append(chosen["id"])
    db.save_user(uid, selected_files=sel)
    await update.message.reply_text(f"Добавил в контекст: {chosen['name']}\nid: {chosen['id']}")


async def clearuse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    db: StateDB = context.application.bot_data["db"]
    db.save_user(update.effective_user.id, selected_files=[])
    await update.message.reply_text("Сбросил выбор документов. Режим Search All.")


async def selected_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    db: StateDB = context.application.bot_data["db"]
    selected = db.get_user(update.effective_user.id)["selected_files"]
    if not selected:
        return await update.message.reply_text("Сейчас выбранных файлов нет (Search All).")
    await update.message.reply_text("Текущие selected file_id:\n" + "\n".join(f"- {x}" for x in selected))


async def relogin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    b: KotaemonBridge = context.application.bot_data["bridge"]
    ok = b.safe_login()
    await update.message.reply_text("Kotaemon relogin: OK" if ok else "Kotaemon relogin: FAIL")


async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    if not admin_ok(update, s, db):
        return await update.message.reply_text("Только для админа.")
    if not context.args:
        return await update.message.reply_text("Использование: /adduser <telegram_id>")
    try:
        uid = int(context.args[0])
    except Exception:
        return await update.message.reply_text("Неверный id")
    db.add_user(uid, admin=False)
    await update.message.reply_text(f"Пользователь {uid} добавлен.")


async def deluser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    if not admin_ok(update, s, db):
        return await update.message.reply_text("Только для админа.")
    if not context.args:
        return await update.message.reply_text("Использование: /deluser <telegram_id>")
    try:
        uid = int(context.args[0])
    except Exception:
        return await update.message.reply_text("Неверный id")
    db.disable_user(uid)
    await update.message.reply_text(f"Пользователь {uid} отключен.")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    if not admin_ok(update, s, db):
        return await update.message.reply_text("Только для админа.")
    rows = db.list_acl()
    if not rows:
        return await update.message.reply_text("ACL пуст")
    txt = []
    for uid, is_admin, enabled in rows:
        role = "admin" if is_admin else "user"
        status = "on" if enabled else "off"
        txt.append(f"- {uid} | {role} | {status}")
    await update.message.reply_text("\n".join(txt))


async def ingest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    b: KotaemonBridge = context.application.bot_data["bridge"]
    if not auth_ok(update, s, db):
        return await deny(update)
    if not admin_ok(update, s, db):
        return await update.message.reply_text("Только для админа.")

    sp = storage_paths()
    incoming = sp["incoming"]

    if not context.args:
        files = sorted(incoming.glob("*.pdf"))
        if not files:
            return await update.message.reply_text("В storage/incoming нет PDF.")
        ok = 0
        for p in files:
            done, _ = ingest_local_pdf(p, p.stem, aliases=[p.name, p.stem])
            if done:
                ok += 1
        return await update.message.reply_text(f"Индексация завершена: {ok}/{len(files)} PDF.")

    needle = " ".join(context.args).strip()
    p = Path(needle)
    cand: Path | None = None
    aliases = [needle]

    if p.exists() and p.is_file() and p.suffix.lower() == ".pdf":
        cand = p
    else:
        # incoming by exact filename, stem or file_id
        direct = incoming / needle
        if direct.exists() and direct.is_file() and direct.suffix.lower() == ".pdf":
            cand = direct
        else:
            with_ext = incoming / f"{needle}.pdf"
            if with_ext.exists() and with_ext.is_file():
                cand = with_ext

    # try map from kotaemon file list (id/name) to incoming
    if cand is None:
        for f in b.list_files():
            if needle == f.get("id") or needle.lower() == str(f.get("name", "")).lower():
                n = str(f.get("name", "")).strip()
                fid = str(f.get("id", "")).strip()
                aliases += [n, fid]
                for x in [incoming / n, incoming / f"{fid}.pdf", incoming / fid, incoming / f"{n}.pdf"]:
                    if x.exists() and x.is_file() and x.suffix.lower() == ".pdf":
                        cand = x
                        break
                if cand:
                    break

    if cand is None:
        return await update.message.reply_text(
            "Не нашёл локальный PDF. Положи файл в storage/incoming с именем <file_id>.pdf или <name>.pdf, затем /ingest <file_id|name>."
        )

    key = storage_key(needle)
    done, pages = ingest_local_pdf(cand, key, aliases=aliases + [cand.name, cand.stem])
    if not done:
        return await update.message.reply_text("Ошибка обработки PDF.")
    await update.message.reply_text(f"Готово: {cand.name} обработан, страниц: {pages}.")


async def _run_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, q: str):
    db: StateDB = context.application.bot_data["db"]
    b: KotaemonBridge = context.application.bot_data["bridge"]
    uid = update.effective_user.id
    st = db.get_user(uid)

    wait_msg = await update.message.reply_text("Думаю...")
    try:
        answer, hist, retrieval, mindmap = b.ask(q, st["selected_files"], st["last_chat_history"])
    except Exception as e:
        log.exception("ask failed: %s", e)
        return await wait_msg.edit_text("Ошибка при запросе к Kotaemon. Попробуй /relogin и повтори вопрос.")

    targets = prepare_pdf_targets(retrieval, context.application.bot_data["settings"].kotaemon_url)
    db.save_user(
        uid,
        last_chat_history=hist,
        last_retrieval_html=retrieval,
        last_mindmap=mindmap,
        last_pdf_targets=targets,
    )

    selected = "Search All" if not st["selected_files"] else "Выбрано file_id: " + ", ".join(st["selected_files"][:5])
    msg = f"{answer}\n\n—\n{selected}"
    if len(msg) > 3900:
        msg = msg[:3900] + "\n..."
    await wait_msg.edit_text(msg, reply_markup=action_keyboard())


async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    if not context.args:
        return await update.message.reply_text("Использование: /ask <вопрос>")
    await _run_ask(update, context, " ".join(context.args).strip())


async def citations_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    st = context.application.bot_data["db"].get_user(update.effective_user.id)
    await update.message.reply_text(parse_citations_text(st["last_retrieval_html"]))


async def sources_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    b: KotaemonBridge = context.application.bot_data["bridge"]
    if not auth_ok(update, s, db):
        return await deny(update)
    st = context.application.bot_data["db"].get_user(update.effective_user.id)

    # Fast path: send one local PDF built from cited pages
    idx = load_storage_index().get("items", {})
    selected_ids = st.get("selected_files", [])
    if not selected_ids:
        return await update.message.reply_text("Сначала выбери документ(ы) через /files.")
    log.info("sources_cmd selected_ids=%s", selected_ids)
    matched_local = False
    for sid in selected_ids:
        target_item = None
        needle = str(sid).replace("\u200b", "").strip().lower()
        # direct key match first
        if needle in idx:
            target_item = idx.get(needle)
        else:
            for _, item in idx.items():
                aliases = [str(x).replace("\u200b", "").strip().lower() for x in item.get("aliases", [])]
                if needle in aliases:
                    target_item = item
                    break
        if target_item:
            matched_local = True
            log.info("sources_cmd local_match sid=%s source=%s", sid, target_item.get("source"))
            cited_pages = cited_pages_for_item(st, target_item, s.kotaemon_url, max_pages=10)
            log.info("sources_cmd cited_pages sid=%s pages=%s", sid, cited_pages)
            if not cited_pages:
                return await update.message.reply_text("Не нашёл страницы цитат для выбранного файла. Сначала задай вопрос по нему.")
            pages_dir = Path(target_item.get("pdf_pages_dir", ""))
            limited = Path("./out") / f"sources_local_{update.effective_user.id}_{storage_key(str(sid))}_cited.pdf"
            okp, np = build_pdf_from_local_pages(pages_dir, limited, cited_pages, max_pages=10)
            log.info("sources_cmd local_build sid=%s ok=%s pages=%s out=%s", sid, okp, np, limited)
            if okp:
                pages_txt = ', '.join(str(x) for x in cited_pages)
                cap = f"стр.: {pages_txt}"
                await update.message.reply_text(f"Страницы PDF: {pages_txt}")
                ok_send = await send_document_retry(update.message, limited, caption=cap)
                if ok_send:
                    return
                await update.message.reply_text("Основная отправка подвисла, запускаю фоновую отправку единого PDF…")
                schedule_background_pdf_send(update.message, limited, caption=cap)
                return
            return await update.message.reply_text("Не удалось собрать локальный единый PDF из страниц цитат. Запусти /ingest <file_id> заново.")

    if selected_ids and not matched_local:
        log.info("sources_cmd no_local_match selected_ids=%s index_keys=%s", selected_ids, list(idx.keys()))
        return await update.message.reply_text("Для выбранного файла нет локального индекса. Сделай /ingest <file_id>.")

    # Best quality: render original PDF pages from cached metadata
    targets = targets_from_state(st, s.kotaemon_url)
    if not targets:
        targets = prepare_pdf_targets(st["last_retrieval_html"], s.kotaemon_url)
        if targets:
            db.save_user(update.effective_user.id, last_pdf_targets=targets)
    if targets:
        pages_txt = format_pages_brief(targets)
        if pages_txt:
            await update.message.reply_text(f"Страницы PDF: {pages_txt}")

        await update.message.reply_text("Собираю единый PDF...")
        combined = Path("./out") / f"sources_combined_{update.effective_user.id}.pdf"
        ok_pdf, pages_in_pdf = build_combined_pdf_from_targets(
            targets,
            combined,
            zoom=2.6,
            cookies=b.client.cookies,
        )
        if ok_pdf:
            log.info("sources_cmd remote_combined out=%s pages=%s", combined, pages_in_pdf)
            ok_send = await send_document_retry(update.message, combined, caption=f"PDF источники: {pages_in_pdf} стр.")
            if ok_send:
                return

        # fallback: send individual pages if combined PDF failed
        sent = 0
        for i, (pdf_url, page_num) in enumerate(targets[:6], 1):
            out = get_or_render_pdf_page_png(pdf_url, page_num, zoom=2.6, cookies=b.client.cookies)
            if out:
                ok_send = await send_document_retry(update.message, out, caption=("Sources (fallback pages)" if i == 1 else None))
                if ok_send:
                    sent += 1
        if sent:
            return

    # Fallback: embedded evidence images
    blobs = extract_embedded_images(st["last_retrieval_html"], limit=6)
    if blobs:
        for i, blob in enumerate(blobs, 1):
            bio = io.BytesIO(blob)
            bio.name = f"source_{i}.png"
            await update.message.reply_document(document=bio, caption=("Sources" if i == 1 else None))
        return

    links = parse_source_links(st["last_retrieval_html"])
    if not links:
        return await update.message.reply_text("Sources: не найдено изображений и ссылок.")
    out = []
    for i, (t, h) in enumerate(links[:20], 1):
        out.append(f"{i}. {t}\n{h}")
    txt = "\n\n".join(out)
    await update.message.reply_text(txt[:3900])


async def prepdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    b: KotaemonBridge = context.application.bot_data["bridge"]
    if not auth_ok(update, s, db):
        return await deny(update)
    if not admin_ok(update, s, db):
        return await update.message.reply_text("Только для админа.")

    if not context.args:
        return await update.message.reply_text(
            "Использование: /prepdf <pdf_url_or_path>\n"
            "Пример: /prepdf /files/manual.pdf"
        )

    arg = " ".join(context.args).strip()
    pdf_url = arg
    if pdf_url.startswith("/") and not Path(pdf_url).exists():
        pdf_url = s.kotaemon_url.rstrip("/") + pdf_url

    is_local = pdf_url.startswith("/") and Path(pdf_url).exists()
    if (not pdf_url.lower().startswith(("http://", "https://"))) and (not is_local):
        return await update.message.reply_text("Нужен прямой URL/путь к PDF (http(s)://... или локальный /path/to/file.pdf)")

    await update.message.reply_text(f"Прогрев всех страниц PDF: {pdf_url}")
    total, rendered, cached = prewarm_pdf_all_pages(pdf_url, zoom=2.6, cookies=b.client.cookies)
    if total <= 0:
        return await update.message.reply_text("Не удалось открыть PDF. Проверь ссылку/путь и доступность файла.")

    await update.message.reply_text(
        f"Готово. Всего страниц: {total}. Новых отрисовано: {rendered}. Уже было в кэше: {cached}."
    )


async def prepdfid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    b: KotaemonBridge = context.application.bot_data["bridge"]
    if not auth_ok(update, s, db):
        return await deny(update)
    if not admin_ok(update, s, db):
        return await update.message.reply_text("Только для админа.")

    if not context.args:
        return await update.message.reply_text("Использование: /prepdfid <file_id>")

    file_id = " ".join(context.args).strip()
    pdf_url = try_resolve_pdf_url_by_id(s, b, file_id)
    if not pdf_url:
        return await update.message.reply_text("Не смог найти PDF по file_id. Дай прямой путь через /prepdf /path/to/file.pdf")

    await update.message.reply_text(f"Прогрев file_id={file_id}\nPDF: {pdf_url}")
    total, rendered, cached = prewarm_pdf_all_pages(pdf_url, zoom=2.6, cookies=b.client.cookies)
    if total <= 0:
        return await update.message.reply_text("Не удалось открыть PDF по найденной ссылке.")
    await update.message.reply_text(
        f"Готово. Всего страниц: {total}. Новых отрисовано: {rendered}. Уже в кэше: {cached}."
    )


async def citsrc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    b: KotaemonBridge = context.application.bot_data["bridge"]
    if not auth_ok(update, s, db):
        return await deny(update)
    st = db.get_user(update.effective_user.id)
    rows = extract_citations_data(st.get("last_retrieval_html", ""), top_n=5)
    if not rows:
        return await update.message.reply_text("Нет цитат для связки с источниками.")

    for i, r in enumerate(rows, 1):
        header = f"{i}) стр. {r['page']} | score {r['score']:g} | {r['doc']}"

        src = (r.get("data_src") or "").strip()
        page = int(r.get("page") or 1)
        out: Path | None = None
        if src:
            if src.startswith("/"):
                src = s.kotaemon_url.rstrip("/") + src
            out = get_or_render_pdf_page_png(src, page, zoom=2.6, cookies=b.client.cookies)

        await send_citsrc_item(update.message, out, header, r.get("snippet", ""))


async def mindmap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    st = context.application.bot_data["db"].get_user(update.effective_user.id)
    mm = st["last_mindmap"]

    out_dir = Path("./out")
    out_dir.mkdir(exist_ok=True)
    png = out_dir / f"mindmap_{update.effective_user.id}.png"

    # 1) direct plotly render
    if mm and render_mindmap_png(mm, png):
        with png.open("rb") as f:
            return await update.message.reply_document(document=f, caption="Mindmap")

    # 2) render markmap markdown as image
    md = extract_mindmap_markdown(st.get("last_retrieval_html", ""))
    pretty_ok = False
    if md:
        pretty_ok = await render_mindmap_markdown_png_pretty(md, png)
    if md and (pretty_ok or render_mindmap_markdown_png(md, png)):
        with png.open("rb") as f:
            return await update.message.reply_document(document=f, caption="Mindmap")

    return await update.message.reply_text("Mindmap недоступен для этого ответа.")


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except BadRequest as e:
        if "Query is too old" not in str(e):
            log.warning("callback answer bad request: %s", e)
    except Exception as e:
        log.warning("callback answer failed (ignored): %s", e)
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)

    data = q.data or ""
    b: KotaemonBridge = context.application.bot_data["bridge"]

    # file picker callbacks
    if data.startswith("file:"):
        b: KotaemonBridge = context.application.bot_data["bridge"]
        db: StateDB = context.application.bot_data["db"]
        st = db.get_user(update.effective_user.id)
        files = b.list_files()

        if data == "file:noop":
            return
        if data == "file:clear":
            db.save_user(update.effective_user.id, selected_files=[])
            kb = files_keyboard(files, [], page=0)
            return await safe_edit_text(
                q.message,
                f"Файлы: {len(files)}. Выбрано: 0.\nНажимай на файл, чтобы добавить/убрать.",
                reply_markup=kb,
            )
        if data.startswith("file:page:"):
            page = 0
            try:
                page = int(data.split(":", 2)[2])
            except Exception:
                page = 0
            kb = files_keyboard(files, st["selected_files"], page=page)
            return await safe_edit_text(
                q.message,
                f"Файлы: {len(files)}. Выбрано: {len(st['selected_files'])}.\nНажимай на файл, чтобы добавить/убрать.",
                reply_markup=kb,
            )
        if data.startswith("file:toggle:"):
            file_id = data.split(":", 2)[2]
            sel = st["selected_files"]
            if file_id in sel:
                sel = [x for x in sel if x != file_id]
            else:
                sel.append(file_id)
            db.save_user(update.effective_user.id, selected_files=sel)
            kb = files_keyboard(files, sel, page=0)
            return await safe_edit_text(
                q.message,
                f"Файлы: {len(files)}. Выбрано: {len(sel)}.\nНажимай на файл, чтобы добавить/убрать.",
                reply_markup=kb,
            )

    if data == "act:cit":
        st = context.application.bot_data["db"].get_user(update.effective_user.id)
        return await q.message.reply_text(parse_citations_text(st["last_retrieval_html"]))
    if data == "act:src":
        st = context.application.bot_data["db"].get_user(update.effective_user.id)

        idx = load_storage_index().get("items", {})
        selected_ids = st.get("selected_files", [])
        if not selected_ids:
            return await q.message.reply_text("Сначала выбери документ(ы) через /files.")
        log.info("act:src selected_ids=%s", selected_ids)
        matched_local = False
        for sid in selected_ids:
            target_item = None
            needle = str(sid).replace("\u200b", "").strip().lower()
            if needle in idx:
                target_item = idx.get(needle)
            else:
                for _, item in idx.items():
                    aliases = [str(x).replace("\u200b", "").strip().lower() for x in item.get("aliases", [])]
                    if needle in aliases:
                        target_item = item
                        break
            if target_item:
                matched_local = True
                log.info("act:src local_match sid=%s source=%s", sid, target_item.get("source"))
                cited_pages = cited_pages_for_item(st, target_item, s.kotaemon_url, max_pages=10)
                log.info("act:src cited_pages sid=%s pages=%s", sid, cited_pages)
                if not cited_pages:
                    return await q.message.reply_text("Не нашёл страницы цитат для выбранного файла. Сначала задай вопрос по нему.")
                pages_dir = Path(target_item.get("pdf_pages_dir", ""))
                limited = Path("./out") / f"sources_local_{update.effective_user.id}_{storage_key(str(sid))}_cited.pdf"
                okp, np = build_pdf_from_local_pages(pages_dir, limited, cited_pages, max_pages=10)
                log.info("act:src local_build sid=%s ok=%s pages=%s out=%s", sid, okp, np, limited)
                if okp:
                    pages_txt = ', '.join(str(x) for x in cited_pages)
                    cap = f"стр.: {pages_txt}"
                    await q.message.reply_text(f"Страницы PDF: {pages_txt}")
                    ok_send = await send_document_retry(q.message, limited, caption=cap)
                    if ok_send:
                        return
                    await q.message.reply_text("Основная отправка подвисла, запускаю фоновую отправку единого PDF…")
                    schedule_background_pdf_send(q.message, limited, caption=cap)
                    return
                return await q.message.reply_text("Не удалось собрать локальный единый PDF из страниц цитат. Запусти /ingest <file_id> заново.")

        if selected_ids and not matched_local:
            log.info("act:src no_local_match selected_ids=%s index_keys=%s", selected_ids, list(idx.keys()))
            return await q.message.reply_text("Для выбранного файла нет локального индекса. Сделай /ingest <file_id>.")

        targets = targets_from_state(st, s.kotaemon_url)
        if not targets:
            targets = prepare_pdf_targets(st["last_retrieval_html"], s.kotaemon_url)
            if targets:
                context.application.bot_data["db"].save_user(update.effective_user.id, last_pdf_targets=targets)
        if targets:
            pages_txt = format_pages_brief(targets)
            if pages_txt:
                await q.message.reply_text(f"Страницы PDF: {pages_txt}")

            await q.message.reply_text("Собираю единый PDF...")
            combined = Path("./out") / f"sources_combined_{update.effective_user.id}.pdf"
            ok_pdf, pages_in_pdf = build_combined_pdf_from_targets(
                targets,
                combined,
                zoom=2.6,
                cookies=b.client.cookies,
            )
            if ok_pdf:
                log.info("act:src remote_combined out=%s pages=%s", combined, pages_in_pdf)
                ok_send = await send_document_retry(q.message, combined, caption=f"PDF источники: {pages_in_pdf} стр.")
                if ok_send:
                    return

            # fallback: send individual pages if combined PDF failed
            sent = 0
            for i, (pdf_url, page_num) in enumerate(targets[:6], 1):
                out = get_or_render_pdf_page_png(pdf_url, page_num, zoom=2.6, cookies=b.client.cookies)
                if out:
                    ok_send = await send_document_retry(q.message, out, caption=("Sources (fallback pages)" if i == 1 else None))
                    if ok_send:
                        sent += 1
            if sent:
                return

        blobs = extract_embedded_images(st["last_retrieval_html"], limit=6)
        if blobs:
            for i, blob in enumerate(blobs, 1):
                bio = io.BytesIO(blob)
                bio.name = f"source_{i}.png"
                await q.message.reply_document(document=bio, caption=("Sources" if i == 1 else None))
            return
        links = parse_source_links(st["last_retrieval_html"])
        if not links:
            return await q.message.reply_text("Sources: не найдено изображений и ссылок.")
        out = "\n\n".join(f"{i+1}. {t}\n{h}" for i, (t, h) in enumerate(links[:20]))
        return await q.message.reply_text(out[:3900])

    if data == "act:collage":
        st = context.application.bot_data["db"].get_user(update.effective_user.id)
        idx = load_storage_index().get("items", {})
        selected_ids = st.get("selected_files", [])
        log.info("act:collage selected_ids=%s", selected_ids)
        if not selected_ids:
            return await q.message.reply_text("Сначала выбери документ(ы) через /files.")
        await q.message.reply_text("Собираю PNG альбом…")

        for sid in selected_ids:
            target_item = None
            needle = str(sid).replace("\u200b", "").strip().lower()
            log.info("act:collage check sid=%s needle=%s", sid, needle)
            if needle in idx:
                target_item = idx.get(needle)
                log.info("act:collage direct key match sid=%s", sid)
            else:
                for key, item in idx.items():
                    aliases = [str(x).replace("\u200b", "").strip().lower() for x in item.get("aliases", [])]
                    if needle in aliases:
                        target_item = item
                        log.info("act:collage alias match sid=%s key=%s", sid, key)
                        break
            if not target_item:
                log.info("act:collage no match sid=%s", sid)
                continue

            pages = cited_pages_for_item(st, target_item, s.kotaemon_url, max_pages=10)
            log.info("act:collage pages sid=%s pages=%s", sid, pages)
            if not pages:
                return await q.message.reply_text("Не нашёл страницы цитат для коллажа.")
            png_dir = Path(target_item.get("png_pages_dir", ""))
            pngs = [png_dir / f"page-{p:04d}.png" for p in pages]
            missing = [str(p) for p in pngs if not p.exists()]
            if missing:
                log.warning("act:collage missing png sid=%s count=%s first=%s", sid, len(missing), missing[0])
            existing_pngs = [p for p in pngs if p.exists()][:10]
            if not existing_pngs:
                return await q.message.reply_text("Не нашёл PNG для отправки.")

            try:
                media = []
                for i, p in enumerate(existing_pngs):
                    with p.open("rb") as f:
                        blob = io.BytesIO(f.read())
                    blob.name = p.name
                    media.append(
                        InputMediaPhoto(
                            media=blob,
                            caption=(f"PNG по цитатам ({len(existing_pngs)} стр.)" if i == 0 else None),
                        )
                    )

                log.info("act:collage send_media_group sid=%s count=%s", sid, len(existing_pngs))
                ok = await send_media_group_retry(q.message, media)
                if not ok:
                    return await q.message.reply_text("Не удалось отправить PNG альбом (таймаут).")
                log.info("act:collage send_media_group success sid=%s", sid)
            except Exception as e:
                log.exception("act:collage send_media_group failed sid=%s err=%s", sid, e)
                return await q.message.reply_text("Не удалось отправить PNG альбом.")
            return

        return await q.message.reply_text("Для выбранного файла нет локального индекса. Сделай /ingest <file_id>.")

    if data == "act:citsrc":
        st = context.application.bot_data["db"].get_user(update.effective_user.id)
        rows = extract_citations_data(st.get("last_retrieval_html", ""), top_n=5)
        if not rows:
            return await q.message.reply_text("Нет цитат для связки с источниками.")
        for i, r in enumerate(rows, 1):
            header = f"{i}) стр. {r['page']} | score {r['score']:g} | {r['doc']}"

            src = (r.get("data_src") or "").strip()
            page = int(r.get("page") or 1)
            out: Path | None = None
            if src:
                if src.startswith("/"):
                    src = s.kotaemon_url.rstrip("/") + src
                out = get_or_render_pdf_page_png(src, page, zoom=2.6, cookies=b.client.cookies)

            await send_citsrc_item(q.message, out, header, r.get("snippet", ""))
        return

    if data == "act:mm":
        st = context.application.bot_data["db"].get_user(update.effective_user.id)
        mm = st["last_mindmap"]
        out_dir = Path("./out")
        out_dir.mkdir(exist_ok=True)
        png = out_dir / f"mindmap_{update.effective_user.id}.png"
        if mm and render_mindmap_png(mm, png):
            with png.open("rb") as f:
                return await q.message.reply_document(document=f, caption="Mindmap")
        md = extract_mindmap_markdown(st.get("last_retrieval_html", ""))
        pretty_ok = False
        if md:
            pretty_ok = await render_mindmap_markdown_png_pretty(md, png)
        if md and (pretty_ok or render_mindmap_markdown_png(md, png)):
            with png.open("rb") as f:
                return await q.message.reply_document(document=f, caption="Mindmap")
        return await q.message.reply_text("Mindmap недоступен для этого ответа.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    try:
        upd = "unknown"
        if isinstance(update, Update):
            upd = f"chat={getattr(update.effective_chat, 'id', None)} user={getattr(update.effective_user, 'id', None)}"
        log.exception("Unhandled bot error (%s): %s", upd, context.error)
    except Exception:
        log.exception("Unhandled bot error (failed to format context): %s", context.error)


async def plain_text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s: Settings = context.application.bot_data["settings"]
    db: StateDB = context.application.bot_data["db"]
    if not auth_ok(update, s, db):
        return await deny(update)
    text = (update.message.text or "").strip()
    if not text:
        return
    await _run_ask(update, context, text)


def main():
    settings = get_settings()
    db = StateDB(settings.state_db, bootstrap_admins=settings.whitelist)
    bridge = KotaemonBridge(settings.kotaemon_url, settings.kotaemon_user, settings.kotaemon_pass)

    app = Application.builder().token(settings.token).build()
    app.bot_data["settings"] = settings
    app.bot_data["db"] = db
    app.bot_data["bridge"] = bridge

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("cmd", cmd_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("use", use_cmd))
    app.add_handler(CommandHandler("clearuse", clearuse_cmd))
    app.add_handler(CommandHandler("selected", selected_cmd))
    app.add_handler(CommandHandler("relogin", relogin_cmd))
    app.add_handler(CommandHandler("adduser", adduser_cmd))
    app.add_handler(CommandHandler("deluser", deluser_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("ingest", ingest_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CommandHandler("citations", citations_cmd))
    app.add_handler(CommandHandler("sources", sources_cmd))
    app.add_handler(CommandHandler("prepdf", prepdf_cmd))
    app.add_handler(CommandHandler("prepdfid", prepdfid_cmd))
    app.add_handler(CommandHandler("citsrc", citsrc_cmd))
    app.add_handler(CommandHandler("mindmap", mindmap_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text_fallback))
    app.add_error_handler(on_error)

    log.info("Bridge V2 started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
