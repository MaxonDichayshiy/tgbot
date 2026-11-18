"""Скачивание видео и аудио по ссылке через yt-dlp (открытый проект, лицензия Unlicense).

Только для личного некоммерческого использования; ответственность за то, что скачивается,
лежит на владельце бота. Бот скачивает по явной просьбе («скачай …»), по одной ссылке, без плейлистов.

Ограничения сделаны под бесплатный хостинг и лимиты Telegram:
  • только ссылки с известных площадок (список ALLOWED_HOSTS) — заодно защита от SSRF;
  • файл до 48 МБ (бот в Telegram не может отправить больше 50 МБ), ролик до 60 минут, трансляции — нет;
  • один скачивающий процесс одновременно, пауза между просьбами одного пользователя.

Необязательно: переменная YTDLP_COOKIES (содержимое cookies.txt) — если площадка требует вход.
Не используйте для этого основной аккаунт: площадка может его ограничить.
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlparse

log = logging.getLogger("media_dl")

ALLOWED_HOSTS = (
    "youtube.com", "youtu.be", "music.youtube.com", "vk.com", "vk.ru", "vkvideo.ru", "rutube.ru",
    "tiktok.com", "vimeo.com", "dailymotion.com", "soundcloud.com", "ok.ru", "twitch.tv",
)
MAX_BYTES = int(os.getenv("DL_MAX_MB", "48")) * 1_000_000
MAX_SECONDS = int(os.getenv("DL_MAX_MINUTES", "60")) * 60
COOLDOWN = 20  # секунд между просьбами одного пользователя
TIMEOUT = 240  # общий предел на скачивание
COOKIES = os.getenv("YTDLP_COOKIES", "")

DL_RE = re.compile(r"(?i)(?<!\w)(скачай\w*|скачать|загрузи\w*|download|выгрузи\w*|вытащи\w*|достань|сохрани\w*)(?!\w)")
AUDIO_RE = re.compile(r"(?i)(mp3|аудио|музык\w*|звук\w*|песн\w*|трек\w*|audio|подкаст\w*)")
URL_RE = re.compile(r"https?://[^\s<>()\"']+")

_sem = asyncio.Semaphore(1)
_last: dict = {}


class DLError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""


@dataclass
class Result:
    path: str
    workdir: str
    title: str
    uploader: str
    duration: int
    width: int
    height: int
    mode: str
    size: int

    def cleanup(self):
        shutil.rmtree(self.workdir, ignore_errors=True)


def host_ok(url: str) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == a or h.endswith("." + a) for a in ALLOWED_HOSTS)


def parse_request(text: str):
    """(url, 'audio'|'video') для фраз вроде «скачай видео https://youtu.be/…», иначе None."""
    if not DL_RE.search(text):
        return None
    for u in URL_RE.findall(text):
        u = u.rstrip(".,;)!?")
        if host_ok(u):
            return u, ("audio" if AUDIO_RE.search(text.replace(u, " ")) else "video")
    return None


def _ffmpeg():
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _opts(mode: str, workdir: str, ffmpeg, height: int = 720) -> dict:
    import yt_dlp

    o = {
        "outtmpl": os.path.join(workdir, "%(id).60s.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True, "restrictfilenames": True,
        "socket_timeout": 20, "retries": 2, "fragment_retries": 2, "max_filesize": MAX_BYTES,
        "match_filter": yt_dlp.utils.match_filter_func(f"duration <=? {MAX_SECONDS} & !is_live"),
        "noprogress": True, "consoletitle": False,
    }
    if ffmpeg:
        o["ffmpeg_location"] = ffmpeg
    if COOKIES:
        cf = os.path.join(workdir, "cookies.txt")
        with open(cf, "w", encoding="utf-8") as f:
            f.write(COOKIES)
        o["cookiefile"] = cf
    if mode == "audio":
        if ffmpeg:
            o["format"] = "bestaudio/best"
            o["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "160"}]
        else:
            o["format"] = "bestaudio[ext=m4a]/bestaudio"
    else:
        h = f"[height<={height}]"
        if ffmpeg:
            o["format"] = f"bv*{h}[ext=mp4]+ba[ext=m4a]/b{h}[ext=mp4]/bv*{h}+ba/b{h}/b"
            o["merge_output_format"] = "mp4"
        else:
            o["format"] = f"b{h}[ext=mp4]/b{h}/b"
    return o


def _download(url: str, mode: str, workdir: str, height: int) -> Result:
    import yt_dlp

    ffmpeg = _ffmpeg()
    with yt_dlp.YoutubeDL(_opts(mode, workdir, ffmpeg, height)) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise DLError("Ролик не подошёл по условиям (слишком длинный, прямой эфир или больше лимита размера).")
        if "entries" in info:  # на случай плейлиста — берём первый
            info = next((e for e in info["entries"] if e), info)
    files = [os.path.join(workdir, f) for f in os.listdir(workdir) if f != "cookies.txt" and not f.endswith((".part", ".ytdl"))]
    if not files:
        raise DLError("Файл не скачался: ролик не подошёл по условиям (длина, эфир или размер).")
    path = max(files, key=os.path.getsize)
    size = os.path.getsize(path)
    return Result(path, workdir, info.get("title") or "Без названия", info.get("uploader") or info.get("channel") or "",
                  int(info.get("duration") or 0), int(info.get("width") or 0), int(info.get("height") or 0), mode, size)


def explain(e: Exception) -> str:
    t = str(e)
    low = t.lower()
    if isinstance(e, DLError):
        return t
    if "not a bot" in low or "confirm you" in low:
        return "Площадка не отдала видео серверу бота (защита от ботов). Попробуйте другую ссылку или платформу."
    if "private" in low or "login" in low or "sign in" in low or "members only" in low:
        return "Видео закрытое, нужен вход в аккаунт."
    if "unsupported url" in low:
        return "Эту ссылку я скачать не могу."
    if "not available" in low or "unavailable" in low or "removed" in low or "blocked" in low:
        return "Видео недоступно (удалено, закрыто или заблокировано в регионе сервера)."
    if "larger than max-filesize" in low or "max-filesize" in low:
        return f"Файл больше {MAX_BYTES // 1_000_000} МБ — Telegram не даёт ботам отправлять такое. Попробуйте аудио или ролик покороче."
    if "timed out" in low or "timeout" in low:
        return "Площадка отвечает слишком долго, попробуйте позже."
    return "Не получилось скачать: " + re.sub(r"\x1b\[[0-9;]*m|ERROR:\s*", "", t)[:160]


async def fetch(url: str, mode: str, user: int = 0) -> Result:
    """Скачивает во временную папку. Вызывающий обязан вызвать result.cleanup() после отправки."""
    now = time.time()
    wait = COOLDOWN - (now - _last.get(user, 0))
    if wait > 0:
        raise DLError(f"Подождите {int(wait) + 1} с перед следующим скачиванием.")
    _last[user] = now
    async with _sem:
        workdir = tempfile.mkdtemp(prefix="dl_")
        try:
            res = await asyncio.wait_for(asyncio.to_thread(_download, url, mode, workdir, 720), TIMEOUT)
            if res.size > MAX_BYTES and mode == "video":  # слишком тяжёлый — пробуем качество пониже
                shutil.rmtree(workdir, ignore_errors=True)
                workdir = tempfile.mkdtemp(prefix="dl_")
                res = await asyncio.wait_for(asyncio.to_thread(_download, url, mode, workdir, 360), TIMEOUT)
            if res.size > MAX_BYTES:
                raise DLError(f"Файл больше {MAX_BYTES // 1_000_000} МБ — Telegram не даёт ботам отправлять такое. "
                              "Попробуйте аудио или ролик покороче.")
            return res
        except asyncio.TimeoutError:
            shutil.rmtree(workdir, ignore_errors=True)
            raise DLError("Скачивание заняло слишком много времени.")
        except Exception as e:  # noqa
            shutil.rmtree(workdir, ignore_errors=True)
            log.info("yt-dlp: %s", str(e)[:200])
            raise DLError(explain(e))
