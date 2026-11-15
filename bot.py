import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, Message
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
SHOW_PROVIDER = os.getenv("SHOW_PROVIDER", "1") == "1"
COMPACT_AT = int(os.getenv("COMPACT_AT", "3500"))  # ~токенов, после чего сжимаем историю
KEEP_LAST = 4  # сколько последних реплик оставлять дословно при сжатии

SC = json.loads((BASE / "scenario.json").read_text(encoding="utf-8"))
STEPS = SC["steps"]
router = Router()


# ================= провайдеры + автопереключение =================
class Provider:
    def __init__(self, c: dict, key: str):
        self.name = c["name"]
        self.model = c["model"]
        self.ctx = int(c.get("ctx", 8000))  # лимит контекста в токенах
        self.client = AsyncOpenAI(
            api_key=key, base_url=c["base_url"], max_retries=0, timeout=60
        )
        self.down_until = 0.0

    @property
    def ready(self) -> bool:
        return self.down_until <= time.time()

    def ban(self, sec: float, why: str):
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
            p.ban(sec, "429 limit")
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
                p.ban(86400, f"{code} key/model")
            elif code == 402:
                p.ban(6 * 3600, "402 no balance")
            elif code >= 500:
                p.ban(120, f"{code} server")
            else:
                p.ban(300, f"{code}")
        except APIConnectionError:
            p.ban(120, "connection/timeout")
        except Exception as e:  # noqa
            log.exception("unexpected")
            p.ban(300, repr(e))
    return None, None


# ================= контекст: сжатие вместо потери =================
def build(summary: str, history: list) -> list:
    sys = SC["system_prompt"]
    if summary:
        sys += "\n\nКраткое содержание предыдущего диалога:\n" + summary
    return [{"role": "system", "content": sys}] + history


async def compact(summary: str, history: list):
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


# ================= UI-хелперы =================
class Flow(StatesGroup):
    choice = State()
    text = State()
    chat = State()


class Safe(dict):
    def __missing__(self, k):
        return ""


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def pbar(i: int, n: int) -> str:
    return "▰" * i + "▱" * (n - i)


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


def kb_menu():
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Начать", callback_data="go")
    b.button(text="ℹ️ О боте", callback_data="about")
    b.adjust(1)
    return b.as_markup()


def kb_after():
    b = InlineKeyboardBuilder()
    b.button(text="💬 Уточнить", callback_data="chat_hint")
    b.button(text="🔄 Заново", callback_data="go")
    b.button(text="🏠 Меню", callback_data="menu")
    b.adjust(1, 2)
    return b.as_markup()


def kb_fail():
    b = InlineKeyboardBuilder()
    b.button(text="🔁 Повторить", callback_data="retry")
    b.button(text="🏠 Меню", callback_data="menu")
    b.adjust(2)
    return b.as_markup()


async def send_result(msg: Message, text: str, kb=None, provider: str = ""):
    parts = list(chunks(text))
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        foot_h = f"\n\n<i>⚙️ {esc(provider)}</i>" if last and provider and SHOW_PROVIDER else ""
        foot_p = f"\n\n⚙️ {provider}" if last and provider and SHOW_PROVIDER else ""
        try:
            await msg.answer(md_to_html(p) + foot_h, reply_markup=kb if last else None)
        except Exception:
            await msg.answer(p + foot_p, reply_markup=kb if last else None, parse_mode=None)


def allowed(uid: int) -> bool:
    return not ALLOWED or uid in ALLOWED


FAIL_TEXT = "⚠️ Все нейросети сейчас на лимите. Подождите пару минут и повторите."


# ================= сценарий =================
async def ask_step(msg: Message, state: FSMContext, idx: int, edit: bool):
    step = STEPS[idx]
    text = (
        f"<b>{esc(SC['title'])}</b>\n"
        f"{pbar(idx, len(STEPS))}  {idx + 1}/{len(STEPS)}\n\n"
        f"{esc(step['question'])}"
    )
    b = InlineKeyboardBuilder()
    if step["type"] == "choice":
        for i, o in enumerate(step["options"]):
            b.button(text=o, callback_data=f"opt:{i}")
        b.adjust(2)
        await state.set_state(Flow.choice)
    else:
        text += "\n\n<i>Напишите ответ сообщением ✍️</i>"
        await state.set_state(Flow.text)
    b.row(InlineKeyboardButton(text="✖️ Отмена", callback_data="menu"))
    await state.update_data(idx=idx)
    if edit:
        try:
            await msg.edit_text(text, reply_markup=b.as_markup())
            return
        except Exception:
            pass
    await msg.answer(text, reply_markup=b.as_markup())


async def advance(msg: Message, state: FSMContext, edit: bool):
    d = await state.get_data()
    nxt = d["idx"] + 1
    if nxt < len(STEPS):
        await ask_step(msg, state, nxt, edit)
    else:
        await finish(msg, state)


async def finish(msg: Message, state: FSMContext):
    d = await state.get_data()
    prompt = SC["final_prompt"].format_map(Safe(d["answers"]))
    history = [{"role": "user", "content": prompt}]
    status = await msg.answer("⏳ <i>Думаю над ответом…</i>")
    await msg.bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    reply, prov = await ask_llm(build("", history))
    try:
        await status.delete()
    except Exception:
        pass
    if not reply:
        return await msg.answer(FAIL_TEXT, reply_markup=kb_fail())
    history.append({"role": "assistant", "content": reply})
    await state.set_state(Flow.chat)
    await state.update_data(history=history, summary="")
    await send_result(msg, reply, kb_after(), prov)


# ================= хендлеры =================
@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message, state: FSMContext):
    if not allowed(m.from_user.id):
        return await m.answer("⛔ Нет доступа.")
    await state.clear()
    await m.answer(f"<b>{esc(SC['title'])}</b>\n\n{esc(SC['welcome'])}", reply_markup=kb_menu())


@router.message(Command("reset"))
async def cmd_reset(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🧹 Сброшено.", reply_markup=kb_menu())


@router.message(Command("status"))
async def cmd_status(m: Message):
    if ALLOWED and m.from_user.id not in ALLOWED:
        return
    lines = []
    for p in PROVIDERS:
        st = "🟢" if p.ready else f"🔴 ещё {int(p.down_until - time.time())} с"
        lines.append(f"{st} <b>{esc(p.name)}</b> · {esc(p.model)} · ctx {p.ctx}")
    await m.answer("\n".join(lines))


@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "/start — меню\n/reset — сбросить диалог\n/status — состояние нейросетей\n\n"
        "Пройдите шаги сценария, затем можно уточнять ответ в чате."
    )


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.answer(
        f"<b>{esc(SC['title'])}</b>\n\n{esc(SC['welcome'])}", reply_markup=kb_menu()
    )
    await c.answer()


@router.callback_query(F.data == "about")
async def cb_about(c: CallbackQuery):
    await c.answer(SC.get("about", "Бот с автопереключением нейросетей"), show_alert=True)


@router.callback_query(F.data == "go")
async def cb_go(c: CallbackQuery, state: FSMContext):
    if not allowed(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    await state.clear()
    await state.update_data(answers={})
    await ask_step(c.message, state, 0, edit=False)
    await c.answer()


@router.callback_query(F.data == "retry")
async def cb_retry(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await c.answer()
    if "answers" in d:
        await finish(c.message, state)
    else:
        await c.message.answer("Начните заново 👇", reply_markup=kb_menu())


@router.callback_query(F.data == "chat_hint")
async def cb_chat_hint(c: CallbackQuery):
    await c.message.answer("💬 Напишите уточнение или вопрос — продолжу с учётом ответа выше.")
    await c.answer()


@router.callback_query(Flow.choice, F.data.startswith("opt:"))
async def cb_opt(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    step = STEPS[d["idx"]]
    value = step["options"][int(c.data.split(":")[1])]
    answers = d["answers"]
    answers[step["key"]] = value
    await state.update_data(answers=answers)
    await c.answer(f"✓ {value}")
    await advance(c.message, state, edit=True)


@router.message(Flow.text, F.text)
async def msg_text_step(m: Message, state: FSMContext):
    d = await state.get_data()
    step = STEPS[d["idx"]]
    answers = d["answers"]
    answers[step["key"]] = m.text.strip()
    await state.update_data(answers=answers)
    await advance(m, state, edit=False)


@router.message(Flow.chat, F.text)
async def msg_chat(m: Message, state: FSMContext):
    d = await state.get_data()
    history, summary = d.get("history", []), d.get("summary", "")
    history.append({"role": "user", "content": m.text})
    await m.bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    summary, history = await compact(summary, history)
    reply, prov = await ask_llm(build(summary, history))
    if not reply:
        history.pop()  # не засоряем контекст неотвеченной репликой
        await state.update_data(history=history, summary=summary)
        return await m.answer(FAIL_TEXT + "\nОтправьте сообщение ещё раз.")
    history.append({"role": "assistant", "content": reply})
    await state.update_data(history=history, summary=summary)
    await send_result(m, reply, kb_after(), prov)


@router.message(F.text)
async def msg_fallback(m: Message):
    await m.answer("Нажмите, чтобы начать 👇", reply_markup=kb_menu())


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
            BotCommand(command="start", description="Меню"),
            BotCommand(command="reset", description="Сбросить диалог"),
            BotCommand(command="status", description="Состояние нейросетей"),
            BotCommand(command="help", description="Помощь"),
        ]
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
