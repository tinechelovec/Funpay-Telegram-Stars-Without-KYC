from __future__ import annotations
import os
import re
import time
import threading
import logging
import asyncio
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv
from FunPayAPI import Account
from FunPayAPI.updater.runner import Runner
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent

from pyrogram import Client, errors as pyerrors
from pyrogram.enums import ChatType
from pyrogram.errors import FloodWait, RPCError
from pyrogram.raw.functions.contacts import ResolveUsername

# ==================== ENV ====================
load_dotenv()

FUNPAY_AUTH_TOKEN = os.getenv("FUNPAY_AUTH_TOKEN")
API_USER = os.getenv("API_USER")
API_PASS = os.getenv("API_PASS")

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
if not API_ID or not API_HASH:
    raise RuntimeError("В .env должны быть API_ID и API_HASH (для PyroFork-сессии).")
API_ID = int(API_ID)

COOLDOWN_SECONDS = float(os.getenv("COOLDOWN_SECONDS", "1"))
AUTO_REFUND = (os.getenv("AUTO_REFUND", "false").strip().lower() in ("1","true","yes","y","on"))
AUTO_DEACTIVATE = (os.getenv("AUTO_DEACTIVATE", "false").strip().lower() in ("1","true","yes","y","on"))

CATEGORY_ID = 2418

_DEACTIVATE_DEFAULT = str(CATEGORY_ID)
DEACTIVATE_CATEGORY_ID = int(os.getenv("DEACTIVATE_CATEGORY_ID", _DEACTIVATE_DEFAULT))

# ==================== LOGGING ====================
try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
except Exception:
    class _Dummy:
        RESET_ALL = ""
    class _Fore(_Dummy):
        RED = GREEN = YELLOW = CYAN = MAGENTA = BLUE = WHITE = ""
    class _Style(_Dummy):
        BRIGHT = NORMAL = ""
    Fore, Style = _Fore(), _Style()

logger = logging.getLogger("StarsBotWithoutKYC")
logger.setLevel(logging.INFO)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s:%(lineno)d | %(message)s"))
logger.addHandler(_console_handler)

_file_handler = logging.FileHandler("log.txt", encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s:%(lineno)d | %(message)s"))
logger.addHandler(_file_handler)

# ==================== CONSTANTS ====================
NEWAPI_BASE = "https://xn--h1aahgceagbyl.xn--p1ai/api"
REQUEST_TIMEOUT = 120

USER_STATES: dict[int, dict] = {}

# ==================== TOKEN FLOW ====================
_NEWAPI_TOKEN: Optional[str] = None
_TOKEN_LOCK = threading.Lock()

def _set_token(tok: str | None):
    global _NEWAPI_TOKEN
    _NEWAPI_TOKEN = tok

def _get_token_raw() -> str:
    url = f"{NEWAPI_BASE}/token"
    payload = {"username": API_USER, "password": API_PASS}
    headers = {"accept": "application/json", "content-type": "application/json"}
    r = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Ошибка авторизации нового API: HTTP {r.status_code} | {r.text[:300]}")
    try:
        data = r.json()
    except Exception:
        raise RuntimeError("Неверный формат ответа при авторизации нового API (ожидался JSON)")
    tok = data.get("access_token") or data.get("token") or data.get("accessToken")
    if not tok:
        raise RuntimeError(f"В ответе нового API нет поля access_token/token: {data}")
    logger.info(Fore.GREEN + "✅ Получен Bearer токен нового API")
    return tok

def _ensure_token():
    if _NEWAPI_TOKEN:
        return
    with _TOKEN_LOCK:
        if not _NEWAPI_TOKEN:
            _set_token(_get_token_raw())

def _refresh_token():
    with _TOKEN_LOCK:
        try:
            _set_token(_get_token_raw())
            logger.info(Fore.CYAN + "🔄 Токен обновлён")
        except Exception as e:
            logger.error(Fore.RED + f"Не удалось обновить токен: {e}")

def start_token_refresher(interval_sec: int = 50*60):
    def _loop():
        while True:
            time.sleep(interval_sec)
            try:
                _refresh_token()
            except Exception:
                logger.exception(Fore.RED + "Исключение в цикле рефреша токена")
    t = threading.Thread(target=_loop, daemon=True)
    t.start()

def _newapi_headers() -> dict:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {_NEWAPI_TOKEN}"
    }

def _api_post(path: str, json_body: dict, retry_on_auth: bool = True) -> requests.Response:
    _ensure_token()
    url = f"{NEWAPI_BASE}{path}"
    r = requests.post(url, json=json_body, headers=_newapi_headers(), timeout=REQUEST_TIMEOUT)
    if r.status_code in (401, 403) and retry_on_auth:
        logger.warning(Fore.YELLOW + f"AUTH {r.status_code} на {path}. Обновляю токен и повторяю запрос…")
        _refresh_token()
        r = requests.post(url, json=json_body, headers=_newapi_headers(), timeout=REQUEST_TIMEOUT)
    return r

# ==================== PyroFork client ====================
_loop = asyncio.new_event_loop()
_app_started = threading.Event()
app: Optional[Client] = None

def _build_client() -> Client:
    return Client("telegram", api_id=API_ID, api_hash=API_HASH, workdir="sessions")

async def _runner_start():
    global app
    app = _build_client()
    await app.start()
    logger.info("🟢 PyroFork client started")
    try:
        await app.get_me()
    except Exception:
        logger.debug("get_me check failed", exc_info=True)
    _app_started.set()
    await asyncio.Future()

def _thread_target():
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_runner_start())
    except Exception:
        logger.exception("🔴 PyroFork failed to start")
        _app_started.set()

threading.Thread(target=_thread_target, daemon=True).start()
_app_started.wait(timeout=20)
if not _app_started.is_set():
    logger.error("PyroFork не запустился — проверки ника будут False")

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")

def nick_looks_valid(txt: str) -> bool:
    if not txt:
        return False
    t = txt.strip()
    if t.startswith("@"):
        t = t[1:]
    return bool(_USERNAME_RE.fullmatch(t))

async def _username_exists_once(username: str) -> bool:
    if app is None:
        return False

    u = (username or "").lstrip("@").strip()
    if not _USERNAME_RE.fullmatch(u):
        return False

    try:
        chat = await app.get_chat(u)
        ctype = getattr(chat, "type", None)
        if ctype in (ChatType.PRIVATE, ChatType.BOT):
            return True
        if str(ctype).lower().endswith("private") or str(ctype).lower().endswith("bot"):
            return True
        return False
    except (pyerrors.UsernameNotOccupied, pyerrors.UsernameInvalid):
        pass
    except FloodWait:
        return False
    except RPCError:
        pass
    except Exception:
        pass

    try:
        res = await app.invoke(ResolveUsername(username=u))
        users = getattr(res, "users", []) or []
        for usr in users:
            if (getattr(usr, "username", "") or "").lower() == u.lower():
                return True
        return False
    except (pyerrors.UsernameNotOccupied, pyerrors.UsernameInvalid):
        return False
    except FloodWait:
        return False
    except RPCError:
        return False
    except Exception:
        return False


def username_exists_sync(username: str, timeout: float = 15.0) -> bool:
    if app is None:
        return False
    fut = asyncio.run_coroutine_threadsafe(_username_exists_once(username), _loop)
    try:
        return bool(fut.result(timeout=timeout))
    except Exception:
        return False

def extract_stars_count(title: str, description: str = "") -> int:
    text = f"{title or ''} {description or ''}".lower()
    m = re.search(r"tg_stars[:=]\s*(\d{1,6})", text)
    if m:
        return max(1, int(m.group(1)))
    for pat in [
        r"(\d{1,6})\s*(?:зв|зв[её]зд|⭐|stars?)",
        r"(?:зв[её]зд[а-я]*\D{0,10})?(\d{1,6})(?=\D*(?:зв|⭐|stars?))",
        r"\b(\d{1,6})\b",
    ]:
        m = re.search(pat, text)
        if m:
            try:
                return max(1, int(m.group(1)))
            except Exception:
                pass
    return 50

def friendly_api_error(resp: requests.Response, default_msg: str = "Сервис временно недоступен.") -> str:
    try:
        data = resp.json()
    except Exception:
        data = {}
    tech = (data.get("message") or data.get("detail") or data.get("error") or resp.text or "").strip()

    if resp.status_code in (401, 403):
        return "Ошибка авторизации сервиса. Мы разберёмся — при необходимости сделаем возврат."
    if resp.status_code == 429:
        return "Сервис перегружен. Попробуйте чуть позже — при необходимости оформим возврат."
    if resp.status_code >= 500:
        return "У сервиса неполадки. Попробуйте позже — средства вернём по запросу."
    if resp.status_code >= 400:
        return f"Запрос отклонён сервисом: {tech[:180]}" if tech else "Запрос отклонён сервисом."
    return default_msg

def buy_stars(username: str, quantity: int) -> Tuple[bool, str, int]:
    payload = {"username": username.lstrip("@"), "quantity": int(quantity)}
    r = _api_post("/buyStars", json_body=payload)
    if r.status_code == 200:
        return True, (r.text or "OK"), 200
    return False, friendly_api_error(r, "Не удалось купить звёзды."), r.status_code

# ==================== FunPay helpers ====================
def get_subcategory_id_safe(order, account) -> Tuple[int | None, object | None]:
    subcat = getattr(order, "subcategory", None) or getattr(order, "sub_category", None)
    if subcat and hasattr(subcat, "id"):
        return subcat.id, subcat
    try:
        full_order = account.get_order(order.id)
        subcat = getattr(full_order, "subcategory", None) or getattr(full_order, "sub_category", None)
        if subcat and hasattr(subcat, "id"):
            return subcat.id, subcat
    except Exception as e:
        logger.warning(Fore.YELLOW + f"Не удалось загрузить полный заказ: {e}")
    return None, None

def order_link(order_id) -> str:
    try:
        return f"https://funpay.com/orders/{int(order_id)}/"
    except Exception:
        return "https://funpay.com/orders/"

# -------- Авто-деактивация лотов --------
def deactivate_category(account: Account, category_id: int):
    try:
        my_lots = account.get_my_subcategory_lots(category_id)
    except Exception as e:
        logger.error(Fore.RED + f"[LOTS] Не удалось получить список лотов категории {category_id}: {e}")
        return 0

    if not my_lots:
        logger.info(Fore.YELLOW + f"[LOTS] В категории {category_id} нет лотов для деактивации.")
        return 0

    deactivated = 0
    for lot in my_lots:
        lot_id = getattr(lot, "id", None)
        if lot_id is None:
            continue
        try:
            fields = account.get_lot_fields(lot_id)
            if fields is None:
                continue
            if isinstance(fields, dict):
                if fields.get("active", fields.get("is_active", True)) is False:
                    continue
                fields["active"] = False
                account.save_lot(fields)
            else:
                for attr in ("active", "is_active", "enabled"):
                    if hasattr(fields, attr):
                        setattr(fields, attr, False)
                        break
                account.save_lot(fields)
            deactivated += 1
            time.sleep(0.2)
        except Exception as e:
            logger.error(Fore.RED + f"[LOTS] Ошибка деактивации лота {lot_id}: {e}")
            continue

    logger.warning(Fore.MAGENTA + f"[LOTS] Авто-деактивировано: {deactivated} (категория {category_id})")
    return deactivated

# ==================== CHECK USERNAME ====================
def check_username_and_reason(uname: str) -> tuple[bool, str]:
    if not nick_looks_valid(uname):
        return False, "Неверный формат ника. Укажите @username (5–32 символов, латиница/цифры/_)."
    if not username_exists_sync(uname):
        return False, "Такого ника нет. Попробуйте другой @username."
    return True, ""

# ==================== HANDLERS ====================
def _notify_new_order(account: Account, order_id, title, stars):
    logger.info(Style.BRIGHT + Fore.WHITE + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info(Fore.CYAN + f"🆕 Новый заказ #{order_id}")
    if title:
        logger.info(Fore.CYAN + f"📦 Товар: {title}")
    logger.info(Fore.MAGENTA + f"💫 К выдаче звёзд: {stars}")
    logger.info(Style.BRIGHT + Fore.WHITE + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

def _nice_refund(account: Account, chat_id, order_id, reason: str):
    logger.warning(Fore.YELLOW + f"↩️ Возврат для заказа {order_id}: {reason}")
    msg = (
        "❌ " + reason + "\n\n" +
        ("Деньги будут возвращены автоматически." if AUTO_REFUND else "⚠️ Автоматический возврат выключен. Свяжитесь с админом для возврата.")
    )
    if chat_id:
        account.send_message(chat_id, msg)
    if AUTO_REFUND and order_id:
        try:
            account.refund(order_id)
        except Exception as e:
            logger.error(Fore.RED + f"[REFUND] Ошибка возврата по заказу {order_id}: {e}")

    if AUTO_DEACTIVATE:
        try:
            deactivated = deactivate_category(account, DEACTIVATE_CATEGORY_ID)
            logger.info(Fore.MAGENTA + f"[LOTS] Авто-деактивация завершена: {deactivated}")
        except Exception as e:
            logger.error(Fore.RED + f"[LOTS] Авто-деактивация не удалась: {e}")

def handle_new_order(account: Account, order):
    subcat_id, _ = get_subcategory_id_safe(order, account)
    if subcat_id != CATEGORY_ID:
        logger.info(Fore.BLUE + f"⏭ Пропуск заказа — подкатегория {subcat_id}, требуется {CATEGORY_ID}")
        return

    title = getattr(order, "title", "") or getattr(order, "short_description", "")
    desc = getattr(order, "full_description", "") or getattr(order, "short_description", "")
    stars = extract_stars_count(title, desc)

    chat_id = getattr(order, "chat_id", None)
    buyer_id = getattr(order, "buyer_id", None)

    _notify_new_order(account, getattr(order, "id", None), title, stars)

    USER_STATES[buyer_id] = {
        "state": "await_username",
        "order_id": getattr(order, "id", None),
        "chat_id": chat_id,
        "stars": stars,
        "temp_nick": None,
    }

    account.send_message(
        chat_id,
        ("""🎉 Спасибо за покупку!

К выдаче: {stars} ⭐

Пожалуйста, пришлите ваш Telegram-тег в формате @username.
Если не знаете свой тег: Telegram → Профиль → Имя пользователя.""").format(stars=stars)
    )

def handle_new_message(account: Account, message):
    user_id = getattr(message, "author_id", None)
    chat_id = getattr(message, "chat_id", None)
    if not user_id or user_id not in USER_STATES:
        return

    text = (getattr(message, "text", "") or "").strip()
    if not text:
        return

    state = USER_STATES[user_id]
    stars = int(state.get("stars", 50))

    if state["state"] == "await_username":
        nick = (text or "").strip()
        if not nick.startswith("@"):
            nick = "@" + nick

        ok, reason = check_username_and_reason(nick)
        if not ok:
            account.send_message(chat_id, f"❌ {reason}")
            return
        state["temp_nick"] = nick
        state["state"] = "await_confirm"
        account.send_message(
            chat_id,
            f"Вы указали: {nick}. Если верно — отправьте `+`. Если нужно изменить — пришлите другой @username."
        )
        return

    if state["state"] == "await_confirm":
        if text == "+":
            username = state.get("temp_nick", "").lstrip("@")
            if not username:
                account.send_message(chat_id, "❌ Не удалось определить имя пользователя. Пришлите @username снова.")
                state["state"] = "await_username"
                return

            order_id = state.get("order_id")
            account.send_message(chat_id, f"🚀 Отправляю {stars} ⭐ пользователю @{username}…")
            try:
                ok, msg, status = buy_stars(username, stars)
            except Exception as e:
                ok, msg, status = False, f"Исключение при покупке звёзд: {e}", 0

            if ok:
                account.send_message(
                    chat_id,
                    (
                        f"✅ Успешно отправлено {stars} ⭐ пользователю @{username}! Спасибо за заказ.\n\n"
                        "🙏 Если всё прошло хорошо, пожалуйста, оставьте короткий отзыв — это очень помогает другим покупателям. Спасибо! ❤️"
                    )
                )
                logger.info(Fore.GREEN + f"✅ @{username} получил {stars} ⭐ | order {order_id}")
            else:
                reason = msg or "Неизвестная ошибка оплаты"
                logger.error(Fore.RED + f"❌ Ошибка покупки звёзд | order {order_id} | HTTP {status} | {reason}")
                _nice_refund(account, chat_id, order_id, reason)

            USER_STATES.pop(user_id, None)
            return
        else:
            new_nick = (text or "").strip()
            if not new_nick.startswith("@"):
                new_nick = "@" + new_nick
            ok, reason = check_username_and_reason(new_nick)
            if not ok:
                account.send_message(chat_id, f"❌ {reason}")
                return
            state["temp_nick"] = new_nick
            account.send_message(chat_id, f"Обновлено: {new_nick}. Если верно — отправьте `+`.")
            return

# ==================== MAIN LOOP ====================
def main():
    if not FUNPAY_AUTH_TOKEN:
        raise RuntimeError("FUNPAY_AUTH_TOKEN не найден в .env")
    if not (API_USER and API_PASS):
        raise RuntimeError("API_USER/API_PASS не заданы в .env")

    _ensure_token()
    start_token_refresher()

    account = Account(FUNPAY_AUTH_TOKEN)
    account.get()
    logger.info(Fore.GREEN + f"🔐 Авторизован как {getattr(account, 'username', '(unknown)')}")
    logger.info(Fore.CYAN + f"Настройки: AUTO_REFUND={AUTO_REFUND}, AUTO_DEACTIVATE={AUTO_DEACTIVATE}, CATEGORY_ID={CATEGORY_ID}, DEACTIVATE_CATEGORY_ID={DEACTIVATE_CATEGORY_ID}")

    runner = Runner(account)
    logger.info(Style.BRIGHT + Fore.WHITE + "🚀 StarsBot запущен. Ожидание событий…")

    last_reply = 0.0
    for event in runner.listen(requests_delay=3.0):
        try:
            now = time.time()
            if now - last_reply < COOLDOWN_SECONDS:
                continue

            if isinstance(event, NewOrderEvent):
                order = account.get_order(event.order.id)
                handle_new_order(account, order)
                last_reply = now
                continue

            if isinstance(event, NewMessageEvent):
                msg = event.message
                if getattr(msg, "author_id", None) == account.id:
                    continue
                handle_new_message(account, msg)
                last_reply = now
                continue
        except Exception:
            logger.exception(Fore.RED + "Исключение в основном цикле")

if __name__ == "__main__":
    main()
