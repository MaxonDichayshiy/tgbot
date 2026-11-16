import asyncio
import base64
import contextvars
import html
import json
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BufferedInputFile,
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InputMediaAudio,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
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
AUTO_CLEAN = os.getenv("AUTO_CLEAN", "1") == "1"  # в личке оставлять только последний вопрос и ответ
CLEAN_DEPTH = int(os.getenv("CLEAN_DEPTH", "1000"))  # на сколько сообщений назад чистить при первом разе
CF_ACCOUNT = os.getenv("CF_ACCOUNT_ID", "")
CF_TOKEN = os.getenv("CF_API_TOKEN", "")
CF_MODEL = os.getenv("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
KEEP_LAST = 4  # сколько последних реплик оставлять дословно при сжатии

SC = json.loads((BASE / "scenario.json").read_text(encoding="utf-8"))
# ================= постоянное хранилище (Upstash Redis, бесплатно) =================
UP_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UP_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
TTL = 90 * 24 * 3600  # данные неактивных чатов сами удаляются через 90 дней
ERR = object()


class KV:
    """Тонкий клиент Upstash REST. Без ключей — выключен, бот работает в памяти, как раньше."""

    enabled = bool(UP_URL and UP_TOKEN)
    _sess = None
    _tasks: set = set()

    @classmethod
    async def cmd(cls, *args):
        if not cls.enabled:
            return ERR
        try:
            if cls._sess is None or cls._sess.closed:
                cls._sess = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
            async with cls._sess.post(
                UP_URL, headers={"Authorization": f"Bearer {UP_TOKEN}"}, json=[str(a) for a in args]
            ) as r:
                data = await r.json(content_type=None)
                if r.status != 200 or "error" in data:
                    log.warning("upstash %s: %s", args[0], str(data)[:150])
                    return ERR
                return data.get("result")
        except Exception as e:  # noqa
            log.warning("upstash %s failed: %s", args[0], e)
            return ERR

    @classmethod
    def bg(cls, *args):
        """Записать в фоне, не задерживая ответ пользователю."""
        if cls.enabled:
            t = asyncio.create_task(cls.cmd(*args))
            cls._tasks.add(t)
            t.add_done_callback(cls._tasks.discard)


class PersistentStorage(BaseStorage):
    """Состояния и данные диалогов (история, резюме, /prompt) переживают перезапуски.
    Чтение один раз с сервера, дальше из памяти; запись — сразу и в память, и на сервер,
    поэтому расход бесплатного лимита (500 тыс. команд/мес.) небольшой."""

    def __init__(self):
        self._state, self._data, self._seen = {}, {}, set()

    @staticmethod
    def _k(key: StorageKey, part: str) -> str:
        return f"fsm:{key.chat_id}:{key.user_id}:{part}"

    async def set_state(self, key: StorageKey, state=None) -> None:
        v = state.state if hasattr(state, "state") else state
        k = self._k(key, "state")
        self._state[k] = v
        self._seen.add(k)
        await (KV.cmd("SET", k, v, "EX", TTL) if v else KV.cmd("DEL", k))

    async def get_state(self, key: StorageKey):
        k = self._k(key, "state")
        if k not in self._seen:
            raw = await KV.cmd("GET", k)
            if raw is not ERR:
                self._state[k] = raw
                self._seen.add(k)
        return self._state.get(k)

    async def set_data(self, key: StorageKey, data) -> None:
        k = self._k(key, "data")
        self._data[k] = dict(data)
        self._seen.add(k)
        await (KV.cmd("SET", k, json.dumps(data, ensure_ascii=False, default=str), "EX", TTL) if data else KV.cmd("DEL", k))

    async def get_data(self, key: StorageKey):
        k = self._k(key, "data")
        if k not in self._seen:
            raw = await KV.cmd("GET", k)
            if raw is not ERR:
                try:
                    self._data[k] = json.loads(raw) if raw else {}
                except Exception:
                    self._data[k] = {}
                self._seen.add(k)
        return dict(self._data.get(k, {}))

    async def close(self) -> None:
        pass


router = Router()
STORAGE = PersistentStorage() if KV.enabled else MemoryStorage()
BOT_NAME = "Слопчик"


# ================= провайдеры + автопереключение =================
class Provider:
    def __init__(self, c: dict, key: str):
        self.name = c["name"]
        self.model = c["model"]
        self.base_url = c["base_url"]
        self.vision = bool(c.get("vision", False))  # понимает изображения
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


def _clen(c) -> int:
    if isinstance(c, str):
        return len(c) // 3
    return sum(len(x.get("text", "")) // 3 if x.get("type") == "text" else 1200 for x in c)  # картинка ≈ 1200 токенов


def est_tokens(msgs: list) -> int:
    return sum(_clen(m["content"]) + 8 for m in msgs)


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


async def ask_llm(messages: list, vision: bool = False):
    """Возвращает (текст, имя_провайдера) или (None, None), если все недоступны.
    vision=True — только нейросети, которые понимают изображения."""
    pool = [p for p in PROVIDERS if p.vision or not vision]
    ready = [p for p in pool if p.ready]
    if not ready:  # все в «карантине» — пробуем тот, что освободится первым
        ready = sorted(pool, key=lambda p: p.down_until)[:1]
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
            elif vision:
                log.info("provider %s не принял изображение: %s", p.name, msg[:120])  # без бана: для текста он годится
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
    sys = f"Тебя зовут {BOT_NAME}. Если спросят, как тебя зовут, отвечай, что ты {BOT_NAME}. " + (
        custom or SC["system_prompt"]
    )
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
JAMENDO_ID = os.getenv("JAMENDO_CLIENT_ID", "")
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


async def search_jamendo(sess, q: str) -> list:
    """Полные треки со свободной лицензией (Creative Commons). Подходит для жанров, настроения и инди-музыки."""
    if not JAMENDO_ID:
        return []
    j = await _json(sess, "https://api.jamendo.com/v3.0/tracks/?format=json&limit=8&audioformat=mp32&include=musicinfo"
                          f"&order=relevance&client_id={JAMENDO_ID}&search={quote(q)}")
    toks = [w for w in re.findall(r"\w+", q.lower()) if len(w) >= 3]
    res = []
    for t in (j or {}).get("results", []):
        if not t.get("audio"):
            continue
        hay = (" ".join(str(t.get(k, "")) for k in ("name", "artist_name", "album_name"))
               + " " + json.dumps(t.get("musicinfo") or {}, ensure_ascii=False)).lower()
        if toks and sum(w in hay for w in toks) < max(1, (len(toks) + 1) // 2):
            continue  # не подсовываем случайный трек, когда просили конкретную песню
        res.append({"title": t.get("name", "Track"), "artist": t.get("artist_name", ""), "preview": t["audio"],
                    "link": t.get("shareurl", ""), "ext": "mp3", "cover": t.get("image", ""),
                    "dur": int(t.get("duration") or 0), "full": True, "license": t.get("license_ccurl", "")})
    return res


async def search_music(sess, q: str) -> list:
    out = await search_jamendo(sess, q)
    n_full = len(out)
    j = await _json(sess, f"https://api.deezer.com/search?q={quote(q)}&limit=5")
    for t in (j or {}).get("data", []):
        if t.get("preview"):
            out.append({"title": t["title"], "artist": t["artist"]["name"], "preview": t["preview"], "link": t.get("link", ""),
                        "ext": "mp3", "cover": (t.get("album") or {}).get("cover_medium", "")})
    if len(out) == n_full:
        j = await _json(sess, f"https://itunes.apple.com/search?term={quote(q)}&entity=song&limit=5")
        for t in (j or {}).get("results", []):
            if t.get("previewUrl"):
                out.append({"title": t["trackName"], "artist": t["artistName"], "preview": t["previewUrl"], "link": t.get("trackViewUrl", ""),
                            "ext": "m4a", "cover": t.get("artworkUrl100", "")})
    for t in out:
        t["yt"] = "https://www.youtube.com/results?search_query=" + quote(f'{t["artist"]} {t["title"]}')
    return out


MUSIC: dict = {}  # key -> {"tracks": [...]} для кнопок ⏮ ⏭


def music_caption(t: dict) -> str:
    if t.get("full"):
        lic = f"\nЛицензия: {t['license']}" if t.get("license") else ""
        return f"🎧 Полный трек · Jamendo{lic}"
    return "🎧 Превью 30 с — полную версию слушайте по кнопке"


def music_kb(key: str, i: int, n: int, t: dict):
    b = InlineKeyboardBuilder()
    if n > 1:
        b.row(
            InlineKeyboardButton(text="⏮", callback_data=f"mus:{key}:{(i - 1) % n}"),
            InlineKeyboardButton(text=f"{i + 1}/{n}", callback_data="mus:noop:0"),
            InlineKeyboardButton(text="⏭", callback_data=f"mus:{key}:{(i + 1) % n}"),
        )
    links = []
    if t.get("link", "").startswith("http"):
        links.append(InlineKeyboardButton(text="🔗 Страница трека" if t.get("full") else "🎧 Слушать полностью", url=t["link"]))
    links.append(InlineKeyboardButton(text="▶️ YouTube", url=t["yt"]))
    b.row(*links)
    return b.as_markup()


async def track_media(sess, t: dict):
    """(аудио, обложка) трека; обложка нужна плееру Telegram."""
    data = await _fetch(sess, t["preview"], ("audio/",), 45_000_000 if t.get("full") else 15_000_000)
    thumb = await _fetch(sess, t["cover"], ("image/",), 200_000) if t.get("cover") else None
    return data, thumb


def _audio_kwargs(t: dict, thumb):
    kw = dict(title=t["title"][:64], performer=t["artist"][:64], duration=t.get("dur") or 30)
    if thumb:
        kw["thumbnail"] = BufferedInputFile(thumb, filename="cover.jpg")
    return kw


async def send_music(m: Message, sess, q: str) -> bool:
    tracks = (await search_music(sess, q))[:8]
    for i, t in enumerate(tracks):
        data, thumb = await track_media(sess, t)
        if not data:
            continue
        key = f"{random.randint(10**5, 10**6 - 1)}"
        if len(MUSIC) > 200:
            MUSIC.pop(next(iter(MUSIC)))
        MUSIC[key] = {"tracks": tracks}
        try:
            await out(m, "audio")(
                BufferedInputFile(data, filename=f'{t["artist"]} - {t["title"]}.{t["ext"]}'),
                caption=music_caption(t), reply_markup=music_kb(key, i, len(tracks), t),
                **_audio_kwargs(t, thumb),
            )
            return True
        except Exception as e:  # noqa
            log.info("audio send failed: %s", e)
    return False


@router.callback_query(F.data.startswith("mus:"))
async def cb_music(c: CallbackQuery):
    _, key, idx = c.data.split(":")
    if key == "noop":
        return await c.answer()
    entry = MUSIC.get(key)
    if not entry:
        return await c.answer("Список устарел — запросите музыку заново", show_alert=True)
    tracks, i = entry["tracks"], int(idx) % len(entry["tracks"])
    t = tracks[i]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as sess:
        data, thumb = await track_media(sess, t)
    if not data:
        return await c.answer("Не удалось загрузить трек", show_alert=True)
    try:
        await c.message.edit_media(
            InputMediaAudio(
                media=BufferedInputFile(data, filename=f'{t["artist"]} - {t["title"]}.{t["ext"]}'),
                caption=music_caption(t), **_audio_kwargs(t, thumb),
            ),
            reply_markup=music_kb(key, i, len(tracks), t),
        )
        await c.answer()
    except Exception as e:  # noqa
        log.info("edit_media failed: %s", e)
        await c.answer("Не получилось заменить трек", show_alert=True)


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
                            await out(m, "animation")(BufferedInputFile(data, filename="anim.gif"), caption=cap)
                        else:
                            await out(m, "photo")(BufferedInputFile(data, filename="pic.jpg"), caption=cap)
                        ok = True
                        break
                    except Exception as e:  # Telegram отклонил файл — пробуем следующий
                        log.info("send failed: %s", e)
            elif tool == "music":
                ok = await send_music(m, sess, q)
            elif tool == "video":
                try:
                    res = await asyncio.to_thread(_ddg, "videos", q)
                    for r in res:
                        url = r.get("content") or r.get("url")
                        if url:
                            await out(m)(f"🎬 <b>{esc((r.get('title') or q)[:150])}</b>\n{html.escape(url)}")
                            ok = True
                            break
                except Exception as e:  # noqa
                    log.warning("ddgs videos: %s", e)
    await sweep(m.bot, m.chat.id, state)
    if not ok:
        err = await out(m)(f"🔍 Ничего не нашёл по запросу «{esc(q[:100])}». Попробуйте переформулировать.")
        await track(state, err)
    await tidy(m, state)


# ================= фото от пользователя =================
PHOTO_DEFAULT_Q = "Опиши, что на фото. Если на нём текст, задание, чертёж или вопрос — разбери и ответь."
NO_VISION_TEXT = ("⚠️ Сейчас нет доступной нейросети, понимающей фото. Нужен ключ Gemini "
                  "(или Mistral) в настройках сервиса.")


async def vision_reply(summary: str, history: list, custom: str, text: str, data: bytes, mime: str = "image/jpeg"):
    """Ответ по фото. В историю кладём только текст («[фото] …»), картинка живёт один запрос."""
    summary, history = await compact(summary, history, custom)
    b64 = base64.b64encode(data).decode()
    vmsg = {"role": "user", "content": [
        {"type": "text", "text": text or PHOTO_DEFAULT_Q},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]}
    reply, prov = await ask_llm(build(summary, history, custom) + [vmsg], vision=True)
    if reply:
        history = history + [
            {"role": "user", "content": f"[Пользователь прислал фото] {text}".strip()},
            {"role": "assistant", "content": reply},
        ]
    return reply, prov, summary, history

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
            await out(msg)(md_to_html(p) + foot_h)
        except Exception:
            await out(msg)(p + foot_p, parse_mode=None)


def allowed(uid: int) -> bool:
    return not ALLOWED or uid in ALLOWED


NAME_RE = re.compile(r"(?i)(?<!\w)(слопчик\w*|slopchik\w*)")
NAME_START_RE = re.compile(r"(?i)^[\s,.:;!\-—–]*(слопчик\w*|slopchik\w*)[\s,.:;!\-—–]*")


def is_group(m: Message) -> bool:
    return m.chat.type in ("group", "supergroup")


def who(m: Message) -> int:
    return m.from_user.id if getattr(m, "from_user", None) else 0


def out(m: Message, kind: str = ""):
    """В группах отвечаем реплаем на сообщение, в личке и каналах — обычным сообщением."""
    pre = "reply" if is_group(m) else "answer"
    f = getattr(m, f"{pre}_{kind}" if kind else pre)

    async def call(*a, **k):
        r = await f(*a, **k)
        lst = _replies.get()
        if lst is not None and getattr(r, "message_id", None):
            lst.append(r.message_id)  # итоговые ответы бота — их при чистке оставляем
        return r

    return call


async def addressed(m: Message, text: str):
    """(обращаются ли к боту, очищенный текст). В личке — всегда да."""
    if not is_group(m):
        return True, text, text
    me = await m.bot.me()
    hit = bool(NAME_RE.search(text)) or f"@{(me.username or '').lower()}" in text.lower()
    rt = m.reply_to_message
    if not hit and rt and rt.from_user and rt.from_user.id == me.id:
        hit = True
    if not hit:
        return False, text, text
    cleaned = re.sub(rf"(?i)@{re.escape(me.username or '')}", "", text)
    cleaned = NAME_START_RE.sub("", cleaned).strip()
    raw = cleaned  # только слова самого пользователя — для разбора команд модерации
    if rt and (rt.text or rt.caption) and not (rt.from_user and rt.from_user.id == me.id):
        quoted = (rt.text or rt.caption)[:1500]
        cleaned = f"Сообщение, на которое отвечают: «{quoted}»\n\n{cleaned}"
    return True, cleaned, raw


def fail_text() -> str:
    why = "\n".join(f"• {p.name}: {p.why or 'нет ответа'}" for p in PROVIDERS)
    return "⚠️ Все нейросети сейчас недоступны. Попробуйте через пару минут.\n" + why[:800]


HELP = (
    "Меня зовут Слопчик. Напишите или надиктуйте сообщение — отвечу. В группах и каналах откликаюсь на имя «Слопчик».\n"
    "• «нарисуй …» — создам картинку\n"
    "• «скинь картинку/гифку/песню/видео …» — найду и пришлю\n\n"
    "/prompt <текст> — задать стиль и роль бота (например: «отвечай кратко, как преподаватель»)\n"
    "/prompt — показать текущую инструкцию, /prompt сброс — вернуть стандартную\n"
    "/image <описание> — нарисовать картинку (можно и просто текстом)\n"
    "/clean — очистить чат (оставлю только последний вопрос и ответ)\n"
    "/reset — начать новый диалог\n"
    "/status — состояние нейросетей\n"
    "/mod — команды модерации для админов групп"
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


# ================= модерация групп =================
WARN_LIMIT = int(os.getenv("WARN_LIMIT", "3"))
WARN_MUTE_MIN = int(os.getenv("WARN_MUTE_MIN", "1440"))
ANON_ADMIN_ID = 1087968824  # «Анонимный админ»: писать так могут только админы группы
SEEN_ID: dict = {}    # (chat, user_id) -> имя
SEEN_NAME: dict = {}  # (chat, username) -> user_id
WARNS: dict = {}      # (chat, user_id) -> число предупреждений (в памяти)
LOCKED: dict = {}     # chat -> права до /lock

ACTIONS = ("ban", "unban", "kick", "mute", "unmute", "warn", "unwarn", "warns",
           "delete", "purge", "pin", "unpin", "lock", "unlock")
RESTRICT, DELETE, PIN = "can_restrict_members", "can_delete_messages", "can_pin_messages"
RIGHT = {"ban": RESTRICT, "unban": RESTRICT, "kick": RESTRICT, "mute": RESTRICT, "unmute": RESTRICT,
         "warn": RESTRICT, "unwarn": RESTRICT, "warns": None, "delete": DELETE, "purge": DELETE,
         "pin": PIN, "unpin": PIN, "lock": RESTRICT, "unlock": RESTRICT}
RIGHT_RU = {RESTRICT: "Блокировка участников", DELETE: "Удаление сообщений", PIN: "Закрепление сообщений"}
NEEDS_TARGET = {"ban", "unban", "kick", "mute", "unmute", "warn", "unwarn", "warns"}
CMD_ACTION = {"ban": "ban", "unban": "unban", "kick": "kick", "mute": "mute", "unmute": "unmute",
              "warn": "warn", "unwarn": "unwarn", "warns": "warns", "del": "delete", "purge": "purge",
              "pin": "pin", "unpin": "unpin", "lock": "lock", "unlock": "unlock"}
MOD_RE = re.compile(
    r"(?i)(?<!\w)(бан\w*|забан\w*|разбан\w*|кик\w*|выгон\w*|выгн\w*|выкин\w*|мут\w*|замут\w*|размут\w*|"
    r"заглуш\w*|заткн\w*|предупред\w*|предупреж\w*|варн\w*|удал\w*|очист\w*|закреп\w*|откреп\w*|"
    r"закро\w*|откро\w*|заблок\w*|разблок\w*)"
)
MOD_PROMPT = (
    "Ты разбираешь команды модерации Telegram-группы от админа. Верни ТОЛЬКО JSON "
    '{"action": "...", "duration_min": 0, "reason": "", "count": 0}.\n'
    "action: ban, unban, kick, mute, unmute, warn, unwarn, warns, delete, purge, pin, unpin, lock, unlock "
    "или none. none — если это не команда модерации (вопрос, разговор, просьба объяснить).\n"
    "duration_min — срок в минутах (0 = навсегда/не указано); для mute без срока ставь 60. "
    "count — сколько последних сообщений удалить (purge), иначе 0. reason — причина, если названа.\n"
    "«выгони/кикни» = kick; «замуть/заткни/заглуши» = mute; «забань/заблокируй» = ban; "
    "«закрой чат» = lock; «удали это/его сообщение» = delete; «удали последние N сообщений» = purge."
)
MOD_HELP = (
    "🛡 Модерация (бот должен быть админом, команды доступны только админам):\n"
    "/ban, /unban, /kick, /mute 30м, /unmute, /warn, /unwarn, /warns — ответом на сообщение или с @username\n"
    "/del — удалить сообщение (ответом), /purge [N] — удалить пачку сообщений\n"
    "/pin, /unpin — закрепить/открепить (ответом)\n"
    "/lock, /unlock — закрыть/открыть чат\n"
    "Можно и словами: «Слопчик, замуть его на час», «Слопчик, удали последние 10 сообщений»."
)


def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{esc(name or "пользователь")}</a>'


def fmt_min(n: int) -> str:
    if not n:
        return "навсегда"
    if n < 60:
        return f"{n} мин"
    if n < 1440:
        return f"{n // 60} ч" + (f" {n % 60} мин" if n % 60 else "")
    return f"{n // 1440} дн" + (f" {n % 1440 // 60} ч" if n % 1440 >= 60 else "")


_DUR_RE = re.compile(r"(?i)^(\d{1,6})\s*(с|сек\w*|s|м|мин\w*|m|ч|час\w*|h|д|дн\w*|д\w*|d)?$")


def parse_duration(tok: str):
    """'30м', '2ч', '1д', '45' (минуты), 'навсегда' -> минуты; None — не срок."""
    if tok.lower() in ("навсегда", "forever", "perm"):
        return 0
    mt = _DUR_RE.match(tok)
    if not mt:
        return None
    n, u = int(mt.group(1)), (mt.group(2) or "м").lower()
    if u.startswith(("с", "s")):
        return max(1, n // 60)
    if u.startswith(("м", "m")):
        return n
    if u.startswith(("ч", "h")):
        return n * 60
    return n * 1440


def parse_args(args: str):
    minutes, count, reason = None, 0, []
    for tok in args.split():
        if tok.startswith("@") or tok.isdigit() and len(tok) >= 7:
            continue
        d = parse_duration(tok)
        if d is not None and minutes is None:
            minutes = d
            if tok.isdigit():
                count = int(tok)
        else:
            reason.append(tok)
    return minutes, " ".join(reason), count


def remember(chat_id: int, u):
    if not u or getattr(u, "is_bot", False):
        return
    if len(SEEN_ID) > 5000:
        SEEN_ID.clear()
        SEEN_NAME.clear()
        KV.bg("DEL", "seen_id")
        KV.bg("DEL", "seen_name")
    if SEEN_ID.get((chat_id, u.id)) != u.full_name:
        SEEN_ID[(chat_id, u.id)] = u.full_name
        KV.bg("HSET", "seen_id", f"{chat_id}:{u.id}", u.full_name)
    if u.username and SEEN_NAME.get((chat_id, u.username.lower())) != u.id:
        SEEN_NAME[(chat_id, u.username.lower())] = u.id
        KV.bg("HSET", "seen_name", f"{chat_id}:{u.username.lower()}", u.id)


_replies: contextvars.ContextVar = contextvars.ContextVar("replies", default=None)
PENDING: dict = {}      # chat -> id сообщений пользователя, которые сейчас обрабатываются
CLEAN_LOCKS: dict = {}  # chat -> Lock
_bg: set = set()


class TurnMiddleware(BaseMiddleware):
    """Начало обработки сообщения: заводим список ответов и помечаем сообщение «в работе»."""

    async def __call__(self, handler, event, data):
        _replies.set([])
        pend = PENDING.setdefault(event.chat.id, set())
        pend.add(event.message_id)
        try:
            return await handler(event, data)
        finally:
            pend.discard(event.message_id)


async def _bulk_delete(bot: Bot, chat_id: int, ids: list):
    """Удаляет пачками, сначала свежие. Старше 48 ч Telegram удалить не даёт — на них останавливаемся."""
    ids = sorted(ids, reverse=True)
    dead = 0
    for i in range(0, len(ids), 100):
        part = ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id, part)
            dead = 0
            continue
        except Exception:
            pass
        done = 0
        for mid in part:
            try:
                await bot.delete_message(chat_id, mid)
                done += 1
            except Exception:
                pass
            await asyncio.sleep(0.03)
        dead = 0 if done else dead + 1
        if dead >= 3:
            break


async def tidy(m: Message, state: FSMContext, depth=None):
    """Личный чат: удаляем всё, кроме последнего вопроса пользователя и ответа бота."""
    if m.chat.type != "private" or not AUTO_CLEAN:
        return
    keep_now = sorted({m.message_id, *(_replies.get() or [])})
    async with CLEAN_LOCKS.setdefault(m.chat.id, asyncio.Lock()):
        d = await state.get_data()
        prev = d.get("keep", [])
        if prev and max(keep_now) <= max(prev):
            ids = keep_now  # уже есть более свежий ответ — эту пару убираем
        else:
            lo = min(prev) if prev else max(1, m.message_id - (depth or CLEAN_DEPTH))
            ids = [i for i in range(lo, max(keep_now)) if i not in keep_now]
            await state.update_data(keep=keep_now, junk=[])
        pend = PENDING.get(m.chat.id, set()) - {m.message_id}
        ids = [i for i in ids if i not in pend]
    t = asyncio.create_task(_bulk_delete(m.bot, m.chat.id, ids))
    _bg.add(t)
    t.add_done_callback(_bg.discard)


class SeenMiddleware(BaseMiddleware):
    """Запоминает участников чата, чтобы понимать «@username» в командах."""

    async def __call__(self, handler, event, data):
        try:
            if event.chat.type in ("group", "supergroup"):
                rt = event.reply_to_message
                for u in (event.from_user, rt.from_user if rt else None, *(event.new_chat_members or [])):
                    remember(event.chat.id, u)
        except Exception:
            pass
        return await handler(event, data)


router.message.outer_middleware(SeenMiddleware())
router.message.outer_middleware(TurnMiddleware())


async def member_of(bot: Bot, chat_id: int, uid: int):
    try:
        return await bot.get_chat_member(chat_id, uid)
    except Exception:
        return None


def can(member, right) -> bool:
    if member is None:
        return False
    if member.status == "creator":
        return True
    return member.status == "administrator" and (right is None or bool(getattr(member, right, False)))


async def requester_can(m: Message, right) -> bool:
    sc = getattr(m, "sender_chat", None)
    if m.from_user and m.from_user.id == ANON_ADMIN_ID and sc and sc.id == m.chat.id:
        return True  # анонимный админ (права проверить нельзя)
    return can(await member_of(m.bot, m.chat.id, who(m)), right)


def resolve_target(m: Message, text: str, bot_username: str):
    """Кого модерируем: @упоминание в тексте > ответ на сообщение. ID — только из Telegram, не от нейросети."""
    for e in (m.entities or []):
        if e.type == "text_mention" and e.user and not e.user.is_bot:
            return e.user.id, e.user.full_name
    for name in re.findall(r"@(\w{4,32})", text):
        if name.lower() == bot_username.lower():
            continue
        uid = SEEN_NAME.get((m.chat.id, name.lower()))
        if uid:
            return uid, SEEN_ID.get((m.chat.id, uid), "@" + name)
    rt = m.reply_to_message
    if rt and rt.from_user and not getattr(rt, "sender_chat", None):
        return rt.from_user.id, rt.from_user.full_name
    return None


def _full_perms() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
        can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True,
    )


async def _chat_perms(bot: Bot, chat_id: int) -> ChatPermissions:
    try:
        return (await bot.get_chat(chat_id)).permissions or _full_perms()
    except Exception:
        return _full_perms()


def _until(minutes):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes) if minutes else None


async def _exec(m: Message, action: str, tid, tname, minutes, reason, count) -> str:
    bot, cid, rt = m.bot, m.chat.id, m.reply_to_message
    who_ = mention(tid, tname) if tid else ""
    why = f"\nПричина: {esc(reason)}" if reason else ""
    if action == "ban":
        await bot.ban_chat_member(cid, tid, until_date=_until(minutes))
        return f"🔨 {who_} заблокирован ({fmt_min(minutes or 0)}).{why}"
    if action == "unban":
        await bot.unban_chat_member(cid, tid, only_if_banned=True)
        return f"✅ {who_} разблокирован."
    if action == "kick":
        await bot.ban_chat_member(cid, tid)
        await bot.unban_chat_member(cid, tid, only_if_banned=True)
        return f"👢 {who_} исключён из чата.{why}"
    if action == "mute":
        minutes = 60 if minutes is None else minutes
        await bot.restrict_chat_member(cid, tid, permissions=ChatPermissions(can_send_messages=False),
                                       until_date=_until(minutes))
        return f"🔇 {who_} в муте ({fmt_min(minutes)}).{why}"
    if action == "unmute":
        await bot.restrict_chat_member(cid, tid, permissions=await _chat_perms(bot, cid))
        return f"🔊 {who_} снова может писать."
    if action == "warn":
        n = WARNS.get((cid, tid), 0) + 1
        if n >= WARN_LIMIT:
            WARNS.pop((cid, tid), None)
            KV.bg("HDEL", "warns", f"{cid}:{tid}")
            await bot.restrict_chat_member(cid, tid, permissions=ChatPermissions(can_send_messages=False),
                                           until_date=_until(WARN_MUTE_MIN))
            return f"⚠️ {who_}: {WARN_LIMIT}/{WARN_LIMIT} предупреждений — мут на {fmt_min(WARN_MUTE_MIN)}.{why}"
        WARNS[(cid, tid)] = n
        KV.bg("HSET", "warns", f"{cid}:{tid}", n)
        return f"⚠️ {who_}: предупреждение {n}/{WARN_LIMIT}.{why}"
    if action == "unwarn":
        WARNS.pop((cid, tid), None)
        KV.bg("HDEL", "warns", f"{cid}:{tid}")
        return f"✅ Предупреждения {who_} сняты."
    if action == "warns":
        return f"ℹ️ У {who_}: {WARNS.get((cid, tid), 0)}/{WARN_LIMIT} предупреждений."
    if action == "delete":
        if not rt:
            return "Ответьте на сообщение, которое нужно удалить."
        await bot.delete_message(cid, rt.message_id)
        return ""
    if action == "purge":
        start = rt.message_id if rt else (m.message_id - count if count else 0)
        if not start:
            return "Ответьте на сообщение, с которого чистить, или укажите число: /purge 20"
        ids = list(range(max(start, m.message_id - 200), m.message_id + 1))
        for i in range(0, len(ids), 100):
            try:
                await bot.delete_messages(cid, ids[i:i + 100])
            except Exception:
                pass
        return f"🧹 Очищено сообщений: до {len(ids) - 1}."
    if action == "pin":
        if not rt:
            return "Ответьте на сообщение, которое нужно закрепить."
        await bot.pin_chat_message(cid, rt.message_id, disable_notification=True)
        return "📌 Закреплено."
    if action == "unpin":
        await bot.unpin_chat_message(cid, message_id=rt.message_id if rt else None)
        return "📌 Откреплено."
    if action == "lock":
        LOCKED[cid] = await _chat_perms(bot, cid)
        try:
            KV.bg("HSET", "locked", cid, LOCKED[cid].model_dump_json(exclude_none=True))
        except Exception as e:  # сбой записи в базу не должен ломать модерацию
            log.warning("lock persist: %s", e)
        await bot.set_chat_permissions(cid, ChatPermissions(can_send_messages=False))
        return "🔒 Чат закрыт для обычных участников. Открыть: /unlock"
    if action == "unlock":
        await bot.set_chat_permissions(cid, LOCKED.pop(cid, None) or _full_perms())
        KV.bg("HDEL", "locked", cid)
        return "🔓 Чат снова открыт."
    return ""


async def do_mod(m: Message, action: str, text: str = "", minutes=None, reason: str = "", count: int = 0):
    if not is_group(m):
        return await m.answer("🛡 Модерация работает только в группах.")
    bot, cid = m.bot, m.chat.id
    right = RIGHT[action]
    if not await requester_can(m, right):
        need = f" с правом «{RIGHT_RU[right]}»" if right else ""
        return await out(m)(f"⛔ Это могут только админы{need}.")
    me = await bot.me()
    if right and not can(await member_of(bot, cid, me.id), right):
        return await out(m)(f"⚠️ Мне нужно право админа «{RIGHT_RU[right]}». Выдайте его в настройках группы.")
    tid = tname = None
    if action in NEEDS_TARGET:
        target = resolve_target(m, text, me.username or "")
        if not target:
            return await out(m)("Укажите, кого: ответьте на его сообщение или напишите @username "
                                "(если он уже писал в этот чат).")
        tid, tname = target
        if tid == me.id:
            return await out(m)("Себя я трогать не буду 🙂")
        tm = await member_of(bot, cid, tid)
        if tm and tm.status in ("creator", "administrator") and action in ("ban", "kick", "mute", "warn"):
            return await out(m)("⛔ Это админ — его я трогать не буду.")
    try:
        msg = await _exec(m, action, tid, tname, minutes, reason, count)
    except TelegramAPIError as e:
        return await out(m)(f"⚠️ Не получилось: {esc(str(e)[:150])}", parse_mode=None)
    if action not in ("purge",):
        try:
            await bot.delete_message(cid, m.message_id)  # убираем саму команду
        except Exception:
            pass
    if msg:
        await bot.send_message(cid, msg)


async def try_moderate(m: Message, raw: str) -> bool:
    """Команда словами. Нейросеть выбирает только действие и срок; цель берётся из Telegram."""
    txt, _ = await ask_llm([{"role": "system", "content": MOD_PROMPT}, {"role": "user", "content": raw}])
    try:
        j = json.loads(re.search(r"\{.*\}", txt or "", re.S).group(0))
        action = str(j.get("action", "none")).lower().strip()
        if action not in ACTIONS:
            return False
        minutes = max(0, min(int(j.get("duration_min") or 0), 525600))
        count = max(0, min(int(j.get("count") or 0), 200))
        reason = str(j.get("reason") or "")[:200]
    except Exception:
        return False
    await do_mod(m, action, text=raw, minutes=minutes, reason=reason, count=count)
    return True


@router.message(Command(*CMD_ACTION))
async def cmd_mod(m: Message, command: CommandObject):
    args = command.args or ""
    minutes, reason, count = parse_args(args)
    await do_mod(m, CMD_ACTION[command.command], text=args, minutes=minutes, reason=reason, count=count)


@router.message(Command("mod"))
async def cmd_mod_help(m: Message):
    await m.answer(MOD_HELP)


# ================= хендлеры =================
@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(m: Message, state: FSMContext):
    if not allowed(who(m)):
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
    if not allowed(who(m)):
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


@router.message(Command("clean"))
async def cmd_clean(m: Message, state: FSMContext):
    """Полная очистка личного чата (вместе с этой командой). Контекст диалога остаётся."""
    if m.chat.type != "private":
        return
    ids = list(range(max(1, m.message_id - 5000), m.message_id + 1))
    await _bulk_delete(m.bot, m.chat.id, ids)
    r = await m.answer("🧹 Чат очищен. Контекст разговора сохранён, сбросить его: /reset")
    await state.update_data(keep=[], junk=[r.message_id])


@router.message(Command("status"))
async def cmd_status(m: Message, state: FSMContext):
    if ALLOWED and who(m) not in ALLOWED:
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
    await out(m, "photo")(BufferedInputFile(data, filename="image.png"), caption=cap)
    await sweep(m.bot, m.chat.id, state)
    await tidy(m, state)


@router.message(Command("image"))
async def cmd_image(m: Message, state: FSMContext, command: CommandObject):
    if not allowed(who(m)):
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
    if is_group(m):
        return
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
        err = await out(m)(fail_text(), parse_mode=None)
        return await track(state, err)
    history.append({"role": "assistant", "content": reply})
    await state.update_data(history=history, summary=summary)
    if heard and SHOW_TRANSCRIPT:
        reply = f"🎙 {heard}\n\n{reply}"
    await send_result(m, reply, prov)
    await sweep(m.bot, m.chat.id, state)  # убираем служебное
    await tidy(m, state)  # в личке остаются только последний вопрос и ответ


@router.message(F.text)
async def msg_chat(m: Message, state: FSMContext):
    if not allowed(who(m)):
        return None if is_group(m) else await m.answer("⛔ Нет доступа.")
    ok, text, raw = await addressed(m, m.text)
    if not ok:
        return  # в группе молчим, пока не позвали по имени
    if not text:
        return await out(m)("Да? 🙂")
    if is_group(m) and MOD_RE.search(raw) and await try_moderate(m, raw):
        return
    await handle_text(m, state, text)


@router.channel_post(F.text)
async def msg_channel(m: Message):
    """Канал: откликаемся, если админ написал пост с именем бота."""
    if not NAME_RE.search(m.text):
        return
    text = NAME_START_RE.sub("", m.text).strip() or m.text
    state = FSMContext(STORAGE, StorageKey(m.bot.id, m.chat.id, m.chat.id))
    await handle_text(m, state, text)


@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def msg_photo(m: Message, state: FSMContext):
    if not allowed(who(m)):
        return None if is_group(m) else await m.answer("⛔ Нет доступа.")
    ok, text, _ = await addressed(m, m.caption or "")
    if not ok:
        return  # в группе реагируем на фото только с обращением по имени или ответом боту
    if m.photo:
        file_id, mime = m.photo[-1].file_id, "image/jpeg"
    else:
        file_id, mime = m.document.file_id, m.document.mime_type
        if (m.document.file_size or 0) > 8_000_000:
            r = await out(m)("⚠️ Файл слишком большой (до 8 МБ). Пришлите фото обычным способом.")
            return await track(state, r)
    d = await state.get_data()
    async with typing(m):
        buf = await m.bot.download(file_id)
        reply, prov, summary, history = await vision_reply(
            d.get("summary", ""), d.get("history", []), d.get("custom", ""), text, buf.read(), mime
        )
    if not reply:
        await sweep(m.bot, m.chat.id, state)
        err = await out(m)(NO_VISION_TEXT + "\n" + fail_text(), parse_mode=None)
        return await track(state, err)
    await state.update_data(history=history, summary=summary)
    await send_result(m, reply, prov)
    await sweep(m.bot, m.chat.id, state)
    await tidy(m, state)


@router.message(F.voice | F.audio)
async def msg_voice(m: Message, state: FSMContext):
    if not allowed(who(m)):
        return None if is_group(m) else await m.answer("⛔ Нет доступа.")
    if is_group(m):  # в группе слушаем только голосовые-ответы боту
        me = await m.bot.me()
        rt = m.reply_to_message
        if not (rt and rt.from_user and rt.from_user.id == me.id):
            return
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


async def load_persistent():
    """Поднимает из Upstash предупреждения, закрытые чаты и известных участников."""
    if not KV.enabled:
        log.info("storage: память (Upstash не подключён)")
        return

    async def pairs(name):
        r = await KV.cmd("HGETALL", name)
        if isinstance(r, dict):
            return list(r.items())
        return list(zip(r[0::2], r[1::2])) if isinstance(r, list) else []

    try:
        for f, v in await pairs("warns"):
            c, u = f.split(":", 1)
            WARNS[(int(c), int(u))] = int(v)
        for f, v in await pairs("locked"):
            LOCKED[int(f)] = ChatPermissions.model_validate_json(v)
        for f, v in await pairs("seen_id"):
            c, u = f.split(":", 1)
            SEEN_ID[(int(c), int(u))] = v
        for f, v in await pairs("seen_name"):
            c, u = f.split(":", 1)
            SEEN_NAME[(int(c), u)] = int(v)
        log.info("storage: Upstash; warns=%s locked=%s seen=%s", len(WARNS), len(LOCKED), len(SEEN_ID))
    except Exception as e:  # noqa
        log.warning("load_persistent: %s", e)


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=STORAGE)
    dp.include_router(router)

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    await load_persistent()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands(
        [
            BotCommand(command="prompt", description="Задать стиль ответов"),
            BotCommand(command="image", description="Нарисовать картинку"),
            BotCommand(command="clean", description="Очистить чат"),
            BotCommand(command="reset", description="Новый диалог"),
            BotCommand(command="status", description="Состояние нейросетей"),
            BotCommand(command="help", description="Помощь"),
        ]
    )
    try:
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in (
                ("ban", "Заблокировать"), ("unban", "Разблокировать"), ("kick", "Исключить"),
                ("mute", "Мут"), ("unmute", "Снять мут"), ("warn", "Предупреждение"),
                ("del", "Удалить сообщение"), ("purge", "Удалить пачку сообщений"),
                ("pin", "Закрепить"), ("lock", "Закрыть чат"), ("unlock", "Открыть чат"), ("mod", "Справка"),
            )],
            scope=BotCommandScopeAllChatAdministrators(),
        )
    except Exception:
        pass
    vk_task = asyncio.create_task(run_vk())
    try:
        await dp.start_polling(bot)
    finally:
        vk_task.cancel()
        if KV._sess and not KV._sess.closed:
            await KV._sess.close()


async def run_vk():
    try:
        from vk_bot import start_vk
    except Exception:
        log.exception("vk: не удалось подключить")
        return
    await start_vk()


if __name__ == "__main__":
    asyncio.run(main())
