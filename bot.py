import asyncio
import base64
import html
import json
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import aiohttp

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BufferedInputFile, Message
from aiohttp import web
from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    BadRequestError,
    RateLimitError,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

BASE = Path(__file__).parent
BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.getenv("PORT", "8080"))
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()}
SHOW_PROVIDER = os.getenv("SHOW_PROVIDER", "0") == "1"
SHOW_TRANSCRIPT = os.getenv("SHOW_TRANSCRIPT", "0") == "1"
COMPACT_AT = int(os.getenv("COMPACT_AT", "3500"))  # ~токенов, после чего сжимаем историю
CF_ACCOUNT = os.getenv("CF_ACCOUNT_ID", "")
CF_TOKEN = os.getenv("CF_API_TOKEN", "")
CF_MODEL = os.getenv("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
KEEP_LAST = 4  # сколько последних реплик оставлять дословно при сжатии

SC = json.loads((BASE / "scenario.json").read_text(encoding="utf-8"))
router = Router()


# ================= провайдеры + автопереключение =================
class Provider:
    def __init__(self, c: dict, key: str):
        self.name = c["name"]
        self.model = c["model"]
        self.base_url = c["base_url"]
        self.ctx = int(c.get("ctx", 8000))  # лимит контекста в токенах
        self.client = AsyncOpenAI(
            api_key=key, base_url=c["base_url"], max_retries=0, timeout=60
        )
        self.down_until = 0.0
        self.why = ""

    @property
    def ready(self) -> bool:
        return self.down_until <= time.time()

    def ban(self, sec: float, why: str):
        self.why = why
        self.down_until = time.time() + sec
        log.warning("provider %s off for %ss: %s", self.name, int(sec), why)


def load_providers() -> list:
    cfg = json.loads((BASE / "providers.json").read_text(encoding="utf-8"))
    out = [Provider(c, os.environ[c["key_env"]]) for c in cfg if os.getenv(c["key_env"])]
    if not out:
        raise SystemExit("Нет ни одного API-ключа. Задай хотя бы один из providers.json")
    log.info("providers: %s", [p.name for p in out])
    return out


PROVIDERS = load_providers()


def est_tokens(msgs: list) -> int:
    return sum(len(m["content"]) // 3 + 8 for m in msgs)


def fit(messages: list, limit: int) -> list:
    """Обрезает самые старые реплики, чтобы влезть в контекст провайдера."""
    if est_tokens(messages) <= limit:
        return messages
    system, rest = messages[0], messages[1:]
    budget = limit - est_tokens([system]) - 500  # запас под ответ
    kept, used = [], 0
    for m in reversed(rest):
        t = est_tokens([m])
        if kept and used + t > budget:
            break
        kept.append(m)
        used += t
    kept.reverse()
    while len(kept) > 1 and kept[0]["role"] != "user":
        kept = kept[1:]
    return [system] + kept


async def ask_llm(messages: list):
    """Возвращает (текст, имя_провайдера) или (None, None), если все недоступны."""
    ready = [p for p in PROVIDERS if p.ready]
    if not ready:  # все в «карантине» — пробуем тот, что освободится первым
        ready = sorted(PROVIDERS, key=lambda p: p.down_until)[:1]
    for p in ready:
        try:
            r = await p.client.chat.completions.create(
                model=p.model,
                messages=fit(messages, p.ctx),
                temperature=0.7,
                max_tokens=1500,
            )
            text = (r.choices[0].message.content or "").strip()
            if text:
                return text, p.name
            p.ban(60, "empty answer")
        except RateLimitError as e:
            msg = str(e).lower()
            daily = any(w in msg for w in ("day", "daily", "quota", "exceeded your"))
            ra = e.response.headers.get("retry-after") if e.response else None
            sec = float(ra) + 1 if ra and ra.replace(".", "").isdigit() else (3600 if daily else 60)
            p.ban(sec, f"429: {msg[:160]}")
        except BadRequestError as e:
            msg = str(e).lower()
            if any(w in msg for w in ("context", "length", "too long", "token")):
                p.ctx = int(p.ctx * 0.7)  # запомним: контекст у него меньше
                log.warning("provider %s: ctx lowered to %s", p.name, p.ctx)
            else:
                p.ban(300, f"400: {msg[:120]}")
        except APIStatusError as e:
            code = e.status_code
            if code in (401, 403, 404):
                p.ban(86400, f"{code}: {str(e)[:160]}")
            elif code == 402:
                p.ban(6 * 3600, f"402: {str(e)[:160]}")
            elif code >= 500:
                p.ban(120, f"{code}: {str(e)[:160]}")
            else:
                p.ban(300, f"{code}: {str(e)[:160]}")
        except APIConnectionError as e:
            p.ban(120, f"connection: {str(e)[:160]}")
        except Exception as e:  # noqa
            log.exception("unexpected")
            p.ban(300, repr(e))
    return None, None


# ================= контекст: сжатие вместо потери =================
def build(summary: str, history: list, custom: str = "") -> list:
    sys = custom or SC["system_prompt"]
    if summary:
        sys += "\n\nКраткое содержание предыдущего диалога:\n" + summary
    return [{"role": "system", "content": sys}] + history


async def compact(summary: str, history: list, custom: str = ""):
    """Если контекст разросся — сжимаем старую часть в резюме (на любом доступном провайдере)."""
    if est_tokens(history) + len(summary) // 3 <= COMPACT_AT or len(history) <= KEEP_LAST:
        return summary, history
    old, keep = history[:-KEEP_LAST], history[-KEEP_LAST:]
    while len(keep) > 1 and keep[0]["role"] != "user":
        old.append(keep.pop(0))
    dialog = "\n".join(f"{m['role']}: {m['content']}" for m in old)
    prompt = (
        f"Прежнее резюме:\n{summary or '—'}\n\nНовые реплики:\n{dialog}\n\n"
        "Обнови краткое резюме диалога (до 1500 символов): задача пользователя, "
        "его данные и предпочтения, принятые решения, важные факты. Только резюме."
    )
    text, _ = await ask_llm(
        [
            {"role": "system", "content": "Ты сжимаешь диалоги без потери важных фактов."},
            {"role": "user", "content": prompt},
        ]
    )
    if text:
        return text, keep
    return summary, history  # не вышло — ничего не теряем, fit() подрежет при вызове



# ================= генерация картинок =================
async def gen_image(prompt: str):
    """Возвращает (bytes, источник) или (None, текст_ошибки). Cloudflare -> Pollinations."""
    errors = []
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        if CF_ACCOUNT and CF_TOKEN:
            try:
                url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{CF_MODEL}"
                async with sess.post(
                    url,
                    headers={"Authorization": f"Bearer {CF_TOKEN}"},
                    json={"prompt": prompt, "steps": 6},
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return base64.b64decode(data["result"]["image"]), "cloudflare"
                    errors.append(f"cloudflare {r.status}")
            except Exception as e:  # noqa
                errors.append(f"cloudflare: {str(e)[:80]}")
        try:
            url = (
                "https://image.pollinations.ai/prompt/" + quote(prompt[:900])
                + f"?width=1024&height=1024&nologo=true&model=flux&seed={random.randint(1, 10**6)}"
            )
            async with sess.get(url) as r:
                if r.status == 200 and r.content_type.startswith("image"):
                    return await r.read(), "pollinations"
                errors.append(f"pollinations {r.status}")
        except Exception as e:  # noqa
            errors.append(f"pollinations: {str(e)[:80]}")
    return None, "; ".join(errors)


async def to_english_prompt(text: str) -> str:
    out, _ = await ask_llm(
        [
            {
                "role": "system",
                "content": "Пользователь просит создать изображение (может быть фраза вроде "
                "«сделай мне картинку кота»). Выдели, что нужно изобразить, и составь "
                "короткий детальный промпт для генератора на английском. Верни только промпт, без кавычек.",
            },
            {"role": "user", "content": text},
        ]
    )
    return (out or text).strip().strip('"')[:900]


# ================= автоопределение «нарисуй» =================
_IMG_VERB = r"(нарису\w*|сгенерир\w*|созда\w*|сдела\w*|изобрази\w*|generate|create|make|draw)"
_IMG_NOUN = r"(картин\w*|изображени\w*|фото\w*|рисун\w*|иллюстраци\w*|постер\w*|аватар\w*|логотип\w*|обои|image|picture|photo|drawing|illustration)"
IMG_RE = re.compile(rf"(?is)^\s*(нарису\w*|draw)\b|\b{_IMG_VERB}\b.{{0,40}}?\b{_IMG_NOUN}")


def is_image_request(text: str) -> bool:
    """Длинные тексты (документы, статьи) картинкой не считаем."""
    return len(text) <= 400 and bool(IMG_RE.search(text))


# ================= распознавание речи =================
STT_OPENAI = [  # (имя, base_url, модель, переменная ключа) — понимают ogg напрямую
    ("groq-whisper", "https://api.groq.com/openai/v1", "whisper-large-v3-turbo", "GROQ_API_KEY"),
    ("mistral-voxtral", "https://api.mistral.ai/v1", "voxtral-mini-latest", "MISTRAL_API_KEY"),
]
STT_PROMPT = (
    "Дословно расшифруй речь из аудио. Верни только текст расшифровки, без комментариев. "
    "Если речи нет — верни пустую строку."
)
_stt_down: dict = {}


async def transcribe(audio: bytes):
    """Возвращает (текст, источник) или (None, ошибки). Перебирает подходящие модели."""
    errors = []
    now = time.time()
    for name, url, model, env in STT_OPENAI:
        key = os.getenv(env)
        if not key or _stt_down.get(name, 0) > now:
            continue
        try:
            cl = AsyncOpenAI(api_key=key, base_url=url, max_retries=0, timeout=60)
            r = await cl.audio.transcriptions.create(model=model, file=("voice.ogg", audio, "audio/ogg"))
            if (r.text or "").strip():
                return r.text.strip(), name
        except Exception as e:  # noqa
            _stt_down[name] = now + 300
            errors.append(f"{name}: {str(e)[:80]}")
    timeout = aiohttp.ClientTimeout(total=90)
    b64 = base64.b64encode(audio).decode()
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        gkey = os.getenv("GEMINI_API_KEY")
        if gkey:
            models = list(dict.fromkeys(p.model for p in PROVIDERS if "generativelanguage" in p.base_url))
            for model in models:
                name = f"gemini:{model}"
                if _stt_down.get(name, 0) > now:
                    continue
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                    body = {"contents": [{"parts": [
                        {"inline_data": {"mime_type": "audio/ogg", "data": b64}},
                        {"text": STT_PROMPT},
                    ]}]}
                    async with sess.post(url, json=body, headers={"x-goog-api-key": gkey}) as r:
                        data = await r.json()
                        if r.status == 200:
                            text = data["candidates"][0]["content"]["parts"][0].get("text", "").strip()
                            if text:
                                return text, name
                            errors.append(f"{name}: пусто")
                        else:
                            _stt_down[name] = now + (3600 if r.status == 429 else 300)
                            errors.append(f"{name}: {r.status} {str(data)[:80]}")
                except Exception as e:  # noqa
                    _stt_down[name] = now + 300
                    errors.append(f"{name}: {str(e)[:80]}")
        if CF_ACCOUNT and CF_TOKEN and _stt_down.get("cloudflare-whisper", 0) <= now:
            try:
                url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/@cf/openai/whisper-large-v3-turbo"
                async with sess.post(url, headers={"Authorization": f"Bearer {CF_TOKEN}"}, json={"audio": b64}) as r:
                    data = await r.json()
                    text = (data.get("result") or {}).get("text", "").strip() if r.status == 200 else ""
                    if text:
                        return text, "cloudflare-whisper"
                    _stt_down["cloudflare-whisper"] = now + 300
                    errors.append(f"cloudflare-whisper: {r.status}")
            except Exception as e:  # noqa
                _stt_down["cloudflare-whisper"] = now + 300
                errors.append(f"cloudflare-whisper: {str(e)[:80]}")
    return None, "; ".join(errors) or "нет ключей для распознавания речи"


# ================= «встроенные боты»: поиск картинок, гифок, музыки, видео =================
# Telegram не даёт ботам обращаться к inline-режиму других ботов (@pic, @gif…) — это умеют
# только пользовательские аккаунты. Поэтому бот ходит в те же открытые источники напрямую.
UA = {"User-Agent": "Mozilla/5.0 (compatible; TgBot/1.0)"}
GIPHY_KEY = os.getenv("GIPHY_API_KEY", "")
TENOR_KEY = os.getenv("TENOR_API_KEY", "")

_M_NOUN = (r"(картин\w*|фото\w*|изображени\w*|пикч\w*|мем\w*|обои|гиф\w*|gif\w*|анимаци\w*|песн\w*|трек\w*|"
           r"музык\w*|мелоди\w*|композици\w*|саундтрек\w*|видео\w*|ролик\w*|клип\w*|трейлер\w*|"
           r"song|track|music|video|clip|picture|image|photo|meme)")
_M_VERB = (r"(скин\w*|кинь\w*|кину\w*|пришл\w*|присл\w*|найд\w*|покаж\w*|отправ\w*|дай\b|включ\w*|"
           r"постав\w*|хочу|нужн\w*|подбер\w*|show|find|send|play|get)")
MEDIA_RE = re.compile(rf"(?is)\b{_M_VERB}\b.*\b{_M_NOUN}|\b{_M_NOUN}\b.*\b{_M_VERB}")
TOOLS = ("pic", "gif", "music", "video", "gen", "chat")

ROUTER_PROMPT = (
    "Ты — маршрутизатор запросов Telegram-бота. Верни ТОЛЬКО JSON вида "
    '{"tool": "...", "query": "..."} без пояснений.\n'
    "tool:\n"
    "- pic — найти готовую картинку/фото в интернете (скинь, найди, покажи картинку/фото);\n"
    "- gif — найти гифку/анимацию;\n"
    "- music — найти песню/трек;\n"
    "- video — найти видео/ролик/клип/трейлер;\n"
    "- gen — нарисовать новую оригинальную картинку (нарисуй, сгенерируй, придумай картинку);\n"
    "- chat — если это вопрос или просьба не про поиск медиа.\n"
    "query — короткий поисковый запрос без слов-команд. ВАЖНО: если описание явно указывает на "
    "известного персонажа, игру, фильм, песню, человека, место или предмет — назови его по имени "
    "(«коротыш в шляпке прыгающий по грибам видеоигра» → «Mario», «синий ёж быстро бегает» → "
    "«Sonic the Hedgehog»). Иначе оставь суть запроса. Для music: «исполнитель название». "
    "Для gen: подробное описание."
)


def is_media_request(text: str) -> bool:
    return len(text) <= 300 and bool(MEDIA_RE.search(text))


def _strip_words(text: str) -> str:
    t = re.sub(rf"(?i)\b{_M_VERB}\b|\b{_M_NOUN}\b|\bмне\b|\bпожалуйста\b", " ", text)
    return re.sub(r"\s+", " ", t).strip() or text


async def route_media(text: str) -> tuple:
    """(tool, query). LLM решает, что нужно, и формулирует запрос (как «@pic Mario»)."""
    out, _ = await ask_llm(
        [{"role": "system", "content": ROUTER_PROMPT}, {"role": "user", "content": text}]
    )
    try:
        j = json.loads(re.search(r"\{.*\}", out or "", re.S).group(0))
        tool = str(j.get("tool", "")).lower().strip()
        if tool in TOOLS:
            return tool, (str(j.get("query") or "").strip() or _strip_words(text))
    except Exception:
        pass
    low = text.lower()  # запасной вариант без нейросети
    for pat, tool in ((r"гиф|gif|анимаци", "gif"), (r"песн|трек|музык|мелоди|композици|саундтрек|song|track|music", "music"),
                      (r"видео|ролик|клип|трейлер|video|clip", "video")):
        if re.search(pat, low):
            return tool, _strip_words(text)
    return "pic", _strip_words(text)


async def _fetch(sess, url: str, kinds: tuple, limit: int):
    try:
        async with sess.get(url, headers=UA) as r:
            if r.status != 200:
                return None
            ct = (r.content_type or "").lower()
            if not (ct.startswith(kinds) or ct == "application/octet-stream"):
                return None
            data = await r.content.read(limit + 1)
            return data if 0 < len(data) <= limit else None
    except Exception:
        return None


async def _json(sess, url: str, **kw):
    try:
        async with sess.get(url, headers=UA, **kw) as r:
            return await r.json(content_type=None) if r.status == 200 else None
    except Exception:
        return None


def _ddg(kind: str, q: str, **kw):
    from ddgs import DDGS
    return getattr(DDGS(), kind)(q, safesearch="moderate", max_results=12, **kw)


async def search_images(sess, q: str, gif: bool) -> list:
    urls = []
    if gif and GIPHY_KEY:
        j = await _json(sess, f"https://api.giphy.com/v1/gifs/search?api_key={GIPHY_KEY}&q={quote(q)}&limit=8&rating=pg-13")
        urls += [x["images"]["original"]["url"] for x in (j or {}).get("data", []) if x.get("images")]
    if gif and TENOR_KEY:
        j = await _json(sess, f"https://tenor.googleapis.com/v2/search?q={quote(q)}&key={TENOR_KEY}&limit=8&media_filter=gif&contentfilter=medium")
        urls += [x["media_formats"]["gif"]["url"] for x in (j or {}).get("results", []) if x.get("media_formats")]
    try:
        kw = {"type_image": "animated_gif"} if gif else {}
        res = await asyncio.to_thread(_ddg, "images", q, **kw)
        urls += [r["image"] for r in res if r.get("image")]
    except Exception as e:  # noqa
        log.warning("ddgs images: %s", e)
    if not gif:
        j = await _json(sess, f"https://api.openverse.org/v1/images/?q={quote(q)}&page_size=10")
        urls += [x["url"] for x in (j or {}).get("results", []) if x.get("url")]
    return list(dict.fromkeys(urls))


async def search_music(sess, q: str) -> list:
    out = []
    j = await _json(sess, f"https://api.deezer.com/search?q={quote(q)}&limit=5")
    for t in (j or {}).get("data", []):
        if t.get("preview"):
            out.append({"title": t["title"], "artist": t["artist"]["name"], "preview": t["preview"], "link": t.get("link", ""), "ext": "mp3"})
    if not out:
        j = await _json(sess, f"https://itunes.apple.com/search?term={quote(q)}&entity=song&limit=5")
        for t in (j or {}).get("results", []):
            if t.get("previewUrl"):
                out.append({"title": t["trackName"], "artist": t["artistName"], "preview": t["previewUrl"], "link": t.get("trackViewUrl", ""), "ext": "m4a"})
    return out


async def run_tool(m: Message, state: FSMContext, tool: str, q: str):
    timeout = aiohttp.ClientTimeout(total=60)
    ok = False
    action = {"pic": ChatAction.UPLOAD_PHOTO, "gif": ChatAction.UPLOAD_PHOTO,
              "music": ChatAction.UPLOAD_VOICE, "video": ChatAction.TYPING}[tool]
    async with typing(m, action):
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            if tool in ("pic", "gif"):
                gif = tool == "gif"
                for url in (await search_images(sess, q, gif))[:10]:
                    data = await _fetch(sess, url, ("image/",), 20_000_000 if gif else 9_500_000)
                    if not data:
                        continue
                    cap = f"🔎 {esc(q[:200])}"
                    try:
                        if gif:
                            await m.answer_animation(BufferedInputFile(data, filename="anim.gif"), caption=cap)
                        else:
                            await m.answer_photo(BufferedInputFile(data, filename="pic.jpg"), caption=cap)
                        ok = True
                        break
                    except Exception as e:  # Telegram отклонил файл — пробуем следующий
                        log.info("send failed: %s", e)
            elif tool == "music":
                for t in (await search_music(sess, q))[:3]:
                    data = await _fetch(sess, t["preview"], ("audio/",), 15_000_000)
                    if not data:
                        continue
                    link = f'<a href="{html.escape(t["link"])}">Слушать полностью</a> · ' if t["link"] else ""
                    try:
                        await m.answer_audio(
                            BufferedInputFile(data, filename=f'{t["artist"]} - {t["title"]}.{t["ext"]}'),
                            title=t["title"], performer=t["artist"], caption=f"🎧 {link}превью 30 с",
                        )
                        ok = True
                        break
                    except Exception as e:  # noqa
                        log.info("audio send failed: %s", e)
            elif tool == "video":
                try:
                    res = await asyncio.to_thread(_ddg, "videos", q)
                    for r in res:
                        url = r.get("content") or r.get("url")
                        if url:
                            await m.answer(f"🎬 <b>{esc((r.get('title') or q)[:150])}</b>\n{html.escape(url)}")
                            ok = True
                            break
                except Exception as e:  # noqa
                    log.warning("ddgs videos: %s", e)
    await sweep(m.bot, m.chat.id, state)
    if not ok:
        err = await m.answer(f"🔍 Ничего не нашёл по запросу «{esc(q[:100])}». Попробуйте переформулировать.")
        await track(state, err)

# ================= UI-хелперы =================
class Flow(StatesGroup):
    image = State()


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def md_to_html(t: str) -> str:
    t = esc(t)
    t = re.sub(r"```(?:\w+)?\n?(.*?)```", r"<pre>\1</pre>", t, flags=re.S)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)\*(?!\*)", r"<i>\1</i>", t)
    t = re.sub(r"^\s*[-*]\s+", "• ", t, flags=re.M)
    return t


def chunks(t: str, n: int = 3800):
    while t:
        if len(t) <= n:
            yield t
            return
        cut = t.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        yield t[:cut]
        t = t[cut:].lstrip("\n")


async def send_result(msg: Message, text: str, provider: str = ""):
    for p in chunks(text):
        foot_h = f"\n\n<i>⚙️ {esc(provider)}</i>" if provider and SHOW_PROVIDER else ""
        foot_p = f"\n\n⚙️ {provider}" if provider and SHOW_PROVIDER else ""
        try:
            await msg.answer(md_to_html(p) + foot_h)
        except Exception:
            await msg.answer(p + foot_p, parse_mode=None)


def allowed(uid: int) -> bool:
    return not ALLOWED or uid in ALLOWED


def fail_text() -> str:
    why = "\n".join(f"• {p.name}: {p.why or 'нет ответа'}" for p in PROVIDERS)
    return "⚠️ Все нейросети сейчас недоступны. Попробуйте через пару минут.\n" + why[:800]


HELP = (
    "Просто напишите или надиктуйте сообщение — отвечу.\n"
    "• «нарисуй …» — создам картинку\n"
    "• «скинь картинку/гифку/песню/видео …» — найду и пришлю\n\n"
    "/prompt <текст> — задать стиль и роль бота (например: «отвечай кратко, как преподаватель»)\n"
    "/prompt — показать текущую инструкцию, /prompt сброс — вернуть стандартную\n"
    "/image <описание> — нарисовать картинку (можно и просто текстом)\n"
    "/reset — начать новый диалог\n"
    "/status — состояние нейросетей"
)


# ================= чистка служебных сообщений =================
async def track(state: FSMContext, *msgs):
    """Запоминает служебные сообщения (бота и команды пользователя) для удаления."""
    d = await state.get_data()
    ids = d.get("junk", []) + [x.message_id for x in msgs if x]
    await state.update_data(junk=ids[-80:])


async def sweep(bot: Bot, chat_id: int, state: FSMContext):
    """Удаляет всё запомненное. Вопросы пользователя и ответы бота не трогаем."""
    d = await state.get_data()
    ids = d.get("junk", [])
    if not ids:
        return
    await state.update_data(junk=[])
    for i in range(0, len(ids), 100):
        part = ids[i : i + 100]
        try:
            await bot.delete_messages(chat_id, part)
        except Exception:
            for mid in part:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception:
                    pass


@asynccontextmanager
async def typing(m: Message, action=ChatAction.TYPING):
    """Показывает «печатает…» вместо отдельных сообщений-заглушек."""

    async def loop():
        while True:
            try:
                await m.bot.send_chat_action(m.chat.id, action)
            except Exception:
                pass
            await asyncio.sleep(4)

    t = asyncio.create_task(loop())
    try:
        yield
    finally:
        t.cancel()


async def spinner(msg: Message, base: str):
    frames = ["", ".", "..", "..."]
    i = 0
    try:
        while True:
            await asyncio.sleep(2)
            i += 1
            await msg.edit_text(f"{base}{frames[i % 4]}")
    except Exception:
        pass


# ================= хендлеры =================
@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(m: Message, state: FSMContext):
    if not allowed(m.from_user.id):
        return await m.answer("⛔ Нет доступа.")
    r = await m.answer(f"<b>{esc(SC['title'])}</b>\n\n{esc(SC['welcome'])}\n\n{esc(HELP)}")
    await track(state, m, r)


@router.message(Command("reset"))
async def cmd_reset(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    await state.update_data(custom=d.get("custom", ""), junk=d.get("junk", []))
    r = await m.answer("🧹 Диалог очищен. Ваша инструкция сохранена.")
    await track(state, m, r)


@router.message(Command("prompt"))
async def cmd_prompt(m: Message, state: FSMContext, command: CommandObject):
    if not allowed(m.from_user.id):
        return await m.answer("⛔ Нет доступа.")
    arg = (command.args or "").strip()
    d = await state.get_data()
    if not arg:
        cur = d.get("custom") or SC["system_prompt"]
        label = "Ваша инструкция" if d.get("custom") else "Стандартная инструкция"
        r = await m.answer(
            f"<b>{label}:</b>\n{esc(cur)}\n\nИзменить: /prompt <i>текст</i>\nСбросить: /prompt сброс"
        )
    elif arg.lower() in ("сброс", "reset"):
        await state.update_data(custom="", history=[], summary="")
        r = await m.answer("✅ Вернул стандартную инструкцию.")
    else:
        await state.update_data(custom=arg[:1500], history=[], summary="")
        r = await m.answer("✅ Инструкция сохранена, диалог начат заново. Пишите сообщение.")
    await track(state, m, r)


@router.message(Command("status"))
async def cmd_status(m: Message, state: FSMContext):
    if ALLOWED and m.from_user.id not in ALLOWED:
        return
    lines = []
    for p in PROVIDERS:
        st = "🟢" if p.ready else f"🔴 ещё {int(p.down_until - time.time())} с"
        lines.append(f"{st} <b>{esc(p.name)}</b> · {esc(p.model)} · ctx {p.ctx}" + (f"\n<i>{esc(p.why)}</i>" if p.why else ""))
    r = await m.answer("\n".join(lines))
    await track(state, m, r)


async def do_image(m: Message, state: FSMContext, text: str):
    status = await m.answer("🎨 <i>Рисую</i>")
    await track(state, status)
    spin = asyncio.create_task(spinner(status, "🎨 <i>Рисую</i>"))
    try:
        async with typing(m, ChatAction.UPLOAD_PHOTO):
            prompt = await to_english_prompt(text)
            data, info = await gen_image(prompt)
    finally:
        spin.cancel()
    if not data:
        await sweep(m.bot, m.chat.id, state)
        err = await m.answer("⚠️ Не удалось создать картинку: " + info, parse_mode=None)
        return await track(state, err)
    cap = f"🎨 {esc(text[:200])}" + (f"\n<i>⚙️ {info}</i>" if SHOW_PROVIDER else "")
    await m.answer_photo(BufferedInputFile(data, filename="image.png"), caption=cap)
    await sweep(m.bot, m.chat.id, state)


@router.message(Command("image"))
async def cmd_image(m: Message, state: FSMContext, command: CommandObject):
    if not allowed(m.from_user.id):
        return await m.answer("⛔ Нет доступа.")
    if command.args:
        return await do_image(m, state, command.args.strip())
    await state.set_state(Flow.image)
    r = await m.answer("🎨 Опишите, что нарисовать ✍️")
    await track(state, m, r)


@router.message(Flow.image, F.text)
async def msg_image(m: Message, state: FSMContext):
    await state.set_state(None)
    if m.text.startswith("/"):
        r = await m.answer("Отменено.")
        return await track(state, m, r)
    await do_image(m, state, m.text.strip())


@router.message(F.text.startswith("/"))
async def msg_unknown_cmd(m: Message, state: FSMContext):
    r = await m.answer("Неизвестная команда. Список: /help")
    await track(state, m, r)


async def handle_text(m: Message, state: FSMContext, text: str, heard: str = ""):
    """Общая точка: и для текста, и для расшифрованного голоса."""
    if is_image_request(text):
        return await do_image(m, state, text)
    if is_media_request(text):
        async with typing(m):
            tool, q = await route_media(text)
        if tool == "gen":
            return await do_image(m, state, q)
        if tool != "chat":
            return await run_tool(m, state, tool, q)
    d = await state.get_data()
    history, summary, custom = d.get("history", []), d.get("summary", ""), d.get("custom", "")
    history.append({"role": "user", "content": text})
    async with typing(m):
        summary, history = await compact(summary, history, custom)
        reply, prov = await ask_llm(build(summary, history, custom))
    if not reply:
        history.pop()  # не засоряем контекст неотвеченной репликой
        await state.update_data(history=history, summary=summary)
        await sweep(m.bot, m.chat.id, state)
        err = await m.answer(fail_text(), parse_mode=None)
        return await track(state, err)
    history.append({"role": "assistant", "content": reply})
    await state.update_data(history=history, summary=summary)
    if heard and SHOW_TRANSCRIPT:
        reply = f"🎙 {heard}\n\n{reply}"
    await send_result(m, reply, prov)
    await sweep(m.bot, m.chat.id, state)  # убираем всё лишнее, остаются вопрос и ответ


@router.message(F.text)
async def msg_chat(m: Message, state: FSMContext):
    if not allowed(m.from_user.id):
        return await m.answer("⛔ Нет доступа.")
    await handle_text(m, state, m.text)


@router.message(F.voice | F.audio)
async def msg_voice(m: Message, state: FSMContext):
    if not allowed(m.from_user.id):
        return await m.answer("⛔ Нет доступа.")
    media = m.voice or m.audio
    if (media.duration or 0) > 300 or (media.file_size or 0) > 19_000_000:
        r = await m.answer("⚠️ Голосовое слишком длинное (максимум 5 минут).")
        return await track(state, r)
    async with typing(m):
        buf = await m.bot.download(media.file_id)
        text, info = await transcribe(buf.read())
    if not text:
        await sweep(m.bot, m.chat.id, state)
        err = await m.answer("⚠️ Не удалось распознать голос: " + info, parse_mode=None)
        return await track(state, err)
    await handle_text(m, state, text, heard=text)


# ================= запуск =================
async def health(_):
    return web.Response(text="ok")


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands(
        [
            BotCommand(command="prompt", description="Задать стиль ответов"),
            BotCommand(command="image", description="Нарисовать картинку"),
            BotCommand(command="reset", description="Новый диалог"),
            BotCommand(command="status", description="Состояние нейросетей"),
            BotCommand(command="help", description="Помощь"),
        ]
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
