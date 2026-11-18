"""Дополнительные возможности Слопчика на базе открытых библиотек.

Тяжёлые библиотеки импортируются лениво (внутри функций), чтобы бот быстро стартовал
и не занимал память на бесплатном хостинге, пока функции не нужны.

  pypdf, python-docx, openpyxl   — чтение документов (PDF / Word / Excel)
  trafilatura                    — вытаскивание текста статьи из веб-страницы
  ddgs                           — веб-поиск (уже используется для картинок и видео)
  edge-tts                       — озвучка текста голосом
  segno                          — QR-коды
  python-docx                    — создание Word-документов
  Open-Meteo, ЦБ РФ              — погода и курсы валют (открытые API без ключей)
"""
import asyncio
import io
import ipaddress
import logging
import re
import socket
from urllib.parse import quote, urljoin, urlparse

import aiohttp

log = logging.getLogger("extras")
UA = {"User-Agent": "Mozilla/5.0 (compatible; SlopchikBot/1.0)"}
MAX_TEXT = 24000  # символов материала, которые отдаём нейросети (~8 тыс. токенов)

DOC_EXT = {"pdf", "docx", "xlsx", "xlsm", "txt", "md", "csv", "tsv", "json", "log", "py", "html", "htm", "xml", "yml", "yaml"}


# ====================== документы ======================
def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251", "utf-16"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", "replace")


def extract_document(data: bytes, filename: str):
    """(текст, примечание). Поддерживаются PDF, DOCX, XLSX и обычные текстовые файлы."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    note = ""
    if ext == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages, total = [], len(reader.pages)
        for i, p in enumerate(reader.pages[:80]):
            pages.append((p.extract_text() or "").strip())
        text = "\n\n".join(t for t in pages if t)
        if total > 80:
            note = f"Прочитаны первые 80 страниц из {total}."
        if not text:
            note = "В PDF нет текстового слоя (похоже на скан). Пришлите страницы фото — распознаю по картинке."
    elif ext == "docx":
        import docx

        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for t in d.tables[:20]:
            for row in t.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        text = "\n".join(parts)
    elif ext in ("xlsx", "xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets[:10]:
            parts.append(f"## Лист «{ws.title}»")
            for r, row in enumerate(ws.iter_rows(values_only=True)):
                if r >= 300:
                    parts.append("… (строки обрезаны)")
                    break
                cells = ["" if v is None else str(v) for v in row[:25]]
                if any(cells):
                    parts.append("\t".join(cells))
        text = "\n".join(parts)
    elif ext in DOC_EXT:
        text = _decode(data)
        if ext in ("html", "htm"):
            text = extract_article(text) or text
    else:
        raise ValueError(f"формат .{ext or '?'} не поддерживается")
    if len(text) > MAX_TEXT:
        text, note = text[:MAX_TEXT], (note + " Текст обрезан до первых ~24 тыс. символов.").strip()
    return text.strip(), note


# ====================== веб-страницы ======================
def _is_public_host(host: str) -> bool:
    """Защита от SSRF: не ходим на внутренние адреса (localhost, 10.x, 192.168.x, метаданные облака)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


async def fetch_page(url: str, max_bytes: int = 3_000_000, timeout: int = 15):
    """(байты, content-type, итоговый_url). Редиректы проверяем вручную, каждый шаг — на публичность."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
        for _ in range(5):
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise ValueError("недопустимая ссылка")
            if not await asyncio.to_thread(_is_public_host, p.hostname):
                raise ValueError("адрес недоступен")
            async with sess.get(url, allow_redirects=False, headers=UA) as r:
                if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                if r.status != 200:
                    raise ValueError(f"сайт ответил {r.status}")
                data = await r.content.read(max_bytes + 1)
                if len(data) > max_bytes:
                    data = data[:max_bytes]
                return data, (r.headers.get("Content-Type") or "").lower(), url
    raise ValueError("слишком много перенаправлений")


def extract_article(html: str, url: str = "") -> str:
    import trafilatura

    return (trafilatura.extract(html, url=url or None, include_tables=True, include_comments=False,
                                favor_recall=True) or "").strip()


async def page_text(url: str):
    """(заголовок, текст страницы). Понимает HTML и PDF."""
    data, ctype, final = await fetch_page(url)
    if "pdf" in ctype or final.lower().split("?")[0].endswith(".pdf"):
        text, _ = await asyncio.to_thread(extract_document, data, "x.pdf")
        return final, text[:MAX_TEXT]
    html = _decode(data)
    text = await asyncio.to_thread(extract_article, html, final)
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else final
    return title, text[:MAX_TEXT]


URL_RE = re.compile(r"https?://[^\s<>()\"']+")


# ====================== веб-поиск ======================
_SEARCH_CMD = (r"загугли|погугли|поищи\s+в\s+(?:интернете|сети|гугле|инете)|найди\s+в\s+(?:интернете|сети|гугле|инете)|"
               r"поиск\s+в\s+(?:интернете|сети)|search\s+the\s+web|google\s+it")
_SEARCH_NEWS = r"последние\s+новости|что\s+нового\s+(?:в|про|о)\b|свежие\s+новости"
SEARCH_CMD_RE = re.compile(rf"(?i)(?<!\w)(?:{_SEARCH_CMD})(?!\w)")
SEARCH_RE = re.compile(rf"(?i)(?<!\w)(?:{_SEARCH_CMD}|{_SEARCH_NEWS})(?!\w)")


def search_query(text: str):
    """Запрос для веб-поиска или None, если это не просьба поискать."""
    if not SEARCH_RE.search(text):
        return None
    q = SEARCH_CMD_RE.sub(" ", text)
    q = re.sub(r"(?i)\b(пожалуйста|мне)\b", " ", q)
    q = re.sub(r"\s+", " ", q).strip(" ,.:;-—?!")
    return q or text.strip()


def _ddg_text(q: str):
    from ddgs import DDGS

    return DDGS().text(q, max_results=6, safesearch="moderate")


async def web_search(q: str):
    """([{title, url, body, text}], …). Берём текст трёх лучших страниц, иначе — сниппеты поисковика."""
    res = await asyncio.to_thread(_ddg_text, q)
    items = [{"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""), "body": r.get("body", "")} for r in res if r.get("href") or r.get("url")]

    async def grab(it):
        try:
            _, t = await asyncio.wait_for(page_text(it["url"]), 10)
            it["text"] = t[:5000]
        except Exception:
            it["text"] = ""
        return it

    top = await asyncio.gather(*(grab(it) for it in items[:3]))
    return list(top) + items[3:]


def search_context(items: list) -> str:
    blocks = []
    for i, it in enumerate(items, 1):
        body = it.get("text") or it.get("body", "")
        blocks.append(f"[{i}] {it['title']}\n{it['url']}\n{body}")
    return "\n\n".join(blocks)[:MAX_TEXT]


def sources_footer(items: list) -> str:
    return "\n".join(f"[{i}] {it['title'][:70]} — {it['url']}" for i, it in enumerate(items[:4], 1))


# ====================== погода (Open-Meteo, без ключа) ======================
WEATHER_RE = re.compile(
    r"(?i)(?<!\w)(?:прогноз\s+)?погод[аыуе]?(?:\s+(?:в|во|на|для|по))?\s+([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\- ]{1,40})")
WEATHER_ASK_RE = re.compile(r"(?i)(?<!\w)погод[аыуе]?(?!\w)")
_W_STOP = re.compile(r"(?i)\s+(сегодня|завтра|сейчас|послезавтра|на\s+неделю|на\s+\d+\s+дн\w*|пожалуйста|будет|как)\b.*$")
WMO = {0: "☀️ ясно", 1: "🌤 малооблачно", 2: "⛅ облачно", 3: "☁️ пасмурно", 45: "🌫 туман", 48: "🌫 изморозь",
       51: "🌦 лёгкая морось", 53: "🌦 морось", 55: "🌧 сильная морось", 61: "🌧 небольшой дождь", 63: "🌧 дождь",
       65: "🌧 сильный дождь", 66: "🌧 ледяной дождь", 67: "🌧 ледяной дождь", 71: "🌨 небольшой снег", 73: "🌨 снег",
       75: "❄️ сильный снег", 77: "❄️ снежные зёрна", 80: "🌦 ливни", 81: "🌧 ливни", 82: "⛈ сильные ливни",
       85: "🌨 снегопад", 86: "❄️ сильный снегопад", 95: "⛈ гроза", 96: "⛈ гроза с градом", 99: "⛈ сильная гроза с градом"}


def weather_city(text: str):
    """Город из фразы «погода в Ижевске» или '' (город не назван) / None (не про погоду)."""
    m = WEATHER_RE.search(text)
    if m:
        city = _W_STOP.sub("", m.group(1)).strip(" ?!.,")
        return city or ""
    return "" if WEATHER_ASK_RE.search(text) and len(text) < 60 else None


async def _get_json(url: str):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(url, headers=UA) as r:
            if r.status != 200:
                raise ValueError(f"{r.status}")
            return await r.json(content_type=None)


def _city_forms(city: str):
    """«Ижевске» → «Ижевске», «Ижевск», «Ижевс»: геокодер ищет по началу названия."""
    forms = [city]
    for cut in (1, 2):
        if len(city) - cut >= 3:
            forms.append(city[:-cut])
    return forms


async def weather(city: str) -> str:
    p = None
    for form in _city_forms(city):
        g = await _get_json(f"https://geocoding-api.open-meteo.com/v1/search?name={quote(form)}&count=1&language=ru")
        if g.get("results"):
            p = g["results"][0]
            break
    if not p:
        return f"Не нашёл город «{city}». Попробуйте написать иначе."
    f = await _get_json(
        "https://api.open-meteo.com/v1/forecast?latitude={}&longitude={}&timezone=auto&wind_speed_unit=ms&forecast_days=3"
        "&current=temperature_2m,apparent_temperature,wind_speed_10m,weather_code"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code".format(p["latitude"], p["longitude"]))
    c, d = f["current"], f["daily"]
    place = ", ".join(x for x in (p.get("name"), p.get("admin1"), p.get("country")) if x)
    lines = [f"<b>{place}</b>",
             f"Сейчас: {WMO.get(c['weather_code'], '')}, <b>{c['temperature_2m']:+.0f}°</b> (ощущается {c['apparent_temperature']:+.0f}°), "
             f"ветер {c['wind_speed_10m']:.0f} м/с", ""]
    names = ["Сегодня", "Завтра", "Послезавтра"]
    for i in range(min(3, len(d["time"]))):
        rain = f", осадки {d['precipitation_sum'][i]:.1f} мм" if d["precipitation_sum"][i] else ""
        lines.append(f"{names[i]}: {WMO.get(d['weather_code'][i], '')}, {d['temperature_2m_min'][i]:+.0f}…{d['temperature_2m_max'][i]:+.0f}°{rain}")
    return "\n".join(lines)


# ====================== курсы валют (ЦБ РФ) ======================
CURRENCY_RE = re.compile(
    r"(?i)(?<!\w)курс\w*\s+(?:\w+\s+)?(доллар\w*|евро|юан\w*|фунт\w*|тенге|гривн\w*|лир\w*|иен\w*|йен\w*|злот\w*|франк\w*|валют\w*|usd|eur|cny)")
_CUR = {"доллар": "USD", "usd": "USD", "евро": "EUR", "eur": "EUR", "юан": "CNY", "cny": "CNY", "фунт": "GBP", "тенге": "KZT",
        "гривн": "UAH", "лир": "TRY", "иен": "JPY", "йен": "JPY", "злот": "PLN", "франк": "CHF"}


def currency_codes(text: str):
    """Коды валют из фразы; пустой список — «покажи основные»; None — не про курс."""
    m = CURRENCY_RE.search(text)
    if not m:
        return None
    w = m.group(1).lower()
    if w.startswith("валют"):
        return []
    for k, code in _CUR.items():
        if w.startswith(k):
            return [code]
    return []


async def rates(codes: list) -> str:
    j = await _get_json("https://www.cbr-xml-daily.ru/daily_json.js")
    codes = codes or ["USD", "EUR", "CNY"]
    lines = [f"<b>Курсы ЦБ РФ</b> на {j['Date'][:10]}"]
    for c in codes:
        v = j["Valute"].get(c)
        if not v:
            continue
        price, prev = v["Value"] / v["Nominal"], v["Previous"] / v["Nominal"]
        diff = price - prev
        arrow = "🔺" if diff > 0 else ("🔻" if diff < 0 else "▫️")
        lines.append(f"{c}: <b>{price:.2f} ₽</b> {arrow} {diff:+.2f}")
    return "\n".join(lines)


# ====================== озвучка (edge-tts) ======================
TTS_RE = re.compile(
    r"(?i)(?<!\w)(озвуч\w*|скажи\s+(?:это\s+)?голосом|ответь\s+голосом|прочитай\s+(?:это\s+)?(?:вслух|голосом)|зачитай)(?!\w)")


def tts_text(text: str):
    """Что озвучивать: текст после команды, '' — озвучить последний ответ бота, None — не про озвучку."""
    m = TTS_RE.search(text)
    if not m:
        return None
    rest = (text[:m.start()] + " " + text[m.end():]).strip(" ,.:;-—")
    rest = re.sub(r"(?i)^(мне|пожалуйста|это|текст)\b[\s,:]*", "", rest).strip()
    return rest if len(rest) >= 3 else ""


async def tts_mp3(text: str, voice: str = "") -> bytes:
    import edge_tts

    text = re.sub(r"[*_`#>]+", "", text)[:3000]
    latin = sum(c.isascii() and c.isalpha() for c in text)
    voice = voice or ("en-US-AriaNeural" if latin > len(text) * 0.6 else "ru-RU-SvetlanaNeural")
    out = bytearray()
    async for ch in edge_tts.Communicate(text, voice).stream():
        if ch["type"] == "audio":
            out += ch["data"]
    if not out:
        raise RuntimeError("пустая озвучка")
    return bytes(out)


# ====================== QR-коды (segno) ======================
QR_RE = re.compile(r"(?i)(?<!\w)(?:qr|кью\s?ар|кьюар)(?:[\s-]*код\w*)?\s*(?:для|на|с|:)?\s*(.{2,500})$")


def qr_payload(text: str):
    if not re.search(r"(?i)(?<!\w)(qr|кью\s?ар|кьюар)", text):
        return None
    m = QR_RE.search(text.strip())
    return m.group(1).strip() if m and m.group(1).strip() else ""


def qr_png(payload: str) -> bytes:
    import segno

    buf = io.BytesIO()
    segno.make(payload, error="m").save(buf, kind="png", scale=12, border=3)
    return buf.getvalue()


# ====================== Word-документ (python-docx) ======================
DOCX_RE = re.compile(
    r"(?i)(?<!\w)(оформи|сделай|создай|сохрани|выгрузи|подготовь|экспортируй)\b.{0,30}?(?<!\w)(word|ворд|docx|вордов\w*|в\s+документ)(?!\w)")


def make_docx(markdown: str, title: str = "") -> bytes:
    import docx

    d = docx.Document()
    if title:
        d.add_heading(title, 0)

    def runs(par, line):
        for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", line)):
            if part:
                par.add_run(part).bold = bool(i % 2)

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        h = re.match(r"^(#{1,4})\s+(.*)", line)
        b = re.match(r"^\s*[-*•]\s+(.*)", line)
        n = re.match(r"^\s*\d+[.)]\s+(.*)", line)
        if h:
            d.add_heading(h.group(2).strip(), min(len(h.group(1)), 4))
        elif b:
            runs(d.add_paragraph(style="List Bullet"), b.group(1))
        elif n:
            runs(d.add_paragraph(style="List Number"), n.group(1))
        else:
            runs(d.add_paragraph(), line)
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()
