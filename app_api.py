"""Веб-приложение Слопчика (PWA для iPhone/Android/ПК) — серверная часть.

Работает на том же сервисе Render, что и бот, и использует тот же «мозг»: нейросети с автопереключением,
память диалога, поиск, документы, картинки, музыка. Установка на iPhone — Safari → «Поделиться» →
«На экран Домой» (без Mac, без аккаунта разработчика, бесплатно).

Вход:
  • Telegram Mini App — личность подтверждается подписью Telegram (initData);
  • iOS — бот выдаёт одноразовый код командой /app, привязанный к реальному Telegram ID.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

log = logging.getLogger("app")
APP_DIR = Path(__file__).parent / "webapp"
DESKTOP_DIR = Path(__file__).parent / "desktop"
TOKEN_TTL = 30 * 24 * 3600
RATE = 25  # запросов в минуту на пользователя
MAX_UPLOAD = 9_000_000


# Одноразовые коды входа. Живут только в памяти процесса и действуют 5 минут.
_APP_LOGIN_CODES: dict = {}
_APP_CODE_ISSUED: dict = {}


def create_login_code(uid: int, name: str) -> str:
    """Создаёт короткоживущий одноразовый код для подтверждённого Telegram-пользователя."""
    now = time.time()
    if now - _APP_CODE_ISSUED.get(uid, 0) < 10:
        raise ValueError("Подождите несколько секунд перед новым запросом кода.")
    code = secrets.token_urlsafe(6)
    digest = hashlib.sha256(code.encode()).hexdigest()
    _APP_LOGIN_CODES[digest] = (int(uid), str(name)[:80], now + 300)
    _APP_CODE_ISSUED[uid] = now
    # Удаляем просроченные записи, чтобы словарь не рос бесконечно.
    for key, value in list(_APP_LOGIN_CODES.items()):
        if value[2] <= now:
            _APP_LOGIN_CODES.pop(key, None)
    return code


# ---------------- токены и вход ----------------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret(c) -> bytes:
    return hashlib.sha256(("slopchik-app|" + c.BOT_TOKEN).encode()).digest()


def make_token(c, uid: int, name: str) -> str:
    payload = _b64(json.dumps({"u": uid, "n": name, "e": int(time.time()) + TOKEN_TTL}, ensure_ascii=False).encode())
    return payload + "." + _b64(hmac.new(_secret(c), payload.encode(), hashlib.sha256).digest())


def read_token(c, token: str):
    try:
        payload, sig = token.split(".", 1)
        if not hmac.compare_digest(_b64(hmac.new(_secret(c), payload.encode(), hashlib.sha256).digest()), sig):
            return None
        d = json.loads(_unb64(payload))
        return (int(d["u"]), d.get("n", "")) if d["e"] > time.time() else None
    except Exception:
        return None


def verify_init_data(c, init: str, max_age: int = 86400):
    """Подпись Telegram Mini App: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app"""
    try:
        data = dict(parse_qsl(init, keep_blank_values=True))
        got = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        key = hmac.new(b"WebAppData", c.BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got):
            return None
        if time.time() - int(data.get("auth_date", 0)) > max_age:
            return None
        return json.loads(data["user"])
    except Exception:
        return None


_hits: dict = {}


def rate_ok(uid: int) -> bool:
    now = time.time()
    h = [t for t in _hits.get(uid, []) if now - t < 60]
    if len(h) >= RATE:
        _hits[uid] = h
        return False
    h.append(now)
    _hits[uid] = h
    return True


# ---------------- ответ ----------------
def _md(t: str) -> str:
    """HTML-теги из ответов инструментов (погода, курсы) → разметка, которую понимает приложение."""
    t = re.sub(r"</?b>", "**", t)
    return re.sub(r"<[^>]+>", "", t)


def _img(data: bytes, mime: str, name: str = "image"):
    return {"type": "image", "mime": mime, "name": name, "b64": base64.b64encode(data).decode()}


def _sniff(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


async def respond(c, state: FSMContext, text: str, att=None) -> dict:
    """Один ход диалога. att = {"name", "mime", "data"} — фото или документ."""
    ex = c.extras
    d = await state.get_data()
    history, summary, custom = d.get("history", []), d.get("summary", ""), d.get("custom", "")
    res = {"text": "", "attachments": [], "provider": ""}

    async def commit(user_text: str, answer: str):
        h = history + [{"role": "user", "content": user_text}, {"role": "assistant", "content": answer[:3000]}]
        await state.update_data(history=h, summary=summary)

    # ----- файл или фото -----
    if att:
        if att["mime"].startswith("image/"):
            reply, prov, summary, hist = await c.vision_reply(summary, history, custom, text, att["data"], att["mime"])
            if not reply:
                res["text"] = c.NO_VISION_TEXT
                return res
            await state.update_data(history=hist, summary=summary)
            res.update(text=reply, provider=prov or "")
            return res
        ext = att["name"].rsplit(".", 1)[-1].lower() if "." in att["name"] else ""
        if ext not in ex.DOC_EXT:
            res["text"] = "Этот формат не читаю. Умею: PDF, DOCX, XLSX, TXT, CSV, JSON, HTML и фото."
            return res
        body, note = await asyncio.to_thread(ex.extract_document, att["data"], att["name"])
        if not body:
            res["text"] = note or "В файле нет текста."
            return res
        q = text or "Кратко перескажи содержание документа и выдели главное."
        reply, prov, summary, hist = await c.grounded_reply(summary, history, custom, q, f"Файл: {att['name']}\n{note}\n\n{body}", f"документ {att['name']}")
        if not reply:
            res["text"] = c.fail_text()
            return res
        await state.update_data(history=hist, summary=summary)
        res.update(text=reply, provider=prov or "")
        return res

    text = text.strip()
    if not text:
        return res

    # ----- быстрые функции без нейросети -----
    p = ex.qr_payload(text)
    if p is not None:
        if p:
            res["attachments"].append(_img(await asyncio.to_thread(ex.qr_png, p), "image/png", "qr.png"))
            res["text"] = f"QR-код: {p[:150]}"
        else:
            res["text"] = "Что закодировать? Например: «QR для https://example.com»"
        return res
    if ex.DOCX_RE.search(text):
        tail = text.split(":", 1)[1].strip() if ":" in text else ""
        body = tail if len(tail) > 40 else c.last_answer(d)
        if not body:
            res["text"] = "Нечего оформлять: сначала получите ответ или пришлите текст после двоеточия."
            return res
        data = await asyncio.to_thread(ex.make_docx, body)
        res["attachments"].append({"type": "file", "name": "Документ.docx", "b64": base64.b64encode(data).decode(),
                                   "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
        res["text"] = "📄 Готово"
        return res
    t = ex.tts_text(text)
    if t is not None:
        body = t or c.last_answer(d)
        if not body:
            res["text"] = "Что озвучить? Напишите текст после слова «озвучь» или попросите после ответа."
            return res
        try:
            mp3 = await ex.tts_mp3(body, os.getenv("TTS_VOICE", ""))
            res["attachments"].append({"type": "audio", "mime": "audio/mpeg", "b64": base64.b64encode(mp3).decode(), "title": "Озвучка"})
        except Exception as e:  # noqa
            log.info("tts: %s", e)
            res["text"] = "Озвучка сейчас недоступна, попробуйте позже."
        return res
    codes = ex.currency_codes(text)
    if codes is not None:
        try:
            res["text"] = _md(await ex.rates(codes))
            await commit(text, res["text"])
        except Exception:
            res["text"] = "Не получилось получить курсы ЦБ, попробуйте позже."
        return res
    city = ex.weather_city(text)
    if city is not None:
        if not city:
            res["text"] = "Для какого города? Например: «погода в Ижевске»"
            return res
        try:
            res["text"] = _md(await ex.weather(city))
            await commit(text, res["text"])
        except Exception:
            res["text"] = "Не получилось получить погоду, попробуйте позже."
        return res
    if c.media_dl.parse_request(text):
        res["text"] = "Скачивание видео и аудио доступно в Telegram и ВК. В приложении пока нет."
        return res

    # ----- картинка по описанию -----
    async def draw(desc: str):
        data, info = await c.gen_image(await c.to_english_prompt(desc))
        if not data:
            res["text"] = "Не удалось создать картинку: " + info
        else:
            res["attachments"].append(_img(data, _sniff(data), "image.png"))
            res["text"] = f"🎨 {desc[:200]}"
            await commit(text, res["text"])

    if c.is_image_request(text):
        await draw(text)
        return res

    # ----- поиск готовых картинок / гифок / музыки / видео -----
    if c.is_media_request(text):
        tool, q = await c.route_media(text)
        if tool == "gen":
            await draw(q or text)
            return res
        if tool in ("pic", "gif", "music", "video"):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as sess:
                if tool in ("pic", "gif"):
                    for url in (await c.search_images(sess, q, tool == "gif"))[:10]:
                        data = await c._fetch(sess, url, ("image/",), 5_000_000)
                        if data:
                            res["attachments"].append(_img(data, _sniff(data), "found"))
                            res["text"] = f"🔎 {q}"
                            break
                elif tool == "music":
                    for tr in (await c.search_music(sess, q))[:4]:
                        res["attachments"].append({"type": "audio", "url": tr["preview"], "title": tr["title"], "artist": tr["artist"],
                                                   "link": tr.get("link", ""), "full": bool(tr.get("full"))})
                    res["text"] = f"🎧 {q}" if res["attachments"] else ""
                else:
                    try:
                        for r in await asyncio.to_thread(c._ddg, "videos", q):
                            u = r.get("content") or r.get("url")
                            if u:
                                res["attachments"].append({"type": "link", "url": u, "title": (r.get("title") or q)[:150]})
                                break
                    except Exception as e:  # noqa
                        log.info("videos: %s", e)
                    res["text"] = f"🎬 {q}" if res["attachments"] else ""
            if not res["attachments"]:
                res["text"] = f"Ничего не нашёл по запросу «{q[:100]}». Попробуйте переформулировать."
            else:
                await commit(text, res["text"])
            return res

    # ----- ссылка или веб-поиск -----
    urls = ex.URL_RE.findall(text)
    q = None if urls else ex.search_query(text)
    if urls or q is not None:
        footer = ""
        try:
            if urls:
                url = urls[0].rstrip(".,;)!?")
                title, body = await ex.page_text(url)
                if not body:
                    raise ValueError("на странице нет читаемого текста")
                question = text.replace(urls[0], "").strip(" ,:;-") or "Кратко перескажи главное из этой страницы."
                context, label = f"Страница: {title}\n{url}\n\n{body}", "ссылка"
            else:
                items = await ex.web_search(q)
                if not items:
                    raise ValueError("поиск ничего не дал")
                question = f"Ответь на запрос, опираясь на результаты поиска (ссылайся на номера источников [1], [2]): {q}"
                context, label = ex.search_context(items), "поиск"
                footer = "\n\nИсточники:\n" + ex.sources_footer(items)
        except Exception as e:  # noqa
            res["text"] = f"Не получилось: {str(e)[:120]}"
            return res
        reply, prov, summary, hist = await c.grounded_reply(summary, history, custom, question, context, label)
        if not reply:
            res["text"] = c.fail_text()
            return res
        await state.update_data(history=hist, summary=summary)
        res.update(text=reply + footer, provider=prov or "")
        return res

    # ----- обычный разговор -----
    history = history + [{"role": "user", "content": text}]
    summary, history = await c.compact(summary, history, custom)
    reply, prov = await c.ask_llm(c.build(summary, history, custom))
    if not reply:
        res["text"] = c.fail_text()
        return res
    history.append({"role": "assistant", "content": reply})
    await state.update_data(history=history, summary=summary)
    res.update(text=reply, provider=prov or "")
    return res


# ---------------- HTTP ----------------
def mount(app: web.Application, c):
    bot_id = int(c.BOT_TOKEN.split(":")[0])

    def state_for(uid: int) -> FSMContext:
        return FSMContext(c.STORAGE, StorageKey(bot_id, uid, uid))

    def auth(request):
        h = request.headers.get("Authorization", "")
        got = read_token(c, h[7:]) if h.startswith("Bearer ") else None
        if not got:
            raise web.HTTPUnauthorized(text=json.dumps({"error": "auth"}), content_type="application/json")
        if not rate_ok(got[0]):
            raise web.HTTPTooManyRequests(text=json.dumps({"error": "Слишком часто, подождите минуту."}), content_type="application/json")
        return got

    async def login(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if body.get("initData"):
            u = verify_init_data(c, body["initData"])
            if not u or not c.allowed(int(u["id"])):
                return web.json_response({"error": "Нет доступа"}, status=403)
            name = u.get("first_name") or u.get("username") or "Telegram"
            return web.json_response({"token": make_token(c, int(u["id"]), name), "name": name, "user_id": str(int(u["id"])), "mode": "telegram"})
        code = str(body.get("code", "")).strip()
        if code:
            digest = hashlib.sha256(code.encode()).hexdigest()
            entry = _APP_LOGIN_CODES.get(digest)
            if entry and entry[2] > time.time() and hmac.compare_digest(digest, hashlib.sha256(code.encode()).hexdigest()):
                # Одноразовое использование: удаляем до выдачи токена.
                _APP_LOGIN_CODES.pop(digest, None)
                uid, name, _ = entry
                if c.allowed(uid):
                    return web.json_response({"token": make_token(c, uid, name), "name": name, "user_id": str(int(uid)), "mode": "telegram"})
        await asyncio.sleep(0.8)
        return web.json_response({"error": "Код неверный или уже истёк. Запросите новый в Telegram через /app."}, status=403)

    async def history(request):
        uid, _ = auth(request)
        d = await state_for(uid).get_data()
        msgs = [{"role": m["role"], "text": m["content"] if isinstance(m["content"], str) else ""} for m in d.get("history", [])[-30:]]
        return web.json_response({"messages": msgs, "custom": bool(d.get("custom"))})

    async def chat(request):
        uid, _ = auth(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        text = str(body.get("text", ""))[:6000]
        att = None
        f = body.get("file")
        if f and f.get("b64"):
            data = base64.b64decode(f["b64"])
            if len(data) > MAX_UPLOAD:
                return web.json_response({"error": "Файл слишком большой (до 9 МБ)."}, status=413)
            att = {"name": str(f.get("name", "file"))[:120], "mime": str(f.get("mime", "")) or "application/octet-stream", "data": data}
        try:
            res = await respond(c, state_for(uid), text, att)
        except Exception as e:  # noqa
            log.exception("chat")
            return web.json_response({"error": "Ошибка на сервере, попробуйте ещё раз."}, status=500)
        return web.json_response(res)

    async def voice(request):
        uid, _ = auth(request)
        data = await request.read()
        if not data or len(data) > MAX_UPLOAD:
            return web.json_response({"error": "Запись пустая или слишком длинная."}, status=400)
        heard, info = await c.transcribe(data, request.headers.get("Content-Type", "audio/mp4"))
        if not heard:
            return web.json_response({"error": "Не удалось распознать голос: " + info[:150]}, status=422)
        res = await respond(c, state_for(uid), heard)
        res["heard"] = heard
        return web.json_response(res)

    async def reset(request):
        uid, _ = auth(request)
        st = state_for(uid)
        d = await st.get_data()
        await st.clear()
        await st.update_data(custom=d.get("custom", ""))
        return web.json_response({"ok": True})

    async def prompt(request):
        uid, _ = auth(request)
        body = await request.json()
        t = str(body.get("text", "")).strip()[:1500]
        st = state_for(uid)
        if body.get("get"):
            return web.json_response({"text": (await st.get_data()).get("custom", "")})
        await st.update_data(custom=t, history=[], summary="")
        return web.json_response({"ok": True, "text": t})

    async def index(request):
        return web.FileResponse(APP_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def desktop_index(request):
        return web.FileResponse(DESKTOP_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def sw(request):
        return web.FileResponse(APP_DIR / "sw.js", headers={"Cache-Control": "no-cache", "Content-Type": "application/javascript"})

    async def manifest(request):
        return web.FileResponse(APP_DIR / "manifest.webmanifest", headers={"Content-Type": "application/manifest+json"})

    app.router.add_post("/api/login", login)
    app.router.add_get("/api/history", history)
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/voice", voice)
    app.router.add_post("/api/reset", reset)
    app.router.add_post("/api/prompt", prompt)
    app.router.add_get("/desktop", lambda r: web.HTTPFound("/desktop/"))
    app.router.add_get("/desktop/", desktop_index)
    app.router.add_get("/app", lambda r: web.HTTPFound("/app/"))
    app.router.add_get("/app/", index)
    app.router.add_get("/app/sw.js", sw)
    app.router.add_get("/app/manifest.webmanifest", manifest)
    ALLOWED_STATIC = re.compile(r"^[\w\-]+\.(png|ico|svg|webmanifest|js|css|html)$")

    async def static(request):
        """Отдаём только файлы приложения: случайно загруженные .py и прочее наружу не уходят."""
        name = request.match_info["name"]
        path = (APP_DIR / name).resolve()
        if not ALLOWED_STATIC.match(name) or APP_DIR.resolve() not in path.parents or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    app.router.add_get("/app/{name}", static)
    log.info("web-приложение: /app/ (коды: %s)", "заданы" if _APP_LOGIN_CODES else "не заданы — вход только через Telegram")
