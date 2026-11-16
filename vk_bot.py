"""Сообщения сообщества ВК. Тот же Слопчик, что в Telegram: те же нейросети, картинки, голос и поиск."""
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from urllib.parse import quote

import aiohttp

log = logging.getLogger("bot.vk")
API = "5.199"
MENTION = re.compile(r"\[(?:id|club|public)\d+\|([^\]]+)\]")
CMD = re.compile(r"^/(\w+)(?:@\S+)?(?:\s+(.*))?$", re.S)
CHAT = 2_000_000_000


def core():
    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "ask_llm"):
        return main
    import bot
    return bot


def vk_allowed(uid: int) -> bool:
    raw = os.getenv("VK_ALLOWED_USERS", "")
    ids = {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}
    return not ids or uid in ids


def plain(text: str) -> str:
    text = MENTION.sub(r"\1", text or "")
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.M)
    return text.strip()


class VkError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code}: {msg}")
        self.code = code


class Vk:
    def __init__(self, c, token: str):
        self.c = c
        self.token = token
        self.group_id = int(os.getenv("VK_GROUP_ID", "0") or 0)
        self.sess = None
        self.box: dict = {}

    def st(self, peer: int) -> dict:
        return self.box.setdefault(
            peer, {"history": [], "summary": "", "custom": "", "junk": [], "image": False}
        )

    def track(self, peer: int, *ids):
        junk = self.st(peer)["junk"]
        for i in ids:
            if i:
                junk.append(int(i))
        self.st(peer)["junk"] = junk[-80:]

    async def api(self, method: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        params["access_token"] = self.token
        params["v"] = API
        async with self.sess.post(
            f"https://api.vk.com/method/{method}",
            data=params,
            timeout=aiohttp.ClientTimeout(total=40),
        ) as r:
            data = await r.json(content_type=None)
        if "error" in data:
            err = data["error"]
            raise VkError(err.get("error_code"), err.get("error_msg"))
        return data.get("response")

    async def send(self, peer: int, text: str = "", attachment: str = ""):
        body = plain(text)[:4000]
        if not body and not attachment:
            return None
        try:
            res = await self.api(
                "messages.send",
                peer_id=peer,
                random_id=random.randint(1, 2_000_000_000),
                message=body or None,
                attachment=attachment or None,
            )
        except VkError as e:
            log.warning("vk send %s: %s", peer, e)
            return None
        if isinstance(res, int):
            return res
        if isinstance(res, dict):
            return res.get("message_id") or res.get("cmid")
        return None

    async def sweep(self, peer: int):
        ids = self.st(peer)["junk"]
        if not ids:
            return
        self.st(peer)["junk"] = []
        for i in range(0, len(ids), 100):
            part = ids[i : i + 100]
            try:
                await self.api(
                    "messages.delete",
                    message_ids=",".join(str(x) for x in part),
                    delete_for_all=1,
                    group_id=self.group_id or None,
                )
            except VkError as e:
                log.info("vk delete: %s", e)

    async def typing(self, peer: int):
        try:
            await self.api("messages.setActivity", peer_id=peer, type="typing")
        except VkError:
            pass

    async def busy(self, peer: int):
        async def loop():
            try:
                while True:
                    await self.typing(peer)
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(loop())
        return task

    async def upload(self, peer: int, data: bytes, filename: str, kind: str):
        try:
            if kind == "photo":
                if data.startswith(b"\x89PNG"):
                    filename, ctype = "image.png", "image/png"
                elif data.startswith(b"GIF8"):
                    filename, ctype = "image.gif", "image/gif"
                else:
                    filename, ctype = "image.jpg", "image/jpeg"
                srv = await self.api("photos.getMessagesUploadServer", peer_id=peer)
                form = aiohttp.FormData()
                form.add_field("photo", data, filename=filename, content_type=ctype)
                async with self.sess.post(srv["upload_url"], data=form, timeout=aiohttp.ClientTimeout(total=90)) as r:
                    up = await r.json(content_type=None)
                if "photo" not in up:
                    raise VkError(0, str(up)[:180])
                saved = await self.api(
                    "photos.saveMessagesPhoto", server=up["server"], photo=up["photo"], hash=up["hash"]
                )
                ph = saved[0]
                att = f"photo{ph['owner_id']}_{ph['id']}"
                if ph.get("access_key"):
                    att += "_" + ph["access_key"]
                return att
            srv = await self.api("docs.getMessagesUploadServer", peer_id=peer, type="doc")
            form = aiohttp.FormData()
            form.add_field("file", data, filename=filename, content_type="application/octet-stream")
            async with self.sess.post(srv["upload_url"], data=form, timeout=aiohttp.ClientTimeout(total=90)) as r:
                up = await r.json(content_type=None)
            saved = await self.api("docs.save", file=up["file"], title=filename[:80])
            doc = saved.get(saved.get("type", "doc")) or saved.get("doc") or {}
            if not doc:
                return None
            return f"{saved.get('type', 'doc')}{doc['owner_id']}_{doc['id']}"
        except Exception as e:  # noqa
            log.warning("vk upload %s: %s", kind, e)
            return None

    async def run(self):
        timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            self.sess = sess
            if not self.group_id:
                info = await self.api("groups.getById")
                groups = info.get("groups") if isinstance(info, dict) else info
                self.group_id = int(groups[0]["id"])
                log.info("vk: сообщество %s (%s)", groups[0].get("name"), self.group_id)
            else:
                log.info("vk: сообщество %s", self.group_id)
            try:
                await self.api(
                    "groups.setLongPollSettings",
                    group_id=self.group_id,
                    enabled=1,
                    api_version=API,
                    message_new=1,
                )
                log.info("vk: слушаю входящие сообщения")
            except VkError as e:
                log.error("vk: не удалось включить входящие сообщения: %s", e)
            while True:
                lp = await self.api("groups.getLongPollServer", group_id=self.group_id)
                server, key, ts = lp["server"], lp["key"], lp["ts"]
                while True:
                    url = server if str(server).startswith("http") else "https://" + server
                    try:
                        async with sess.get(
                            url,
                            params={"act": "a_check", "key": key, "ts": ts, "wait": 25},
                            timeout=aiohttp.ClientTimeout(total=40),
                        ) as r:
                            data = await r.json(content_type=None)
                    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                        log.info("vk poll: %s", e)
                        continue
                    failed = data.get("failed")
                    if failed == 1:
                        ts = data.get("ts", ts)
                        continue
                    if failed in (2, 3):
                        break
                    ts = data.get("ts", ts)
                    for upd in data.get("updates") or []:
                        try:
                            await self.on_update(upd)
                        except Exception:
                            log.exception("vk update")

    async def load(self, peer: int):
        """Диалог из Upstash (если подключён) — после перезапуска история не теряется."""
        kv = getattr(self.c, "KV", None)
        if not kv or not kv.enabled or peer in self.box:
            return
        raw = await kv.cmd("GET", f"vk:{peer}")
        if raw is self.c.ERR:
            return
        try:
            d = json.loads(raw) if raw else {}
        except Exception:
            d = {}
        st = self.st(peer)
        st["history"] = d.get("history", [])
        st["summary"] = d.get("summary", "")
        st["custom"] = d.get("custom", "")

    def save(self, peer: int):
        kv = getattr(self.c, "KV", None)
        if not kv or not kv.enabled or peer not in self.box:
            return
        st = self.box[peer]
        data = {"history": st["history"], "summary": st["summary"], "custom": st["custom"]}
        kv.bg("SET", f"vk:{peer}", json.dumps(data, ensure_ascii=False), "EX", self.c.TTL)

    async def on_update(self, upd: dict):
        if upd.get("type") != "message_new":
            return
        obj = upd.get("object") or {}
        m_ = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        peer = int(m_.get("peer_id") or m_.get("from_id") or 0) if isinstance(m_, dict) else 0
        if peer:
            await self.load(peer)
        try:
            await self._on_update(upd)
        finally:
            if peer:
                self.save(peer)

    async def _on_update(self, upd: dict):
        if upd.get("type") != "message_new":
            return
        obj = upd.get("object") or {}
        msg = obj.get("message") if isinstance(obj, dict) and isinstance(obj.get("message"), dict) else obj
        if not isinstance(msg, dict) or msg.get("out"):
            return
        from_id = int(msg.get("from_id") or 0)
        if from_id <= 0:
            return
        peer = int(msg.get("peer_id") or from_id)
        text = MENTION.sub(r"\1", msg.get("text") or "").strip()
        if os.getenv("VK_LOG_TEXT") == "1":  # текст сообщений в логи — только для отладки
            log.info("vk: сообщение от %s в %s: %s", from_id, peer, (text or "без текста")[:120])
        else:
            log.info("vk: сообщение в %s (%s симв.)", peer, len(text))
        reply = msg.get("reply_message") or {}
        reply_from = int(reply.get("from_id") or 0)
        atts = msg.get("attachments") or []
        mid = msg.get("id")
        try:
            await self.api("messages.markAsRead", peer_id=peer)
        except VkError:
            pass
        if not vk_allowed(from_id):
            await self.send(peer, "⛔ Нет доступа.")
            return
        photos = [a["photo"] for a in atts if a.get("type") == "photo"]
        if photos:
            return await self.on_photo(peer, text, photos[0], reply_from)
        voices = [a["audio_message"] for a in atts if a.get("type") == "audio_message"]
        if not text and voices:
            return await self.on_voice(peer, from_id, voices[0], mid, reply_from)
        if not text:
            return
        await self.on_text(peer, from_id, text, mid, reply_from)

    def addressed(self, peer: int, text: str, reply_from: int):
        if peer < CHAT:
            return True, text
        hit = bool(self.c.NAME_RE.search(text)) or reply_from == -self.group_id
        if not hit:
            return False, text
        cleaned = self.c.NAME_START_RE.sub("", text).strip()
        return True, cleaned

    async def on_text(self, peer, from_id, text, mid, reply_from):
        ok, text = self.addressed(peer, text, reply_from)
        if not ok:
            return
        m = CMD.match(text)
        if m:
            return await self.on_cmd(peer, m.group(1).lower(), (m.group(2) or "").strip(), mid)
        if not text:
            await self.send(peer, "Да? 🙂")
            return
        if self.st(peer)["image"]:
            self.st(peer)["image"] = False
            return await self.do_image(peer, text)
        await self.answer(peer, text)

    async def on_cmd(self, peer, cmd, arg, mid):
        c = self.c
        st = self.st(peer)
        if cmd in ("start", "help"):
            sent = await self.send(
                peer,
                f"{c.SC['title']}\n\n{c.SC['welcome']}\n\n"
                "В беседах отвечаю, если написать «Слопчик» или ответить на моё сообщение.\n"
                "/prompt текст — стиль ответов. /prompt — показать, /prompt сброс — как было.\n"
                "/image описание — картинка. «нарисуй …» тоже работает.\n"
                "«скинь картинку / гифку / песню / видео …» — найду готовое.\n"
                "/reset — новый диалог. /status — нейросети.",
            )
            self.track(peer, mid, sent)
            return
        if cmd == "reset":
            st.update(history=[], summary="", image=False)
            sent = await self.send(peer, "🧹 Диалог очищен. Ваша инструкция сохранена.")
            self.track(peer, mid, sent)
            return
        if cmd == "prompt":
            if not arg:
                cur = st["custom"] or c.SC["system_prompt"]
                label = "Ваша инструкция" if st["custom"] else "Стандартная инструкция"
                sent = await self.send(peer, f"{label}:\n{cur}\n\nИзменить: /prompt текст\nСбросить: /prompt сброс")
            elif arg.lower() in ("сброс", "reset"):
                st.update(custom="", history=[], summary="")
                sent = await self.send(peer, "✅ Вернул стандартную инструкцию.")
            else:
                st.update(custom=arg[:1500], history=[], summary="")
                sent = await self.send(peer, "✅ Инструкция сохранена, диалог начат заново. Пишите сообщение.")
            self.track(peer, mid, sent)
            return
        if cmd == "status":
            lines = []
            now = time.time()
            for p in c.PROVIDERS:
                mark = "🟢" if p.ready else f"🔴 ещё {int(p.down_until - now)} с"
                lines.append(f"{mark} {p.name} · {p.model}" + (f"\n{p.why}" if p.why else ""))
            sent = await self.send(peer, "\n".join(lines))
            self.track(peer, mid, sent)
            return
        if cmd == "image":
            if arg:
                return await self.do_image(peer, arg)
            st["image"] = True
            sent = await self.send(peer, "🎨 Опишите, что нарисовать.")
            self.track(peer, mid, sent)
            return
        sent = await self.send(peer, "Неизвестная команда. Список: /help")
        self.track(peer, mid, sent)

    async def on_photo(self, peer, text, photo, reply_from):
        """Фото от пользователя: нейросеть с поддержкой изображений отвечает по картинке и подписи."""
        c = self.c
        ok, text = self.addressed(peer, text, reply_from)
        if not ok:
            return
        sizes = photo.get("sizes") or []
        best = max(sizes, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0), default=None)
        if not best or not best.get("url"):
            return
        try:
            async with self.sess.get(best["url"], timeout=aiohttp.ClientTimeout(total=40)) as r:
                data = await r.content.read(8_000_001)
        except Exception as e:  # noqa
            log.info("vk photo download: %s", e)
            return
        if not data or len(data) > 8_000_000:
            sent = await self.send(peer, "⚠️ Фото слишком большое или не загрузилось.")
            self.track(peer, sent)
            return
        st = self.st(peer)
        spin = await self.busy(peer)
        try:
            reply, prov, summary, history = await c.vision_reply(
                st["summary"], st["history"], st["custom"], text if not text.startswith("/") else "", data
            )
        finally:
            spin.cancel()
        if not reply:
            await self.sweep(peer)
            sent = await self.send(peer, c.NO_VISION_TEXT + "\n" + c.fail_text())
            self.track(peer, sent)
            return
        st["history"], st["summary"] = history, summary
        if prov and c.SHOW_PROVIDER:
            reply += f"\n\n⚙️ {prov}"
        for part in c.chunks(reply, 3500):
            await self.send(peer, part)
        await self.sweep(peer)

    async def on_voice(self, peer, from_id, voice, mid, reply_from):
        if peer >= CHAT and reply_from != -self.group_id:
            return
        if (voice.get("duration") or 0) > 300:
            sent = await self.send(peer, "⚠️ Голосовое слишком длинное (максимум 5 минут).")
            self.track(peer, sent)
            return
        url = voice.get("link_ogg") or voice.get("link_mp3")
        if not url:
            return
        await self.typing(peer)
        try:
            async with self.sess.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                audio = await r.read()
        except Exception as e:  # noqa
            sent = await self.send(peer, "⚠️ Не удалось скачать голосовое.")
            self.track(peer, sent)
            log.info("vk voice download: %s", e)
            return
        text, info = await self.c.transcribe(audio)
        if not text:
            await self.sweep(peer)
            sent = await self.send(peer, "⚠️ Не удалось распознать голос: " + str(info)[:500])
            self.track(peer, sent)
            return
        await self.answer(peer, text, heard=text)

    async def answer(self, peer: int, text: str, heard: str = ""):
        c = self.c
        if c.is_image_request(text):
            return await self.do_image(peer, text)
        if c.is_media_request(text):
            await self.typing(peer)
            tool, q = await c.route_media(text)
            if tool == "gen":
                return await self.do_image(peer, q or text)
            if tool != "chat":
                return await self.run_tool(peer, tool, q)
        st = self.st(peer)
        history, summary, custom = st["history"], st["summary"], st["custom"]
        history.append({"role": "user", "content": text})
        spin = await self.busy(peer)
        try:
            summary, history = await c.compact(summary, history, custom)
            reply, prov = await c.ask_llm(c.build(summary, history, custom))
        finally:
            spin.cancel()
        if not reply:
            history.pop()
            st["history"], st["summary"] = history, summary
            await self.sweep(peer)
            sent = await self.send(peer, c.fail_text())
            self.track(peer, sent)
            return
        history.append({"role": "assistant", "content": reply})
        st["history"], st["summary"] = history, summary
        if heard and c.SHOW_TRANSCRIPT:
            reply = f"🎙 {heard}\n\n{reply}"
        if prov and c.SHOW_PROVIDER:
            reply += f"\n\n⚙️ {prov}"
        for part in c.chunks(reply, 3500):
            await self.send(peer, part)
        await self.sweep(peer)

    async def do_image(self, peer: int, text: str):
        c = self.c
        spin = await self.busy(peer)
        try:
            prompt = await c.to_english_prompt(text)
            data, info = await c.gen_image(prompt)
        finally:
            spin.cancel()
        if not data:
            await self.sweep(peer)
            sent = await self.send(peer, "⚠️ Не удалось создать картинку: " + str(info)[:500])
            self.track(peer, sent)
            return
        att = await self.upload(peer, data, "image.jpg", "photo")
        cap = text[:200] + (f"\n⚙️ {info}" if c.SHOW_PROVIDER else "")
        if not att:
            await self.sweep(peer)
            sent = await self.send(peer, "⚠️ Картинка создалась, но ВК её не принял.")
            self.track(peer, sent)
            return
        await self.send(peer, "🎨 " + cap, att)
        await self.sweep(peer)

    async def run_tool(self, peer: int, tool: str, q: str):
        c = self.c
        ok = False
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            if tool in ("pic", "gif"):
                gif = tool == "gif"
                for url in (await c.search_images(sess, q, gif))[:8]:
                    data = await c._fetch(sess, url, ("image/",), 8_000_000)
                    if not data:
                        continue
                    name = "anim.gif" if gif else "pic.jpg"
                    att = await self.upload(peer, data, name, "doc" if gif else "photo")
                    if att and await self.send(peer, f"🔎 {q[:200]}", att):
                        ok = True
                        break
            elif tool == "music":
                for t in (await c.search_music(sess, q))[:5]:
                    data = await c._fetch(sess, t["preview"], ("audio/",), 15_000_000)
                    if not data:
                        continue
                    name = re.sub(r'[\\/:*?"<>|]', "", f"{t['artist']} - {t['title']}")[:70] + "." + t["ext"]
                    att = await self.upload(peer, data, name, "doc")
                    vk_music = "https://vk.com/audio?q=" + quote(f"{t['artist']} {t['title']}")
                    cap = (f"🎧 {t['artist']} — {t['title']}\nФайл — превью 30 с."
                           f"\nВ VK Музыке целиком: {vk_music}"
                           + (f"\nDeezer: {t['link']}" if t.get("link") else ""))
                    if att and await self.send(peer, cap, att):
                        ok = True
                        break
                if not ok:  # файл не загрузился — хотя бы ссылка на полную версию
                    for t in (await c.search_music(sess, q))[:1]:
                        link = "https://vk.com/audio?q=" + quote(f"{t['artist']} {t['title']}")
                        ok = bool(await self.send(peer, f"🎧 {t['artist']} — {t['title']}\nСлушать в VK Музыке: {link}"))
            elif tool == "video":
                try:
                    res = await asyncio.to_thread(c._ddg, "videos", q)
                    for r in res:
                        url = r.get("content") or r.get("url")
                        if url:
                            await self.send(peer, f"🎬 {(r.get('title') or q)[:150]}\n{url}")
                            ok = True
                            break
                except Exception as e:  # noqa
                    log.warning("vk video: %s", e)
        await self.sweep(peer)
        if not ok:
            sent = await self.send(peer, f"🔍 Ничего не нашёл по запросу «{q[:100]}». Попробуйте переформулировать.")
            self.track(peer, sent)


async def start_vk():
    token = (os.getenv("VK_GROUP_TOKEN") or os.getenv("VK_TOKEN") or "").strip()
    c = core()
    if not token:
        c.log.info("vk: выключен, нет VK_GROUP_TOKEN")
        return
    vk = Vk(c, token)
    while True:
        try:
            await vk.run()
        except asyncio.CancelledError:
            raise
        except VkError as e:
            c.log.error(
                "vk: %s. Проверьте ключ сообщества и что в настройках включён Long Poll API, событие «Входящее сообщение».",
                e,
            )
            await asyncio.sleep(15)
        except Exception:
            c.log.exception("vk: цикл упал, повтор через 5 с")
            await asyncio.sleep(5)
