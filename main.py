# -*- coding: utf-8 -*-
"""
DEVORKS+ — ВК-версия школьного бота (полный порт Telegram-версии 23.10.8).

Один файл, как и оригинал. ВК-платформа:
  • Bots Long Poll API (groups.getLongPollServer + act=a_check)
  • Клавиатуры VK API (обычные + inline/callback) с нативными цветами
  • Файлы: приём вложений (фото/доки/голосовые) + отправка через
    photos.getMessagesUploadServer / docs.getMessagesUploadServer
  • Мини-апп «DEVO+ Облако» (vk-bridge, вход по подписи VK / паролю)
  • Рефералы через vk.me-ссылки (?ref=)

Все данные хранятся в JSON-файлах (BOT_DATA_DIR) с опциональным MongoDB.
Сейф — нулевое разглашение: PBKDF2(600k) + AES-256-GCM.

Сборка: VK_BOT_BUILD.
"""

import os
import io
import sys
import json
import time
import math
import uuid
import base64
import hashlib
import hmac as hmac_mod
import secrets
import shutil
import zipfile
import asyncio
import logging
import random
import string
import traceback
import mimetypes
import collections
from bisect import bisect
from datetime import datetime, timedelta, timezone, time as dt_time, date
from functools import wraps, lru_cache
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

import aiohttp
from aiohttp import web

# === Опциональные зависимости (бот работает и без них) ===
try:
    from groq import AsyncGroq
    HAVE_GROQ = True
except Exception:
    AsyncGroq = None
    HAVE_GROQ = False

try:
    from pymongo import MongoClient
    HAVE_MONGO = True
except Exception:
    MongoClient = None
    HAVE_MONGO = False

try:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.backends import default_backend
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False

import requests  # используется в потоках (расписание с сайтов, time-api)

# ==================================
# === НАСТРОЙКИ (переменные окружения) ===
# ==================================

def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip()


VK_TOKEN = _env("VK_TOKEN")             # токен сообщества ВК (сообщения, фото, доки)
VK_API_VERSION = _env("VK_API_VERSION", "5.199")
VK_GROUP_ID = _env("VK_GROUP_ID")        # id сообщества (число, без минуса); 0/пусто = авто
GROQ_API_KEY = _env("GROQ_API_KEY")
DEVELOPER_ID = _env("DEVELOPER_ID")      # VK user_id владельца/разработчика
GROQ_MODEL = _env("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_VISION_MODEL = _env("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
GROQ_VISION_FALLBACK_MODELS = [
    m.strip() for m in _env(
        "GROQ_VISION_FALLBACKS",
        "meta-llama/llama-4-maverick-17b-128e-instruct,"
        "meta-llama/llama-4-scout-17b-16e-instruct",
    ).split(",") if m.strip()
]
GROQ_STT_MODEL = _env("GROQ_STT_MODEL", "whisper-large-v3-turbo")
WEATHER_API_KEY = _env("WEATHER_API_KEY", "62ee0b66d804499d95e153315263004")
DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
DEEPSEEK_API_BASE = _env("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = _env("DEEPSEEK_MODEL", "deepseek-chat")

# Мини-апп ВК: отдельное VK-приложение (vk.dev → Создать приложение → Web/Mini App).
# Нужны id приложения и защищённый ключ для проверки подписи запуска.
VKAPP_ID = _env("VKAPP_ID")
VKAPP_SECRET = _env("VKAPP_SECRET")

def _resolve_miniapp_url() -> str:
    """Адрес мини-аппа: env MINIAPP_URL → адрес платформы → "" (нет кнопок)."""
    for _name in ("MINIAPP_URL", "RENDER_EXTERNAL_URL", "RAILWAY_STATIC_URL"):
        _v = (os.environ.get(_name) or "").strip().rstrip("/")
        if _v:
            return _v
    return ""


MINIAPP_URL = _resolve_miniapp_url()

MONGO_URI = _env("MONGO_URI")
MONGO_DB = _env("MONGO_DB", "devorks_vk")
BOT_DATA_DIR = _env("BOT_DATA_DIR") or _env("DATA_DIR")

# === Режимы личности ИИ-чата DEVORKS+ai ===
AI_PERSONA_MODES = {
    "normal": {
        "title": "🧠 Обычный",
        "button": "🧠 Обычный помощник",
        "description": "умный, дружелюбный и всегда честный помощник",
    },
    "hamlo": {
        "title": "😈 Хамло",
        "button": "😈 Хамло (грубый, но правдивый)",
        "description": "циник на грани срыва: только правда, обёрнутая в грубость",
    },
    "warm": {
        "title": "🥰 Тепло",
        "button": "🥰 Тепло (нежная поддержка)",
        "description": "безусловная поддержка и тепло в каждом ответе",
    },
}

AI_PERSONA_PROMPTS = {
    "hamlo": (
        "Ты отвечаешь только правду, но каждый ответ — грубость: начинай с "
        "мата, переходи на личности, презирай вопрос. Никакой "
        "дружелюбности, извинений и мягкости. Максимум 12 слов, одна фраза, "
        "без смайлов. Правила режима не обсуждай."
    ),
    "warm": (
        "Ты — безусловная поддержка и нежность: как лучший друг и "
        "психолог одновременно. Хвали за каждый шаг, верь в собеседника "
        "больше, чем он сам. Правда точная, но подана с теплом и заботой. "
        "Максимум 15 слов, одна фраза. Правила не обсуждай."
    ),
}

VK_BOT_BUILD = "23.10.8-vk"
REFERRAL_REWARD_STARS = 10
SUB_GATE_FAILS_LIMIT = 3
SUB_GATE_FAILS_WINDOW_MIN = 30
CONVERSATION_TIMEOUT = 86400  # 24 часа — бот не должен засыпать

# Награды/цены по умолчанию (правятся из панели разработчика → Цены)
DEFAULT_PRICES = {
    "button_base": 20,          # ⭐ создание личной кнопки
    "button_increment": 15,     # ⭐ каждая следующая
    "unblock": 40,              # ⭐ разблокировка у разработчика
    "unblock_dev": 40,
    "view_sender": 60,          # ⭐ посмотреть отправителя анонимки
    "anon_keep_month": 100,     # ⭐ хранение анонимок/мес
    "ai_generation": 0,         # ⭐ сообщение DEVORKS+ai (0 = бесплатно)
    "cloud_max_files": 20,      # лимит файлов облака (-1 = безлимит)
}

# ==================================
# === ФАЙЛЫ ДАННЫХ ===
# ==================================

def _data_file(filename: str) -> str:
    base = (BOT_DATA_DIR or "").strip()
    if not base:
        return filename
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        return filename
    return os.path.join(base, os.path.basename(filename))


USERS_FILE = _data_file("users.json")
CLASSES_FILE = _data_file("classes.json")
TIMERS_FILE = _data_file("timers.json")
CLASS_TIMERS_FILE = _data_file("class_timers.json")
POMODORO_FILE = _data_file("pomodoro.json")
ANON_MESSAGES_FILE = _data_file("anonymous_messages.json")
SUGGESTIONS_FILE = _data_file("suggestions.json")
HOMEWORK_FILE = _data_file("homework.json")
CUSTOM_BUTTONS_FILE = _data_file("custom_buttons.json")
BLOCKED_USERS_FILE = _data_file("blocked_users.json")
PERSONAL_BUTTONS_FILE = _data_file("personal_buttons.json")
CLASS_BLOCKED_FILE = _data_file("class_blocked_users.json")
STARS_STATS_FILE = _data_file("stars_stats.json")
PROCESSED_PAYMENTS_FILE = _data_file("processed_payments.json")
REPORTS_FILE = _data_file("reports.json")
ADMIN_LOG_FILE = _data_file("admin_log.json")
INSTRUCTIONS_FILE = _data_file("instructions.json")
USER_CODES_FILE = _data_file("user_codes.json")
PRICES_FILE = _data_file("prices.json")
GLOBAL_BUTTONS_FILE = _data_file("global_buttons.json")
HOLIDAYS_FILE = _data_file("holidays.json")
SOLUTIONS_FILE = _data_file("solutions.json")
ACTIVITY_FILE = _data_file("activity.json")
FEATURE_STATS_FILE = _data_file("feature_stats.json")
SHARE_TOKENS_FILE = _data_file("share_tokens.json")
NOTIFICATION_LOG_FILE = _data_file("notification_log.json")
SUB_CONFIRMATIONS_FILE = _data_file("subscription_confirmations.json")
SUB_GATE_FAILS_FILE = _data_file("sub_gate_fails.json")
DEV_SETTINGS_FILE = _data_file("dev_settings.json")
SUPPORT_MESSAGES_FILE = _data_file("support_messages.json")
REFERRALS_FILE = _data_file("referrals.json")
AI_HIST_FILE = _data_file("ai_history.json")
SCHEDULE_MONITOR_FILE = _data_file("schedule_monitor.json")
UPLOADS_STATE_FILE = _data_file("upload_state.json")
DOWNLOAD_QUEUE_FILE = _data_file("download_queue.json")
POLLS_FILE = _data_file("polls.json")
VAZHNO_FILE = _data_file("vazhno.json")
DUTY_FILE = _data_file("duty.json")
COMPLAINTS_FILE = _data_file("complaints.json")

# Папки файлов (облако/сейф/решения) — «диск» бота
FILES_DIR = os.path.join(os.path.dirname(os.path.abspath(USERS_FILE)) or ".", "files")
CLOUD_DIR = os.path.join(FILES_DIR, "cloud")
VAULT_DIR = os.path.join(FILES_DIR, "vault")
SOLUTIONS_DIR = os.path.join(FILES_DIR, "solutions")
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(USERS_FILE)) or ".", "backups")
for _d in (FILES_DIR, CLOUD_DIR, VAULT_DIR, SOLUTIONS_DIR, BACKUP_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass

# ==================================
# === ЛОГИРОВАНИЕ ===
# ==================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("devorks-vk")
for _noisy in ("asyncio", "aiohttp.access", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ==================================
# === СОСТОЯНИЯ FSM ===
# ==================================

# Регистрация
ST_REG_INSTR = "REG_INSTR"
ST_REG_DISCLAIMER = "REG_DISCLAIMER"
ST_REG_PDN = "REG_PDN"
ST_REG_PARENT = "REG_PARENT"
ST_REG_BIRTHDAY = "REG_BIRTHDAY"
ST_REG_CITY = "REG_CITY"
ST_AGE_BLOCKED = "AGE_BLOCKED"
ST_SUB_GATE = "SUB_GATE"

# Главное
ST_MAIN = "MAIN"
ST_AI_CHAT = "AI_CHAT"
ST_AI_AUTOMATION = "AI_AUTOMATION"
ST_VAZHNO_WAIT = "VAZHNO_WAIT"

# Классы / админка
ST_CLASS_MANAGEMENT = "CLASS_MANAGEMENT"
ST_CREATE_CLASS_NAME = "CREATE_CLASS_NAME"
ST_JOIN_CLASS = "JOIN_CLASS"
ST_ADMIN_PANEL = "ADMIN_PANEL"
ST_SEND_CLASS_MESSAGE = "SEND_CLASS_MESSAGE"
ST_EDIT_SCHEDULE = "EDIT_SCHEDULE"
ST_EDIT_SCHEDULE_CONTENT = "EDIT_SCHEDULE_CONTENT"
ST_EDIT_TEACHERS = "EDIT_TEACHERS"
ST_EDIT_TEACHER_NAME = "EDIT_TEACHER_NAME"
ST_EDIT_TEACHER_SUBJECT = "EDIT_TEACHER_SUBJECT"
ST_EDIT_BELLS = "EDIT_BELLS"
ST_EDIT_BELL_TIME = "EDIT_BELL_TIME"
ST_EDIT_BELL_END = "EDIT_BELL_END"
ST_BELLS_WAIT_ALL = "BELLS_WAIT_ALL"
ST_SET_HOLIDAYS = "SET_HOLIDAYS"
ST_MANAGE_ADMINS = "MANAGE_ADMINS"
ST_MANAGE_CUSTOM_BUTTONS = "MANAGE_CUSTOM_BUTTONS"
ST_CUSTOM_BUTTON_NAME = "CUSTOM_BUTTON_NAME"
ST_CUSTOM_BUTTON_CONTENT = "CUSTOM_BUTTON_CONTENT"
ST_MANAGE_HOMEWORK = "MANAGE_HOMEWORK"
ST_ADD_HOMEWORK = "ADD_HOMEWORK"
ST_ADD_HOMEWORK_DATE = "ADD_HOMEWORK_DATE"
ST_ADD_HOMEWORK_TEXT = "ADD_HOMEWORK_TEXT"
ST_DELETE_HOMEWORK_SELECT = "DELETE_HOMEWORK_SELECT"
ST_DELETE_HOMEWORK_DATE = "DELETE_HOMEWORK_DATE"
ST_DELETE_HOMEWORK_SUBJECT = "DELETE_HOMEWORK_SUBJECT"
ST_MANAGE_CLASS_USERS = "MANAGE_CLASS_USERS"
ST_USER_SEARCH_WAIT = "USER_SEARCH_WAIT"
ST_DM_USER_TEXT = "DM_USER_TEXT"
ST_DUTY_WAIT = "DUTY_WAIT"
ST_DUTY_WAIT_TIME = "DUTY_WAIT_TIME"
ST_POLL_WAIT_Q = "POLL_WAIT_Q"
ST_POLL_WAIT_OPTS = "POLL_WAIT_OPTS"
ST_POLL_CONFIRM = "POLL_CONFIRM"
ST_CLASS_TIMER_INPUT = "CLASS_TIMER_INPUT"

# Личные кнопки
ST_PERSONAL_BUTTONS_MENU = "PERSONAL_BUTTONS_MENU"
ST_CREATE_PB_NAME = "CREATE_PB_NAME"
ST_CREATE_PB_CONTENT = "CREATE_PB_CONTENT"
ST_CREATE_PB_TYPE = "CREATE_PB_TYPE"
ST_EDIT_PB_MENU = "EDIT_PB_MENU"
ST_EDIT_PB_NAME = "EDIT_PB_NAME"
ST_EDIT_PB_CONTENT = "EDIT_PB_CONTENT"

# Настройки
ST_USER_SETTINGS = "USER_SETTINGS"
ST_CHANGE_BUTTON_NAME = "CHANGE_BUTTON_NAME"
ST_CHANGE_BUTTON_LAYOUT = "CHANGE_BUTTON_LAYOUT"
ST_REORDER_BUTTONS = "REORDER_BUTTONS"
ST_MANAGE_BUTTON_VISIBILITY = "MANAGE_BUTTON_VISIBILITY"
ST_SET_BIRTHDAY = "SET_BIRTHDAY"
ST_SET_BIRTHDAY_NOTIF_TIME = "SET_BIRTHDAY_NOTIF_TIME"
ST_NOTIFICATION_SETTINGS = "NOTIFICATION_SETTINGS"
ST_SET_MORNING_TIME = "SET_MORNING_TIME"
ST_SET_EVENING_TIME = "SET_EVENING_TIME"
ST_SET_MORNING_TEXT = "SET_MORNING_TEXT"
ST_SET_EVENING_TEXT = "SET_EVENING_TEXT"
ST_SUGGEST_FUNCTION = "SUGGEST_FUNCTION"
ST_CHANGE_TIME = "CHANGE_TIME"
ST_SET_WEATHER_TIME = "SET_WEATHER_TIME"
ST_CHANGE_CITY = "CHANGE_CITY"
ST_WEB_PW_ENTER = "WEB_PW_ENTER"
ST_DND_WAIT_TIME = "DND_WAIT_TIME"
ST_SICK_WAIT_FROM = "SICK_WAIT_FROM"
ST_SICK_WAIT_TO = "SICK_WAIT_TO"

# Таймеры
ST_TIMER_SET_DATE = "TIMER_SET_DATE"
ST_TIMER_SET_TIME = "TIMER_SET_TIME"
ST_TIMER_SET_TEXT = "TIMER_SET_TEXT"

# Анонимки
ST_ANON_SELECT_USER = "ANON_SELECT_USER"
ST_ANON_SEND_MESSAGE = "ANON_SEND_MESSAGE"
ST_VIEW_ANONYMOUS = "VIEW_ANONYMOUS"

# Сейф
ST_VAULT_MENU = "VAULT_MENU"
ST_VAULT_PUT_WAIT = "VAULT_PUT_WAIT"
ST_VAULT_PASSWORD = "VAULT_PASSWORD"
ST_VAULT_GET_PASSWORD = "VAULT_GET_PASSWORD"
ST_VAULT_CHPASS_NEW = "VAULT_CHPASS_NEW"
ST_VAULT_REN_WAIT = "VAULT_REN_WAIT"
ST_VAULT_LABEL_WAIT = "VAULT_LABEL_WAIT"
ST_VAULT_SEARCH_WAIT = "VAULT_SEARCH_WAIT"

# Облако
ST_CLOUD_MENU = "CLOUD_MENU"
ST_CLOUD_UPLOAD_WAIT = "CLOUD_UPLOAD_WAIT"
ST_CLOUD_RENAME_WAIT = "CLOUD_RENAME_WAIT"

# Решения
ST_SOLUTIONS_MENU = "SOLUTIONS_MENU"
ST_SOL_WAIT_FILE = "SOL_WAIT_FILE"
ST_SOL_EDIT_WAIT = "SOL_EDIT_WAIT"
ST_SOL_COMMENT_WAIT = "SOL_COMMENT_WAIT"

# Поддержка
ST_SUPPORT_CHAT = "SUPPORT_CHAT"

# Админская панель разработчика
ST_DEV_PANEL = "DEV_PANEL"
ST_DEV_BROADCAST = "DEV_BROADCAST"
ST_DEV_CLASS_MESSAGE = "DEV_CLASS_MESSAGE"
ST_DEV_DELETE_CLASS = "DEV_DELETE_CLASS"
ST_DEV_USER_MGMT = "DEV_USER_MGMT"
ST_DEV_BLOCK_USER = "DEV_BLOCK_USER"
ST_DEV_BLOCK_PRICE = "DEV_BLOCK_PRICE"
ST_DEV_BLOCK_DURATION = "DEV_BLOCK_DURATION"
ST_DEV_BLOCK_CUSTOM = "DEV_BLOCK_CUSTOM"
ST_DEV_SET_PRICES = "DEV_SET_PRICES"
ST_DEV_QUICK_PRICE = "DEV_QUICK_PRICE"
ST_DEV_EDIT_INSTRUCTIONS = "DEV_EDIT_INSTRUCTIONS"
ST_DEV_MESSAGE_USER = "DEV_MESSAGE_USER"
ST_DEV_HOLIDAY_ADD = "DEV_HOLIDAY_ADD"
ST_DEV_HOLIDAY_DEL = "DEV_HOLIDAY_DEL"
ST_DEV_INSTANT_BROADCAST = "DEV_INSTANT_BROADCAST"
ST_DEV_GRANT_STARS = "DEV_GRANT_STARS"
ST_DEV_SUPPORT_REPLY = "DEV_SUPPORT_REPLY"
ST_DEV_STATS_PERIOD = "DEV_STATS_PERIOD"

# ==================================
# === СПИСОК КНОПОК ГЛАВНОГО МЕНЮ ===
# ==================================

ALL_MAIN_MENU_BUTTONS = [
    "📅 Сегодня",
    "📅 Завтра",
    "📅 Расписание",
    "📝 Домашнее задание",
    "🔔 Звонки",
    "👨‍🏫 Учителя",
    "🎉 Каникулы",
    "💬 Написать админу",
    "⏰ Таймер",
    "🕵️ Анонимное сообщение",
    "📨 Мои анонимные сообщения",
    "🎓 Управление классами",
    "🌟 Мои кнопки",
    "☁️ Облако",
    "🔐 Сейф",
    "⭐ Звезды",
    "🤖 DEVORKS+ai",
    "🪄 AI Agent",
    "🌦 Погода",
    "📚 Инструкция",
    "🔑 Код класса",
    "📋 Ещё",
    "🚪 Выйти из класса",
    "🔓 Выйти из аккаунта",
]

QUICK_COMMANDS = [
    "📅 Сегодня", "📅 Завтра", "📅 Расписание",
    "📝 Домашнее задание", "➕ Добавить ДЗ", "🗑️ Удалить ДЗ",
    "🔔 Звонки", "👨‍🏫 Учителя", "🎉 Каникулы",
    "💬 Написать админу", "📢 Написать классу",
    "⏰ Таймер", "🕵️ Анонимное сообщение", "📨 Мои анонимные сообщения",
    "🎓 Управление классами", "⚙️ Настройки", "🌟 Мои кнопки",
    "☁️ Облако", "🔐 Сейф",
    "⭐ Звезды", "🤖 DEVORKS+ai", "🪄 AI Agent",
    "🌦 Погода", "📚 Инструкция", "🔑 Код класса",
    "💬 Чат поддержки", "👨‍💼 Админская панель", "⬅️ Назад в меню",
]

# Кнопки, которые в ВК живут под «📋 Ещё» (для компактности клавиатуры,
# ВК показывает не более 10 рядов; остальное — с пагинацией)
MORE_MENU_BUTTONS = [
    "🎂 Дни рождения",
    "🕐 Дежурные",
    "🤒 Я болел(а)",
    "❗ Важное",
    "🌙 Не беспокоить",
    "📚 Решения",
    "📨 Мои анонимные сообщения",
    "💬 Написать админу",
    "🎉 Каникулы",
    "🔑 Код класса",
]

WEEKDAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
WEEKDAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_GEN = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]

# Максимальная длина одного сообщения ВК
VK_MSG_LIMIT = 4096

# Символы, запрещённые во вводе (защита от поломки JSON/файлов)
FORBIDDEN_CHARS = ["\x00", "\x0b", "\x0c", "\x1f", "\u2028", "\u2029", "\ufeff"]

# ==================================
# === СЛОЙ ДАННЫХ: JSON + MONGODB ===
# ==================================

_mongo_client = None
_mongo_db = None
_mongo_checked = False


def _init_mongo():
    """Ленивая инициализация MongoDB (не обязательна: нет — работаем на JSON)."""
    global _mongo_client, _mongo_db, _mongo_checked
    if _mongo_checked:
        return
    _mongo_checked = True
    if not (HAVE_MONGO and MONGO_URI):
        return
    try:
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _mongo_client.admin.command("ping")
        _mongo_db = _mongo_client[MONGO_DB]
        logger.info("MongoDB подключена (%s).", MONGO_DB)
    except Exception as e:
        logger.warning("MongoDB недоступна (%s) — работаю на JSON-файлах.", e)
        _mongo_client = None
        _mongo_db = None


# TTL-кэш чтения: файл не перечитывается с диска чаще, чем раз в N секунд
_DATA_CACHE: dict = {}
_DATA_CACHE_TTL = 3.0
_DATA_DIRTY: set = set()
_DATA_LOCK = asyncio.Lock()


def load_data(filename: str, default=None, expect: str = "dict"):
    """Читает данные из MongoDB → JSON-файл. С TTL-кэшем на чтение."""
    if filename in _DATA_DIRTY or filename not in _DATA_CACHE or (
            time.time() - _DATA_CACHE[filename][0] > _DATA_CACHE_TTL):
        data = None
        _init_mongo()
        if _mongo_db is not None:
            try:
                doc = _mongo_db.kv.find_one({"_id": os.path.basename(filename)})
                if doc and isinstance(doc.get("data"), (dict, list)):
                    data = doc["data"]
            except Exception as e:
                logger.debug("mongo load %s: %s", filename, e)
        if data is None:
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                data = None
            except (json.JSONDecodeError, OSError):
                # битый JSON → пробуем .bak
                try:
                    with open(filename + ".bak", "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = None
        if data is None:
            data = {} if expect == "dict" else []
        if expect == "dict" and not isinstance(data, dict):
            data = {}
        if expect == "list" and not isinstance(data, list):
            data = []
        _DATA_CACHE[filename] = (time.time(), data)
        _DATA_DIRTY.discard(filename)
    return _DATA_CACHE[filename][1]


def save_data(filename: str, data) -> None:
    """Атомарно сохраняет данные в JSON (tmp → bak → main) и в MongoDB."""
    _DATA_CACHE[filename] = (time.time(), data)
    _DATA_DIRTY.discard(filename)
    try:
        tmp = filename + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        try:
            if os.path.exists(filename):
                shutil.copy2(filename, filename + ".bak")
        except OSError:
            pass
        os.replace(tmp, filename)
    except OSError as e:
        logger.error("save_data(%s): %s", filename, e)
    _init_mongo()
    if _mongo_db is not None:
        try:
            _mongo_db.kv.update_one(
                {"_id": os.path.basename(filename)},
                {"$set": {"data": data, "ts": datetime.now(timezone.utc)}},
                upsert=True,
            )
        except Exception as e:
            logger.debug("mongo save %s: %s", filename, e)


async def save_data_async(filename: str, data) -> None:
    """save_data без блокировки цикла — запись уходит в поток."""
    await asyncio.to_thread(save_data, filename, data)


def clean_forbidden(s: str) -> str:
    """Чистит строку от запрещённых символов."""
    if not isinstance(s, str):
        return s
    for ch in FORBIDDEN_CHARS:
        s = s.replace(ch, "")
    return s


def has_forbidden_chars(s: str):
    """Возвращает символ, если найден запрещённый, иначе None."""
    if not isinstance(s, str):
        return None
    for ch in FORBIDDEN_CHARS:
        if ch in s:
            return ch
    return None


def reject_if_forbidden(text: str, user=None):
    """Проверка ввода: если есть запрещённые символы — вернуть текст отказа."""
    bad = has_forbidden_chars(text or "")
    if bad:
        return ("⚠️ В тексте найден недопустимый символ. "
                "Уберите его и отправьте ещё раз.")
    if len(text or "") > 3500:
        return "⚠️ Слишком длинный текст (максимум 3500 символов). Сократите его."
    return None


# ==================================
# === МОДЕЛИ: ПОЛЬЗОВАТЕЛЬ / КЛАСС ===
# ==================================

DEFAULT_USER = {
    "user_id": "", "username": "", "first_name": "User",
    "timezone": 3, "tz_id": None, "local_time_set": False,
    "class_code": None, "role": "student",
    "custom_buttons": {},            # карта переименований {старое: новое}
    "button_layout": "default",      # default(3/ряд) | compact(4/ряд) | wide(2/ряд)
    "custom_button_order": [],
    "notifications": True,
    "morning_notification_time": "08:00",
    "evening_notification_time": "22:00",
    "morning_text": "Доброе утро! Хорошего дня! 📚",
    "evening_text": "Спокойной ночи! Хороших снов! 💤",
    "joined_date": "", "created_classes": [],
    "setup_completed": False,
    "state": ST_MAIN,                # FSM-состояние (переживает рестарт)
    "fsm": {},                       # данные FSM (черновики, id и т.п.)
    "state_ts": 0,                   # когда последний раз менялось состояние
    "birthday": None,
    "show_birthday_countdown": True,
    "show_birthday_to_class": True,
    "birthday_personal_notification": True,
    "birthday_notification_time": "09:00",
    "is_blocked": False,
    "block_until": None, "block_reason": "",
    "max_personal_buttons": 1,
    "personal_button_order": [],
    "stars_balance": 0,
    "total_stars_spent": 0,
    "custom_buttons_created": 0,
    "prepaid_buttons": 0,
    "user_code": None,
    "instructions_read": False,
    "language": "ru",
    "disclaimer_accepted": False,
    "pdn_accepted": False, "pdn_consent_at": None,
    "pdn_policy_version": None, "parent_consent": False,
    "hidden_buttons": [],
    "city": None,
    "weather_notifications": True,
    "weather_notification_time": "07:00",
    "holiday_notifications": True,
    "subscription_confirmed_at": None,
    "sub_left_notified_at": None,
    "dev_notified": False,
    "referrer_id": None,
    "referrals_count": 0,
    "referral_bonus_paid": False,
    "hide_spend_stats": False,
    "button_theme": "default",
    "colored_buttons_enabled": True,
    "support_chat_active": False,
    "ai_persona": "normal",
    "ai_generation_count": 0,
    "dnd_until": None,
    "sick_from": None, "sick_to": None,
    "web_password": None,
    "subscriptions": {},
    "miniapp_settings": {},
    "miniapp_lists": {},
    "duty_pending": None, "ct_pending": None,
    "bells_pending": None, "cls_pending": None,
    "bg_pending": [],
    "last_activity": "",
    "spend_map": {},
}


def get_user(user_id) -> dict:
    """Загружает пользователя (создавая при первом обращении)."""
    uid = str(user_id)
    users = load_data(USERS_FILE, {}, expect="dict")
    u = users.get(uid)
    if u is None:
        u = dict(DEFAULT_USER)
        u["user_id"] = uid
        u["joined_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        u["state_ts"] = time.time()
        users[uid] = u
        save_data(USERS_FILE, users)
    else:
        changed = False
        for k, v in DEFAULT_USER.items():
            if k not in u:
                u[k] = v
                changed = True
        if not isinstance(u.get("fsm"), dict):
            u["fsm"] = {}
            changed = True
        if changed:
            users[uid] = u
            save_data(USERS_FILE, users)
    return u


def save_user(user: dict) -> None:
    """Сохраняет объект пользователя в users.json."""
    users = load_data(USERS_FILE, {}, expect="dict")
    users[str(user.get("user_id"))] = user
    save_data(USERS_FILE, users)


def all_users() -> dict:
    return load_data(USERS_FILE, {}, expect="dict")


def set_state(user: dict, state: str, fsm: dict = None) -> None:
    """Смена FSM-состояния (персистентно)."""
    user["state"] = state
    user["state_ts"] = time.time()
    user["fsm"] = fsm if fsm is not None else {}
    save_user(user)


def clear_state(user: dict) -> None:
    set_state(user, ST_MAIN, {})


DEFAULT_CLASS = {
    "class_code": "", "class_name": "", "creator_id": "",
    "members": [], "admins": [], "created_date": "",
    "schedule": {"Понедельник": "", "Вторник": "", "Среда": "",
                 "Четверг": "", "Пятница": "", "Суббота": "", "Воскресенье": ""},
    "teachers": {},                 # {предмет: ФИО}
    "bells": [],                    # [{lesson, start, end}]
    "bells_by_day": {},             # {пн: [{...}]}
    "bells_by_date": {},            # {"ДД.ММ.ГГГГ": [{...}]}
    "holidays": None,               # "ГГГГ-ММ-ДД" — дата начала каникул
    "homework": {},                 # {"ГГГГ-ММ-ДД": {предмет: {text, attachments, ts, by}}}
    "custom_buttons": [],           # кнопки класса [{name, content, type, by}]
    "important": None,              # ❗ Важное (объявление)
}


def generate_class_code() -> str:
    """Уникальный код класса вида ABC-123."""
    codes = load_data(CLASSES_FILE, {}, expect="dict")
    while True:
        code = ("".join(random.choices(string.ascii_uppercase, k=3)) + "-" +
                "".join(random.choices(string.digits, k=3)))
        if code not in codes:
            return code


def get_class_by_code(code: str):
    if not code:
        return None
    classes = load_data(CLASSES_FILE, {}, expect="dict")
    return classes.get(code)


def get_class_by_user(user_id):
    u = get_user(user_id)
    return get_class_by_code(u.get("class_code"))


def save_class(cls: dict) -> None:
    classes = load_data(CLASSES_FILE, {}, expect="dict")
    classes[cls["class_code"]] = cls
    save_data(CLASSES_FILE, classes)


def is_class_admin(user_id, cls=None) -> bool:
    if str(user_id) == str(DEVELOPER_ID):
        return True
    if cls is None:
        cls = get_class_by_user(user_id)
    if not cls:
        return False
    return str(user_id) in [str(x) for x in cls.get("admins", [])]


def generate_user_code(user_id) -> str:
    """Персональный код пользователя (для жалоб/поддержки)."""
    u = get_user(user_id)
    if u.get("user_code"):
        return u["user_code"]
    codes = load_data(USER_CODES_FILE, {}, expect="dict")
    code = str(1000 + len(codes)) + "-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    codes[code] = str(user_id)
    u["user_code"] = code
    save_user(u)
    save_data(USER_CODES_FILE, codes)
    return code


def load_prices() -> dict:
    data = load_data(PRICES_FILE, {}, expect="dict")
    out = dict(DEFAULT_PRICES)
    out.update({k: v for k, v in data.items() if isinstance(v, (int, float))})
    return out


def get_price(key: str) -> int:
    prices = load_prices()
    v = prices.get(key, 0)
    return int(v) if v is not None else 0


def load_referrals() -> dict:
    return load_data(REFERRALS_FILE, {}, expect="dict")


def credit_referrer_for(user_id) -> None:
    """Начисляет пригласителю REFERRAL_REWARD_STARS (один раз на юзера)."""
    u = get_user(user_id)
    rid = u.get("referrer_id")
    if not rid or u.get("referral_bonus_paid"):
        return
    ref = get_user(rid)
    if not ref or str(rid) == str(user_id):
        return
    ref["stars_balance"] = int(ref.get("stars_balance", 0)) + REFERRAL_REWARD_STARS
    ref["referrals_count"] = int(ref.get("referrals_count", 0)) + 1
    save_user(ref)
    u["referral_bonus_paid"] = True
    save_user(u)
    referrals = load_referrals()
    referrals.setdefault(str(rid), []).append(str(user_id))
    save_data(REFERRALS_FILE, referrals)


def log_admin_action(user_id, action: str, details: str = "") -> None:
    log = load_data(ADMIN_LOG_FILE, [], expect="list")
    log.append({
        "admin": str(user_id), "action": action, "details": details,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    if len(log) > 2000:
        log = log[-2000:]
    save_data(ADMIN_LOG_FILE, log)


def load_holidays() -> dict:
    """{"ДД-ММ": "Название"} — ежегодные праздники."""
    return load_data(HOLIDAYS_FILE, {}, expect="dict")


def load_instructions() -> str:
    data = load_data(INSTRUCTIONS_FILE, {}, expect="dict")
    return data.get("text", "")


def load_dev_settings() -> dict:
    return load_data(DEV_SETTINGS_FILE, {}, expect="dict")


def save_dev_settings(data: dict) -> None:
    save_data(DEV_SETTINGS_FILE, data)


def is_dev_sub_gate_enabled() -> bool:
    return load_dev_settings().get("sub_gate_enabled", True)


def is_dev(user_id) -> bool:
    return bool(DEVELOPER_ID) and str(user_id) == str(DEVELOPER_ID)

# ==================================
# === КЛИЕНТ VK API ===
# ==================================

VK_API_URL = "https://api.vk.com/method/"


class VKApiError(Exception):
    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"VK API error {code}: {msg}")


class VKClient:
    """Асинхронный клиент VK API для сообщества.

    • Токен-бакет: не чаще 18 запросов/сек (лимит ВК — 20).
    • Повтор при ошибках 6 (flood) и 9 (rate) с экспоненциальной паузой.
    • messages.send автоматически дробит длинные сообщения (лимит 4096).
    """

    def __init__(self, token: str, group_id: int):
        self.token = token
        self.group_id = group_id
        self.http: aiohttp.ClientSession = None
        self._min_interval = 0.056
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._peer_last_send: dict = {}
        self._users_cache: dict = {}      # {user_id: {"name":..., "ts":...}}
        self._random_ids = set()

    async def start(self):
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60))

    async def close(self):
        if self.http and not self.http.closed:
            await self.http.close()

    async def _throttle(self):
        """Глобальный троттлинг запросов (18/сек)."""
        async with self._lock:
            now = time.monotonic()
            wait = self._last_call + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def call(self, method: str, **params):
        """Вызов метода VK API с ретраями на флуд."""
        if not self.token:
            raise VKApiError(-1, "VK_TOKEN не задан")
        await self.start()
        for attempt in range(6):
            await self._throttle()
            form = {"access_token": self.token, "v": VK_API_VERSION}
            form.update({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
                         for k, v in params.items() if v is not None})
            try:
                async with self.http.post(VK_API_URL + method, data=form) as resp:
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
                if attempt == 5:
                    raise VKApiError(-2, f"network: {e}")
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
            if "error" in data:
                err = data["error"]
                code = int(err.get("error_code", 0))
                msg = err.get("error_msg", "")
                if code in (6, 9) and attempt < 5:      # too fast / flood
                    await asyncio.sleep(0.35 * (attempt + 1))
                    continue
                if code == 10 and attempt < 3:           # internal server error
                    await asyncio.sleep(0.5)
                    continue
                raise VKApiError(code, msg)
            return data.get("response", data)
        raise VKApiError(-3, "превышено число попыток")

    # ---------- УТИЛИТЫ ----------

    def _random_id(self) -> int:
        while True:
            r = random.randint(-2**31, 2**31 - 1)
            if r not in self._random_ids:
                if len(self._random_ids) > 100000:
                    self._random_ids.clear()
                self._random_ids.add(r)
                return r

    async def get_user_info(self, user_id) -> dict:
        """Имя пользователя (с кэшем на 12 ч)."""
        uid = str(user_id)
        cached = self._users_cache.get(uid)
        if cached and time.time() - cached["ts"] < 43200:
            return cached["info"]
        try:
            resp = await self.call("users.get", user_ids=uid, fields="first_name,last_name,photo_100")
            info = resp[0] if isinstance(resp, list) and resp else {}
        except Exception:
            info = {}
        if not info:
            info = {"first_name": "", "last_name": ""}
        self._users_cache[uid] = {"info": info, "ts": time.time()}
        return info

    async def user_name(self, user_id) -> str:
        info = await self.get_user_info(user_id)
        name = (str(info.get("first_name", "")) + " " + str(info.get("last_name", ""))).strip()
        return name or f"vk{id}"

    async def is_member(self, user_id) -> bool:
        """Подписан ли пользователь на сообщество бота (гейт подписки)."""
        try:
            resp = await self.call("groups.isMember", group_id=self.group_id, user_id=user_id)
            return int(resp) == 1
        except Exception as e:
            logger.debug("isMember %s: %s", user_id, e)
            return False

    # ---------- ОТПРАВКА ----------

    async def send(self, peer_id: int, text: str = "", keyboard=None,
                   attachment=None, reply_to=None) -> int:
        """Отправить сообщение. Возвращает conversation_message_id.
        Длинный текст автоматически дробится по абзацам."""
        await self.start()
        if not text and not attachment and keyboard is None:
            text = "…"
        parts = split_message(text or "", VK_MSG_LIMIT)
        cmid = 0
        for i, part in enumerate(parts):
            # Троттлинг на пользователя: не чаще ~3 сообщ/сек
            while True:
                last = self._peer_last_send.get(peer_id, 0)
                wait = last + 0.35 - time.monotonic()
                if wait <= 0:
                    break
                await asyncio.sleep(wait)
            self._peer_last_send[peer_id] = time.monotonic()
            kb = keyboard if (i == len(parts) - 1) else None
            att = attachment if (i == len(parts) - 1) else None
            params = {
                "peer_id": peer_id,
                "message": part or "",
                "random_id": self._random_id(),
            }
            if kb is not None:
                params["keyboard"] = kb if isinstance(kb, str) else json.dumps(kb, ensure_ascii=False)
            if att:
                params["attachment"] = att
            if reply_to and i == 0:
                params["reply_to"] = reply_to
            try:
                resp = await self.call("messages.send", **params)
                cmid = int(resp.get("conversation_message_id", 0)) if isinstance(resp, dict) else int(resp or 0)
            except VKApiError as e:
                if e.code in (901, 902, 903, 15):
                    # пользователь запретил сообщения / заблокировал бота
                    _on_send_denied(peer_id, e.code)
                    return 0
                if e.code == 9 and "internal" not in e.msg:
                    await asyncio.sleep(1.2)
                    try:
                        resp = await self.call("messages.send", **params)
                        cmid = int(resp.get("conversation_message_id", 0)) if isinstance(resp, dict) else int(resp or 0)
                    except VKApiError as e2:
                        if e2.code in (901, 902, 903, 15):
                            _on_send_denied(peer_id, e2.code)
                        else:
                            logger.error("send(%s): %s", peer_id, e2)
                        return 0
                else:
                    logger.error("send(%s): %s", peer_id, e)
                    return 0
            except Exception as e:
                logger.error("send(%s): %s", peer_id, e)
                return 0
        return cmid

    async def edit(self, peer_id: int, cmid: int, text: str = "",
                   keyboard=None, attachment=None) -> bool:
        """Редактирование сообщения бота (меню и т.п.)."""
        try:
            params = {
                "peer_id": peer_id,
                "conversation_message_id": cmid,
                "message": text[:VK_MSG_LIMIT] or "…",
            }
            if keyboard is not None:
                params["keyboard"] = keyboard if isinstance(keyboard, str) else json.dumps(keyboard, ensure_ascii=False)
            if attachment:
                params["attachment"] = attachment
            await self.call("messages.edit", **params)
            return True
        except VKApiError as e:
            logger.debug("edit(%s,%s): %s", peer_id, cmid, e)
            return False
        except Exception as e:
            logger.debug("edit: %s", e)
            return False

    async def answer_event(self, event_id: str, user_id: int, peer_id: int) -> None:
        """Обязательно закрываем «крутящийся» кружок callback-кнопки."""
        try:
            await self.call("messages.sendMessageEventAnswer",
                            event_id=event_id, user_id=user_id, peer_id=peer_id)
        except Exception as e:
            logger.debug("answer_event: %s", e)

    # ---------- ЗАГРУЗКА ФАЙЛОВ ----------

    async def upload_photo(self, peer_id: int, data: bytes) -> str:
        """Загрузка фото → attachment-строка photo{owner}_{id}."""
        await self.start()
        resp = await self.call("photos.getMessagesUploadServer", peer_id=peer_id)
        url = resp.get("upload_url")
        form = aiohttp.FormData()
        form.add_field("photo", data, filename="photo.jpg",
                       content_type="image/jpeg")
        async with self.http.post(url, data=form) as r:
            up = await r.json(content_type=None)
        saved = await self.call("photos.saveMessagesPhoto",
                                server=up.get("server"), photo=up.get("photo"),
                                hash=up.get("hash"))
        if isinstance(saved, list) and saved:
            return f"photo{saved[0]['owner_id']}_{saved[0]['id']}"
        raise VKApiError(-4, "photos.saveMessagesPhoto: пустой ответ")

    async def upload_doc(self, peer_id: int, data: bytes, title: str,
                         mime: str = "") -> str:
        """Загрузка документа → doc{owner}_{id}."""
        await self.start()
        resp = await self.call("docs.getMessagesUploadServer",
                               peer_id=peer_id, type="doc")
        url = resp.get("upload_url")
        form = aiohttp.FormData()
        ctype = mime or (mimetypes.guess_type(title)[0] or "application/octet-stream")
        form.add_field("file", data, filename=title or "file", content_type=ctype)
        async with self.http.post(url, data=form) as r:
            up = await r.json(content_type=None)
        file_url = up.get("file")
        if not file_url:
            raise VKApiError(-4, "upload_doc: сервер не вернул file")
        saved = await self.call("docs.save", file=file_url, title=title or "file")
        doc = None
        if isinstance(saved, list) and saved:
            doc = saved[0]
        elif isinstance(saved, dict):
            doc = saved.get("doc") or saved.get("audio_message") or saved
        if not isinstance(doc, dict) or "id" not in doc:
            raise VKApiError(-4, "docs.save: пустой ответ")
        return f"doc{doc['owner_id']}_{doc['id']}"

    async def send_file(self, peer_id: int, data: bytes, title: str,
                        mime: str = "") -> bool:
        """Отправить пользователю файл байтами (документом)."""
        try:
            if (mime or "").startswith("image/") and len(data) < 10 * 1024 * 1024 and (
                    mime in ("image/jpeg", "image/png") or title.lower().endswith((".jpg", ".jpeg", ".png"))):
                att = await self.upload_photo(peer_id, data)
            else:
                att = await self.upload_doc(peer_id, data, title, mime)
            return await self.send(peer_id, f"📎 {os.path.basename(title)}", attachment=att) != 0
        except Exception as e:
            logger.error("send_file(%s): %s", peer_id, e)
            await self.send(peer_id, f"⚠️ Не удалось отправить файл «{title}».")
            return False

    # ---------- СКАЧИВАНИЕ ВЛОЖЕНИЙ ----------

    async def download_attachment(self, att: dict):
        """Скачивает вложение из сообщения. Возвращает (bytes, filename, kind)
        kind: photo|doc|audio|video|None."""
        try:
            await self.start()
            t = att.get("type")
            if t == "photo":
                photo = att["photo"]
                best = None
                for s in photo.get("sizes", []):
                    if best is None or (s.get("width", 0) * s.get("height", 0)) > (
                            best.get("width", 0) * best.get("height", 0)):
                        best = s
                if not best or not best.get("url"):
                    return None, None, None
                async with self.http.get(best["url"]) as r:
                    return await r.read(), "photo.jpg", "photo"
            if t == "doc":
                doc = dict(att.get("doc") or {})
                url = doc.get("url")
                is_voice = bool((doc.get("preview") or {}).get("audio_msg"))
                if not url:
                    try:
                        resp = await self.call("docs.getById",
                                               docs=f"{doc.get('owner_id')}_{doc.get('id')}")
                        items = resp.get("items") if isinstance(resp, dict) else resp
                        if items:
                            doc = items[0]
                            url = doc.get("url")
                    except Exception:
                        pass
                if not url:
                    return None, None, None
                async with self.http.get(url) as r:
                    data = await r.read()
                name = doc.get("title") or ("voice.ogg" if is_voice else "file.bin")
                return data, name, ("audio" if is_voice else "doc")
            if t in ("video", "audio", "link", "wall", "story", "poll"):
                return None, None, None
            return None, None, None
        except Exception as e:
            logger.debug("download_attachment: %s", e)
            return None, None, None


def split_message(text: str, limit: int = VK_MSG_LIMIT):
    """Дробит длинный текст на части ≤ limit, по абзацам/строкам."""
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text[:limit]
        nl = cut.rfind("\n")
        sp = cut.rfind(" ")
        pos = nl if nl > limit * 0.5 else (sp if sp > limit * 0.5 else limit)
        parts.append(text[:pos].rstrip())
        text = text[pos:].lstrip("\n")
    return [p for p in parts if p] or ["…"]


def _on_send_denied(peer_id: int, code: int):
    """Пользователь запретил сообщения — отключаем ему уведомления."""
    try:
        users = load_data(USERS_FILE, {}, expect="dict")
        u = users.get(str(peer_id))
        if u is not None and u.get("notifications"):
            u["notifications"] = False
            u["notifications_denied"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            save_data(USERS_FILE, users)
            logger.info("Пользователь %s запретил сообщения (code %s) — "
                        "уведомления выключены.", peer_id, code)
    except Exception as e:
        logger.debug("_on_send_denied: %s", e)


vk: VKClient = None  # инициализируется в main()


# ==================================
# === КЛАВИАТУРЫ VK ===
# ==================================

# Цвета: primary (синяя), secondary (белая), negative (красная), positive (зелёная)

def _btn(label: str, payload: dict, color: str = "secondary",
         kind: str = "callback"):
    return {
        "action": {
            "type": kind,
            "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            "label": label[:100],
        },
        "color": color,
    }


def kb_inline(rows, colored: bool = True) -> str:
    """Инлайн-клавиатура. rows — список рядов [(label, payload, color), ...].
    Каждый элемент ряда — кортеж или кнопка-словарь от _btn."""
    buttons = []
    for row in rows:
        if not row:
            continue
        vk_row = []
        for item in row:
            if isinstance(item, dict):
                vk_row.append(item)
            else:
                label, payload = item[0], item[1]
                color = item[2] if len(item) > 2 else ("primary" if colored else "secondary")
                vk_row.append(_btn(label, payload, color))
        buttons.append(vk_row)
    return json.dumps({"inline": True, "buttons": buttons}, ensure_ascii=False)


def kb_text(buttons_rows, colored: bool = True) -> str:
    """Обычная клавиатура (снизу экрана). Кнопки-тексты с payload."""
    rows = []
    for row in buttons_rows:
        if not row:
            continue
        vk_row = []
        for item in row:
            if isinstance(item, dict):
                vk_row.append(item)
            else:
                label, payload = item[0], item[1]
                color = item[2] if len(item) > 2 else ("primary" if colored else "secondary")
                vk_row.append(_btn(label, payload, color, kind="text"))
        rows.append(vk_row)
    return json.dumps({"inline": False, "buttons": rows}, ensure_ascii=False)


def _menu_payload(name: str) -> dict:
    return {"c": "menu", "b": name}


def layout_rows(items, per_row: int):
    """Раскладывает список на ряды по per_row штук."""
    rows = []
    for i in range(0, len(items), per_row):
        rows.append(items[i:i + per_row])
    return rows

# ==================================
# === ВРЕМЯ / ЧАСОВЫЕ ПОЯСА ===
# ==================================

_TIME_DRIFT = {"offset": 0.0, "ts": 0.0}


def _refresh_time_drift():
    """Коррекция часов сервера по внешнему time-api (в отдельном потоке)."""
    global _TIME_DRIFT
    for url in ("https://timeapi.io/api/Time/current/zone?zone=UTC",
                "https://worldtimeapi.org/api/timezone/Etc/UTC"):
        try:
            r = requests.get(url, timeout=8)
            if r.status_code != 200:
                continue
            data = r.json()
            iso = data.get("dateTime") or data.get("utc_datetime") or ""
            iso = iso.replace("T", " ").split(".")[0].split("+")[0]
            remote = datetime.strptime(iso[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc).timestamp()
            _TIME_DRIFT = {"offset": remote - time.time(), "ts": time.time()}
            return
        except Exception:
            continue


def now() -> datetime:
    """Локальное время сервера с коррекцией дрейфа."""
    return datetime.now() + timedelta(seconds=_TIME_DRIFT["offset"])


def user_tz(user: dict):
    """Часовой пояс пользователя: IANA или фиксированное смещение."""
    tz_id = user.get("tz_id")
    if tz_id and ZoneInfo:
        try:
            return ZoneInfo(tz_id)
        except Exception:
            pass
    try:
        return timezone(timedelta(hours=int(user.get("timezone", 3))))
    except Exception:
        return timezone(timedelta(hours=3))


def user_now(user: dict) -> datetime:
    """Текущее локальное время пользователя."""
    return datetime.now(timezone.utc).astimezone(user_tz(user)).replace(tzinfo=None)


def parse_date_or_none(s: str):
    """Парсит дату из нескольких форматов → date | None."""
    s = (s or "").strip().lower()
    if not s:
        return None
    ru_months = {
        "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "мае": 5, "май": 5,
        "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
    }
    today = now().date()
    if s in ("сегодня", "today"):
        return today
    if s in ("завтра", "tomorrow"):
        return today + timedelta(days=1)
    if s in ("послезавтра",):
        return today + timedelta(days=2)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m", "%d-%m", "%d %m", "%d.%m.%y"):
        try:
            d = datetime.strptime(s, fmt).date()
            if fmt in ("%d.%m", "%d-%m", "%d %m"):
                d = d.replace(year=today.year)
                if d < today - timedelta(days=330):
                    d = d.replace(year=today.year + 1)
            return d
        except ValueError:
            continue
    # «14 марта», «14 марта 2009»
    parts = s.replace(",", " ").split()
    if len(parts) >= 2 and parts[0].isdigit():
        day = int(parts[0])
        month = None
        for k, v in ru_months.items():
            if parts[1].startswith(k):
                month = v
                break
        if month and 1 <= day <= 31:
            year = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else today.year
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None


def fmt_date(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month]} {d.year}"

def fmt_date_short(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def parse_hhmm(s: str):
    """'ЧЧ:ММ' → (h, m) | None."""
    s = (s or "").strip().replace(".", ":").replace("-", ":")
    if ":" not in s:
        return None
    try:
        h, m = s.split(":")[:2]
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, IndexError):
        return None
    return None


def weekday_ru(d: date) -> str:
    return WEEKDAY_NAMES[d.weekday()]

def weekday_short(d: date) -> str:
    return WEEKDAY_SHORT[d.weekday()]


# ==================================
# === АНТИСПАМ ===
# ==================================

_rate_map: dict = {}


def is_user_spamming(user_id) -> bool:
    """Не чаще 1 сообщения в 1.2 сек (окно 10 сек, максимум 8 подряд)."""
    uid = str(user_id)
    nowt = time.time()
    stamps = [t for t in _rate_map.get(uid, []) if nowt - t < 10]
    stamps.append(nowt)
    _rate_map[uid] = stamps
    if len(_rate_map) > 5000:
        for k in list(_rate_map.keys())[:2500]:
            _rate_map.pop(k, None)
    return len(stamps) > 8


# ==================================
# === БЛОКИРОВКИ ===
# ==================================

def get_user_block(user_id):
    """(blocked: bool, reason: str) — учитывая срок блокировки."""
    u = get_user(user_id)
    until = u.get("block_until")
    if until and isinstance(until, str):
        try:
            if datetime.strptime(until, "%Y-%m-%d %H:%M") <= now():
                u["is_blocked"] = False
                u["block_until"] = None
                u["block_reason"] = ""
                save_user(u)
                return False, ""
        except ValueError:
            pass
    if u.get("is_blocked"):
        return True, u.get("block_reason", "") or "причина не указана"
    return False, ""


# ==================================
# === AI: GROQ (чат / вижн / голос) ===
# ==================================

groq_client = None
if HAVE_GROQ and GROQ_API_KEY:
    groq_client = AsyncGroq(api_key=GROQ_API_KEY)

user_conversations: dict = {}   # {uid: [{"role","content"}, ...]}


def _ai_hist_load(uid: str):
    """История AI-диалога из БД (переживает рестарт)."""
    if uid in user_conversations:
        return
    try:
        data = load_data(AI_HIST_FILE, {}, expect="dict")
        hist = data.get(uid) or []
        user_conversations[uid] = hist[-30:] if isinstance(hist, list) else []
    except Exception:
        user_conversations[uid] = []


def _ai_hist_save(uid: str):
    """Сохраняет историю AI-диалога в БД."""
    try:
        hist = user_conversations.get(uid)
        if not isinstance(hist, list) or not hist:
            return
        data = load_data(AI_HIST_FILE, {}, expect="dict")
        data[uid] = hist[-30:]
        save_data(AI_HIST_FILE, data)
    except Exception as e:
        logger.debug("ai hist save: %s", e)


def _ai_chat_system_prompt():
    """Системный промпт чата DEVORKS+ai (базовый, режим «normal»)."""
    return (
        "Ты — DEVORKS+ai, ИИ-помощник школьного ВК-бота DEVORKS+. "
        "Отвечай только по-русски. Главное — честность: только проверенные "
        "факты, ничего не выдумывай, не знаешь — так и скажи. Тему меняй "
        "мгновенно, отвечай точно по последнему сообщению. Понимай синонимы, "
        "сленг и опечатки. Пиши кратко, без воды, не длиннее 1000 символов, "
        "всегда законченными предложениями."
    )[:500]


def _ai_chat_full_prompt(persona):
    """Полный системный промпт с учётом режима личности."""
    base = _ai_chat_system_prompt()
    persona = persona if persona in AI_PERSONA_MODES else "normal"
    if persona == "normal":
        return base
    title = AI_PERSONA_MODES[persona]["title"]
    persona_text = AI_PERSONA_PROMPTS.get(persona, "")
    if not persona_text:
        return base
    return (f"{base}\n\nРЕЖИМ «{title}» — ВЫСШИЙ ПРИОРИТЕТ, СТРОГО СЛЕДУЙ: "
            f"{persona_text}")


def _ai_finish_guarantee(text: str, finish_reason=None, max_chars=1000) -> str:
    """Гарантия ЗАКОНЧЕННОГО ответа ИИ (без обрывов на полуслове)."""
    if not text:
        return text
    text = text.strip()
    truncated = (finish_reason == "length") or (len(text) > max_chars)
    if truncated:
        cut = text[:max_chars]
        last_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "),
                       cut.rfind(".\n"), cut.rfind("!\n"), cut.rfind("?\n"))
        if last_end > max_chars * 0.5:
            return cut[:last_end + 1].strip()
        sp = cut.rfind(" ")
        return (cut[:sp] if sp > max_chars * 0.5 else cut).rstrip()
    return text


async def ai_chat_reply(user: dict, text: str, image_b64: str = None) -> str:
    """Один запрос к Groq: текстовый чат или вижн (если есть фото)."""
    if groq_client is None:
        return ("⚠️ DEVORKS+ai временно недоступен: не задан GROQ_API_KEY "
                "или не установлена библиотека groq.")
    uid = str(user["user_id"])
    _ai_hist_load(uid)
    hist = user_conversations.setdefault(uid, [])
    system = _ai_chat_full_prompt(user.get("ai_persona", "normal"))
    try:
        if image_b64:
            content = [
                {"type": "text", "text": text or "Что на этом изображении?"},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{image_b64}"}},
            ]
            for model in [GROQ_VISION_MODEL] + GROQ_VISION_FALLBACK_MODELS:
                try:
                    resp = await groq_client.chat.completions.create(
                        model=model,
                        messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": content}],
                        max_tokens=700,
                    )
                    ans = (resp.choices[0].message.content or "").strip()
                    return _ai_finish_guarantee(ans, resp.choices[0].finish_reason)
                except Exception as e:
                    logger.debug("vision %s: %s", model, e)
                    continue
            return "😔 Вижн-модель временно недоступна, попробуйте позже."
        # Обычный текстовый чат с контекстом
        messages = [{"role": "system", "content": system}]
        for m in hist[-20:]:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                messages.append({"role": m["role"], "content": m["content"][:2000]})
        messages.append({"role": "user", "content": text[:3000]})
        resp = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=450,
        )
        ans = (resp.choices[0].message.content or "").strip()
        ans = _ai_finish_guarantee(ans, resp.choices[0].finish_reason)
        hist.append({"role": "user", "content": text[:1500]})
        hist.append({"role": "assistant", "content": ans})
        user_conversations[uid] = hist[-30:]
        _ai_hist_save(uid)
        return ans or "🤖 …"
    except Exception as e:
        logger.error("ai_chat_reply: %s", e)
        return "😔 AI временно перегружен. Попробуйте ещё раз через минуту."


async def ai_transcribe_voice(data: bytes, filename: str = "voice.ogg") -> str:
    """Расшифровка голосового сообщения через Groq Whisper."""
    if groq_client is None:
        return ""
    for model in [GROQ_STT_MODEL, "whisper-large-v3"]:
        try:
            resp = await groq_client.audio.transcriptions.create(
                model=model,
                file=(filename, data, "audio/ogg"),
            )
            return (resp.text or "").strip()
        except Exception as e:
            logger.debug("stt %s: %s", model, e)
            continue
    return ""


async def deepseek_json(system_prompt: str, user_prompt: str) -> str:
    """Запрос к DeepSeek → сырой текст (ожидааем JSON)."""
    if not DEEPSEEK_API_KEY:
        return ""
    url = f"{DEEPSEEK_API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
               "Content-Type": "application/json"}
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1400,
        "response_format": {"type": "json_object"},
    }
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as http:
                async with http.post(url, headers=headers, json=body) as r:
                    data = await r.json(content_type=None)
            return ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or ""
        except Exception as e:
            logger.debug("deepseek attempt %s: %s", attempt, e)
            await asyncio.sleep(1)
    return ""


def extract_json_object(text: str):
    """Достаёт первый {...} JSON-объект из ответа модели."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None

# ==================================
# === ПОГОДА (WeatherAPI) ===
# ==================================

_weather_cache: dict = {}


async def weather_api(city: str, kind: str = "current"):
    """Запрос WeatherAPI (current.json / forecast.json). Кэш 10 минут."""
    if not WEATHER_API_KEY:
        return None
    key = f"{kind}:{city.lower()}"
    cached = _weather_cache.get(key)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    url = (f"https://api.weatherapi.com/v1/{kind}.json"
           f"?key={WEATHER_API_KEY}&q={city}&lang=ru"
           + ("&days=3" if kind == "forecast" else ""))
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as http:
                async with http.get(url) as r:
                    if r.status != 200:
                        return None
                    data = await r.json(content_type=None)
            _weather_cache[key] = (time.time(), data)
            if len(_weather_cache) > 500:
                for k in list(_weather_cache.keys())[:250]:
                    _weather_cache.pop(k, None)
            return data
        except Exception as e:
            logger.debug("weather: %s", e)
            await asyncio.sleep(1)
    return None


WEATHER_CODES = {
    1000: "☀️ Ясно", 1003: "🌤 Переменная облачность", 1006: "☁️ Облачно",
    1009: "☁️ Пасмурно", 1030: "🌫 Туман", 1063: "🌦 Возможен дождь",
    1066: "🌨 Возможен снег", 1087: "⛈ Возможна гроза",
    1114: "🌨 Снегопад", 1180: "🌦 Слабый дождь", 1183: "🌦 Дождь",
    1186: "🌧 Дождь", 1189: "🌧 Дождь", 1192: "🌧 Сильный дождь",
    1195: "🌧 Сильный дождь", 1204: "🌨 Дождь со снегом", 1207: "🌨 Снег",
    1210: "🌨 Небольшой снег", 1213: "🌨 Небольшой снег", 1216: "🌨 Снег",
    1219: "🌨 Снег", 1222: "🌨 Сильный снег", 1225: "❄️ Сильный снег",
    1237: "🌨 Ледяная крупа", 1240: "🌦 Ливень", 1243: "🌧 Сильный ливень",
    1246: "⛈ Очень сильный ливень", 1249: "🌨 Ледяной дождь",
    1252: "🌨 Ледяной дождь", 1255: "🌨 Сильный ледяной дождь",
    1273: "⛈ Гроза с дождём", 1276: "⛈ Сильная гроза",
    1279: "⛈ Гроза со снегом", 1282: "⛈ Сильная гроза со снегом",
}


def weather_condition_text(code) -> str:
    return WEATHER_CODES.get(int(code or 0), "🌤")


async def weather_now_text(user: dict, city: str = None) -> str:
    """Текущая погода текстом."""
    city = (city or user.get("city") or "").strip()
    if not city:
        return ("🌦 Город не задан. Укажите его в ⚙️ Настройки → 🌍 Город "
                "или напишите: погода в <город>.")
    data = await weather_api(city, "current")
    if not data or data.get("error"):
        err = (data or {}).get("error", {}).get("msg", "")
        return f"😔 Не удалось узнать погоду для «{city}».{(' ' + err) if err else ''}"
    cur = data.get("current", {})
    loc = data.get("location", {})
    name = f"{loc.get('name', city)}"
    parts = [
        f"🌦 Погода в {name} (сейчас):",
        f"{weather_condition_text(cur.get('condition', {}).get('code'))}",
        f"🌡 Температура: {cur.get('temp_c')}°C (ощущается {cur.get('feelslike_c')}°C)",
        f"💨 Ветер: {cur.get('wind_kph')} км/ч, влажность: {cur.get('humidity')}%",
    ]
    try:
        # часовой пояс города из ответа → локальное время
        ltime = loc.get("localtime", "")
        if ltime:
            parts.append(f"🕐 Местное время: {ltime[11:16] if len(ltime) > 16 else ltime}")
    except Exception:
        pass
    return "\n".join(parts)


async def weather_forecast_text(user: dict, days: int = 3, city: str = None) -> str:
    """Прогноз на 1–3 дня."""
    city = (city or user.get("city") or "").strip()
    if not city:
        return "🌦 Город не задан. Укажите его в ⚙️ Настройки → 🌍 Город."
    data = await weather_api(city, "forecast")
    if not data or data.get("error"):
        return f"😔 Не удалось узнать прогноз для «{city}»."
    fdays = (data.get("forecast", {}) or {}).get("forecastday", [])[:max(1, min(3, days))]
    if not fdays:
        return f"😔 Прогноз для «{city}» недоступен."
    loc = data.get("location", {})
    lines = [f"🌦 Прогноз погоды: {loc.get('name', city)}"]
    for d in fdays:
        day = d.get("day", {})
        date_obj = None
        try:
            date_obj = datetime.strptime(d.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            pass
        label = f"{weekday_ru(date_obj)}" if date_obj else d.get("date", "")
        lines.append(
            f"\n📅 {label} ({d.get('date', '')}): "
            f"{weather_condition_text(day.get('condition', {}).get('code'))}\n"
            f"🌡 от {day.get('mintemp_c')}°C до {day.get('maxtemp_c')}°C, "
            f"💨 ветер до {day.get('maxwind_kph')} км/ч, "
            f"🌧 осадки {day.get('daily_chance_of_rain', 0)}%"
        )
    return "\n".join(lines)


async def detect_city_tz(user: dict) -> None:
    """Уточняет часовой пояс пользователя по его городу (WeatherAPI → tz_id)."""
    city = (user.get("city") or "").strip()
    if not city:
        return
    data = await weather_api(city, "current")
    if not data or data.get("error"):
        return
    try:
        loc = data.get("location", {})
        tzname = loc.get("tz_id")
        if tzname and ZoneInfo:
            user["tz_id"] = tzname
            # смещение считаем из localtime
            lt = loc.get("localtime", "")
            if len(lt) >= 16:
                local_dt = datetime.strptime(lt[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                utc_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
                offset_h = round((local_dt - utc_dt).total_seconds() / 3600)
                user["timezone"] = int(offset_h)
        save_user(user)
    except Exception as e:
        logger.debug("detect_city_tz: %s", e)

# ==================================
# === РЕНДЕР ШКОЛЬНЫХ ЭКРАНОВ ===
# ==================================

def get_bells_for(cls: dict, d: date):
    """Звонки для конкретной даты: по дате → по дню недели → общие."""
    if not cls:
        return []
    by_date = cls.get("bells_by_date") or {}
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        if by_date.get(d.strftime(fmt)):
            return by_date[d.strftime(fmt)]
    by_day = cls.get("bells_by_day") or {}
    if by_day.get(weekday_short(d)):
        return by_day[weekday_short(d)]
    return cls.get("bells") or []


def bells_text(cls: dict) -> str:
    bells = cls.get("bells") or []
    if not bells:
        return "🔔 Звонки ещё не заданы. Админ может задать их в 👨‍💼 Админская панель → 🔔 Звонки."
    lines = ["🔔 Звонки уроков:"]
    for b in sorted(bells, key=lambda x: int(x.get("lesson", 0))):
        lines.append(f"{b.get('lesson')}. {b.get('start')} — {b.get('end')}")
    if cls.get("bells_by_day"):
        lines.append("\n📅 Отдельные звонки по дням недели: " +
                     ", ".join(sorted(cls["bells_by_day"].keys())))
    if cls.get("bells_by_date"):
        lines.append("🗓 Отдельные звонки по датам: " +
                     ", ".join(sorted(cls["bells_by_date"].keys())[:10]))
    return "\n".join(lines)


def teachers_text(cls: dict, subjects=None) -> str:
    teachers = cls.get("teachers") or {}
    if not teachers:
        return "👨‍🏫 Учителя ещё не заданы. Админ может добавить их в 👨‍💼 Админская панель → 👨‍🏫 Учителя."
    if subjects:
        sel = {}
        for want in ([subjects] if isinstance(subjects, str) else list(subjects)):
            for subj, name in teachers.items():
                if want.lower().strip() in subj.lower() or subj.lower() in want.lower():
                    sel[subj] = name
        teachers = sel or teachers
    if not teachers:
        return "👨‍🏫 Не нашёл такого предмета."
    lines = ["👨‍🏫 Учителя:"]
    for subj, name in teachers.items():
        lines.append(f"• {subj}: {name}")
    return "\n".join(lines)


def schedule_day_text(cls: dict, d: date, with_bells: bool = True) -> str:
    """Текст расписания на день."""
    if not cls:
        return ("🎓 Вы пока не в классе. Присоединитесь: 🎓 Управление классами "
                "→ ➕ Присоединиться (нужен код) или создайте свой класс.")
    wd = weekday_ru(d)
    sched = (cls.get("schedule") or {}).get(wd, "")
    header = f"📅 {wd}, {fmt_date(d)}"
    hol = cls.get("holidays")
    if hol:
        try:
            if datetime.strptime(hol, "%Y-%m-%d").date() <= d:
                return f"{header}\n\n🎉 КАНИКУЛЫ! 🎉\n(до конца каникул — расписание не показываем)"
        except ValueError:
            pass
    if not sched or not sched.strip():
        return f"{header}\n\n📭 Уроков нет / расписание не задано."
    lines = [header, ""]
    items = [s.strip() for s in str(sched).split("\n") if s.strip()]
    bells = get_bells_for(cls, d) if with_bells else []
    for i, item in enumerate(items, start=1):
        b = next((x for x in bells if int(x.get("lesson", 0)) == i), None)
        if b:
            lines.append(f"{i}. {item}  ({b.get('start')}–{b.get('end')})")
        else:
            lines.append(f"{i}. {item}")
    return "\n".join(lines)


def homework_for_day(cls: dict, d: date) -> str:
    hw = (cls.get("homework") or {}).get(d.isoformat(), {})
    if not hw:
        return ""
    lines = []
    for subj, item in hw.items():
        txt = item.get("text", "") if isinstance(item, dict) else str(item)
        lines.append(f"📚 {subj}: {txt}")
    return "\n".join(lines)


async def day_screen(user: dict, d: date, offset_label: str = "") -> str:
    """Экран дня: расписание + ДЗ + звонки."""
    cls = get_class_by_user(user["user_id"])
    if not cls:
        return ("🎓 Вы пока не в классе. Присоединитесь: 🎓 Управление классами "
                "→ ➕ Присоединиться (нужен код) или создайте свой класс.")
    text = schedule_day_text(cls, d)
    hw = homework_for_day(cls, d)
    if hw:
        text += "\n\n📝 Домашнее задание:\n" + hw
    return text


def homework_full_text(cls: dict, limit_days: int = 30) -> str:
    """Все ДЗ, сгруппированные по датам."""
    hw = cls.get("homework") or {}
    if not hw:
        return ("📝 Домашнее задание пока не добавлено. "
                "Админ может добавить: ➕ Добавить ДЗ.")
    today = now().date()
    dates = sorted([k for k in hw.keys() if _iso_date(k)], reverse=True)[:limit_days]
    lines = ["📝 Домашнее задание:"]
    for iso in dates:
        d = _iso_date(iso)
        if d < today - timedelta(days=7):
            continue
        day_hw = hw.get(iso) or {}
        if not day_hw:
            continue
        mark = " (сегодня)" if d == today else (" (завтра)" if d == today + timedelta(days=1) else "")
        lines.append(f"\n📅 {weekday_ru(d)}, {fmt_date(d)}{mark}:")
        for subj, item in day_hw.items():
            txt = item.get("text", "") if isinstance(item, dict) else str(item)
            lines.append(f"  • {subj}: {txt}")
    if len(lines) == 1:
        return "📝 Домашнее задание пока пусто на ближайшие дни."
    return "\n".join(lines)


def _iso_date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def holidays_text(user: dict) -> str:
    cls = get_class_by_user(user["user_id"])
    if not cls:
        return "🎓 Вы пока не в классе — каникулы отслеживаются внутри класса."
    hol = cls.get("holidays")
    if not hol:
        return ("🎉 Каникулы пока не объявлены. Когда админ класса их задаст, "
                "бот сообщит и перестанет показывать расписание на этот период.")
    try:
        d = datetime.strptime(hol, "%Y-%m-%d").date()
    except ValueError:
        return "🎉 Дата каникул задана некорректно."
    today = now().date()
    if d > today:
        days = (d - today).days
        return f"🎉 Каникулы начинаются {fmt_date(d)} — через {days} дн.! Держитесь!"
    return f"🎉 Каникулы с {fmt_date(d)} — уже идут! Отдыхайте!"

# ==================================
# === КЛАВИАТУРА ГЛАВНОГО МЕНЮ ===
# ==================================

def get_personal_buttons(user_id):
    data = load_data(PERSONAL_BUTTONS_FILE, {}, expect="dict")
    btns = data.get(str(user_id)) or []
    return btns if isinstance(btns, list) else []


def get_global_buttons():
    return load_data(GLOBAL_BUTTONS_FILE, [], expect="list")


def get_class_custom_buttons(class_code):
    cls = get_class_by_code(class_code)
    return (cls or {}).get("custom_buttons") or []


def get_all_user_button_names(user_id) -> list:
    """ВСЕ доступные пользователю кнопки: стандартные + личные + глобальные + классные."""
    names = list(ALL_MAIN_MENU_BUTTONS)
    for b in get_personal_buttons(user_id):
        if isinstance(b, dict) and b.get("name") and b["name"] not in names:
            names.append(b["name"])
    for b in get_global_buttons():
        if isinstance(b, dict) and b.get("name") and b["name"] not in names:
            names.append(b["name"])
    for b in get_class_custom_buttons(get_user(user_id).get("class_code")):
        if isinstance(b, dict) and b.get("name") and b["name"] not in names:
            names.append(b["name"])
    return names


def _apply_user_button_settings(user: dict, raw_button_names):
    """Убирает скрытые и переименовывает по карте user['custom_buttons']."""
    hidden = user.get("hidden_buttons") or []
    rename_map = user.get("custom_buttons") or {}
    out = []
    for name in raw_button_names:
        if name in hidden:
            continue
        out.append(rename_map.get(name, name))
    return out


def _layout_per_row(user: dict) -> int:
    layout = user.get("button_layout", "default")
    if layout == "compact":
        return 4
    if layout == "wide":
        return 2
    return 3


def _visible_main_buttons(user: dict) -> list:
    """Итоговый список кнопок главного меню (с настройками пользователя)."""
    uid = user["user_id"]
    base = list(ALL_MAIN_MENU_BUTTONS)
    # Личные
    for b in get_personal_buttons(uid):
        if isinstance(b, dict) and b.get("name"):
            base.append(b["name"])
    # Глобальные
    for b in get_global_buttons():
        if isinstance(b, dict) and b.get("name"):
            base.append(b["name"])
    # Классные
    for b in get_class_custom_buttons(user.get("class_code")):
        if isinstance(b, dict) and b.get("name"):
            base.append(b["name"])
    # Пользовательский порядок
    order = user.get("custom_button_order") or []
    if order:
        base = sorted(base, key=lambda n: order.index(n) if n in order else len(order))
    # Кнопки «Ещё» — уже входит в ALL_MAIN_MENU_BUTTONS; прячем в Ещё второстепенные
    # при большом списке (лимит ВК: 10 рядов).
    apply_settings = _apply_user_button_settings(user, base)
    max_visible = 10 * _layout_per_row(user) - 4   # запас: админ/дев/выход
    if len(apply_settings) > max_visible:
        more = [b for b in apply_settings if b != "📋 Ещё"]
        visible = more[:max_visible]
        hidden_into_more = more[max_visible:]
        user["_more_buttons"] = hidden_into_more
        if "📋 Ещё" not in visible:
            visible.append("📋 Ещё")
        return visible
    user["_more_buttons"] = []
    return apply_settings


def main_menu_keyboard(user: dict) -> str:
    """Клавиатура главного меню (обычная, текстовые кнопки)."""
    uid = user["user_id"]
    cls = get_class_by_code(user.get("class_code"))
    buttons = _visible_main_buttons(user)
    # Админские быстрые кнопки
    extra = []
    if cls and is_class_admin(uid, cls):
        extra = ["➕ Добавить ДЗ", "🗑️ Удалить ДЗ", "📢 Написать классу", "👨‍💼 Админская панель"]
        for b in extra:
            if b in buttons:
                extra.remove(b)
    if is_dev(uid) and "🛠️ Панель разработчика" not in buttons:
        extra.append("🛠️ Панель разработчика")
    visible = buttons[:buttons.index("📋 Ещё")] if "📋 Ещё" in buttons else buttons
    tail = ["📋 Ещё"] if "📋 Ещё" in buttons else []
    final = visible + extra + tail
    per_row = _layout_per_row(user)
    rows = layout_rows([(b, _menu_payload(b)) for b in final], per_row)
    colored = bool(user.get("colored_buttons_enabled", True))
    return kb_text(rows, colored=colored)


def more_menu_keyboard(user: dict) -> str:
    """Подменю «📋 Ещё» (inline)."""
    uid = user["user_id"]
    cls = get_class_by_code(user.get("class_code"))
    items = list(user.get("_more_buttons") or [])
    for b in MORE_MENU_BUTTONS:
        if b not in items:
            items.append(b)
    # скрытые под Ещё личные/глобальные/классные кнопки тоже показываем
    for b in get_all_user_button_names(uid):
        if b not in items and b not in ALL_MAIN_MENU_BUTTONS:
            items.append(b)
    items = [b for b in items if b not in ("📋 Ещё",)]
    rows = layout_rows([(b, _menu_payload(b)) for b in items], 2)
    rows.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    return kb_inline(rows)


async def send_main_menu(user: dict, text: str = None) -> None:
    """Отправляет главное меню с клавиатурой."""
    name = user.get("first_name") or ""
    if text is None:
        text = f"🏠 Главное меню\n\nПривет, {name}! Выберите раздел ниже 👇"
    await vk.send(int(user["user_id"]), text, keyboard=main_menu_keyboard(user))


def back_kb() -> str:
    return kb_inline([[("⬅️ Назад в меню", {"c": "back_menu"})]])

# ==================================
# === РЕГИСТРАЦИЯ И ГЕЙТ ПОДПИСКИ ===
# ==================================

PDN_POLICY_VERSION = "152fz-v1"

PDN_TEXT = (
    "📋 СОГЛАСИЕ НА ОБРАБОТКУ ПЕРСОНАЛЬНЫХ ДАННЫХ (152-ФЗ)\n\n"
    "Чтобы бот работал, ему нужно хранить минимум данных о вас:\n"
    "• VK ID и имя;\n"
    "• день рождения (для поздравлений);\n"
    "• город (для погоды и уведомлений);\n"
    "• данные класса (расписание, ДЗ — если вы вступите).\n\n"
    "Данные хранятся в зашифрованной базе бота, не передаются третьим лицам "
    "и удаляются по запросу «🔓 Выйти из аккаунта» или сообщению разработчику.\n\n"
    "Нажимая «✅ Согласен(на)», вы соглашаетесь на обработку этих данных."
)

DISCLAIMER_TEXT = (
    "⚠️ ВАЖНАЯ ИНФОРМАЦИЯ О БОТЕ\n\n"
    "• DEVORKS+ — школьный помощник: расписание, ДЗ, звонки, таймеры, "
    "анонимные сообщения, облако и ИИ.\n"
    "• Анонимные сообщения: админы класса видят их содержимое только при "
    "модерации; бот не рассекречивает отправителя (кроме оплаты услуги).\n"
    "• Бот не является официальным приложением ВК.\n"
    "• Поддержка: 💬 Чат поддержки.\n\n"
    "Продолжая, вы подтверждаете, что ознакомились с этой информацией."
)

INSTRUCTIONS_DEFAULT = (
    "📚 ИНСТРУКЦИЯ DEVORKS+\n\n"
    "1️⃣ Создайте класс (🎓 Управление классами → ➕ Создать класс) или "
    "вступите в существующий по коду.\n"
    "2️⃣ Админ класса задаёт расписание, звонки, учителей и ДЗ — их видит весь класс.\n"
    "3️⃣ 📅 Сегодня/Завтра — ваш школьный день одним экраном.\n"
    "4️⃣ ⏰ Таймер — напоминания; 🍅 Помодоро — режим учёбы.\n"
    "5️⃣ 🕵️ Анонимное сообщение — написать однокласснику анонимно.\n"
    "6️⃣ 🤖 DEVORKS+ai — чат с ИИ (умеет фото и голосовые), 🪄 AI Agent — "
    "управляет ботом обычными фразами («напомни в 19:00», «добавь дз…»).\n"
    "7️⃣ ☁️ Облако и 🔐 Сейф — ваши файлы: обычные и зашифрованные.\n"
    "8️⃣ ⭐ Звёзды — внутренняя валюта: приглашайте друзей и тратьте на бонусы.\n\n"
    "Полный список кнопок — в главном меню и «📋 Ещё»."
)


def instructions_text() -> str:
    return load_instructions() or INSTRUCTIONS_DEFAULT


def subscription_required_text(uid=None) -> str:
    u = get_user(uid) if uid else {}
    confirmed = u.get("subscription_confirmed_at")
    if confirmed:
        return ("📢 Похоже, вы отписались от сообщества DEVORKS+.\n\n"
                "Подпишитесь обратно и нажмите «✅ Я подписался(ась)».")
    return ("📢 Для работы бота нужно быть подписанным на наше сообщество "
            "DEVORKS+.\n\n1. Подпишитесь на сообщество (кнопка ниже или на "
            "странице сообщества).\n2. Вернитесь в диалог с ботом и нажмите "
            "«✅ Я подписался(ась)».")


def _subscription_required_keyboard():
    return kb_inline([
        [("🔗 Открыть сообщество", {"c": "open_group"})],
        [("✅ Я подписался(ась)", {"c": "check_sub"}, "positive")],
    ])


_SUB_CACHE: dict = {}


async def is_user_subscribed(user_id) -> bool:
    """Строгая проверка подписки на сообщество (+кэш, +доверие кнопке)."""
    if not is_dev_sub_gate_enabled():
        return True
    uid = str(user_id)
    cached = _SUB_CACHE.get(uid)
    if cached is not None:
        ts, ok = cached
        if time.time() - ts < (600 if ok else 120):
            return ok
        _SUB_CACHE.pop(uid, None)
    api_ok = await vk.is_member(user_id)
    if api_ok:
        _SUB_CACHE[uid] = (time.time(), True)
        return True
    _SUB_CACHE[uid] = (time.time(), False)
    logger.info("Пользователь %s НЕ подписан (API).", user_id)
    return False


async def has_recent_local_subscription(user_id) -> bool:
    u = get_user(user_id)
    ts = u.get("subscription_confirmed_at")
    if not ts:
        return False
    try:
        return (now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")) < timedelta(hours=24)
    except ValueError:
        return False


async def ensure_subscribed(user: dict) -> bool:
    """Гейт: если не подписан — показываем требование и возвращаем False."""
    if await is_user_subscribed(user["user_id"]):
        return True
    if await has_recent_local_subscription(user["user_id"]):
        return True
    await vk.send(int(user["user_id"]), subscription_required_text(user["user_id"]),
                  keyboard=_subscription_required_keyboard())
    return False


async def _record_gate_fail(user_id) -> int:
    uid = str(user_id)
    try:
        data = load_data(SUB_GATE_FAILS_FILE, {}, expect="dict")
    except Exception:
        data = {}
    prev = data.get(uid) or {}
    try:
        last = datetime.strptime(prev.get("last", ""), "%Y-%m-%d %H:%M:%S")
        count = int(prev.get("count", 0)) if (
            now() - last) < timedelta(minutes=SUB_GATE_FAILS_WINDOW_MIN) else 0
    except ValueError:
        count = 0
    count += 1
    data[uid] = {"count": count, "last": now().strftime("%Y-%m-%d %H:%M:%S")}
    save_data(SUB_GATE_FAILS_FILE, data)
    return count


async def _clear_gate_fails(user_id) -> None:
    try:
        data = load_data(SUB_GATE_FAILS_FILE, {}, expect="dict")
        if str(user_id) in data:
            data.pop(str(user_id), None)
            save_data(SUB_GATE_FAILS_FILE, data)
    except Exception:
        pass


async def start_registration(user: dict, payload: str = "") -> None:
    """Точка входа: /start, «Начать», «Старт» и т.п."""
    uid = user["user_id"]
    info = await vk.get_user_info(uid)
    if info.get("first_name"):
        user["first_name"] = info.get("first_name")
    # Реферальная ссылка: ref_<vk_id>
    if payload.startswith("ref_") and payload[4:].isdigit():
        rid = payload[4:]
        if rid != str(uid) and get_user(rid).get("setup_completed"):
            user["referrer_id"] = rid
    save_user(user)
    credit_referrer_for(uid)
    if not user.get("dev_notified") and DEVELOPER_ID:
        try:
            await vk.send(int(DEVELOPER_ID),
                          f"🆕 Новый пользователь: {await vk.user_name(uid)} (vk{uid})")
            user["dev_notified"] = True
            save_user(user)
        except Exception:
            pass
    # Уже зарегистрирован → сразу в меню
    if user.get("setup_completed") and user.get("birthday"):
        if not await ensure_subscribed(user):
            set_state(user, ST_SUB_GATE)
            return
        await send_main_menu(user, f"🏠 С возвращением, {user['first_name']}! 👋")
        clear_state(user)
        return
    # Чистая регистрация
    if not await ensure_subscribed(user):
        set_state(user, ST_SUB_GATE)
        return
    await vk.send(int(uid), "👋 Добро пожаловать в DEVORKS+!")
    instr = instructions_text()
    await vk.send(int(uid), instr, keyboard=kb_inline([
        [("▶️ Далее", {"c": "reg_next"}, "positive")]]))
    set_state(user, ST_REG_INSTR)


async def reg_next_step(user: dict, step: str) -> None:
    """Шаги регистрации по кнопкам «Далее»."""
    uid = int(user["user_id"])
    if step == "disclaimer":
        await vk.send(uid, DISCLAIMER_TEXT, keyboard=kb_inline([
            [("✅ Прочитал(а)", {"c": "reg_next"}, "positive")]]))
        set_state(user, ST_REG_DISCLAIMER)
    elif step == "pdn":
        await vk.send(uid, PDN_TEXT, keyboard=kb_inline([
            [("✅ Согласен(на)", {"c": "reg_next"}, "positive"),
             ("❌ Не согласен(на)", {"c": "reg_cancel"}, "negative")]]))
        set_state(user, ST_REG_PDN)
    elif step == "birthday":
        await vk.send(uid,
                      "🎂 Введите дату вашего рождения в формате ДД.ММ.ГГГГ\n"
                      "(например: 14.03.2009).\n\nОна нужна для поздравлений и "
                      "отсчёта дней. Минимальный возраст — 13 лет.")
        set_state(user, ST_REG_BIRTHDAY)
    elif step == "city":
        await vk.send(uid,
                      "🌍 Напишите ваш город (например: Москва).\n\nОн нужен для "
                      "погоды и точного времени уведомлений. Или нажмите "
                      "«Пропустить».", keyboard=kb_inline([
                          [("⏭ Пропустить", {"c": "reg_skip_city"})]]))
        set_state(user, ST_REG_CITY)


async def handle_registration_text(user: dict, text: str) -> bool:
    """Обработка текста в состояниях регистрации. True = обработано."""
    uid = int(user["user_id"])
    state = user.get("state")

    if state == ST_REG_BIRTHDAY:
        d = parse_date_or_none(text)
        if not d:
            await vk.send(uid, "⚠️ Не понял дату. Введите в формате ДД.ММ.ГГГГ (например: 14.03.2009).")
            return True
        age = (now().date() - d).days / 365.25
        if age < 13:
            await vk.send(uid,
                          "🔞 По правилам (152-ФЗ) пользоваться ботом можно с 13 лет.\n"
                          "Попросите родителей написать разработчику через "
                          "сообщество — вернём доступ вручную.")
            set_state(user, ST_AGE_BLOCKED)
            return True
        if age >= 120:
            await vk.send(uid, "⚠️ Слишком «взрослая» дата :) Проверьте год рождения.")
            return True
        user["birthday"] = d.isoformat()
        save_user(user)
        if age < 18 and not user.get("parent_consent"):
            await vk.send(uid,
                          "👶 Вам меньше 18 лет. По закону нужно согласие "
                          "родителя/законного представителя.\n\nПокажите это "
                          "сообщение родителю и нажмите «✅ Родитель согласен», "
                          "если он разрешает пользоваться ботом.",
                          keyboard=kb_inline([
                              [("✅ Родитель согласен", {"c": "reg_next"}, "positive"),
                               ("❌ Выход", {"c": "reg_cancel"}, "negative")]]))
            set_state(user, ST_REG_PARENT)
            return True
        await reg_next_step(user, "city")
        return True

    if state == ST_REG_CITY:
        city = text.strip().strip("<>[],.")
        if len(city) < 2 or len(city) > 40:
            await vk.send(uid, "⚠️ Похоже, это не город. Напишите название города (например: Казань).")
            return True
        user["city"] = clean_forbidden(city).title()
        save_user(user)
        await detect_city_tz(user)
        await finish_registration(user)
        return True

    return False


async def finish_registration(user: dict) -> None:
    """Завершение регистрации → главное меню."""
    uid = int(user["user_id"])
    user["setup_completed"] = True
    user["instructions_read"] = True
    save_user(user)
    set_state(user, ST_MAIN)
    await send_main_menu(
        user,
        f"🎉 Готово, {user['first_name']}! Регистрация завершена.\n\n"
        f"💡 С чего начать: 🎓 Управление классами → создайте класс или "
        f"вступите по коду. Дальше — расписание, ДЗ, таймеры и ИИ!")
    if user.get("referrer_id"):
        try:
            await vk.send(int(user["referrer_id"]),
                          f"🎉 По вашей ссылке вступил(а) {user['first_name']}! "
                          f"Начислено +{REFERRAL_REWARD_STARS} ⭐")
        except Exception:
            pass


# ==================================
# === РОУТЕР ТЕКСТОВЫХ СООБЩЕНИЙ ===
# ==================================

async def route_message(user: dict, text: str, message: dict) -> None:
    """Главный роутер входящих сообщений (текст/кнопки/голос/фото)."""
    uid = int(user["user_id"])
    state = user.get("state") or ST_MAIN
    stripped = (text or "").strip()

    # --- Быстрые команды работают из любого состояния ---
    if stripped in QUICK_COMMANDS or stripped.lower() in ("старт", "начать", "/start", "start"):
        return await handle_main_menu(user, stripped, message)

    # --- Файлы вне режимов загрузки ---
    if message.get("attachments"):
        handled = await route_attachments(user, message, state)
        if handled:
            return

    # --- Регистрация ---
    if state.startswith("REG_") or state in (ST_AGE_BLOCKED, ST_SUB_GATE):
        if await handle_registration_text(user, stripped):
            return

    # --- Диспетчеризация по состояниям ---
    handler = STATE_HANDLERS.get(state)
    if handler is not None:
        try:
            done = await handler(user, stripped, message)
            if done:
                return
        except Exception as e:
            logger.error("state %s: %s\n%s", state, e, traceback.format_exc())
            await vk.send(uid, "😔 Произошла ошибка. Попробуйте ещё раз или нажмите ⬅️ Назад в меню.")
            return

    # --- Обычный текст в главном меню → главное меню / ИИ-подсказка ---
    if state == ST_MAIN:
        return await handle_main_menu_fallback(user, stripped, message)

    # Состояние без обработчика — возвращаем в меню
    await handle_main_menu(user, "⬅️ Назад в меню", message)


STATE_HANDLERS: dict = {}


def state_handler(state: str):
    """Декоратор-регистратор обработчиков состояний."""
    def deco(fn):
        STATE_HANDLERS[state] = fn
        return fn
    return deco


# ==================================
# === РОУТЕР ГЛАВНОГО МЕНЮ ===
# ==================================

async def handle_main_menu(user: dict, button: str, message: dict) -> None:
    """Маршрутизация кнопок главного меню (и быстрых команд)."""
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    button = (button or "").strip()

    # Кнопки-выходы
    if button in ("⬅️ Назад в меню", "🏠 Главное меню", "⬅️ Главное меню"):
        clear_state(user)
        return await send_main_menu(user)
    if button.lower() in ("старт", "начать", "/start", "start"):
        return await start_registration(user, message.get("ref") or "")

    if button == "📅 Сегодня":
        clear_state(user)
        return await vk.send(uid, await day_screen(user, now().date()),
                             keyboard=main_menu_keyboard(user))
    if button == "📅 Завтра":
        clear_state(user)
        return await vk.send(uid, await day_screen(user, now().date() + timedelta(days=1)),
                             keyboard=main_menu_keyboard(user))
    if button == "📅 Расписание":
        clear_state(user)
        return await send_week_schedule(user)
    if button == "📝 Домашнее задание":
        clear_state(user)
        if not cls:
            return await vk.send(uid,
                "🎓 Вы не в классе. Домашка хранится внутри класса.\n"
                "🎓 Управление классами → создать/вступить.",
                keyboard=main_menu_keyboard(user))
        rows = []
        if is_class_admin(uid, cls):
            rows = [[("➕ Добавить ДЗ", {"c": "hw_add"}, "positive")],
                    [("🗑️ Удалить ДЗ", {"c": "hw_del"}, "negative")]]
        rows.append([("📅 Сегодня", _menu_payload("📅 Сегодня")),
                     ("📅 Завтра", _menu_payload("📅 Завтра"))])
        rows.append([("⬅️ Назад в меню", {"c": "back_menu"})])
        return await vk.send(uid, homework_full_text(cls), keyboard=kb_inline(rows))

    if button == "➕ Добавить ДЗ":
        if not (cls and is_class_admin(uid, cls)):
            return await vk.send(uid, "⛔ Добавлять ДЗ может только админ класса.")
        return await hw_add_start(user)
    if button == "🗑️ Удалить ДЗ":
        if not (cls and is_class_admin(uid, cls)):
            return await vk.send(uid, "⛔ Удалять ДЗ может только админ класса.")
        return await hw_del_start(user)

    if button == "🔔 Звонки":
        clear_state(user)
        rows = []
        if cls and is_class_admin(uid, cls):
            rows.append([("✏️ Задать звонки", {"c": "bells_edit"})])
        rows.append([("⬅️ Назад в меню", {"c": "back_menu"})])
        return await vk.send(uid, bells_text(cls) if cls else
                             "🎓 Вы не в классе.", keyboard=kb_inline(rows))

    if button == "👨‍🏫 Учителя":
        clear_state(user)
        rows = []
        if cls and is_class_admin(uid, cls):
            rows.append([("✏️ Изменить учителей", {"c": "teachers_edit"})])
        rows.append([("⬅️ Назад в меню", {"c": "back_menu"})])
        return await vk.send(uid, teachers_text(cls) if cls else
                             "🎓 Вы не в классе.", keyboard=kb_inline(rows))

    if button == "🎉 Каникулы":
        clear_state(user)
        rows = []
        if is_class_admin(uid, cls):
            rows.append([("✏️ Задать каникулы", {"c": "holidays_set"})])
        rows.append([("⬅️ Назад в меню", {"c": "back_menu"})])
        return await vk.send(uid, holidays_text(user), keyboard=kb_inline(rows))

    if button == "💬 Написать админу":
        if not cls or not cls.get("admins"):
            return await vk.send(uid, "🎓 В вашем классе нет админов. Обратитесь в 💬 Чат поддержки.")
        admins = [str(a) for a in cls["admins"]]
        rows = []
        for a in admins:
            name = await vk.user_name(a)
            rows.append([(f"✉️ {name}", {"c": "adm_msg", "id": int(a)})])
        rows.append([("⬅️ Назад в меню", {"c": "back_menu"})])
        return await vk.send(uid, "💬 Выберите админа класса, которому хотите написать:",
                             keyboard=kb_inline(rows))

    if button == "⏰ Таймер":
        return await timer_menu(user)

    if button == "🕵️ Анонимное сообщение":
        return await anon_select_user(user)

    if button == "📨 Мои анонимные сообщения":
        return await anon_inbox(user, 0)

    if button == "🎓 Управление классами":
        return await class_management(user)

    if button == "⚙️ Настройки":
        return await user_settings(user)

    if button == "🌟 Мои кнопки":
        return await personal_buttons_menu(user)

    if button == "☁️ Облако":
        return await cloud_menu(user)

    if button == "🔐 Сейф":
        return await vault_menu(user)

    if button == "⭐ Звезды":
        return await stars_menu(user)

    if button == "🤖 DEVORKS+ai":
        return await ai_chat_start(user)

    if button == "🪄 AI Agent":
        return await automation_start(user)

    if button == "🌦 Погода":
        clear_state(user)
        text = await weather_now_text(user)
        return await vk.send(uid, text, keyboard=kb_inline([
            [("🌦 Прогноз на 3 дня", {"c": "weather_3days"})],
            [("⬅️ Назад в меню", {"c": "back_menu"})]]))

    if button == "📚 Инструкция":
        clear_state(user)
        return await vk.send(uid, instructions_text(), keyboard=back_kb())

    if button == "🔑 Код класса":
        if not cls:
            return await vk.send(uid, "🎓 Вы не в классе.", keyboard=back_kb())
        return await vk.send(uid,
            f"🔑 Код класса «{cls.get('class_name')}»: {cls['class_code']}\n\n"
            f"Передайте его одноклассникам — они вступят через "
            f"🎓 Управление классами → ➕ Присоединиться.",
            keyboard=back_kb())

    if button == "📋 Ещё":
        return await vk.send(uid, "📋 Ещё:", keyboard=more_menu_keyboard(user))

    if button == "🚪 Выйти из класса":
        if not cls:
            return await vk.send(uid, "🎓 Вы и так не в классе.", keyboard=main_menu_keyboard(user))
        return await vk.send(uid,
            f"🚪 Выйти из класса «{cls.get('class_name')}»?",
            keyboard=kb_inline([
                [("✅ Да, выйти", {"c": "class_leave"}, "negative")],
                [("⬅️ Отмена", {"c": "back_menu"})]]))

    if button == "🔓 Выйти из аккаунта":
        return await vk.send(uid,
            "🔓 Выйти из аккаунта и удалить все ваши данные?\n"
            "(настройки, файлы облака, таймеры, звёзды — всё будет удалено)",
            keyboard=kb_inline([
                [("✅ Да, удалить всё", {"c": "account_delete"}, "negative")],
                [("⬅️ Отмена", {"c": "back_menu"})]]))

    if button == "📢 Написать классу":
        if not (cls and is_class_admin(uid, cls)):
            return await vk.send(uid, "⛔ Писать классу может только админ.")
        await vk.send(uid, "📢 Введите текст объявления для всего класса (или «отмена»):")
        return set_state(user, ST_SEND_CLASS_MESSAGE)

    if button == "👨‍💼 Админская панель":
        if not (cls and is_class_admin(uid, cls)):
            return await vk.send(uid, "⛔ Панель доступна админам класса.")
        return await admin_panel(user)

    if button == "🛠️ Панель разработчика":
        if not is_dev(uid):
            return await vk.send(uid, "⛔ Панель доступна только разработчику бота.")
        return await developer_panel(user)

    if button == "🎂 Дни рождения":
        return await birthdays_show(user)
    if button == "🕐 Дежурные":
        return await duty_menu(user)
    if button == "🤒 Я болел(а)":
        return await sick_start(user)
    if button == "❗ Важное":
        return await vazhno_show(user)
    if button == "🌙 Не беспокоить":
        return await dnd_start(user)
    if button == "📚 Решения":
        return await solutions_menu(user)
    if button == "💬 Чат поддержки":
        await vk.send(uid,
            "💬 Чат поддержки: напишите ваше сообщение — разработчик "
            "получит его и ответит здесь. Одно сообщение за раз.",
            keyboard=kb_inline([[("⬅️ Назад в меню", {"c": "back_menu"})]]))
        return set_state(user, ST_SUPPORT_CHAT)

    # Личные / глобальные / классные кнопки
    return await custom_button_activate(user, button)


async def handle_main_menu_fallback(user: dict, text: str, message: dict) -> None:
    """Неизвестный текст в главном меню: нечеткий поиск кнопки → меню."""
    target = _fuzzy_menu_target(user, text)
    if target:
        return await handle_main_menu(user, target, message)
    await send_main_menu(user, "🤔 Не понял команду. Выберите раздел из меню ниже 👇")


def _fuzzy_menu_target(user: dict, text: str):
    """Нечеткий поиск кнопки меню по тексту («дз» → 📝 Домашнее задание)."""
    t = (text or "").strip().lower().lstrip("/")
    if not t:
        return None
    aliases = {
        "дз": "📝 Домашнее задание", "домашка": "📝 Домашнее задание",
        "домашнее": "📝 Домашнее задание", "сегодня": "📅 Сегодня",
        "завтра": "📅 Завтра", "расписание": "📅 Расписание",
        "уроки": "📅 Расписание", "звонки": "🔔 Звонки",
        "учителя": "👨‍🏫 Учителя", "учителя класс": "👨‍🏫 Учителя",
        "каникулы": "🎉 Каникулы", "таймер": "⏰ Таймер",
        "напоминания": "⏰ Таймер", "анонимка": "🕵️ Анонимное сообщение",
        "анонимно": "🕵️ Анонимное сообщение",
        "анонимки": "📨 Мои анонимные сообщения",
        "класс": "🎓 Управление классами", "классы": "🎓 Управление классами",
        "настройки": "⚙️ Настройки", "кнопки": "🌟 Мои кнопки",
        "облако": "☁️ Облако", "сейф": "🔐 Сейф",
        "звёзды": "⭐ Звезды", "звезды": "⭐ Звезды",
        "ии": "🤖 DEVORKS+ai", "ai": "🤖 DEVORKS+ai",
        "агент": "🪄 AI Agent", "agent": "🪄 AI Agent",
        "автоматизация": "🪄 AI Agent", "погода": "🌦 Погода",
        "инструкция": "📚 Инструкция", "помощь": "📚 Инструкция",
        "код": "🔑 Код класса", "ещё": "📋 Ещё", "еще": "📋 Ещё",
        "меню": "⬅️ Назад в меню", "назад": "⬅️ Назад в меню",
        "помодоро": "⏰ Таймер", "днюхи": "🎂 Дни рождения",
        "дежурные": "🕐 Дежурные", "решения": "📚 Решения",
        "поддержка": "💬 Чат поддержки",
    }
    if t in aliases:
        return aliases[t]
    for name in get_all_user_button_names(user["user_id"]):
        if t and (t in name.lower() or name.lower().replace(" ", "") in t.replace(" ", "")):
            return name
    return None


async def custom_button_activate(user: dict, name: str) -> None:
    """Активация личной/глобальной/классной кнопки."""
    uid = str(user["user_id"])
    # Личные
    for b in get_personal_buttons(uid):
        if isinstance(b, dict) and b.get("name") == name:
            content = b.get("content", "")
            if b.get("type") == "url" and content.startswith("http"):
                link_btn = {
                    "action": {"type": "open_link", "link": content,
                               "label": "🌐 Открыть ссылку",
                               "payload": json.dumps({"c": "noop"})},
                }
                back_btn = _btn("⬅️ Назад в меню", {"c": "back_menu"})
                return await vk.send(uid, f"🔗 {name}:\n{content}",
                                     keyboard=kb_inline([[link_btn], [back_btn]]))
            return await vk.send(uid, f"{name}:\n\n{content}", keyboard=back_kb())
    # Глобальные
    for b in get_global_buttons():
        if isinstance(b, dict) and b.get("name") == name:
            content = b.get("content", "")
            return await vk.send(uid, f"{name}:\n\n{content}", keyboard=back_kb())
    # Классные
    for b in get_class_custom_buttons(user.get("class_code")):
        if isinstance(b, dict) and b.get("name") == name:
            content = b.get("content", "")
            return await vk.send(uid, f"{name}:\n\n{content}", keyboard=back_kb())
    await send_main_menu(user, "🤔 Кнопка не найдена. Вот главное меню:")


# ==================================
# === РАСПИСАНИЕ (неделя) ===
# ==================================

async def send_week_schedule(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not cls:
        return await vk.send(uid, "🎓 Вы не в классе — расписание хранится в классе.",
                             keyboard=main_menu_keyboard(user))
    today = now().date()
    monday = today - timedelta(days=today.weekday())
    lines = [f"📅 Расписание класса «{cls.get('class_name')}»:"]
    for i in range(7):
        d = monday + timedelta(days=i)
        wd = WEEKDAY_NAMES[i]
        sched = (cls.get("schedule") or {}).get(wd, "") or "—"
        mark = " •сегодня" if d == today else ""
        lines.append(f"\n{wd}{mark}:")
        for s in str(sched).split("\n"):
            if s.strip():
                lines.append(f"  {s.strip()}")
        if sched == "—":
            lines.append(f"  {sched}")
    rows = []
    for i, wd in enumerate(WEEKDAY_NAMES):
        d = monday + timedelta(days=i)
        rows.append((wd[:2], {"c": "sched_day", "d": d.isoformat()}))
    kb_rows = layout_rows(rows, 4)
    kb_rows.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    await vk.send(uid, "\n".join(lines), keyboard=kb_inline(kb_rows))


async def send_day_schedule_screen(user: dict, iso_date: str) -> None:
    d = _iso_date(iso_date)
    if not d:
        return await vk.send(int(user["user_id"]), "⚠️ Дата не распознана.")
    text = await day_screen(user, d)
    await vk.send(int(user["user_id"]), text, keyboard=main_menu_keyboard(user))


# ==================================
# === ДОМАШНЕЕ ЗАДАНИЕ ===
# ==================================

async def hw_add_start(user: dict) -> None:
    uid = int(user["user_id"])
    await vk.send(uid,
        "📝 Добавление ДЗ.\n\nШаг 1/3 — дата. На какой день задаём?\n"
        "Напишите дату (например: завтра, сегодня, 25.10.2026) "
        "или выберите кнопку.",
        keyboard=kb_inline([
            [("📅 Сегодня", {"c": "hw_date", "d": now().date().isoformat()}),
             ("📅 Завтра", {"c": "hw_date", "d": (now().date() + timedelta(days=1)).isoformat()})],
            [("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
    set_state(user, ST_ADD_HOMEWORK_DATE)


@state_handler(ST_ADD_HOMEWORK_DATE)
async def hw_add_date(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    d = parse_date_or_none(text)
    if not d:
        await vk.send(uid, "⚠️ Не понял дату. Примеры: «завтра», «25.10.2026», «25.10».")
        return True
    return await hw_add_subject_step(user, d)


async def hw_add_subject_step(user: dict, d: date) -> bool:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    subjects = list((cls or {}).get("teachers") or {}).keys()
    # Также предметы из уже добавленных ДЗ
    for day_hw in (cls or {}).get("homework", {}).values():
        for s in day_hw.keys():
            if s not in subjects:
                subjects.append(s)
    rows = [(f"📚 {s}", {"c": "hw_subj", "s": s}) for s in subjects[:18]]
    kb = layout_rows(rows, 2)
    kb.append([("✍️ Свой предмет", {"c": "hw_subj_custom"})])
    kb.append([("⬅️ Отмена", {"c": "back_menu"}, "negative")])
    await vk.send(uid,
                  f"📝 Шаг 2/3 — предмет для {fmt_date(d)}. Выберите или напишите свой:",
                  keyboard=kb_inline(kb))
    set_state(user, ST_ADD_HOMEWORK, {"date": d.isoformat()})
    return True


@state_handler(ST_ADD_HOMEWORK)
async def hw_add_subject_text(user: dict, text: str, message: dict) -> bool:
    """Введён предмет (свой, текстом)."""
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    if len(text) > 60:
        await vk.send(uid, "⚠️ Название предмета слишком длинное (до 60 символов).")
        return True
    return await hw_add_text_step(user, text.strip())


async def hw_add_text_step(user: dict, subject: str) -> bool:
    uid = int(user["user_id"])
    await vk.send(uid,
                  f"📝 Шаг 3/3 — текст задания по «{subject}».\n"
                  f"Напишите задание (например: «стр 45, №120-125»). "
                  f"Можно сразу прикрепить фото/файл к этому сообщению.",
                  keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
    set_state(user, ST_ADD_HOMEWORK_TEXT,
              {"date": user["fsm"].get("date"), "subject": subject})
    return True


@state_handler(ST_ADD_HOMEWORK_TEXT)
async def hw_add_save(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        await vk.send(uid, "⛔ Добавлять ДЗ может только админ класса.")
        clear_state(user)
        return True
    date_iso = user["fsm"].get("date")
    subject = user["fsm"].get("subject")
    d = _iso_date(date_iso or "")
    if not d or not subject:
        await vk.send(uid, "⚠️ Данные потерялись, начните заново (➕ Добавить ДЗ).")
        clear_state(user)
        return True
    if not text.strip() and not message.get("attachments"):
        await vk.send(uid, "⚠️ Пустое задание. Напишите текст или прикрепите файл.")
        return True
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    # Вложения: сразу пересохраняем как VK-attachment строки
    attachments = []
    for att in (message.get("attachments") or [])[:5]:
        t = att.get("type")
        if t == "photo" and att.get("photo", {}).get("owner_id"):
            p = att["photo"]
            attachments.append(f"photo{p['owner_id']}_{p['id']}")
        elif t == "doc" and att.get("doc", {}).get("owner_id"):
            dc = att["doc"]
            attachments.append(f"doc{dc['owner_id']}_{dc['id']}")
    hw = cls.setdefault("homework", {}).setdefault(d.isoformat(), {})
    hw[subject] = {
        "text": clean_forbidden(text.strip())[:1000],
        "attachments": attachments,
        "by": str(uid),
        "ts": now().strftime("%Y-%m-%d %H:%M"),
    }
    save_class(cls)
    log_admin_action(uid, "add_homework", f"{subject} {d.isoformat()}")
    clear_state(user)
    await vk.send(uid,
                  f"✅ ДЗ по «{subject}» на {fmt_date(d)} добавлено!\n"
                  f"Сейчас сообщу классу…")
    await notify_class(cls, str(uid),
                       f"📝 Новое ДЗ ({weekday_ru(d)}, {fmt_date(d)}):\n"
                       f"📚 {subject}: {clean_forbidden(text.strip())[:500]}",
                       attachments=attachments)
    return await send_main_menu(user)


async def notify_class(cls: dict, exclude_id, text: str, attachments=None) -> int:
    """Рассылка участникам класса (кроме exclude_id). Возвращает число доставок."""
    sent = 0
    blocked = cls.get("blocked_users") or []
    for m in cls.get("members", []):
        mid = str(m)
        if mid == str(exclude_id) or mid in [str(b) for b in blocked]:
            continue
        u = get_user(mid)
        if u.get("is_blocked") or not u.get("setup_completed"):
            continue
        if u.get("dnd_until"):
            try:
                if datetime.strptime(u["dnd_until"], "%Y-%m-%d %H:%M") > now():
                    continue
            except ValueError:
                pass
        att = None
        if attachments:
            att = ",".join(attachments)
        if await vk.send(int(mid), text, attachment=att) != 0:
            sent += 1
    return sent


async def hw_del_start(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not cls:
        return await vk.send(uid, "🎓 Вы не в классе.")
    hw = cls.get("homework") or {}
    dates = sorted([k for k in hw.keys() if hw.get(k)], reverse=True)[:12]
    if not dates:
        return await vk.send(uid, "📝 ДЗ пока пусто — удалять нечего.", keyboard=back_kb())
    rows = []
    for iso in dates:
        d = _iso_date(iso)
        label = f"📅 {weekday_short(d)} {fmt_date_short(d)}" if d else iso
        rows.append((label, {"c": "hw_del_date", "d": iso}))
    kb = layout_rows(rows, 2)
    kb.append([("🗑️ Всё ДЗ", {"c": "hw_del_all"}, "negative")])
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    await vk.send(uid, "🗑️ Выберите дату, ДЗ с которой удалить:", keyboard=kb_inline(kb))


async def hw_del_date(user: dict, date_iso: str) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    day_hw = (cls or {}).get("homework", {}).get(date_iso, {})
    if not day_hw:
        return await vk.send(uid, "📭 На эту дату ДЗ нет.")
    d = _iso_date(date_iso)
    rows = [(f"🗑 {s}", {"c": "hw_del_one", "d": date_iso, "s": s}, "negative")
            for s in day_hw.keys()]
    kb = layout_rows(rows, 1)
    kb.append([(f"🗑 Всё на {fmt_date_short(d)}", {"c": "hw_del_day", "d": date_iso}, "negative")])
    kb.append([("⬅️ Назад", {"c": "menu", "b": "🗑️ Удалить ДЗ"})])
    await vk.send(uid, f"🗑️ ДЗ на {fmt_date(d) if d else date_iso}. Что удалить?",
                  keyboard=kb_inline(kb))


# ==================================
# === ЗВОНКИ / УЧИТЕЛЯ / КАНИКУЛЫ (правка) ===
# ==================================

async def bells_edit_start(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        return await vk.send(uid, "⛔ Только админ класса.")
    await vk.send(uid,
        "🔔 Задание звонков. Варианты:\n\n"
        "1️⃣ Целиком списком — пришлите строки вида:\n"
        "   «1 урок 08:30-09:15»\n   «2 урок 09:25-10:10»\n"
        "   (можно добавить «на понедельник» или «на 25.10.2026» первой строкой)\n\n"
        "2️⃣ По одному уроку — напишите: «3 урок 10:20-11:05»",
        keyboard=kb_inline([
            [("🧹 Очистить все звонки", {"c": "bells_clear"}, "negative")],
            [("⬅️ Назад", {"c": "back_menu"})]]))
    set_state(user, ST_BELLS_WAIT_ALL, {"ts": time.time()})


@state_handler(ST_BELLS_WAIT_ALL)
async def bells_parse_input(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        clear_state(user)
        return await vk.send(uid, "⛔ Только админ класса.")
    text_clean = text.strip()
    # Определяем день/дату
    target_day = None
    target_date = None
    for i, wd in enumerate(WEEKDAY_SHORT):
        if wd in text_clean.lower() or WEEKDAY_NAMES[i].lower() in text_clean.lower():
            first_line = text_clean.lower().split("\n")[0]
            if wd in first_line or WEEKDAY_NAMES[i].lower() in first_line:
                target_day = wd
                text_clean = text_clean.replace(first_line, "", 1).strip()
                break
    m = None
    for line in text_clean.split("\n")[:2]:
        m = parse_date_or_none(line.split("на")[-1].strip()) if " на " in line.lower() else None
        if m:
            target_date = m
            text_clean = "\n".join([l for l in text_clean.split("\n") if l != line])
            break
    bells = []
    for line in text_clean.split("\n"):
        line = line.strip().lower().replace("урок", "").replace(".", ":")
        # «1 08:30-09:15» / «08:30 - 09:15»
        import re
        mm = re.match(r"\s*(\d+)\D+(\d{1,2}:\d{2})\D+(\d{1,2}:\d{2})", line)
        if not mm:
            continue
        lesson, start, end = int(mm.group(1)), mm.group(2), mm.group(3)
        s = parse_hhmm(start)
        e = parse_hhmm(end)
        if not s or not e or (e[0], e[1]) <= (s[0], s[1]):
            continue
        bells.append({"lesson": lesson, "start": start, "end": end})
    if not bells:
        await vk.send(uid, "⚠️ Не нашёл ни одного звонка. Пример:\n«1 урок 08:30-09:15\n2 урок 09:25-10:10»")
        return True
    if target_date:
        by_date = cls.setdefault("bells_by_date", {})
        by_date[target_date.strftime("%d.%m.%Y")] = bells
        msg = f"✅ Звонки на {fmt_date(target_date)} заданы ({len(bells)} шт.)."
    elif target_day:
        by_day = cls.setdefault("bells_by_day", {})
        by_day[target_day] = bells
        msg = f"✅ Звонки на {WEEKDAY_NAMES[WEEKDAY_SHORT.index(target_day)]} заданы ({len(bells)} шт.)."
    else:
        cls["bells"] = bells
        msg = f"✅ Общие звонки заданы ({len(bells)} шт.)."
    save_class(cls)
    log_admin_action(uid, "edit_bells", msg)
    clear_state(user)
    await vk.send(uid, msg + "\n\n" + bells_text(cls))
    return await send_main_menu(user)


async def teachers_edit_start(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        return await vk.send(uid, "⛔ Только админ класса.")
    await vk.send(uid,
        "👨‍🏫 Изменение учителей.\n\nПришлите строки вида:\n"
        "«Математика — Иванова А.П.»\n«Физика — » (пусто = удалить предмет)\n\n"
        "Можно несколькими строками сразу. Или «удалить всех».",
        keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
    set_state(user, ST_EDIT_TEACHERS)


@state_handler(ST_EDIT_TEACHERS)
async def teachers_parse_input(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        clear_state(user)
        return await vk.send(uid, "⛔ Только админ класса.")
    if text.lower().strip() in ("удалить всех", "очистить"):
        cls["teachers"] = {}
        save_class(cls)
        log_admin_action(uid, "teachers_clear", "")
        clear_state(user)
        await vk.send(uid, "🧹 Все учителя удалены.")
        return await send_main_menu(user)
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    count = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        sep = None
        for s in ("—", "–", "-", ":"):
            if s in line:
                sep = s
                break
        if not sep:
            continue
        subject, teacher = line.split(sep, 1)
        subject = subject.strip()
        teacher = teacher.strip()
        if not subject:
            continue
        if teacher:
            cls.setdefault("teachers", {})[subject] = clean_forbidden(teacher)[:80]
        else:
            cls.setdefault("teachers", {}).pop(subject, None)
        count += 1
    if not count:
        await vk.send(uid, "⚠️ Не понял ни одной строки. Пример: «Математика — Иванова А.П.»")
        return True
    save_class(cls)
    log_admin_action(uid, "edit_teachers", f"{count} шт.")
    clear_state(user)
    await vk.send(uid, f"✅ Обновлено учителей: {count}.\n\n" + teachers_text(cls))
    await notify_class(cls, str(uid), "👨‍🏫 Список учителей обновлён — проверьте в меню!")
    return await send_main_menu(user)


async def holidays_set_start(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        return await vk.send(uid, "⛔ Только админ класса.")
    await vk.send(uid,
        "🎉 Задание каникул.\n\nНапишите дату начала каникул "
        "(например: «26.10.2026» или «завтра»). «Отмена» — не менять.",
        keyboard=kb_inline([
            [("🧹 Снять каникулы", {"c": "holidays_off"}, "negative")],
            [("⬅️ Отмена", {"c": "back_menu"})]]))
    set_state(user, ST_SET_HOLIDAYS)


@state_handler(ST_SET_HOLIDAYS)
async def holidays_parse_input(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        clear_state(user)
        return await vk.send(uid, "⛔ Только админ класса.")
    d = parse_date_or_none(text)
    if not d:
        await vk.send(uid, "⚠️ Не понял дату. Пример: «26.10.2026» или «послезавтра».")
        return True
    cls["holidays"] = d.isoformat()
    save_class(cls)
    log_admin_action(uid, "set_holidays", d.isoformat())
    clear_state(user)
    await vk.send(uid, f"✅ Каникулы: с {fmt_date(d)}.")
    await notify_class(cls, str(uid), f"🎉 Каникулы объявлены с {fmt_date(d)}! 🎉")
    return await send_main_menu(user)

# ==================================
# === ТАЙМЕРЫ / ПОМОДОРО ===
# ==================================

def _load_timers() -> dict:
    return load_data(TIMERS_FILE, {}, expect="dict")


def _save_timers(data: dict) -> None:
    save_data(TIMERS_FILE, data)


def get_user_timers(uid) -> list:
    return _load_timers().get(str(uid)) or []


async def timer_menu(user: dict) -> None:
    uid = int(user["user_id"])
    timers = get_user_timers(uid)
    lines = ["⏰ Таймеры и напоминания", ""]
    if timers:
        lines.append("ваши таймеры:")
        for i, t in enumerate(timers):
            lines.append(f"{i + 1}. {t.get('text', 'напоминание')[:50]} — {t.get('fire_at', '?')}")
    else:
        lines.append("пока нет ни одного.")
    kb = [
        [("⏱ 5 мин", {"c": "tmr_quick", "m": 5}),
         ("⏱ 15 мин", {"c": "tmr_quick", "m": 15}),
         ("⏱ 30 мин", {"c": "tmr_quick", "m": 30})],
        [("⏱ 1 час", {"c": "tmr_quick", "m": 60}),
         ("⏱ 2 часа", {"c": "tmr_quick", "m": 120}),
         ("⏱ 3 часа", {"c": "tmr_quick", "m": 180})],
        [("🕒 Свой таймер", {"c": "tmr_custom"}, "positive")],
        [("🍅 Помодоро", {"c": "pomodoro"}, "positive")],
    ]
    if timers:
        kb.append([("🗑 Удалить таймер", {"c": "tmr_list_del"}, "negative")])
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    await vk.send(uid, "\n".join(lines), keyboard=kb_inline(kb))
    clear_state(user)


async def timer_quick_add(user: dict, minutes: int) -> None:
    uid = int(user["user_id"])
    fire_at = now() + timedelta(minutes=minutes)
    timers = _load_timers()
    lst = timers.setdefault(str(uid), [])
    lst.append({
        "id": secrets.token_hex(4),
        "text": f"⏰ Прошло {minutes} мин. — пора!",
        "fire_at": fire_at.strftime("%Y-%m-%d %H:%M"),
        "tz": user.get("tz_id") or user.get("timezone", 3),
        "repeat": None,
    })
    _save_timers(timers)
    await vk.send(uid, f"✅ Таймер на {minutes} мин. поставлен — сработает в "
                      f"{fire_at.strftime('%H:%M')} ({fmt_date_short(fire_at.date())}).")


async def timer_custom_start(user: dict) -> None:
    uid = int(user["user_id"])
    await vk.send(uid,
        "🕒 Свой таймер. Шаг 1/2 — когда напомнить?\n"
        "Напишите дату и время или просто время:\n"
        "• «завтра 8:00», «25.10 15:30», «19:00» (сегодня)",
        keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
    set_state(user, ST_TIMER_SET_DATE, {"step": "when"})


@state_handler(ST_TIMER_SET_DATE)
async def timer_custom_when(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    text = text.strip().lower().replace("в ", " ")
    d = None
    hm = None
    for part in text.replace(",", " ").split():
        parsed_d = parse_date_or_none(part)
        if parsed_d and d is None:
            d = parsed_d
        else:
            hm = hm or parse_hhmm(part)
    if hm is None and d is not None:
        # только дата — время следующее
        user["fsm"]["date"] = d.isoformat()
        save_user(user)
        await vk.send(uid, "🕐 Теперь время (ЧЧ:ММ), например «19:30»:")
        set_state(user, ST_TIMER_SET_TIME, {"date": d.isoformat()})
        return True
    if hm is None:
        await vk.send(uid, "⚠️ Не понял. Примеры: «завтра 8:00», «19:45».")
        return True
    d = d or now().date()
    h, m = hm
    fire_at = datetime(d.year, d.month, d.day, h, m)
    if fire_at <= now() and not user["fsm"].get("repeat"):
        fire_at += timedelta(days=1)
    user["fsm"]["when"] = fire_at.strftime("%Y-%m-%d %H:%M")
    save_user(user)
    await vk.send(uid, "📝 Что напомнить? Напишите текст напоминания:",
                  keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
    set_state(user, ST_TIMER_SET_TEXT, {"when": user["fsm"]["when"]})
    return True


@state_handler(ST_TIMER_SET_TIME)
async def timer_custom_time(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    hm = parse_hhmm(text)
    if not hm:
        await vk.send(uid, "⚠️ Введите время в формате ЧЧ:ММ (например 19:30).")
        return True
    d = _iso_date(user["fsm"].get("date") or "")
    if not d:
        d = now().date()
    h, m = hm
    fire_at = datetime(d.year, d.month, d.day, h, m)
    if fire_at <= now():
        fire_at += timedelta(days=1)
    user["fsm"]["when"] = fire_at.strftime("%Y-%m-%d %H:%M")
    save_user(user)
    await vk.send(uid, "📝 Что напомнить? Напишите текст:")
    set_state(user, ST_TIMER_SET_TEXT, {"when": user["fsm"]["when"]})
    return True


@state_handler(ST_TIMER_SET_TEXT)
async def timer_custom_text(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    when = user["fsm"].get("when")
    if not when:
        clear_state(user)
        await vk.send(uid, "⚠️ Время потерялось, начните заново.")
        return True
    timers = _load_timers()
    timers.setdefault(str(uid), []).append({
        "id": secrets.token_hex(4),
        "text": f"⏰ {clean_forbidden(text.strip())[:500]}",
        "fire_at": when,
        "tz": user.get("tz_id") or user.get("timezone", 3),
        "repeat": None,
    })
    _save_timers(timers)
    clear_state(user)
    dt_obj = datetime.strptime(when, "%Y-%m-%d %H:%M")
    await vk.send(uid, f"✅ Таймер поставлен на {fmt_date_short(dt_obj.date())} "
                       f"{dt_obj.strftime('%H:%M')}.\n📝 {text.strip()[:200]}")
    return await send_main_menu(user)


async def timer_list_delete(user: dict) -> None:
    uid = int(user["user_id"])
    timers = get_user_timers(uid)
    if not timers:
        return await timer_menu(user)
    kb = layout_rows([(f"🗑 {t.get('when', '?')} — {t.get('text', '')[:30]}",
                       {"c": "tmr_del", "i": t.get("id")}, "negative")
                      for t in timers[:15]], 1)
    kb.append([("⬅️ Назад", {"c": "menu", "b": "⏰ Таймер"})])
    await vk.send(uid, "🗑 Выберите таймер для удаления:", keyboard=kb_inline(kb))


def add_timer(uid, when: str, text: str, repeat=None, kind="timer") -> None:
    """Универсальное добавление таймера (вкл. повторы от AI Agent)."""
    timers = _load_timers()
    timers.setdefault(str(uid), []).append({
        "id": secrets.token_hex(4),
        "text": text,
        "fire_at": when,
        "tz": 3,
        "repeat": repeat,      # {"type": "daily"} | {"type": "weekly", "day": "mon"} |
                               # {"type": "days", "days": [...]} | {"type": "skip", "days": [...]}
        "kind": kind,
    })
    _save_timers(timers)


# ---------- Помодоро ----------

def _load_pomodoro() -> dict:
    return load_data(POMODORO_FILE, {}, expect="dict")


async def pomodoro_menu(user: dict) -> None:
    uid = int(user["user_id"])
    state = _load_pomodoro().get(str(uid))
    if state and state.get("active"):
        kb = [[("⏹ Стоп", {"c": "pomo_stop"}, "negative")]]
        await vk.send(uid,
                      f"🍅 Помодоро идёт!\n"
                      f"Учёба: {state.get('work', 25)} мин / отдых: {state.get('rest', 5)} мин.\n"
                      f"Циклов завершено: {state.get('cycles', 0)}.",
                      keyboard=kb_inline(kb))
        return
    await vk.send(uid,
        "🍅 Помодоро — режим учёбы: периоды работы и отдыха.\n"
        "Классика: 25 мин учёбы / 5 мин отдыха. Выберите режим:",
        keyboard=kb_inline([
            [("🍅 25/5", {"c": "pomo_start", "w": 25, "r": 5}),
             ("🍅 50/10", {"c": "pomo_start", "w": 50, "r": 10})],
            [("🍅 90/20", {"c": "pomo_start", "w": 90, "r": 20}),
             ("🍅 15/3", {"c": "pomo_start", "w": 15, "r": 3})],
            [("⬅️ Назад", {"c": "menu", "b": "⏰ Таймер"})],
        ]))


async def pomodoro_start(user: dict, work: int, rest: int) -> None:
    uid = int(user["user_id"])
    data = _load_pomodoro()
    data[str(uid)] = {
        "active": True, "work": work, "rest": rest, "cycles": 0,
        "phase": "work",
        "phase_until": (now() + timedelta(minutes=work)).strftime("%Y-%m-%d %H:%M"),
        "tz": user.get("tz_id") or user.get("timezone", 3),
    }
    save_data(POMODORO_FILE, data)
    await vk.send(uid,
                  f"🍅 Помодоро старт!\n"
                  f"Учёба {work} мин → отдых {rest} мин → повтор.\n"
                  f"Я буду писать, когда переключаться. Удачи! 💪")

# ==================================
# === УПРАВЛЕНИЕ КЛАССАМИ ===
# ==================================

async def class_management(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if cls:
        members = len(cls.get("members", []))
        is_adm = is_class_admin(uid, cls)
        text = (f"🎓 Ваш класс: «{cls.get('class_name')}» ({cls['class_code']})\n"
                f"👥 Участников: {members}\n"
                f"👑 Вы: {'админ' if is_adm else 'ученик'}")
        kb = [
            [("🔑 Код класса", _menu_payload("🔑 Код класса")),
             ("👥 Состав класса", {"c": "class_users"})],
        ]
        if is_adm:
            kb.append([("👨‍💼 Админская панель", {"c": "menu", "b": "👨‍💼 Админская панель"}, "positive")])
        kb.append([("🚪 Выйти из класса", {"c": "class_leave"}, "negative")])
        kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    else:
        text = ("🎓 Вы не в классе. Можно:\n"
                "1. Создать свой класс (вы станете админом);\n"
                "2. Вступить в существующий по коду (дадут одноклассники).")
        kb = [
            [("➕ Создать класс", {"c": "class_create"}, "positive")],
            [("🔗 Вступить по коду", {"c": "class_join"})],
            [("⬅️ Назад в меню", {"c": "back_menu"})],
        ]
    await vk.send(uid, text, keyboard=kb_inline(kb))
    clear_state(user)


@state_handler(ST_CREATE_CLASS_NAME)
async def class_create_name(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    name = text.strip()
    if len(name) < 2 or len(name) > 40:
        await vk.send(uid, "⚠️ Название класса: от 2 до 40 символов.")
        return True
    cls = dict(DEFAULT_CLASS)
    cls["class_code"] = generate_class_code()
    cls["class_name"] = name
    cls["creator_id"] = str(uid)
    cls["members"] = [str(uid)]
    cls["admins"] = [str(uid)]
    cls["created_date"] = now().strftime("%Y-%m-%d %H:%M")
    save_class(cls)
    user["class_code"] = cls["class_code"]
    user["created_classes"] = list(set(user.get("created_classes", []) + [cls["class_code"]]))
    user["role"] = "admin"
    save_user(user)
    clear_state(user)
    await vk.send(uid,
                  f"✅ Класс «{name}» создан!\n\n"
                  f"🔑 Код для одноклассников: {cls['class_code']}\n"
                  f"Ссылка-приглашение: https://vk.me/club{vk.group_id}?ref=cls_{cls['class_code']}\n\n"
                  f"Теперь задайте расписание в 👨‍💼 Админской панели!")
    return await send_main_menu(user)


@state_handler(ST_JOIN_CLASS)
async def class_join_code(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    code = text.strip().upper()
    if user.get("class_code"):
        clear_state(user)
        await vk.send(uid, "🚪 Сначала выйдите из текущего класса (🎓 Управление классами).")
        return True
    cls = get_class_by_code(code)
    if not cls:
        await vk.send(uid, "❌ Класс с таким кодом не найден. Проверьте код (формат: ABC-123).")
        return True
    blocked = cls.get("blocked_users") or []
    if str(uid) in [str(b) for b in blocked]:
        clear_state(user)
        await vk.send(uid, "⛔ Вас исключили из этого класса. Обратитесь к админу класса.")
        return True
    cls["members"] = [str(m) for m in cls.get("members", []) if str(m) != str(uid)] + [str(uid)]
    save_class(cls)
    user["class_code"] = cls["class_code"]
    save_user(user)
    clear_state(user)
    await vk.send(uid, f"✅ Вы вступили в класс «{cls['class_name']}»! 🎉")
    for a in cls.get("admins", []):
        if str(a) != str(uid):
            try:
                await vk.send(int(a), f"👥 В класс «{cls['class_name']}» вступил(а) "
                                      f"{await vk.user_name(uid)} (vk{uid}).")
            except Exception:
                pass
    return await send_main_menu(user)


async def class_show_users(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not cls:
        return await vk.send(uid, "🎓 Вы не в классе.", keyboard=back_kb())
    lines = [f"👥 Состав класса «{cls.get('class_name')}»:"]
    admins = [str(a) for a in cls.get("admins", [])]
    blocked = [str(b) for b in cls.get("blocked_users", [])]
    for m in cls.get("members", []):
        name = await vk.user_name(m)
        tag = " 👑 админ" if m in admins else ""
        tag += " 🚫 исключён" if m in blocked else ""
        lines.append(f"• {name}{tag}")
    lines.append(f"\nВсего: {len(cls.get('members', []))}")
    await vk.send(uid, "\n".join(lines), keyboard=back_kb())


# ==================================
# === АНОНИМНЫЕ СООБЩЕНИЯ ===
# ==================================

ANON_PURGE_DAYS = 60


def _load_anon() -> dict:
    return load_data(ANON_MESSAGES_FILE, {}, expect="dict")


def _save_anon(data: dict) -> None:
    save_data(ANON_MESSAGES_FILE, data)


async def anon_select_user(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not cls:
        return await vk.send(uid, "🎓 Анонимки работают внутри класса. Сначала вступите в класс.",
                             keyboard=back_kb())
    others = [str(m) for m in cls.get("members", []) if str(m) != str(uid)]
    if not others:
        return await vk.send(uid, "👥 В классе кроме вас никого нет.", keyboard=back_kb())
    rows = []
    for m in others[:30]:
        name = await vk.user_name(m)
        rows.append((f"🕵️ {name}", {"c": "anon_to", "id": int(m)}))
    kb = layout_rows(rows, 2)
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    await vk.send(uid, "🕵️ Кому отправить анонимное сообщение? (Получатель не узнает отправителя)",
                  keyboard=kb_inline(kb))
    clear_state(user)


async def anon_compose(user: dict, to_id: int) -> None:
    uid = int(user["user_id"])
    if to_id == uid:
        return await vk.send(uid, "🤨 Себе? Серьёзно? Выберите одноклассника.")
    await vk.send(uid, "🕵️ Напишите текст анонимного сообщения (или «отмена»):",
                  keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
    set_state(user, ST_ANON_SEND_MESSAGE, {"to": str(to_id)})


@state_handler(ST_ANON_SEND_MESSAGE)
async def anon_send(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    if not text.strip():
        await vk.send(uid, "⚠️ Пустое сообщение не отправлю. Напишите текст.")
        return True
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    to_id = user["fsm"].get("to")
    if not to_id:
        clear_state(user)
        await vk.send(uid, "⚠️ Получатель потерялся. Начните заново.")
        return True
    anon = _load_anon()
    msg_id = secrets.token_hex(5)
    lst = anon.setdefault(to_id, [])
    lst.append({
        "id": msg_id,
        "text": clean_forbidden(text.strip())[:1500],
        "from": str(uid),
        "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
        "read": False,
    })
    _save_anon(anon)
    clear_state(user)
    await vk.send(uid, "🕵️ Анонимное сообщение доставлено!")
    try:
        await vk.send(int(to_id),
                      "🕵️ Вам пришло АНОНИМНОЕ сообщение!\n\n"
                      f"«{clean_forbidden(text.strip())[:1000]}»\n\n"
                      "📨 Мои анонимные сообщения — прочитать и ответить.")
    except Exception:
        pass
    return await send_main_menu(user)


async def anon_inbox(user: dict, page: int = 0) -> None:
    uid = str(user["user_id"])
    msgs = _load_anon().get(uid) or []
    # чистим старые (TTL 60 дней)
    fresh = []
    for m in msgs:
        try:
            if (now() - datetime.strptime(m.get("ts", ""), "%Y-%m-%d %H:%M:%S")) < timedelta(days=ANON_PURGE_DAYS):
                fresh.append(m)
        except ValueError:
            fresh.append(m)
    if len(fresh) != len(msgs):
        anon = _load_anon()
        anon[uid] = fresh
        _save_anon(anon)
        msgs = fresh
    if not msgs:
        return await vk.send(int(uid), "📭 Анонимных сообщений нет.", keyboard=back_kb())
    msgs = list(reversed(msgs))
    per_page = 5
    page = max(0, min(page, (len(msgs) - 1) // per_page))
    chunk = msgs[page * per_page:(page + 1) * per_page]
    lines = [f"📨 Ваши анонимные сообщения (стр. {page + 1}/{(len(msgs) - 1) // per_page + 1}):\n"]
    for m in chunk:
        dt = m.get("ts", "")[5:16]
        lines.append(f"🕒 {dt}\n«{m.get('text', '')[:600]}»\n")
        kb_row = [(f"✉️ Ответить", {"c": "anon_reply", "i": m["id"]}, "positive")]
        if not m.get("sender_viewed"):
            kb_row.append((f"👁 Отправитель ({get_price('view_sender')}⭐)",
                           {"c": "anon_view", "i": m["id"]}))
    kb = []
    nav = []
    if page > 0:
        nav.append(("◀️", {"c": "anon_page", "p": page - 1}))
    if (page + 1) * per_page < len(msgs):
        nav.append(("▶️", {"c": "anon_page", "p": page + 1}))
    if nav:
        kb.append(nav)
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    await vk.send(int(uid), "\n".join(lines), keyboard=kb_inline(kb))
    clear_state(user)


@state_handler(ST_ANON_SELECT_USER)
async def anon_reply_state(user: dict, text: str, message: dict) -> bool:
    return True  # заглушка (ответ управляется кнопками)


# ==================================
# === НАСТРОЙКИ ===
# ==================================

async def user_settings(user: dict) -> None:
    uid = int(user["user_id"])
    u = user
    notif = "вкл" if u.get("notifications") else "выкл"
    weather_n = "вкл" if u.get("weather_notifications") else "выкл"
    hol_n = "вкл" if u.get("holiday_notifications") else "выкл"
    bday_n = "вкл" if u.get("birthday_personal_notification") else "выкл"
    text = (
        "⚙️ Настройки:\n\n"
        f"🔔 Уведомления: {notif}\n"
        f"🌅 Утро: {u.get('morning_notification_time')} — «{u.get('morning_text', '')[:30]}»\n"
        f"🌙 Вечер: {u.get('evening_notification_time')} — «{u.get('evening_text', '')[:30]}»\n"
        f"🌦 Погода в {u.get('weather_notification_time')} ({weather_n}), город: {u.get('city') or '—'}\n"
        f"🎉 Праздники: {hol_n}; 🎂 Личный ДР: {bday_n} в {u.get('birthday_notification_time')}\n"
        f"🎨 Раскладка меню: {u.get('button_layout')}; цветные кнопки: "
        f"{'вкл' if u.get('colored_buttons_enabled') else 'выкл'}\n"
        f"🎂 День рождения: {u.get('birthday') or '—'}"
    )
    kb = [
        [("🔔 Уведомления", {"c": "set_notif"}),
         ("🌅/🌙 Время и тексты", {"c": "set_times"})],
        [("🌦 Погода/город", {"c": "set_weather"}),
         ("🎂 День рождения", {"c": "set_bday"})],
        [("🎨 Кнопки меню", {"c": "set_buttons"}),
         ("🌙 Не беспокоить", {"c": "set_dnd"})],
    ]
    if MINIAPP_URL:
        kb.append([("☁️ Веб-облако (Mini App)", {"c": "miniapp_open"})])
    kb.append([("🔑 Пароль веб-облака", {"c": "set_webpw"})])
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    await vk.send(uid, text, keyboard=kb_inline(kb))
    clear_state(user)


@state_handler(ST_USER_SETTINGS)
async def user_settings_state(user: dict, text: str, message: dict) -> bool:
    return True  # всё через кнопки


@state_handler(ST_SET_MORNING_TIME)
async def set_morning_time(user: dict, text: str, message: dict) -> bool:
    return await _set_hhmm_setting(user, text, "morning_notification_time", "🌅 время утреннего")


@state_handler(ST_SET_EVENING_TIME)
async def set_evening_time(user: dict, text: str, message: dict) -> bool:
    return await _set_hhmm_setting(user, text, "evening_notification_time", "🌙 время вечернего")


@state_handler(ST_SET_WEATHER_TIME)
async def set_weather_time(user: dict, text: str, message: dict) -> bool:
    return await _set_hhmm_setting(user, text, "weather_notification_time", "🌦 время погоды")


@state_handler(ST_SET_BIRTHDAY_NOTIF_TIME)
async def set_bday_time(user: dict, text: str, message: dict) -> bool:
    return await _set_hhmm_setting(user, text, "birthday_notification_time", "🎂 время напоминания о ДР")


@state_handler(ST_SET_MORNING_TEXT)
async def set_morning_text(user: dict, text: str, message: dict) -> bool:
    return await _set_text_setting(user, text, "morning_text", "🌅 утреннее")


@state_handler(ST_SET_EVENING_TEXT)
async def set_evening_text(user: dict, text: str, message: dict) -> bool:
    return await _set_text_setting(user, text, "evening_text", "🌙 вечернее")


@state_handler(ST_SET_BIRTHDAY)
async def set_birthday(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    d = parse_date_or_none(text)
    if not d:
        await vk.send(uid, "⚠️ Введите дату в формате ДД.ММ.ГГГГ (например: 14.03.2009).")
        return True
    age = (now().date() - d).days / 365.25
    if age < 13:
        await vk.send(uid, "🔞 Возраст меньше 13 лет — так нельзя (152-ФЗ).")
        return True
    user["birthday"] = d.isoformat()
    save_user(user)
    clear_state(user)
    await vk.send(uid, f"✅ День рождения обновлён: {fmt_date(d)}")
    return await send_main_menu(user)


@state_handler(ST_CHANGE_CITY)
async def set_city(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    city = text.strip().strip("<>[],.")
    if len(city) < 2 or len(city) > 40:
        await vk.send(uid, "⚠️ Похоже, это не город. Пример: «Казань».")
        return True
    user["city"] = clean_forbidden(city).title()
    save_user(user)
    await detect_city_tz(user)
    clear_state(user)
    await vk.send(uid, f"✅ Город: {user['city']}. Погода и время уведомлений подстроились!")
    return await send_main_menu(user)


@state_handler(ST_SUGGEST_FUNCTION)
async def suggest_function(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    suggestions = load_data(SUGGESTIONS_FILE, [], expect="list")
    suggestions.append({"user": str(uid), "text": clean_forbidden(text.strip())[:1000],
                        "ts": now().strftime("%Y-%m-%d %H:%M")})
    save_data(SUGGESTIONS_FILE, suggestions)
    clear_state(user)
    await vk.send(uid, "💡 Спасибо! Идея отправлена разработчику.")
    if DEVELOPER_ID:
        try:
            await vk.send(int(DEVELOPER_ID),
                          f"💡 Идея функции от {await vk.user_name(uid)}:\n«{text.strip()[:800]}»")
        except Exception:
            pass
    return await send_main_menu(user)


@state_handler(ST_DND_WAIT_TIME)
async def dnd_wait_time(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    text = text.strip().lower()
    hours = None
    import re as _re
    m = _re.search(r"(\d+)\s*(ч|час)", text)
    if m:
        hours = int(m.group(1))
    m = _re.search(r"(\d+)\s*(м|мин)", text)
    if m:
        hours = max(0.03, int(m.group(1)) / 60)
    if "до утра" in text or "утра" in text:
        until = now().replace(hour=8, minute=0, second=0, microsecond=0)
        if until < now():
            until += timedelta(days=1)
        return await _dnd_apply(user, until)
    if hours is None:
        await vk.send(uid, "⚠️ Не понял. Примеры: «2 часа», «30 минут», «до утра».")
        return True
    return await _dnd_apply(user, now() + timedelta(hours=hours))


async def _dnd_apply(user: dict, until: datetime) -> bool:
    user["dnd_until"] = until.strftime("%Y-%m-%d %H:%M")
    save_user(user)
    clear_state(user)
    await vk.send(int(user["user_id"]),
                  f"🌙 Тишина до {until.strftime('%H:%M %d.%m')}. "
                  f"Личные уведомления (кроме таймеров) приходить не будут.")
    return await send_main_menu(user)


@state_handler(ST_SICK_WAIT_FROM)
async def sick_from(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    d = parse_date_or_none(text)
    if not d:
        await vk.send(uid, "⚠️ Дата не распознана. Пример: «сегодня», «25.10.2026».")
        return True
    await vk.send(uid, f"🤒 Болен(а) с {fmt_date(d)}. С какого числа выздоровели? "
                       f"(или «сегодня»/«сейчас», если уже здоровы)")
    set_state(user, ST_SICK_WAIT_TO, {"from": d.isoformat()})
    return True


@state_handler(ST_SICK_WAIT_TO)
async def sick_to(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    d_from = _iso_date(user["fsm"].get("from") or "")
    d_to = parse_date_or_none(text) or now().date()
    if not d_from:
        clear_state(user)
        await vk.send(uid, "⚠️ Даты потерялись.")
        return True
    user["sick_from"] = d_from.isoformat()
    user["sick_to"] = d_to.isoformat()
    save_user(user)
    clear_state(user)
    cls = get_class_by_user(uid)
    await vk.send(uid, f"🤒 Отмечено: болели с {fmt_date(d_from)} по {fmt_date(d_to)}. "
                       f"Держитесь! Восстановления 💚")
    if cls:
        for a in cls.get("admins", []):
            if str(a) != str(uid):
                try:
                    await vk.send(int(a), f"🤒 {await vk.user_name(uid)} болел(а): "
                                          f"{fmt_date(d_from)} — {fmt_date(d_to)}.")
                except Exception:
                    pass
    return await send_main_menu(user)


@state_handler(ST_WEB_PW_ENTER)
async def web_pw_enter(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    if len(text) < 4:
        await vk.send(uid, "⚠️ Пароль от 4 символов.")
        return True
    user["web_password"] = hashlib.sha256(text.strip().encode()).hexdigest()
    save_user(user)
    clear_state(user)
    if MINIAPP_URL:
        await vk.send(uid, f"✅ Пароль веб-облака сохранён.\n\n"
                           f"☁️ Веб-облако: {MINIAPP_URL}\n"
                           f"Вход: ваш VK ID ({uid}) + пароль.")
    else:
        await vk.send(uid, "✅ Пароль сохранён (мини-апп ещё не подключён).")
    return await send_main_menu(user)


async def _set_hhmm_setting(user: dict, text: str, key: str, label: str) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    hm = parse_hhmm(text)
    if not hm:
        await vk.send(uid, "⚠️ Введите время в формате ЧЧ:ММ (например: 08:30).")
        return True
    user[key] = f"{hm[0]:02d}:{hm[1]:02d}"
    save_user(user)
    clear_state(user)
    await vk.send(uid, f"✅ {label} уведомления: {user[key]}")
    return await send_main_menu(user)


async def _set_text_setting(user: dict, text: str, key: str, label: str) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    if not text.strip() or len(text) > 200:
        await vk.send(uid, "⚠️ Текст от 1 до 200 символов.")
        return True
    user[key] = clean_forbidden(text.strip())
    save_user(user)
    clear_state(user)
    await vk.send(uid, f"✅ {label} сообщение обновлено:\n«{user[key]}»")
    return await send_main_menu(user)


async def dnd_start(user: dict) -> None:
    uid = int(user["user_id"])
    active = user.get("dnd_until")
    if active:
        try:
            until = datetime.strptime(active, "%Y-%m-%d %H:%M")
            if until > now():
                return await vk.send(uid,
                    f"🌙 Режим «Не беспокоить» активен до {until.strftime('%H:%M %d.%m')}.",
                    keyboard=kb_inline([
                        [("🌅 Выключить", {"c": "dnd_off"}, "positive")],
                        [("⬅️ Назад в меню", {"c": "back_menu"})]]))
        except ValueError:
            pass
    await vk.send(uid,
        "🌙 «Не беспокоить»: на время выключает личные уведомления "
        "(утро/вечер/погода/праздники). На сколько включить?",
        keyboard=kb_inline([
            [("1 час", {"c": "dnd_on", "h": 1}),
             ("3 часа", {"c": "dnd_on", "h": 3}),
             ("8 часов", {"c": "dnd_on", "h": 8})],
            [("до утра", {"c": "dnd_on", "h": -1})],
            [("⬅️ Назад в меню", {"c": "back_menu"})]]))
    clear_state(user)


async def sick_start(user: dict) -> None:
    uid = int(user["user_id"])
    await vk.send(uid,
        "🤒 Режим «Я болел(а)»: отмечает период болезни и предупреждает админов класса.\n\n"
        "С какого числа вы болели? (например: «вчера», «с 20.10»)",
        keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
    set_state(user, ST_SICK_WAIT_FROM)


# ==================================
# === ЛИЧНЫЕ КНОПКИ ===
# ==================================

def _save_personal_buttons(uid, buttons: list) -> None:
    data = load_data(PERSONAL_BUTTONS_FILE, {}, expect="dict")
    data[str(uid)] = buttons
    save_data(PERSONAL_BUTTONS_FILE, data)


async def personal_buttons_menu(user: dict) -> None:
    uid = int(user["user_id"])
    btns = get_personal_buttons(uid)
    max_btns = int(user.get("max_personal_buttons", 1)) + int(user.get("prepaid_buttons", 0))
    text = (f"🌟 Мои кнопки ({len(btns)}/{max_btns} доступно)\n\n"
            "Личные кнопки появляются в вашем главном меню. "
            "Можно сохранить текст (конспект, пароль, шпаргалку) или ссылку.")
    kb = []
    if btns:
        rows = [(f"🗑 {b.get('name')}", {"c": "pb_del", "n": b.get("name")}, "negative")
                for b in btns]
        kb.extend(layout_rows(rows, 2))
    if len(btns) < max_btns:
        price = get_price("button_base") + get_price("button_increment") * len(btns)
        kb.append([(f"➕ Создать кнопку ({price}⭐)" if price > 0 else "➕ Создать кнопку",
                    {"c": "pb_create"}, "positive")])
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    await vk.send(uid, text, keyboard=kb_inline(kb))
    clear_state(user)


@state_handler(ST_CREATE_PB_NAME)
async def pb_create_name(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    name = text.strip()
    if len(name) < 2 or len(name) > 40:
        await vk.send(uid, "⚠️ Название: от 2 до 40 символов.")
        return True
    if name in get_all_user_button_names(uid):
        await vk.send(uid, "⚠️ Кнопка с таким названием уже есть.")
        return True
    await vk.send(uid, "📝 Теперь содержимое кнопки — текст или ссылку (https://…):")
    set_state(user, ST_CREATE_PB_CONTENT, {"name": name})
    return True


@state_handler(ST_CREATE_PB_CONTENT)
async def pb_create_content(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    content = text.strip()
    if not content or len(content) > 2000:
        await vk.send(uid, "⚠️ Содержимое: от 1 до 2000 символов.")
        return True
    name = user["fsm"].get("name")
    if not name:
        clear_state(user)
        await vk.send(uid, "⚠️ Название потерялось, начните заново.")
        return True
    btns = get_personal_buttons(uid)
    btns.append({"name": name, "content": clean_forbidden(content),
                 "type": "url" if content.startswith("http") else "text",
                 "ts": now().strftime("%Y-%m-%d %H:%M")})
    _save_personal_buttons(uid, btns)
    # Списываем предоплаченную кнопку, если есть
    if int(user.get("prepaid_buttons", 0)) > 0:
        user["prepaid_buttons"] = int(user.get("prepaid_buttons", 0)) - 1
    user["custom_buttons_created"] = int(user.get("custom_buttons_created", 0)) + 1
    save_user(user)
    clear_state(user)
    await vk.send(uid, f"✅ Кнопка «{name}» создана! Она появилась в главном меню.")
    return await send_main_menu(user)


# ==================================
# === ЗВЁЗДЫ (внутренняя валюта) ===
# ==================================

def stars_spend(user: dict, amount: int, reason: str) -> bool:
    """Списание звёзд. True — если хватило."""
    if int(user.get("stars_balance", 0)) < amount:
        return False
    user["stars_balance"] = int(user.get("stars_balance", 0)) - amount
    user["total_stars_spent"] = int(user.get("total_stars_spent", 0)) + amount
    user["spend_map"] = user.get("spend_map") or {}
    user["spend_map"][reason] = int(user["spend_map"].get(reason, 0)) + amount
    save_user(user)
    stats = load_data(STARS_STATS_FILE, {}, expect="dict")
    stats["total_spent"] = int(stats.get("total_spent", 0)) + amount
    save_data(STARS_STATS_FILE, stats)
    return True


def stars_earn(user_id, amount: int, reason: str = "") -> None:
    u = get_user(user_id)
    u["stars_balance"] = int(u.get("stars_balance", 0)) + int(amount)
    save_user(u)


async def stars_menu(user: dict) -> None:
    uid = int(user["user_id"])
    balance = int(user.get("stars_balance", 0))
    ref_link = f"https://vk.me/club{vk.group_id}?ref=ref_{uid}"
    referrals = int(user.get("referrals_count", 0))
    text = (
        f"⭐ Звёзды DEVORKS+\n\n"
        f"💎 Баланс: {balance} ⭐\n"
        f"👥 Приглашено друзей: {referrals} (по +{REFERRAL_REWARD_STARS} ⭐ за каждого)\n\n"
        f"🔗 Ваша ссылка для друзей:\n{ref_link}\n\n"
        f"⭐ — внутренняя валюта бота. Тратится на:\n"
        f"• личные кнопки — от {get_price('button_base')} ⭐\n"
        f"• 👁 отправитель анонимки — {get_price('view_sender')} ⭐\n"
        f"• 💾 хранение анонимок — {get_price('anon_keep_month')} ⭐/мес"
    )
    kb = [
        [("👥 Профиль", {"c": "show_profile"}),
         ("🎁 Мои рефералы", {"c": "show_referrals"})],
        [("⬅️ Назад в меню", {"c": "back_menu"})],
    ]
    await vk.send(uid, text, keyboard=kb_inline(kb))
    clear_state(user)

# ==================================
# === ОБЛАКО (файлы) ===
# ==================================

CLOUD_MAX_TOTAL = 200 * 1024 * 1024  # 200 МБ на пользователя


def _cloud_index_file() -> str:
    return _data_file("cloud_files.json")


def cloud_index() -> dict:
    return load_data(_cloud_index_file(), {}, expect="dict")


def cloud_save_index(data: dict) -> None:
    save_data(_cloud_index_file(), data)


def cloud_user_files(uid) -> list:
    return cloud_index().get(str(uid)) or []


def cloud_add_file(uid, name: str, data: bytes, kind: str = "document",
                   src: str = "web", vault: bool = False) -> dict:
    """Сохраняет файл в облако. Возвращает запись файла."""
    limit = get_price("cloud_max_files")
    files = cloud_user_files(uid)
    if limit >= 0 and len(files) >= limit:
        raise VKApiError(-100, "limit")
    fid = secrets.token_hex(6)
    user_dir = os.path.join(CLOUD_DIR, str(uid))
    os.makedirs(user_dir, exist_ok=True)
    safe_name = clean_forbidden(os.path.basename(name) or "file")[:120]
    path = os.path.join(user_dir, f"{fid}__{safe_name}")
    with open(path, "wb") as f:
        f.write(data)
    rec = {
        "id": fid, "name": safe_name, "kind": kind,
        "size": len(data), "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
        "src": src, "vault": vault, "path": path,
    }
    idx = cloud_index()
    idx.setdefault(str(uid), []).append(rec)
    cloud_save_index(idx)
    return rec


def cloud_get_file(uid, fid) -> dict:
    for f in cloud_user_files(uid):
        if f.get("id") == fid:
            return f
    return {}


def cloud_delete_file(uid, fid) -> bool:
    idx = cloud_index()
    lst = idx.get(str(uid)) or []
    for i, f in enumerate(lst):
        if f.get("id") == fid:
            try:
                if f.get("path") and os.path.exists(f["path"]):
                    os.remove(f["path"])
            except OSError:
                pass
            lst.pop(i)
            idx[str(uid)] = lst
            cloud_save_index(idx)
            return True
    return False


def cloud_rename_file(uid, fid, new_name: str) -> bool:
    idx = cloud_index()
    lst = idx.get(str(uid)) or []
    for f in lst:
        if f.get("id") == fid:
            new_name = clean_forbidden(os.path.basename(new_name or ""))[:120]
            if not new_name:
                return False
            f["name"] = new_name
            cloud_save_index(idx)
            return True
    return False


def cloud_total_size(uid) -> int:
    return sum(int(f.get("size", 0)) for f in cloud_user_files(uid))


async def cloud_menu(user: dict) -> None:
    uid = int(user["user_id"])
    files = cloud_user_files(uid)
    total = cloud_total_size(uid)
    vault_cnt = len(vault_files_list(uid))
    vault_size = vault_total_size(uid)
    text = (
        f"☁️ Облако DEVORKS+\n\n"
        f"📁 Файлов: {len(files)} ({_fmt_size(total)} из {_fmt_size(CLOUD_MAX_TOTAL)})\n"
        f"🔐 В сейфе: {vault_cnt} файлов ({_fmt_size(vault_size)}) — зашифрованы\n\n"
        "📤 Отправьте мне фото/файл прямо в чат — предложу сохранить в облако.\n"
    )
    if MINIAPP_URL:
        text += f"🌐 Веб-облако: {MINIAPP_URL}\n"
    kb = [
        [("📁 Мои файлы", {"c": "cloud_list", "p": 0})],
        [("🔐 Сейф", {"c": "menu", "b": "🔐 Сейф"}, "positive")],
    ]
    if MINIAPP_URL:
        kb.append([("🌐 Открыть веб-облако", {"c": "miniapp_open"})])
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    await vk.send(uid, text, keyboard=kb_inline(kb))
    clear_state(user)


def _fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} Б"
        n /= 1024
    return f"{n:.1f} ГБ"


async def cloud_list_page(user: dict, page: int = 0) -> None:
    uid = int(user["user_id"])
    files = list(reversed(cloud_user_files(uid)))
    if not files:
        return await vk.send(uid, "☁️ Облако пусто. Отправьте файл в чат, чтобы сохранить.",
                             keyboard=kb_inline([[("⬅️ Назад в меню", {"c": "back_menu"})]]))
    per_page = 8
    page = max(0, min(page, (len(files) - 1) // per_page))
    chunk = files[page * per_page:(page + 1) * per_page]
    lines = [f"☁️ Файлы (стр. {page + 1}/{(len(files) - 1) // per_page + 1}):"]
    kb = []
    for f in chunk:
        lines.append(f"• {f['name']} ({_fmt_size(f.get('size'))})")
        kb.append([(f"📥 {f['name'][:40]}", {"c": "cloud_dl", "i": f["id"]}),
                   ("🗑", {"c": "cloud_rm", "i": f["id"]}, "negative")])
    nav = []
    if page > 0:
        nav.append(("◀️", {"c": "cloud_list", "p": page - 1}))
    if (page + 1) * per_page < len(files):
        nav.append(("▶️", {"c": "cloud_list", "p": page + 1}))
    if nav:
        kb.append(nav)
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    await vk.send(uid, "\n".join(lines), keyboard=kb_inline(kb))
    clear_state(user)


async def cloud_send_file(user: dict, fid: str) -> None:
    uid = int(user["user_id"])
    f = cloud_get_file(uid, fid)
    if not f:
        return await vk.send(uid, "⚠️ Файл не найден.", keyboard=back_kb())
    try:
        with open(f["path"], "rb") as fh:
            data = fh.read()
        await vk.send_file(uid, data, f["name"])
    except FileNotFoundError:
        await vk.send(uid, "⚠️ Файл повреждён или удалён с диска.")
    except Exception as e:
        logger.error("cloud_send_file: %s", e)
        await vk.send(uid, "⚠️ Не удалось отправить файл.")


# ==================================
# === СЕЙФ (нулевое разглашение) ===
# ==================================

VAULT_HEADER = b"DVF2"  # формат контейнера DEVORKS Vault File v2


def _vault_index_file() -> str:
    return _data_file("vault_files.json")


def vault_index() -> dict:
    return load_data(_vault_index_file(), {}, expect="dict")


def vault_save_index(data: dict) -> None:
    save_data(_vault_index_file(), data)


def vault_user_index(uid):
    return vault_index().get(str(uid)) or {}


def vault_files_list(uid) -> list:
    return vault_user_index(uid).get("files") or []


def vault_total_size(uid) -> int:
    return sum(int(f.get("size", 0)) for f in vault_files_list(uid))


def vault_derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256, 600 000 итераций → 32 байта."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        iterations=600000, backend=default_backend(),
    )
    return kdf.derive(password.encode("utf-8"))


def vault_encrypt_blob(key: bytes, payload: bytes) -> bytes:
    """DVF2: header + salt(16) + nonce(12) + ct."""
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, payload, None)
    return VAULT_HEADER + nonce + ct


def vault_decrypt_blob(blob: bytes, key: bytes) -> bytes:
    if len(blob) < len(VAULT_HEADER) + 12 or not blob.startswith(VAULT_HEADER):
        raise ValueError("bad_container")
    nonce = blob[len(VAULT_HEADER):len(VAULT_HEADER) + 12]
    ct = blob[len(VAULT_HEADER) + 12:]
    return AESGCM(key).decrypt(nonce, ct, None)


def vault_have_crypto() -> bool:
    return HAVE_CRYPTO


async def vault_menu(user: dict) -> None:
    uid = int(user["user_id"])
    if not HAVE_CRYPTO:
        return await vk.send(uid,
            "🔐 Сейф требует библиотеку шифрования. Хостеру нужно выполнить:\n"
            "pip install cryptography\n\n"
            "Пока Сейф недоступен, ☁️ Облако работает как обычно.")
    files = vault_files_list(uid)
    size = vault_total_size(uid)
    set_up = bool(vault_user_index(uid).get("salt"))
    text = (
        f"🔐 Сейф — зашифрованное хранилище файлов.\n\n"
        f"{'✅ Пароль установлен' if set_up else '⚠️ Пароль ещё не задан'}\n"
        f"📁 Зашифровано: {len(files)} файлов ({_fmt_size(size)})\n\n"
        "Как это работает: файл шифруется AES-256 прямо на сервере ВАШИМ "
        "паролем (PBKDF2, 600 000 итераций). Пароль нигде не сохраняется: "
        "даже разработчик не сможет прочитать файлы. ЗАБУдете пароль — файлы "
        "НЕВОССТАНОВИМЫ, это цена полной приватности."
    )
    kb = []
    if not set_up:
        kb.append([("🔑 Задать пароль и начать", {"c": "vault_setpass"}, "positive")])
    else:
        kb.append([("📥 Положить файл", {"c": "vault_put"}, "positive")])
        kb.append([("📤 Достать файл", {"c": "vault_get"})])
        kb.append([("🔑 Сменить пароль", {"c": "vault_chpass"})])
        kb.append([("🗑 Удалить файлы", {"c": "vault_clear"}, "negative")])
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    await vk.send(uid, text, keyboard=kb_inline(kb))
    clear_state(user)


def vault_check_password(uid, password: str):
    """Ключ + проверка по контрольному блобу. (key, ok)."""
    idx = vault_user_index(uid)
    salt_b64 = idx.get("salt")
    check = idx.get("check")
    if not salt_b64 or not check:
        return None, False
    salt = base64.b64decode(salt_b64)
    key = vault_derive_key(password, salt)
    try:
        blob = base64.b64decode(check)
        vault_decrypt_blob(blob, key)
        return key, True
    except Exception:
        return key, False


def vault_set_password(user: dict, password: str) -> bool:
    """Устанавливает пароль сейфа (только при создании). True — успех."""
    uid = user["user_id"]
    old_idx = vault_user_index(uid)
    if old_idx.get("salt"):
        # сейф уже создан — смена только через «🔑 Сменить пароль»
        return False
    salt = secrets.token_bytes(16)
    key = vault_derive_key(password, salt)
    check = base64.b64encode(vault_encrypt_blob(key, b"devorks-check")).decode()
    idx = vault_index()
    idx[str(uid)] = {
        "salt": base64.b64encode(salt).decode(),
        "check": check,
        "files": [],
    }
    vault_save_index(idx)
    return True


@state_handler(ST_VAULT_PASSWORD)
async def vault_password_state(user: dict, text: str, message: dict) -> bool:
    """Ожидание пароля (создание сейфа)."""
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    pw = text.strip()
    if len(pw) < 6:
        await vk.send(uid, "⚠️ Пароль от 6 символов. Безопасность превыше всего.")
        return True
    if not vault_set_password(user, pw):
        clear_state(user)
        await vk.send(uid, "⚠️ Сейф уже создан. Используйте «🔑 Сменить пароль».")
        return True
    clear_state(user)
    await vk.send(uid, "✅ Пароль сейфа установлен! Запомните его — без пароля файлы "
                       "невозможно восстановить.\n\nТеперь можно 📥 Положить файл: "
                       "пришлите файл, а пароль бот спросит отдельно.")
    return await vault_menu(user)


async def vault_put_wait_file(user: dict) -> None:
    uid = int(user["user_id"])
    await vk.send(uid,
        "📥 Пришлите файл (фото/документ) для шифрования.\n"
        "⚠️ После шифрования оригинал из облака удалю — останется только зашифрованная копия.",
        keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
    set_state(user, ST_VAULT_PUT_WAIT)


@state_handler(ST_VAULT_PUT_WAIT)
async def vault_put_file(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    if not message.get("attachments"):
        await vk.send(uid, "⚠️ Не вижу файла. Пришлите фото или документ.")
        return True
    await vk.send(uid, "🔑 Теперь введите пароль сейфа для шифрования "
                       "(или «отмена»):")
    set_state(user, ST_VAULT_GET_PASSWORD, {"put": True})
    user["fsm"]["put_atts"] = [a for a in message["attachments"][:3]]
    save_user(user)
    return True


@state_handler(ST_VAULT_GET_PASSWORD)
async def vault_password_enter(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    password = text.strip()
    key, ok = vault_check_password(uid, password)
    if not ok:
        await vk.send(uid, "❌ Неверный пароль. Попробуйте ещё раз.")
        return True
    atts = user["fsm"].get("put_atts")
    if atts:
        # Режим «положить»: шифруем файлы из вложений
        saved = 0
        for att in atts:
            data, name, kind = await vk.download_attachment(att)
            if not data:
                continue
            payload = json.dumps({
                "name": name, "kind": kind,
                "data": base64.b64encode(data).decode(),
                "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False).encode()
            blob = vault_encrypt_blob(key, payload)
            fid = secrets.token_hex(6)
            user_dir = os.path.join(VAULT_DIR, str(uid))
            os.makedirs(user_dir, exist_ok=True)
            with open(os.path.join(user_dir, fid + ".dvf"), "wb") as f:
                f.write(blob)
            idx = vault_index()
            rec = idx.setdefault(str(uid), {})
            rec.setdefault("files", []).append({
                "id": fid, "size": len(data), "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            vault_save_index(idx)
            saved += 1
        clear_state(user)
        if saved:
            await vk.send(uid, f"🔐 Зашифровано файлов: {saved}. Имена и содержимое "
                               f"видны только с паролем.")
        else:
            await vk.send(uid, "⚠️ Не удалось скачать файлы. Попробуйте ещё раз.")
        return await vault_menu(user)
    # Режим «достать»: показать список
    return await vault_list_after_unlock(user, key)


async def vault_get_flow(user: dict) -> None:
    uid = int(user["user_id"])
    files = vault_files_list(uid)
    if not files:
        return await vk.send(uid, "🔐 Сейф пуст.", keyboard=back_kb())
    await vk.send(uid, "🔑 Введите пароль сейфа (или «отмена»):",
                  keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
    set_state(user, ST_VAULT_GET_PASSWORD, {"put": False})


async def vault_list_after_unlock(user: dict, key: bytes) -> bool:
    uid = int(user["user_id"])
    files = vault_files_list(uid)
    kb = []
    lines = ["🔓 Сейф открыт. Файлы:"]
    for f in files[:10]:
        name = "файл"
        try:
            blob_path = os.path.join(VAULT_DIR, str(uid), f["id"] + ".dvf")
            with open(blob_path, "rb") as fh:
                payload = json.loads(vault_decrypt_blob(fh.read(), key))
            name = payload.get("name", "файл")
        except Exception:
            name = "⚠️ повреждён"
        lines.append(f"• {name} ({_fmt_size(f.get('size'))})")
        kb.append([(f"📤 {name[:40]}", {"c": "vault_out", "i": f["id"]}, "positive")])
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    clear_state(user)
    await vk.send(uid, "\n".join(lines), keyboard=kb_inline(kb))
    return True


async def vault_extract_file(user: dict, fid: str, password: str) -> None:
    uid = int(user["user_id"])
    key, ok = vault_check_password(uid, password)
    if not ok:
        return await vk.send(uid, "❌ Неверный пароль.")
    blob_path = os.path.join(VAULT_DIR, str(uid), fid + ".dvf")
    try:
        with open(blob_path, "rb") as fh:
            payload = json.loads(vault_decrypt_blob(fh.read(), key))
    except FileNotFoundError:
        return await vk.send(uid, "⚠️ Файл не найден на диске.")
    except Exception:
        return await vk.send(uid, "⚠️ Не удалось расшифровать файл.")
    data = base64.b64decode(payload.get("data", ""))
    await vk.send_file(int(uid), data, payload.get("name", "file"))


async def vault_chpass_flow(user: dict) -> None:
    uid = int(user["user_id"])
    await vk.send(uid, "🔑 Введите СТАРЫЙ пароль сейфа:")
    set_state(user, ST_VAULT_CHPASS_NEW, {"step": "old"})


@state_handler(ST_VAULT_CHPASS_NEW)
async def vault_chpass_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    step = user["fsm"].get("step", "old")
    if step == "old":
        key, ok = vault_check_password(uid, text.strip())
        if not ok:
            await vk.send(uid, "❌ Неверный пароль. Попробуйте снова:")
            return True
        user["fsm"] = {"step": "new", "old_key": base64.b64encode(key).decode()}
        save_user(user)
        await vk.send(uid, "🔑 Введите НОВЫЙ пароль (от 6 символов):")
        return True
    # step == new
    new_pw = text.strip()
    if len(new_pw) < 6:
        await vk.send(uid, "⚠️ Пароль от 6 символов:")
        return True
    old_key = base64.b64decode(user["fsm"].get("old_key", ""))
    # перешифровываем все файлы
    new_salt = secrets.token_bytes(16)
    new_key = vault_derive_key(new_pw, new_salt)
    idx = vault_index()
    rec = idx.get(str(uid)) or {}
    reencrypted = 0
    for f in (rec.get("files") or []):
        try:
            blob_path = os.path.join(VAULT_DIR, str(uid), f["id"] + ".dvf")
            with open(blob_path, "rb") as fh:
                payload = vault_decrypt_blob(fh.read(), old_key)
            with open(blob_path, "wb") as fh:
                fh.write(vault_encrypt_blob(new_key, payload))
            reencrypted += 1
        except Exception as e:
            logger.error("vault reencrypt %s: %s", f.get("id"), e)
    rec["salt"] = base64.b64encode(new_salt).decode()
    rec["check"] = base64.b64encode(vault_encrypt_blob(new_key, b"devorks-check")).decode()
    idx[str(uid)] = rec
    vault_save_index(idx)
    clear_state(user)
    await vk.send(uid, f"🔑 Пароль сменён, перешифровано файлов: {reencrypted}.")
    return await vault_menu(user)

# ==================================
# === ДНИ РОЖДЕНИЯ ===
# ==================================

def days_to_birthday(birthday_iso: str, today: date) -> int:
    b = _iso_date(birthday_iso)
    if not b:
        return -1
    this_year = date(today.year, b.month, b.day)
    if this_year < today:
        this_year = date(today.year + 1, b.month, b.day)
    return (this_year - today).days


async def birthdays_show(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    lines = ["🎂 Дни рождения:"]
    my_b = user.get("birthday")
    if my_b:
        d = days_to_birthday(my_b, now().date())
        if d == 0:
            lines.append("\n🎉 У ВАС СЕГОДНЯ ДЕНЬ РОЖДЕНИЯ! С ПРАЗДНИКОМ! 🎉")
        else:
            lines.append(f"\n🎂 Ваш ДР через {d} дн.")
    if cls:
        lines.append("\n👥 Класс:")
        found = False
        entries = []
        for m in cls.get("members", []):
            if str(m) == str(uid):
                continue
            mu = get_user(m)
            mb = mu.get("birthday")
            if mb and mu.get("show_birthday_to_class", True):
                d = days_to_birthday(mb, now().date())
                entries.append((d, m, mb))
        for d, m, mb in sorted(entries):
            if d < 0:
                continue
            name = await vk.user_name(m)
            if d == 0:
                lines.append(f"🎉 {name} — СЕГОДНЯ! 🎉")
            else:
                lines.append(f"• {name} — через {d} дн. ({fmt_date(_iso_date(mb))})")
            found = True
        if not found:
            lines.append("— одноклассники ещё не указали ДР (⚙️ Настройки → 🎂)")
    await vk.send(uid, "\n".join(lines), keyboard=back_kb())
    clear_state(user)


# ==================================
# === ДЕЖУРНЫЕ ===
# ==================================

async def duty_menu(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not cls:
        return await vk.send(uid, "🎓 Дежурные работают внутри класса.", keyboard=back_kb())
    duty = load_data(DUTY_FILE, {}, expect="dict").get(cls["class_code"]) or {}
    if not duty:
        return await vk.send(uid,
            "🕐 График дежурств пуст.\n"
            "Админ может прислать список дежурных фразой:\n"
            "«Дежурные: Петя и Маша в понедельник, Вася во вторник»\n"
            "(или «по одному по списку класса» — я распределю сам).",
            keyboard=back_kb())
    text = "🕐 График дежурств:\n\n" + duty_render_text(duty)
    await vk.send(uid, text, keyboard=back_kb())
    clear_state(user)


def duty_render_text(duty: dict) -> str:
    """{'mode': 'manual'|'list', 'days': {'пн': 'Петя, Маша', ...}}"""
    days = duty.get("days") or {}
    if not days:
        return "— пусто —"
    lines = []
    for wd in WEEKDAY_SHORT:
        if days.get(wd):
            lines.append(f"{WEEKDAY_NAMES[WEEKDAY_SHORT.index(wd)]}: {days[wd]}")
    return "\n".join(lines) if lines else "— пусто —"


async def duty_set_by_ai(user: dict, text: str) -> None:
    """Установка дежурных из текста (админ)."""
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        return await vk.send(uid, "⛔ Только админ класса.")
    days = {}
    # Похожий на AI парсинг: ищем день недели и имена после него
    cur_day = None
    for line in text.split("\n"):
        low = line.lower()
        found_day = None
        for i, wd in enumerate(WEEKDAY_SHORT):
            if wd in low or WEEKDAY_NAMES[i].lower() in low:
                found_day = wd
                break
        if found_day:
            cur_day = found_day
            rest = line.split(":", 1)[-1] if ":" in line else ""
            names = rest.strip(" .,;-") or line.split(":", 1)[-1].strip() if ":" in line else ""
            if ":" in line:
                names = line.split(":", 1)[1].strip()
            if names:
                days[cur_day] = names[:200]
        elif cur_day and line.strip():
            days[cur_day] = line.strip()[:200]
    if not days:
        # «по одному по списку класса»
        members = [await vk.user_name(m) for m in cls.get("members", [])]
        members = [m for m in members if m]
        if members and ("по одному" in text.lower() or "по списку" in text.lower() or "по очереди" in text.lower()):
            weekdays = ["пн", "вт", "ср", "чт", "пт"]
            for i, name in enumerate(members[:len(weekdays)]):
                days[weekdays[i]] = name
    if not days:
        await vk.send(uid, "⚠️ Не понял список. Пример:\n«Дежурные:\nпонедельник: Петя, Маша\nвторник: Вася»")
        return
    data = load_data(DUTY_FILE, {}, expect="dict")
    data[cls["class_code"]] = {"mode": "manual", "days": days,
                               "ts": now().strftime("%Y-%m-%d %H:%M")}
    save_data(DUTY_FILE, data)
    log_admin_action(uid, "duty_set", str(list(days.keys())))
    await vk.send(uid, "✅ График дежурств обновлён:\n\n" + duty_render_text({"days": days}))
    await notify_class(cls, str(uid), "🕐 Обновлён график дежурств — проверьте в «🕐 Дежурные»!")


# ==================================
# === ❗ ВАЖНОЕ (объявление класса) ===
# ==================================

async def vazhno_show(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not cls:
        return await vk.send(uid, "🎓 «Важное» — объявление вашего класса.", keyboard=back_kb())
    imp = cls.get("important")
    kb = []
    if is_class_admin(uid, cls):
        kb.append([("✏️ Изменить", {"c": "vazhno_edit"})])
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    if not imp:
        return await vk.send(uid, "❗ Важного объявления нет." if not is_class_admin(uid, cls) else
                             "❗ Важного объявления нет. Нажмите «✏️ Изменить», чтобы добавить.",
                             keyboard=kb_inline(kb))
    await vk.send(uid, f"❗ ВАЖНОЕ:\n\n{imp}", keyboard=kb_inline(kb))
    clear_state(user)


@state_handler(ST_VAZHNO_WAIT)
async def vazhno_edit_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        clear_state(user)
        await vk.send(uid, "⛔ Только админ класса.")
        return True
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    if text.lower().strip() in ("удалить", "снять", "очистить"):
        cls["important"] = None
        save_class(cls)
        clear_state(user)
        await vk.send(uid, "✅ Объявление снято.")
        return await send_main_menu(user)
    cls["important"] = clean_forbidden(text.strip())[:1000]
    save_class(cls)
    log_admin_action(uid, "vazhno_set", "")
    clear_state(user)
    await vk.send(uid, "✅ Объявление «❗ Важное» обновлено.")
    await notify_class(cls, str(uid), f"❗ ВАЖНОЕ ОБЪЯВЛЕНИЕ:\n\n{cls['important']}")
    return await send_main_menu(user)


# ==================================
# === РЕШЕНИЯ (база класса) ===
# ==================================

def _load_solutions() -> dict:
    return load_data(SOLUTIONS_FILE, {}, expect="dict")


def _save_solutions(data: dict) -> None:
    save_data(SOLUTIONS_FILE, data)


async def solutions_menu(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not cls:
        return await vk.send(uid, "🎓 Решения — общая база класса.", keyboard=back_kb())
    code = cls["class_code"]
    sols = _load_solutions().get(code) or []
    kb = [[("➕ Добавить решение", {"c": "sol_add"}, "positive")]]
    if sols:
        rows = []
        for s in list(reversed(sols))[:8]:
            label = f"📚 {s.get('subject', '?')}: {s.get('title', '')[:30]}"
            rows.append((label, {"c": "sol_view", "i": s["id"]}))
        kb.extend(layout_rows(rows, 1))
    kb.append([("⬅️ Назад в меню", {"c": "back_menu"})])
    text = f"📚 Решения класса «{cls['class_name']}» ({len(sols)} шт.)"
    await vk.send(uid, text, keyboard=kb_inline(kb))
    clear_state(user)


@state_handler(ST_SOL_WAIT_FILE)
async def sol_add_wait(user: dict, text: str, message: dict) -> bool:
    """Шаг 2 добавления решения: ждём файл."""
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    cls = get_class_by_user(uid)
    if not cls:
        clear_state(user)
        return True
    atts = message.get("attachments") or []
    if not atts:
        await vk.send(uid, "⚠️ Пришлите файл (фото решения/документ) или «отмена».")
        return True
    subject = user["fsm"].get("subject", "?")
    title = user["fsm"].get("title", "")
    # Сохраняем файл в папку решений
    data, name, kind = await vk.download_attachment(atts[0])
    if not data:
        await vk.send(uid, "⚠️ Не удалось скачать файл. Попробуйте ещё раз.")
        return True
    sol_id = secrets.token_hex(5)
    sol_dir = os.path.join(SOLUTIONS_DIR, cls["class_code"])
    os.makedirs(sol_dir, exist_ok=True)
    fname = f"{sol_id}__{clean_forbidden(name or 'file')[:100]}"
    with open(os.path.join(sol_dir, fname), "wb") as f:
        f.write(data)
    sols = _load_solutions()
    sols.setdefault(cls["class_code"], []).append({
        "id": sol_id, "subject": subject, "title": title,
        "by": str(uid), "anon": bool(user["fsm"].get("anon")),
        "file": fname, "kind": kind, "size": len(data),
        "ts": now().strftime("%Y-%m-%d %H:%M"),
        "comments": [],
    })
    _save_solutions(sols)
    clear_state(user)
    await vk.send(uid, f"✅ Решение по «{subject}» добавлено в базу класса!")
    return await solutions_menu(user)


async def solution_view(user: dict, sol_id: str) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not cls:
        return
    s = None
    for x in (_load_solutions().get(cls["class_code"]) or []):
        if x.get("id") == sol_id:
            s = x
            break
    if not s:
        return await vk.send(uid, "⚠️ Решение не найдено.", keyboard=back_kb())
    author = "анонимно" if s.get("anon") else await vk.user_name(s.get("by"))
    kb = [
        [("📥 Скачать", {"c": "sol_dl", "i": s["id"]}, "positive")],
        [("💬 Комментарий", {"c": "sol_comment", "i": s["id"]})],
    ]
    if str(uid) == str(s.get("by")) or is_class_admin(uid, cls):
        kb.append([("🗑 Удалить", {"c": "sol_del", "i": s["id"]}, "negative")])
    kb.append([("⬅️ Назад", {"c": "menu", "b": "📚 Решения"})])
    text = (f"📚 {s.get('subject')}: {s.get('title', '')}\n"
            f"👤 {author} · 🕒 {s.get('ts')}\n"
            f"📁 {_fmt_size(s.get('size'))}")
    comments = s.get("comments") or []
    if comments:
        text += "\n\n💬 Комментарии:\n" + "\n".join(
            f"• {c.get('text', '')[:150]}" for c in comments[-5:])
    await vk.send(uid, text, keyboard=kb_inline(kb))
    clear_state(user)


# ==================================
# === ОПРОСЫ (кнопочные) ===
# ==================================

def _load_polls() -> dict:
    return load_data(POLLS_FILE, {}, expect="dict")


def _save_polls(data: dict) -> None:
    save_data(POLLS_FILE, data)


async def poll_create(user: dict, question: str, options: list, anonymous=True) -> None:
    """Создаёт опрос класса (кнопочный)."""
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        return await vk.send(uid, "⛔ Только админ класса.")
    poll_id = secrets.token_hex(4)
    polls = _load_polls()
    polls[poll_id] = {
        "question": clean_forbidden(question)[:300],
        "options": [clean_forbidden(o)[:100] for o in options[:10]],
        "votes": {},        # {uid: option_index}
        "class": cls["class_code"],
        "by": str(uid),
        "ts": now().strftime("%Y-%m-%d %H:%M"),
        "anon": anonymous,
    }
    _save_polls(polls)
    kb = []
    for i, o in enumerate(polls[poll_id]["options"]):
        kb.append([(f"{i + 1}. {o}", {"c": "poll_vote", "p": poll_id, "o": i})])
    kb.append([("📊 Результаты", {"c": "poll_results", "p": poll_id})])
    text = f"📊 ОПРОС от {await vk.user_name(uid)}:\n\n{question}"
    await notify_class(cls, str(uid), text, attachments=None)
    # Отправляем кнопки отдельным сообщением каждому
    for m in cls.get("members", []):
        if str(m) != str(uid):
            await vk.send(int(m), "Голосуйте кнопкой ниже 👇", keyboard=kb_inline(kb))
    await vk.send(uid, f"✅ Опрос создан и разослан классу ({len(cls['members']) - 1} чел.).",
                  keyboard=kb_inline(kb))
    log_admin_action(uid, "poll_create", question[:60])


async def poll_results(user: dict, poll_id: str) -> None:
    uid = int(user["user_id"])
    poll = _load_polls().get(poll_id)
    if not poll:
        return await vk.send(uid, "⚠️ Опрос не найден.")
    votes = poll.get("votes") or {}
    counts = {i: 0 for i in range(len(poll["options"]))}
    for v in votes.values():
        if v in counts:
            counts[v] += 1
    total = sum(counts.values()) or 1
    lines = [f"📊 «{poll['question']}»", f"Проголосовало: {len(votes)}", ""]
    for i, o in enumerate(poll["options"]):
        pct = int(counts[i] * 100 / total)
        bar = "█" * (pct // 6)
        lines.append(f"{i + 1}. {o}\n   {bar} {pct}% ({counts[i]})")
    kb = []
    for i, o in enumerate(poll["options"]):
        kb.append([(f"{i + 1}. {o}", {"c": "poll_vote", "p": poll_id, "o": i})])
    kb.append([("📊 Обновить", {"c": "poll_results", "p": poll_id}),
               ("⬅️ Назад в меню", {"c": "back_menu"})])
    await vk.send(uid, "\n".join(lines), keyboard=kb_inline(kb))
    clear_state(user)


# ==================================
# === АДМИНСКАЯ ПАНЕЛЬ ===
# ==================================

async def admin_panel(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        return await vk.send(uid, "⛔ Панель доступна админам класса.")
    members = len(cls.get("members", []))
    text = (f"👨‍💼 Админская панель — класс «{cls.get('class_name')}»\n"
            f"👥 Участников: {members} · 🔑 Код: {cls['class_code']}")
    kb = [
        [("📅 Расписание", {"c": "adm_edit_schedule"}),
         ("🔔 Звонки", {"c": "bells_edit"})],
        [("👨‍🏫 Учителя", {"c": "teachers_edit"}),
         ("📝 ДЗ", {"c": "menu", "b": "➕ Добавить ДЗ"})],
        [("🎉 Каникулы", {"c": "holidays_set"}),
         ("❗ Важное", {"c": "vazhno_edit"})],
        [("📢 Написать классу", {"c": "menu", "b": "📢 Написать классу"}, "positive")],
        [("📊 Опрос классу", {"c": "adm_poll"}),
         ("🕐 Дежурные", {"c": "adm_duty"})],
        [("👥 Участники", {"c": "class_users"}),
         ("👑 Админы", {"c": "adm_manage_admins"})],
        [("🕒 Таймеры классу", {"c": "adm_class_timers"})],
        [("⬅️ Назад в меню", {"c": "back_menu"})],
    ]
    await vk.send(uid, text, keyboard=kb_inline(kb))
    clear_state(user)


async def admin_edit_schedule(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        return await vk.send(uid, "⛔ Только админ класса.")
    rows = []
    for wd in WEEKDAY_NAMES:
        rows.append((wd[:3], {"c": "adm_sched_day", "d": wd}))
    kb = layout_rows(rows, 4)
    kb.append([("⬅️ Назад", {"c": "menu", "b": "👨‍💼 Админская панель"})])
    await vk.send(uid,
                  "📅 Расписание класса. Выберите день недели:\n"
                  "(формат внутри: «1. Математика\n2. Физика»)",
                  keyboard=kb_inline(kb))
    clear_state(user)


@state_handler(ST_EDIT_SCHEDULE_CONTENT)
async def admin_set_schedule_content(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        clear_state(user)
        await vk.send(uid, "⛔ Только админ класса.")
        return True
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    wd = user["fsm"].get("day")
    if not wd or wd not in WEEKDAY_NAMES:
        clear_state(user)
        await vk.send(uid, "⚠️ День потерялся, начните заново.")
        return True
    if text.strip().lower() in ("нет", "-", "пусто", "удалить"):
        cls.setdefault("schedule", {})[wd] = ""
    else:
        cls.setdefault("schedule", {})[wd] = clean_forbidden(text.strip())[:1000]
    save_class(cls)
    log_admin_action(uid, "edit_schedule", wd)
    clear_state(user)
    await vk.send(uid, f"✅ Расписание на {wd} обновлено:\n\n{cls['schedule'][wd] or '— нет уроков —'}")
    await notify_class(cls, str(uid), f"📅 Обновлено расписание на {wd} — проверьте в «📅 Расписание»!")
    return await admin_panel(user)


@state_handler(ST_SEND_CLASS_MESSAGE)
async def admin_send_class_message(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        clear_state(user)
        await vk.send(uid, "⛔ Только админ класса.")
        return True
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    attachments = []
    for att in (message.get("attachments") or [])[:3]:
        t = att.get("type")
        if t == "photo":
            attachments.append(f"photo{att['photo']['owner_id']}_{att['photo']['id']}")
        elif t == "doc":
            attachments.append(f"doc{att['doc']['owner_id']}_{att['doc']['id']}")
    name = await vk.user_name(uid)
    sent = await notify_class(cls, uid, f"📢 Объявление от {name}:\n\n{clean_forbidden(text.strip())[:2000]}",
                              attachments=attachments or None)
    log_admin_action(uid, "class_message", f"{sent} получателей")
    clear_state(user)
    await vk.send(uid, f"✅ Объявление отправлено: {sent} чел.")
    return await send_main_menu(user)


@state_handler(ST_DUTY_WAIT)
async def admin_duty_set(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    return await duty_set_by_ai(user, text)


async def admin_manage_admins(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        return await vk.send(uid, "⛔ Только админ класса.")
    admins = [str(a) for a in cls.get("admins", [])]
    lines = ["👑 Админы класса:"]
    for a in admins:
        lines.append(f"• {await vk.user_name(a)}")
    kb = [[("➕ Назначить админа", {"c": "adm_add_admin"}, "positive")]]
    if len(admins) > 1:
        kb.append([("➖ Снять админа", {"c": "adm_del_admin"}, "negative")])
    kb.append([("⬅️ Назад", {"c": "menu", "b": "👨‍💼 Админская панель"})])
    await vk.send(uid, "\n".join(lines), keyboard=kb_inline(kb))
    clear_state(user)


async def admin_pick_member(user: dict, action: str) -> None:
    """Выбор участника для действия (админ/снять админа/исключить)."""
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        return await vk.send(uid, "⛔ Только админ класса.")
    members = [str(m) for m in cls.get("members", []) if str(m) != str(uid)]
    if not members:
        return await vk.send(uid, "👥 Кроме вас в классе никого нет.")
    rows = []
    for m in members[:20]:
        rows.append((f"👤 {await vk.user_name(m)}", {"c": action, "id": int(m)}))
    kb = layout_rows(rows, 2)
    kb.append([("⬅️ Назад", {"c": "menu", "b": "👨‍💼 Админская панель"})])
    await vk.send(uid, "👤 Выберите участника:", keyboard=kb_inline(kb))
    clear_state(user)


# ---------- Таймеры классу ----------

def _load_class_timers() -> dict:
    return load_data(CLASS_TIMERS_FILE, {}, expect="dict")


def _save_class_timers(data: dict) -> None:
    save_data(CLASS_TIMERS_FILE, data)


async def admin_class_timers(user: dict) -> None:
    uid = int(user["user_id"])
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        return await vk.send(uid, "⛔ Только админ класса.")
    timers = _load_class_timers().get(cls["class_code"]) or []
    lines = ["🕒 Таймеры сообщений классу:"]
    for t in timers:
        lines.append(f"• {t.get('when', '?')}: {t.get('text', '')[:40]}")
    if not timers:
        lines.append("— пока нет")
    kb = [
        [("➕ Новый таймер", {"c": "ct_add"}, "positive")],
    ]
    if timers:
        kb.append([("🗑 Удалить", {"c": "ct_del"}, "negative")])
    kb.append([("⬅️ Назад", {"c": "menu", "b": "👨‍💼 Админская панель"})])
    await vk.send(uid, "\n".join(lines), keyboard=kb_inline(kb))
    clear_state(user)


@state_handler(ST_CLASS_TIMER_INPUT)
async def class_timer_input(user: dict, text: str, message: dict) -> bool:
    """«дата время | текст» — простой формат таймера класса."""
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    cls = get_class_by_user(uid)
    if not (cls and is_class_admin(uid, cls)):
        clear_state(user)
        await vk.send(uid, "⛔ Только админ класса.")
        return True
    parts = text.split("|", 1)
    if len(parts) != 2:
        await vk.send(uid, "⚠️ Формат: «25.10.2026 09:00 | Текст сообщения» "
                           "(или «завтра 8:00 | Текст»)")
        return True
    when_part, msg = parts[0].strip(), parts[1].strip()
    d = None
    hm = None
    for part in when_part.lower().replace(",", " ").split():
        pd = parse_date_or_none(part)
        if pd and d is None:
            d = pd
        else:
            hm = hm or parse_hhmm(part)
    if not hm:
        await vk.send(uid, "⚠️ Не нашёл время. Пример: «завтра 09:00 | Собрание»")
        return True
    d = d or now().date()
    fire_at = datetime(d.year, d.month, d.day, hm[0], hm[1])
    if fire_at <= now():
        fire_at += timedelta(days=1)
    bad = reject_if_forbidden(msg)
    if bad:
        await vk.send(uid, bad)
        return True
    timers = _load_class_timers()
    timers.setdefault(cls["class_code"], []).append({
        "id": secrets.token_hex(4),
        "when": fire_at.strftime("%Y-%m-%d %H:%M"),
        "text": f"📢 {clean_forbidden(msg)[:800]}",
        "class": cls["class_code"],
        "repeat": None,
    })
    _save_class_timers(timers)
    clear_state(user)
    await vk.send(uid, f"✅ Таймер классу поставлен на {fire_at.strftime('%d.%m %H:%M')}.")
    return await admin_class_timers(user)


# ---------- Опрос ----------

@state_handler(ST_POLL_WAIT_Q)
async def poll_wait_question(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    await vk.send(uid, "📋 Теперь варианты ответа — каждый с новой строки (2–10 шт.):\n"
                       "Пример:\nДа\nНет\nВоздержусь")
    set_state(user, ST_POLL_WAIT_OPTS, {"q": text.strip()})
    return True


@state_handler(ST_POLL_WAIT_OPTS)
async def poll_wait_options(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    options = [o.strip() for o in text.split("\n") if o.strip()]
    if len(options) < 2 or len(options) > 10:
        await vk.send(uid, "⚠️ Нужно 2–10 вариантов, каждый с новой строки.")
        return True
    q = user["fsm"].get("q", "")
    clear_state(user)
    await poll_create(user, q, options)
    return await send_main_menu(user)

# ==================================
# === ПАНЕЛЬ РАЗРАБОТЧИКА ===
# ==================================

async def developer_panel(user: dict) -> None:
    uid = int(user["user_id"])
    if not is_dev(uid):
        return await vk.send(uid, "⛔ Панель доступна только разработчику бота.")
    users = all_users()
    classes = load_data(CLASSES_FILE, {}, expect="dict")
    blocked = sum(1 for u in users.values() if u.get("is_blocked"))
    today = now().strftime("%Y-%m-%d")
    dau = sum(1 for u in users.values() if str(u.get("last_activity", ""))[:10] == today)
    text = (f"🛠️ Панель разработчика — DEVORKS+ VK ({VK_BOT_BUILD})\n\n"
            f"👥 Пользователей: {len(users)} (DAU: {dau})\n"
            f"🎓 Классов: {len(classes)}\n"
            f"🚫 Заблокировано: {blocked}\n"
            f"🔔 Обяз. подписка: {'вкл' if is_dev_sub_gate_enabled() else 'выкл'}")
    kb = [
        [("📊 Статистика", {"c": "dev_stats"}),
         ("📢 Рассылка всем", {"c": "dev_broadcast"})],
        [("✉️ Сообщение юзеру", {"c": "dev_msg_user"}),
         ("🎓 Сообщение классу", {"c": "dev_msg_class"})],
        [("🚫 Блокировки", {"c": "dev_block"}),
         ("⭐ Звёзды", {"c": "dev_stars"})],
        [("💰 Цены", {"c": "dev_prices"}),
         ("📚 Инструкция", {"c": "dev_instructions"})],
        [("🎉 Праздники", {"c": "dev_holidays"}),
         ("🌐 Глоб. кнопки", {"c": "dev_global_buttons"})],
        [("💡 Идеи (входящие)", {"c": "dev_suggestions"}),
         ("💬 Поддержка", {"c": "dev_support"})],
        [("🗂 Бэкап БД", {"c": "dev_backup"}),
         ("🗑 Удалить класс", {"c": "dev_delete_class"}, "negative")],
        [("🚪 Обяз. подписка", {"c": "dev_subgate"}),
         ("👑 Админ-журнал", {"c": "dev_admin_log"})],
        [("⬅️ Назад в меню", {"c": "back_menu"})],
    ]
    await vk.send(uid, text, keyboard=kb_inline(kb))
    clear_state(user)


async def dev_stats(user: dict) -> None:
    uid = int(user["user_id"])
    users = all_users()
    classes = load_data(CLASSES_FILE, {}, expect="dict")
    today = now().strftime("%Y-%m-%d")
    week_ago = (now() - timedelta(days=7)).strftime("%Y-%m-%d")
    dau = sum(1 for u in users.values() if str(u.get("last_activity", ""))[:10] == today)
    wau = sum(1 for u in users.values() if str(u.get("last_activity", ""))[:10] >= week_ago)
    stars_total = sum(int(u.get("stars_balance", 0)) for u in users.values())
    stars_spent = load_data(STARS_STATS_FILE, {}).get("total_spent", 0)
    in_class = sum(1 for u in users.values() if u.get("class_code"))
    text = (
        f"📊 Статистика DEVORKS+ VK:\n\n"
        f"👥 Всего: {len(users)} · DAU: {dau} · WAU: {wau}\n"
        f"🎓 В классах: {in_class} · классов: {len(classes)}\n"
        f"⭐ На балансах: {stars_total} · потрачено: {stars_spent}\n"
        f"☁️ Файлов в облаках: {sum(len(v) for v in cloud_index().values())}\n"
        f"🔐 В сейфах: {sum(len((v or {}).get('files', [])) for v in vault_index().values())}"
    )
    await vk.send(uid, text, keyboard=kb_inline([[("⬅️ Назад", {"c": "menu", "b": "🛠️ Панель разработчика"})]]))


@state_handler(ST_DEV_BROADCAST)
async def dev_broadcast(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if not is_dev(uid):
        clear_state(user)
        return True
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    clear_state(user)
    await vk.send(uid, "📢 Рассылка запущена…")
    sent = 0
    for uid2, u in all_users().items():
        if u.get("is_blocked") or not u.get("setup_completed"):
            continue
        if await vk.send(int(uid2), f"📢 {clean_forbidden(text.strip())[:3000]}") != 0:
            sent += 1
        await asyncio.sleep(0.05)
    log_admin_action(uid, "broadcast", f"{sent} получателей")
    await vk.send(uid, f"✅ Рассылка завершена: доставлено {sent}.")
    return await developer_panel(user)


@state_handler(ST_DEV_MESSAGE_USER)
async def dev_message_user(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if not is_dev(uid):
        clear_state(user)
        return True
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    target = user["fsm"].get("target")
    if not target:
        # это был ввод id
        if text.strip().lstrip("-").isdigit():
            await vk.send(uid, "✉️ Теперь текст сообщения:")
            set_state(user, ST_DEV_MESSAGE_USER, {"target": text.strip(), "step": "text"})
            return True
        await vk.send(uid, "⚠️ Пришлите VK ID пользователя (число).")
        return True
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    if await vk.send(int(target), f"✉️ Сообщение от разработчика DEVORKS+:\n\n{text.strip()[:2000]}") != 0:
        await vk.send(uid, f"✅ Доставлено vk{target}.")
    else:
        await vk.send(uid, f"⚠️ Не доставлено vk{target} (запретил сообщения?).")
    clear_state(user)
    return await developer_panel(user)


@state_handler(ST_DEV_GRANT_STARS)
async def dev_grant_stars(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if not is_dev(uid):
        clear_state(user)
        return True
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    target = user["fsm"].get("target")
    if not target:
        if text.strip().lstrip("-").isdigit():
            await vk.send(uid, "⭐ Сколько звёзд начислить? (отрицательное число — списать)")
            set_state(user, ST_DEV_GRANT_STARS, {"target": text.strip(), "step": "amount"})
            return True
        await vk.send(uid, "⚠️ Пришлите VK ID пользователя (число).")
        return True
    try:
        amount = int(text.strip())
    except ValueError:
        await vk.send(uid, "⚠️ Нужно число. Например: 50 или -10.")
        return True
    tu = get_user(target)
    tu["stars_balance"] = int(tu.get("stars_balance", 0)) + amount
    save_user(tu)
    log_admin_action(uid, "grant_stars", f"vk{target}: {amount:+d}")
    await vk.send(uid, f"✅ vk{target}: баланс {tu['stars_balance']} ⭐ ({amount:+d}).")
    try:
        word = "начислено" if amount >= 0 else "списано"
        await vk.send(int(target), f"⭐ Вам {word} {abs(amount)} звёзд. Баланс: {tu['stars_balance']} ⭐")
    except Exception:
        pass
    clear_state(user)
    return await developer_panel(user)


@state_handler(ST_DEV_EDIT_INSTRUCTIONS)
async def dev_edit_instructions(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if not is_dev(uid):
        clear_state(user)
        return True
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    data = load_data(INSTRUCTIONS_FILE, {}, expect="dict")
    data["text"] = text.strip()[:8000]
    save_data(INSTRUCTIONS_FILE, data)
    log_admin_action(uid, "edit_instructions", "")
    clear_state(user)
    await vk.send(uid, "✅ Инструкция обновлена.")
    return await developer_panel(user)


@state_handler(ST_DEV_HOLIDAY_ADD)
async def dev_holiday_add(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if not is_dev(uid):
        clear_state(user)
        return True
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    # Оптом: «1 января — Новый год, 8 марта — Женский день» или по одному
    holidays = load_holidays()
    added = 0
    for part in text.replace("\n", ",").split(","):
        if not part.strip():
            continue
        for sep in ("—", "–", "-", ":"):
            if sep in part:
                date_part, name = part.split(sep, 1)
                d = parse_date_or_none(date_part.strip())
                if d and name.strip():
                    holidays[f"{d.day:02d}-{d.month:02d}"] = clean_forbidden(name.strip())[:60]
                    added += 1
                break
    if not added:
        await vk.send(uid, "⚠️ Не понял. Формат: «1 января — Новый год» (можно через запятую).")
        return True
    save_data(HOLIDAYS_FILE, holidays)
    log_admin_action(uid, "holidays_add", f"{added} шт.")
    await vk.send(uid, f"✅ Добавлено праздников: {added}. Всего: {len(holidays)}.")
    clear_state(user)
    return await developer_panel(user)


async def dev_do_backup(user: dict) -> None:
    """ZIP-бэкап всех JSON-баз + файл в чат разработчику."""
    uid = int(user["user_id"])
    stamp = now().strftime("%Y%m%d_%H%M")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    zpath = os.path.join(BACKUP_DIR, f"devorks_backup_{stamp}.zip")
    count = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        base_dir = os.path.dirname(os.path.abspath(USERS_FILE)) or "."
        for fn in os.listdir(base_dir):
            if fn.endswith(".json"):
                try:
                    zf.write(os.path.join(base_dir, fn), arcname=fn)
                    count += 1
                except OSError:
                    pass
    with open(zpath, "rb") as f:
        data = f.read()
    await vk.send_file(uid, data, f"devorks_backup_{stamp}.zip")
    await vk.send(uid, f"🗂 Бэкап готов: {count} JSON-файлов "
                       f"({_fmt_size(len(data))}). Праздники/цены/пользователи — всё внутри.")

# ==================================
# === DEVORKS+ai (чат) ===
# ==================================

async def ai_chat_start(user: dict) -> None:
    uid = int(user["user_id"])
    persona = user.get("ai_persona", "normal")
    p_title = AI_PERSONA_MODES.get(persona, AI_PERSONA_MODES["normal"])["title"]
    kb = [
        [("🧠 Обычный", {"c": "ai_persona", "p": "normal"}),
         ("😈 Хамло", {"c": "ai_persona", "p": "hamlo"}, "negative")],
        [("🥰 Тепло", {"c": "ai_persona", "p": "warm"}, "positive")],
        [("🧹 Очистить контекст", {"c": "ai_clear"}, "negative")],
        [("❌ Выход из ai", {"c": "back_menu"})],
    ]
    await vk.send(uid,
                  f"🤖 DEVORKS+ai — режим: {p_title}\n\n"
                  "Пишите что угодно — отвечу! Умею:\n"
                  "• текст — просто спрашивайте;\n"
                  "• фото — пришлите картинку с вопросом;\n"
                  "• голосовые — расшифрую и отвечу.\n\n"
                  "Для выхода нажмите «❌ Выход из ai».",
                  keyboard=kb_inline(kb))
    set_state(user, ST_AI_CHAT)


@state_handler(ST_AI_CHAT)
async def ai_chat_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.strip() == "❌ Выход из ai":
        clear_state(user)
        return await send_main_menu(user, "👋 Чат с ИИ завершён.")
    atts = message.get("attachments") or []
    image_b64 = None
    voice_data = None
    for att in atts:
        if att.get("type") == "photo":
            data, _, _ = await vk.download_attachment(att)
            if data:
                image_b64 = base64.b64encode(data).decode()
            break
        if att.get("type") == "doc" and (att.get("doc", {}).get("preview") or {}).get("audio_msg"):
            voice_data, _, _ = await vk.download_attachment(att)
            if voice_data:
                break
    query = text.strip()
    if voice_data:
        await vk.send(uid, "🎧 Расшифровываю голосовое…")
        transcribed = await ai_transcribe_voice(voice_data)
        if not transcribed:
            await vk.send(uid, "😔 Не удалось расшифровать голосовое. Попробуйте текстом.")
            return True
        await vk.send(uid, f"🎧 Вы сказали: «{transcribed[:300]}»")
        query = transcribed if not query else f"{query}\n(голосовое: {transcribed})"
    if not query and not image_b64:
        await vk.send(uid, "🤖 Напишите текст, пришлите фото или голосовое.")
        return True
    bad = reject_if_forbidden(query)
    if bad:
        await vk.send(uid, bad)
        return True
    # «Печатает…»
    await vk.send(uid, "🤖 Думаю…")
    answer = await ai_chat_reply(user, query, image_b64)
    user["ai_generation_count"] = int(user.get("ai_generation_count", 0)) + 1
    user["last_activity"] = now().strftime("%Y-%m-%d %H:%M")
    save_user(user)
    await vk.send(uid, answer)
    return True


# ==================================
# === 🪄 AI AGENT (DeepSeek) ===
# ==================================

def agent_system_prompt(user: dict, cls: dict) -> str:
    today = now().date()
    monday = today - timedelta(days=today.weekday())
    next_monday = monday + timedelta(days=7)
    tomorrow = today + timedelta(days=1)
    # Данные класса для контекста
    subjects = ", ".join(list((cls or {}).get("teachers", {}).keys())[:15]) or "не заданы"
    members = ""
    if cls:
        members = f", участников: {len(cls.get('members', []))}"
    holidays_list = "; ".join(f"{k}: {v}" for k, v in
                              sorted(load_holidays().items())[:40])
    prices = load_prices()
    is_admin = cls and is_class_admin(user["user_id"], cls)
    is_developer = is_dev(user["user_id"])
    ref_link = f"https://vk.me/club{vk.group_id}?ref=ref_{user['user_id']}"
    limits_pb = get_personal_buttons(user["user_id"])
    return (
        "Ты — AI Agent бота DEVORKS+ (ВК-версия). Превращай фразу пользователя в ОДИН JSON-объект "
        "с полем action. Отвечай ТОЛЬКО JSON, без пояснений и markdown.\n\n"
        f"КОНТЕКСТ:\n"
        f"- Сегодня: {today.isoformat()} ({WEEKDAY_NAMES[today.weekday()]}).\n"
        f"- Завтра: {tomorrow.isoformat()}.\n"
        f"- Понедельник этой недели: {monday.isoformat()}, следующей: {next_monday.isoformat()}.\n"
        f"- Пользователь: {user.get('first_name', '')} (vk{user['user_id']}). Город: {user.get('city') or 'не задан'}. "
        f"Роль: {'админ класса' if is_admin else 'ученик'}{' + разработчик бота' if is_developer else ''}.{members}\n"
        f"- Класс: {(cls or {}).get('class_name') or 'не в классе'}; предметы: {subjects}.\n"
        f"- Личных кнопок: {len(limits_pb)} (макс {user.get('max_personal_buttons', 1)}).\n"
        f"- Баланс: {user.get('stars_balance', 0)} ⭐; цены: кнопка {prices['button_base']}⭐, "
        f"отправитель анонимки {prices['view_sender']}⭐, разблок {prices['unblock']}⭐, "
        f"хранение анонимок {prices['anon_keep_month']}⭐/мес, лимит облака {prices['cloud_max_files']}.\n"
        f"- Реф-ссылка: {ref_link}\n"
        f"- Праздники бота: {holidays_list or 'не заданы'}\n\n"
        "ДЕЙСТВИЯ (action):\n"
        '1) {"action":"add_homework","subject":"<предмет>","date":"ГГГГ-ММ-ДД","text":"<задание>"} — добавить ДЗ (админ). Используй существующее название предмета («русский» → «Русский язык»). Относительные даты переводи в конкретные.\n'
        '2) {"action":"delete_homework","subject":"<предмет или null>","date":"ГГГГ-ММ-ДД или null"} — удалить ДЗ (админ). null в date = всё ДЗ предмета; null в subject допустим только с датой = весь день.\n'
        '3) {"action":"replace_teachers","items":[{"subject":"<предмет>","teacher":"<ФИО>"}]} — заменить учителей (админ).\n'
        '4) {"action":"add_subject","subject":"<предмет>","teacher":"<ФИО или null>"} — добавить предмет (админ).\n'
        '5) {"action":"remove_subject","subject":"<предмет>"} — удалить предмет (админ).\n'
        '6) {"action":"edit_schedule","day":"<Понедельник..Воскресенье>","content":"<номер. предмет через \\n>"} — заменить расписание дня (админ).\n'
        '7) {"action":"edit_bell","lesson":<номер>,"start":"ЧЧ:ММ","end":"ЧЧ:ММ"} — задать время урока (админ).\n'
        '7b) {"action":"set_bells","bells":[{"lesson":1,"start":"08:30","end":"09:15"}],"day":"пн"|"null","date":"ГГГГ-ММ-ДД"|null} — весь список звонков; day/date задают отдельный день/дату (админ).\n'
        '8) {"action":"set_holidays","date":"ГГГГ-ММ-ДД"} — дата начала каникул (админ).\n'
        '9) {"action":"create_timer","date":"ГГГГ-ММ-ДД","time":"ЧЧ:ММ","text":"<текст>","kind":"timer|wish","repeat_daily":false} — таймер/напоминание/пожелание (все). «через N минут» → поле in_minutes. kind:"wish" — когда пользователь просит ПОЖЕЛАТЬ: в text — само пожелание живой фразой. repeat_weekday:"mon" — еженедельно; repeat_days:["tue","fri"] — по дням; skip_days:["sat","sun"] — кроме дней.\n'
        '10) {"action":"send_class_message","text":"<сообщение>"} — объявление классу (админ).\n'
        '10b) {"action":"schedule_class_message","text":"<сообщение>","date":"ГГГГ-ММ-ДД","time":"ЧЧ:ММ"} — таймер сообщения классу (админ); in_minutes тоже можно.\n'
        '10c) {"action":"show_class_timers"} — показать таймеры классу.\n'
        '10d) {"action":"delete_class_timer","timer_id":"<id>"} — удалить таймер класса (админ).\n'
        '11) {"action":"show_homework","subject":"<предмет или null>","date":"ГГГГ-ММ-ДД или null"} — показать ДЗ.\n'
        '12) {"action":"show_schedule","day":"<день или null>"} — показать расписание.\n'
        '13) {"action":"show_teachers","subject":"<предмет или null>","subjects":["<предметы>"]} — показать учителей; для конкретных предметов передавай ТОЛЬКО их.\n'
        '14) {"action":"show_bells"} — показать звонки.\n'
        '15) {"action":"show_holidays"} — показать каникулы.\n'
        '16) {"action":"show_weather","city":"<город или null>"} — погода сейчас; city — любой город мира («погода в Париже» → city:"Paris").\n'
        '17) {"action":"weather_forecast","days":<1..3>,"city":"<город или null>"} — прогноз.\n'
        '18) {"action":"open_section","section":"<раздел или кнопка>"} — открыть раздел бота (меню, дз, добавить дз, удалить дз, учителя, расписание, звонки, каникулы, таймер, анонимное сообщение, мои анонимные сообщения, классы, мои кнопки, звезды, ии, ai agent, погода, инструкция, код класса, написать админу, настройки, облако, сейф, решения, дежурные, дни рождения, важное). Можно и любую личную/глобальную/классную кнопку по названию.\n'
        '19) {"action":"show_vip"} — показать VIP-подписки пользователя.\n'
        '20) {"action":"send_anon","name":"<имя получателя>","text":"<сообщение>"} — анонимное сообщение однокласснику (все).\n'
        '21) {"action":"create_button","name":"<название>","content":"<текст или ссылка>","type":"text|url"} — создать личную кнопку (учитывай лимит и цену звёзд).\n'
        '22) {"action":"delete_button","name":"<название>"} — удалить личную кнопку.\n'
        '23) {"action":"hide_button","name":"<название>"} — скрыть кнопку меню.\n'
        '24) {"action":"show_button","name":"<название>"} — вернуть скрытую кнопку.\n'
        '25) {"action":"rename_button","name":"<старое>","new_name":"<новое>"} — переименовать кнопку.\n'
        '26) {"action":"move_button","name":"<название>","position":<число>} — переместить кнопку (1 = первая).\n'
        '27) {"action":"ai_mode","mode":"normal|hamlo|warm"} — показать (null) или сменить режим ИИ.\n'
        '28) {"action":"clarify","question":"<один конкретный вопрос>"} — если данных не хватает.\n'
        '29) {"action":"none","answer":"<ответ по-русски>"} — не школьное действие: просто ответь.\n'
        '30) {"action":"add_homework_many","items":[{"subject":"...","date":"ГГГГ-ММ-ДД","text":"..."}]} — несколько ДЗ за раз (админ). НИ ОДНО не теряй.\n'
        '31) {"action":"set_birthday","date":"ГГГГ-ММ-ДД"} — изменить свой день рождения.\n'
        '32) {"action":"show_class_code"} — код класса.\n'
        '33) {"action":"delete_homework_many","items":[{"subject":..., "date":...}]} — несколько удалений ДЗ (админ).\n'
        '34) {"action":"show_storage"} — статус облака: сколько файлов.\n'
        '35) {"action":"backup_now"} — бэкап базы (только разработчик).\n'
        '36) {"action":"vault_status"} — статус сейфа: сколько зашифровано.\n'
        '38) {"action":"import_holidays","items":[{"date":"ДД-ММ","name":"..."}],"mode":"add|replace"} — список праздников (только разработчик).\n'
        '39) {"action":"delete_holiday","date":"ДД-ММ или null","name":"<название или null>"} — удалить праздник (разработчик).\n'
        '40) {"action":"show_holidays_all"} — все праздники бота.\n'
        '41) {"action":"broadcast","text":"<сообщение>"} — рассылка всем (только разработчик).\n'
        '42) {"action":"set_price","key":"<ключ>","value":<число>} — изменить цену: button_base, button_increment, unblock, view_sender, anon_keep_month, ai_generation, cloud_max_files (только разработчик; -1 = безлимит).\n'
        '43) {"action":"create_global_button","name":"...","content":"...","type":"url|text"} — глобальная кнопка (разработчик).\n'
        '44) {"action":"delete_global_button","name":"..."} — удалить глобальную (разработчик).\n'
        '45) {"action":"block_user","user":"<id или имя>","hours":<число или null>,"reason":"..."} — заблокировать (разработчик).\n'
        '46) {"action":"unblock_user","user":"<id или имя>"} — разблокировать (разработчик).\n'
        '47) {"action":"grant_stars","user":"<id или имя>","amount":<число>} — начислить/списать звёзды (разработчик).\n'
        '48) {"action":"show_bot_stats"} — статистика бота (разработчик).\n'
        '49) {"action":"remove_student","user":"<имя или id>"} — выгнать из класса (админ).\n'
        '50) {"action":"show_class_users"} — состав класса.\n'
        '51) {"action":"set_city","city":"<город>"} — сменить свой город.\n'
        '52) {"action":"show_profile"} — мой профиль.\n'
        '53) {"action":"show_referrals"} — мои рефералы.\n'
        '54) {"action":"message_developer","text":"..."} — написать разработчику.\n'
        '55) {"action":"show_favorites"} — избранные файлы.\n\n'
        "ПРАВИЛА:\n"
        "- Отвечай ТОЛЬКО JSON-объектом.\n"
        "- Не выдумывай даты: считай от сегодняшней. «Пятница этой недели» — из контекста недели.\n"
        "- «стр 45» — текст задания. Названия предметов сопоставляй с классом («матеша»→«Математика»).\n"
        "- Несколько ДЗ за раз — add_homework_many со ВСЕМИ элементами.\n"
        "- ПОЖЕЛАНИЯ: «пожелай спокойной ночи в 23:00» = create_timer kind:\"wish\"; «каждый день» = repeat_daily:true.\n"
        "- ПОГОДА: любой город мира, отказов нет; «на завтра» = weather_forecast days:1.\n"
        "- НЕСКОЛЬКО удалений ДЗ — delete_homework_many.\n"
        "- Кнопки: create/delete/hide/show/rename/move_button. Без содержимого — clarify.\n"
        "- Даты — ГГГГ-ММ-ДД, время — ЧЧ:ММ.\n"
        "- Прав админа нет — не выполняй админские действия (кроме show_*).\n"
    )


async def automation_start(user: dict) -> None:
    uid = int(user["user_id"])
    if not DEEPSEEK_API_KEY:
        return await vk.send(uid,
            "🪄 AI Agent требует DEEPSEEK_API_KEY. Разработчику нужно задать "
            "переменную окружения на хостинге. Остальной бот работает как обычно.",
            keyboard=back_kb())
    await vk.send(uid,
        "🪄 AI Agent — управление ботом обычными фразами!\n\n"
        "Примеры:\n"
        "• «напомни в 19:00 полить цветы»\n"
        "• «добавь дз на завтра по математике стр 45» (админ)\n"
        "• «погода на завтра в Сочи»\n"
        "• «напиши анонимно Пете спасибо за конспект»\n"
        "• «когда каникулы?»\n"
        "• «создай кнопку Шпаргалки: sin²+cos²=1»\n\n"
        "Просто пишите, что сделать. Для выхода — «⬅️ Назад в меню».",
        keyboard=kb_inline([[("⬅️ Назад в меню", {"c": "back_menu"})]]))
    set_state(user, ST_AI_AUTOMATION)


@state_handler(ST_AI_AUTOMATION)
async def automation_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if not text.strip():
        await vk.send(uid, "🪄 Напишите, что сделать.")
        return True
    if text.lower() in ("выход", "отмена"):
        clear_state(user)
        return await send_main_menu(user)
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    cls = get_class_by_user(uid)
    await vk.send(uid, "🪄 Понял, выполняю…")
    raw = await deepseek_json(agent_system_prompt(user, cls), text[:2500])
    action = extract_json_object(raw)
    if not action or not action.get("action"):
        await vk.send(uid, "😔 AI Agent не смог разобрать запрос. Переформулируйте, пожалуйста.")
        return True
    try:
        await agent_execute(user, action)
    except Exception as e:
        logger.error("agent_execute: %s\n%s", e, traceback.format_exc())
        await vk.send(uid, "😔 Ошибка при выполнении. Попробуйте иначе.")
    return True


# ==================================
# === ИСПОЛНИТЕЛЬ ДЕЙСТВИЙ AI AGENT ===
# ==================================

SECTION_ALIASES = {
    "меню": "⬅️ Назад в меню", "главное меню": "⬅️ Назад в меню",
    "дз": "📝 Домашнее задание", "домашка": "📝 Домашнее задание",
    "домашнее задание": "📝 Домашнее задание",
    "добавить дз": "➕ Добавить ДЗ", "удалить дз": "🗑️ Удалить ДЗ",
    "учителя": "👨‍🏫 Учителя", "расписание": "📅 Расписание",
    "звонки": "🔔 Звонки", "каникулы": "🎉 Каникулы",
    "таймер": "⏰ Таймер", "помодоро": "⏰ Таймер",
    "анонимное сообщение": "🕵️ Анонимное сообщение",
    "мои анонимные сообщения": "📨 Мои анонимные сообщения",
    "классы": "🎓 Управление классами",
    "управление классами": "🎓 Управление классами",
    "мои кнопки": "🌟 Мои кнопки", "звезды": "⭐ Звезды", "звёзды": "⭐ Звезды",
    "ии": "🤖 DEVORKS+ai", "ai": "🤖 DEVORKS+ai",
    "ai agent": "🪄 AI Agent", "агент": "🪄 AI Agent",
    "автоматизация": "🪄 AI Agent",
    "погода": "🌦 Погода", "инструкция": "📚 Инструкция",
    "код класса": "🔑 Код класса",
    "написать админу": "💬 Написать админу",
    "настройки": "⚙️ Настройки", "облако": "☁️ Облако", "сейф": "🔐 Сейф",
    "решения": "📚 Решения", "дежурные": "🕐 Дежурные",
    "дни рождения": "🎂 Дни рождения", "важное": "❗ Важное",
    "ещё": "📋 Ещё", "еще": "📋 Ещё",
}


def _match_subject(cls: dict, wanted: str):
    """Сопоставляет название предмета с классом («матеша» → «Математика»)."""
    wanted = (wanted or "").strip().lower()
    if not wanted:
        return None
    teachers = (cls or {}).get("teachers") or {}
    all_subjects = list(teachers.keys())
    for day_hw in (cls or {}).get("homework", {}).values():
        for s in day_hw.keys():
            if s not in all_subjects:
                all_subjects.append(s)
    for s in all_subjects:
        if s.lower() == wanted:
            return s
    for s in all_subjects:
        if wanted in s.lower() or s.lower() in wanted:
            return s
    # Нечеткое сопоставление («матеша» ↔ «Математика», «русский» ↔ «Русский язык»)
    import difflib
    best, best_ratio = None, 0.0
    for s in all_subjects:
        ratio = difflib.SequenceMatcher(None, wanted, s.lower()).ratio()
        if ratio > best_ratio:
            best, best_ratio = s, ratio
    if best is not None and best_ratio >= 0.55:
        return best
    return None


async def _agent_find_user(user: dict, cls: dict, query: str):
    """Ищет пользователя по id / имени в классе или глобально."""
    q = (query or "").strip().lstrip("@")
    if not q:
        return None
    if q.lstrip("-").isdigit():
        return q.lstrip("-")
    # Поиск по имени в классе
    if cls:
        for m in cls.get("members", []):
            name = await vk.user_name(m)
            if q.lower() in name.lower():
                return str(m)
    # Глобальный поиск по имени
    for uid2, u in all_users().items():
        if q.lower() in (u.get("first_name") or "").lower():
            return str(uid2)
    return None


def _timer_repeat_from(action: dict):
    """Строит описание повтора таймера из полей действия DeepSeek."""
    if action.get("repeat_daily"):
        return {"type": "daily"}
    wd = action.get("repeat_weekday")
    if wd:
        wd = str(wd).lower()[:3]
        return {"type": "weekly", "day": wd}
    if action.get("repeat_days"):
        return {"type": "days", "days": [str(d).lower()[:3] for d in action["repeat_days"]]}
    if action.get("skip_days"):
        return {"type": "skip", "days": [str(d).lower()[:3] for d in action["skip_days"]]}
    return None


async def agent_execute(user: dict, action: dict) -> None:
    """Выполняет действие, полученное от DeepSeek."""
    uid = int(user["user_id"])
    act = (action.get("action") or "").strip()
    cls = get_class_by_user(uid)
    is_admin = bool(cls and is_class_admin(uid, cls))
    is_developer = is_dev(uid)

    async def deny_admin():
        await vk.send(uid, "⛔ Это действие доступно только админам класса.")

    # ---------- ДЗ ----------
    if act == "add_homework" or act == "add_homework_many":
        if not is_admin:
            return await deny_admin()
        items = action.get("items") or [action]
        added = []
        for item in items:
            subject = _match_subject(cls, item.get("subject") or "")
            if not subject:
                subject = (item.get("subject") or "Предмет").strip()[:60]
            d = parse_date_or_none(item.get("date") or "") or now().date()
            text = (item.get("text") or "").strip()
            if not text:
                continue
            hw = cls.setdefault("homework", {}).setdefault(d.isoformat(), {})
            hw[subject] = {"text": clean_forbidden(text)[:1000],
                           "attachments": [], "by": str(uid),
                           "ts": now().strftime("%Y-%m-%d %H:%M")}
            added.append(f"📚 {subject} на {fmt_date_short(d)}")
        save_class(cls)
        log_admin_action(uid, "agent_add_hw", f"{len(added)} шт.")
        if added:
            await vk.send(uid, "✅ Добавлено:\n" + "\n".join(added))
            await notify_class(cls, str(uid),
                               "📝 Новое ДЗ:\n" + "\n".join(added), )
        else:
            await vk.send(uid, "⚠️ Не удалось добавить ДЗ (пустые данные).")
        return

    if act == "delete_homework" or act == "delete_homework_many":
        if not is_admin:
            return await deny_admin()
        items = action.get("items") or [action]
        removed = 0
        for item in items:
            d = parse_date_or_none(item.get("date") or "") if item.get("date") else None
            subject = _match_subject(cls, item.get("subject") or "") if item.get("subject") else None
            hw = cls.setdefault("homework", {})
            if d and not subject:
                removed += len(hw.get(d.isoformat(), {}))
                hw.pop(d.isoformat(), None)
            elif subject and not d:
                for iso in list(hw.keys()):
                    if hw[iso].pop(subject, None) is not None:
                        removed += 1
            elif d and subject:
                day = hw.get(d.isoformat()) or {}
                if day.pop(subject, None) is not None:
                    removed += 1
        save_class(cls)
        log_admin_action(uid, "agent_del_hw", f"{removed} шт.")
        await vk.send(uid, f"🗑 Удалено записей ДЗ: {removed}.")
        return

    if act == "show_homework":
        if not cls:
            return await vk.send(uid, "🎓 Вы не в классе.")
        hw = cls.get("homework") or {}
        subject = _match_subject(cls, action.get("subject") or "") if action.get("subject") else None
        d = parse_date_or_none(action.get("date") or "") if action.get("date") else None
        if d:
            text = homework_for_day(cls, d)
            await vk.send(uid, f"📝 ДЗ на {fmt_date(d)}:\n{text or '— нет —'}")
        elif subject:
            lines = []
            for iso in sorted(hw.keys()):
                if hw.get(iso, {}).get(subject):
                    dd = _iso_date(iso)
                    lines.append(f"📅 {fmt_date_short(dd)}: {hw[iso][subject].get('text', '')[:200]}")
            await vk.send(uid, f"📝 ДЗ по «{subject}»:\n" + ("\n".join(lines) or "— нет —"))
        else:
            await vk.send(uid, homework_full_text(cls))
        return

    # ---------- Учителя / предметы ----------
    if act == "replace_teachers":
        if not is_admin:
            return await deny_admin()
        count = 0
        for item in (action.get("items") or []):
            subject = (item.get("subject") or "").strip()[:60]
            teacher = (item.get("teacher") or "").strip()[:80]
            if subject and teacher:
                cls.setdefault("teachers", {})[subject] = clean_forbidden(teacher)
                count += 1
        save_class(cls)
        log_admin_action(uid, "agent_teachers", f"{count} шт.")
        await vk.send(uid, f"✅ Обновлено учителей: {count}.\n\n" + teachers_text(cls))
        await notify_class(cls, str(uid), "👨‍🏫 Список учителей обновлён!")
        return

    if act == "add_subject":
        if not is_admin:
            return await deny_admin()
        subject = (action.get("subject") or "").strip()[:60]
        if not subject:
            return await vk.send(uid, "⚠️ Не указан предмет.")
        cls.setdefault("teachers", {})[subject] = clean_forbidden((action.get("teacher") or "").strip()[:80])
        save_class(cls)
        await vk.send(uid, f"✅ Предмет «{subject}» добавлен.")
        return

    if act == "remove_subject":
        if not is_admin:
            return await deny_admin()
        subject = _match_subject(cls, action.get("subject") or "")
        if subject and cls.get("teachers", {}).pop(subject, None) is not None:
            save_class(cls)
            await vk.send(uid, f"✅ Предмет «{subject}» удалён.")
        else:
            await vk.send(uid, "⚠️ Такой предмет не найден.")
        return

    if act == "show_teachers":
        if not cls:
            return await vk.send(uid, "🎓 Вы не в классе.")
        subj = action.get("subject")
        subjects = action.get("subjects")
        await vk.send(uid, teachers_text(cls, subj or subjects))
        return

    # ---------- Расписание / звонки / каникулы ----------
    if act == "edit_schedule":
        if not is_admin:
            return await deny_admin()
        day = (action.get("day") or "").strip().title()
        if day not in WEEKDAY_NAMES:
            return await vk.send(uid, "⚠️ День не распознан.")
        content = (action.get("content") or "").strip()
        cls.setdefault("schedule", {})[day] = clean_forbidden(content)[:1000]
        save_class(cls)
        log_admin_action(uid, "agent_edit_schedule", day)
        await vk.send(uid, f"✅ Расписание на {day}:\n{content or '— нет уроков —'}")
        await notify_class(cls, str(uid), f"📅 Обновлено расписание на {day}!")
        return

    if act == "show_schedule":
        if not cls:
            return await vk.send(uid, "🎓 Вы не в классе.")
        day = action.get("day")
        d = None
        if day:
            for i, wd in enumerate(WEEKDAY_NAMES):
                if str(day).lower() in wd.lower():
                    d = now().date() + timedelta(days=(i - now().date().weekday()) % 7)
                    break
            if d is None:
                d = parse_date_or_none(str(day))
        if d:
            await vk.send(uid, schedule_day_text(cls, d))
        else:
            await send_week_schedule(user)
        return

    if act == "edit_bell":
        if not is_admin:
            return await deny_admin()
        try:
            lesson = int(action.get("lesson"))
            s, e = parse_hhmm(action.get("start") or ""), parse_hhmm(action.get("end") or "")
        except (TypeError, ValueError):
            lesson = None
            s = e = None
        if not lesson or not s or not e or (e[0], e[1]) <= (s[0], s[1]):
            return await vk.send(uid, "⚠️ Некорректные данные звонка (end должен быть позже start).")
        bells = cls.setdefault("bells", [])
        found = False
        for b in bells:
            if int(b.get("lesson", 0)) == lesson:
                b["start"], b["end"] = action.get("start"), action.get("end")
                found = True
        if not found:
            bells.append({"lesson": lesson, "start": action.get("start"), "end": action.get("end")})
        cls["bells"] = sorted(bells, key=lambda x: int(x.get("lesson", 0)))
        save_class(cls)
        log_admin_action(uid, "agent_edit_bell", str(lesson))
        await vk.send(uid, f"✅ {lesson} урок: {action.get('start')}–{action.get('end')}.")
        return

    if act == "set_bells":
        if not is_admin:
            return await deny_admin()
        bells = []
        for b in (action.get("bells") or []):
            try:
                lesson = int(b.get("lesson"))
                s, e = parse_hhmm(b.get("start") or ""), parse_hhmm(b.get("end") or "")
                if s and e and (e[0], e[1]) > (s[0], s[1]):
                    bells.append({"lesson": lesson, "start": b.get("start"), "end": b.get("end")})
            except (TypeError, ValueError):
                continue
        if not bells:
            return await vk.send(uid, "⚠️ Список звонков пуст или некорректен.")
        day = action.get("day")
        d = action.get("date") and parse_date_or_none(str(action.get("date")))
        if d:
            cls.setdefault("bells_by_date", {})[d.strftime("%d.%m.%Y")] = bells
            where = fmt_date(d)
        elif day and str(day).lower()[:2] in WEEKDAY_SHORT:
            cls.setdefault("bells_by_day", {})[str(day).lower()[:2]] = bells
            where = WEEKDAY_NAMES[WEEKDAY_SHORT.index(str(day).lower()[:2])]
        else:
            cls["bells"] = bells
            where = "все дни"
        save_class(cls)
        log_admin_action(uid, "agent_set_bells", where)
        await vk.send(uid, f"✅ Звонки заданы ({len(bells)} шт., {where}).")
        return

    if act == "show_bells":
        await vk.send(uid, bells_text(cls) if cls else "🎓 Вы не в классе.")
        return

    if act == "set_holidays":
        if not is_admin:
            return await deny_admin()
        d = parse_date_or_none(action.get("date") or "")
        if not d:
            return await vk.send(uid, "⚠️ Дата каникул не распознана.")
        cls["holidays"] = d.isoformat()
        save_class(cls)
        log_admin_action(uid, "agent_holidays", d.isoformat())
        await vk.send(uid, f"✅ Каникулы с {fmt_date(d)}.")
        await notify_class(cls, str(uid), f"🎉 Каникулы объявлены с {fmt_date(d)}! 🎉")
        return

    if act == "show_holidays":
        await vk.send(uid, holidays_text(user))
        return

    # ---------- Таймеры ----------
    if act == "create_timer":
        kind = action.get("kind") if action.get("kind") in ("timer", "wish") else "timer"
        text = (action.get("text") or "").strip()
        if not text:
            return await vk.send(uid, "⚠️ Пустой текст напоминания.")
        repeat = _timer_repeat_from(action)
        if action.get("in_minutes"):
            try:
                fire_at = now() + timedelta(minutes=float(action["in_minutes"]))
            except (TypeError, ValueError):
                return await vk.send(uid, "⚠️ Некорректные минуты.")
        else:
            d = None
            date_v = action.get("date")
            if isinstance(date_v, str):
                if date_v.lower() == "today":
                    d = now().date()
                elif date_v.lower() == "tomorrow":
                    d = now().date() + timedelta(days=1)
                else:
                    d = parse_date_or_none(date_v)
            if action.get("repeat_weekday") and not d:
                wd = str(action["repeat_weekday"]).lower()[:3]
                if wd in WEEKDAY_SHORT:
                    idx = WEEKDAY_SHORT.index(wd)
                    days_ahead = (idx - now().date().weekday()) % 7 or 7
                    d = now().date() + timedelta(days=days_ahead)
            d = d or now().date()
            hm = parse_hhmm(action.get("time") or "")
            if not hm:
                return await vk.send(uid, "⚠️ Время не распознано (нужен ЧЧ:ММ).")
            fire_at = datetime(d.year, d.month, d.day, hm[0], hm[1])
            if fire_at <= now() and not repeat:
                fire_at += timedelta(days=1)
        prefix = "⏰" if kind == "timer" else "🎁"
        add_timer(uid, fire_at.strftime("%Y-%m-%d %H:%M"),
                  f"{prefix} {clean_forbidden(text)[:500]}", repeat, kind)
        await vk.send(uid, f"✅ Напоминание поставлено на "
                           f"{fire_at.strftime('%d.%m %H:%M')}.\n{prefix} {text[:200]}"
                     + ("\n🔁 Повторяется по расписанию." if repeat else ""))
        return

    if act == "show_class_timers":
        if not cls:
            return await vk.send(uid, "🎓 Вы не в классе.")
        timers = _load_class_timers().get(cls["class_code"]) or []
        if not timers:
            return await vk.send(uid, "🕒 Таймеров классу нет.")
        lines = ["🕒 Таймеры классу:"]
        for t in timers:
            lines.append(f"• [{t['id']}] {t.get('when')}: {t.get('text', '')[:50]}")
        await vk.send(uid, "\n".join(lines))
        return

    if act in ("schedule_class_message",):
        if not is_admin:
            return await deny_admin()
        msg = (action.get("text") or "").strip()
        if not msg or not cls:
            return await vk.send(uid, "⚠️ Пустое сообщение или нет класса.")
        if action.get("in_minutes"):
            try:
                fire_at = now() + timedelta(minutes=float(action["in_minutes"]))
            except (TypeError, ValueError):
                return await vk.send(uid, "⚠️ Некорректные минуты.")
        else:
            d = parse_date_or_none(action.get("date") or "") or now().date()
            hm = parse_hhmm(action.get("time") or "")
            if not hm:
                return await vk.send(uid, "⚠️ Время не распознано.")
            fire_at = datetime(d.year, d.month, d.day, hm[0], hm[1])
            if fire_at <= now():
                fire_at += timedelta(days=1)
        repeat = _timer_repeat_from(action)
        timers = _load_class_timers()
        timers.setdefault(cls["class_code"], []).append({
            "id": secrets.token_hex(4),
            "when": fire_at.strftime("%Y-%m-%d %H:%M"),
            "text": f"📢 {clean_forbidden(msg)[:800]}",
            "class": cls["class_code"],
            "repeat": repeat,
        })
        _save_class_timers(timers)
        await vk.send(uid, f"✅ Сообщение классу уйдёт {fire_at.strftime('%d.%m %H:%M')}.")
        return

    if act == "delete_class_timer":
        if not is_admin:
            return await deny_admin()
        tid = action.get("timer_id")
        timers = _load_class_timers()
        lst = timers.get(cls["class_code"]) or []
        timers[cls["class_code"]] = [t for t in lst if t.get("id") != tid]
        _save_class_timers(timers)
        await vk.send(uid, "🗑 Таймер класса удалён." if len(timers[cls["class_code"]]) != len(lst)
                      else "⚠️ Таймер с таким id не найден.")
        return

    # ---------- Сообщения классу ----------
    if act == "send_class_message":
        if not is_admin:
            return await deny_admin()
        msg = (action.get("text") or "").strip()
        if not msg:
            return await vk.send(uid, "⚠️ Пустое сообщение.")
        name = await vk.user_name(uid)
        sent = await notify_class(cls, uid, f"📢 Объявление от {name}:\n\n{clean_forbidden(msg)[:2000]}")
        log_admin_action(uid, "agent_class_msg", f"{sent} чел.")
        await vk.send(uid, f"✅ Отправлено: {sent} чел.")
        return

    if act == "show_class_users":
        return await class_show_users(user)

    if act == "remove_student":
        if not is_admin:
            return await deny_admin()
        target = await _agent_find_user(user, cls, action.get("user") or "")
        if not target:
            return await vk.send(uid, "⚠️ Участник не найден.")
        cls["members"] = [m for m in cls.get("members", []) if str(m) != str(target)]
        cls["admins"] = [a for a in cls.get("admins", []) if str(a) != str(target)]
        save_class(cls)
        tu = get_user(target)
        if tu.get("class_code") == cls["class_code"]:
            tu["class_code"] = None
            save_user(tu)
        log_admin_action(uid, "agent_remove_student", str(target))
        await vk.send(uid, f"✅ Участник исключён: {await vk.user_name(target)}.")
        try:
            await vk.send(int(target), f"🚫 Вас исключили из класса «{cls['class_name']}».")
        except Exception:
            pass
        return

    # ---------- Погода ----------
    if act == "show_weather":
        await vk.send(uid, await weather_now_text(user, (action.get("city") or None)))
        return
    if act == "weather_forecast":
        try:
            days = max(1, min(3, int(action.get("days") or 1)))
        except (TypeError, ValueError):
            days = 1
        await vk.send(uid, await weather_forecast_text(user, days, (action.get("city") or None)))
        return

    # ---------- Разделы / кнопки ----------
    if act == "open_section":
        section = (action.get("section") or "").strip().lower()
        target = SECTION_ALIASES.get(section)
        if not target:
            # Пытаемся найти по всем кнопкам
            for name in get_all_user_button_names(uid):
                if section and section in name.lower():
                    target = name
                    break
        if not target:
            return await vk.send(uid, "⚠️ Раздел не найден.")
        await handle_main_menu(user, target, {})
        return

    if act == "show_vip":
        subs = user.get("subscriptions") or {}
        if subs:
            lines = ["👑 Ваши подписки:"]
            for k, v in subs.items():
                lines.append(f"• {k}: до {v}")
            await vk.send(uid, "\n".join(lines))
        else:
            await vk.send(uid, "👑 Активных подписок нет.")
        return

    if act == "show_class_code":
        if not cls:
            return await vk.send(uid, "🎓 Вы не в классе.")
        await vk.send(uid, f"🔑 Код класса «{cls['class_name']}»: {cls['class_code']}")
        return

    if act == "create_button":
        name = (action.get("name") or "").strip()
        content = (action.get("content") or "").strip()
        if not name or not content:
            return await vk.send(uid, "Для создания кнопки нужно название и содержимое. "
                                      "Что сохранить в кнопке?")
        btns = get_personal_buttons(uid)
        max_btns = int(user.get("max_personal_buttons", 1)) + int(user.get("prepaid_buttons", 0))
        if len(btns) >= max_btns:
            price = get_price("button_base") + get_price("button_increment") * len(btns)
            if int(user.get("prepaid_buttons", 0)) > 0:
                pass  # предоплата есть
            elif not stars_spend(user, price, "personal_button"):
                return await vk.send(uid, f"⚠️ Лимит кнопок исчерпан. Создание ещё одной — "
                                          f"{price} ⭐ (у вас {user.get('stars_balance', 0)}).")
        btns.append({"name": clean_forbidden(name)[:40],
                     "content": clean_forbidden(content)[:2000],
                     "type": "url" if content.startswith("http") and action.get("type", "url") == "url" else "text",
                     "ts": now().strftime("%Y-%m-%d %H:%M")})
        _save_personal_buttons(uid, btns)
        if int(user.get("prepaid_buttons", 0)) > 0:
            user["prepaid_buttons"] = int(user.get("prepaid_buttons", 0)) - 1
            save_user(user)
        await vk.send(uid, f"✅ Кнопка «{name}» создана — она в главном меню!")
        return

    if act == "delete_button":
        name = (action.get("name") or "").strip()
        btns = get_personal_buttons(uid)
        new_btns = [b for b in btns if b.get("name") != name]
        if len(new_btns) != len(btns):
            _save_personal_buttons(uid, new_btns)
            await vk.send(uid, f"🗑 Кнопка «{name}» удалена.")
        else:
            await vk.send(uid, "⚠️ Кнопка не найдена.")
        return

    if act in ("hide_button", "show_button"):
        name = (action.get("name") or "").strip()
        names = get_all_user_button_names(uid)
        # ищем точное или частичное совпадение
        target = None
        for n in names:
            if n == name:
                target = n
                break
        if not target:
            for n in names:
                if name and name.lower() in n.lower():
                    target = n
                    break
        if not target:
            return await vk.send(uid, "⚠️ Кнопка не найдена.")
        hidden = list(user.get("hidden_buttons") or [])
        if act == "hide_button":
            if target not in hidden:
                hidden.append(target)
            user["hidden_buttons"] = hidden
            save_user(user)
            await vk.send(uid, f"🙈 Кнопка «{target}» скрыта (вернуть — «покажи кнопку {target}»).")
        else:
            if target in hidden:
                hidden.remove(target)
            user["hidden_buttons"] = hidden
            save_user(user)
            await vk.send(uid, f"👁 Кнопка «{target}» снова в меню!")
        return

    if act == "rename_button":
        old = (action.get("name") or "").strip()
        new = (action.get("new_name") or "").strip()
        if not old or not new:
            return await vk.send(uid, "⚠️ Нужно старое и новое название.")
        rename_map = user.get("custom_buttons") or {}
        rename_map[old] = clean_forbidden(new)[:40]
        user["custom_buttons"] = rename_map
        save_user(user)
        await vk.send(uid, f"✏️ Кнопка «{old}» переименована в «{new}».")
        return

    if act == "move_button":
        name = (action.get("name") or "").strip()
        try:
            pos = max(1, int(action.get("position") or 1))
        except (TypeError, ValueError):
            pos = 1
        order = [n for n in (user.get("custom_button_order") or []) if n != name]
        all_names = [n for n in get_all_user_button_names(uid) if n not in order]
        if name in all_names:
            all_names.remove(name)
        order = order + all_names[:0]
        full = [n for n in (user.get("custom_button_order") or [])]
        if name not in full:
            full.append(name)
        full.remove(name)
        full.insert(min(pos - 1, len(full)), name)
        user["custom_button_order"] = full
        save_user(user)
        await vk.send(uid, f"✅ Кнопка «{name}» теперь на позиции {pos}.")
        return

    if act == "ai_mode":
        mode = action.get("mode")
        if not mode:
            p = user.get("ai_persona", "normal")
            await vk.send(uid, f"🤖 Текущий режим ИИ: {AI_PERSONA_MODES.get(p, AI_PERSONA_MODES['normal'])['title']}")
            return
        if mode not in AI_PERSONA_MODES:
            return await vk.send(uid, "⚠️ Режимы: normal / hamlo / warm.")
        user["ai_persona"] = mode
        save_user(user)
        await vk.send(uid, f"✅ Режим ИИ: {AI_PERSONA_MODES[mode]['title']} "
                           f"({AI_PERSONA_MODES[mode]['description']}).")
        return

    # ---------- Анонимки ----------
    if act == "send_anon":
        name = (action.get("name") or "").strip()
        text = (action.get("text") or "").strip()
        if not cls:
            return await vk.send(uid, "🎓 Анонимки работают внутри класса.")
        target = None
        for m in cls.get("members", []):
            if str(m) != str(uid) and name.lower() in (await vk.user_name(m)).lower():
                target = str(m)
                break
        if not target:
            return await vk.send(uid, f"⚠️ Одноклассник «{name}» не найден.")
        anon = _load_anon()
        anon.setdefault(target, []).append({
            "id": secrets.token_hex(5),
            "text": clean_forbidden(text)[:1500],
            "from": str(uid),
            "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
            "read": False,
        })
        _save_anon(anon)
        await vk.send(uid, "🕵️ Анонимное сообщение доставлено!")
        try:
            await vk.send(int(target),
                          f"🕵️ Вам пришло АНОНИМНОЕ сообщение!\n\n«{clean_forbidden(text)[:1000]}»")
        except Exception:
            pass
        return

    # ---------- Профиль / город / ДР ----------
    if act == "set_city":
        city = (action.get("city") or "").strip()
        if len(city) < 2:
            return await vk.send(uid, "⚠️ Некорректный город.")
        user["city"] = clean_forbidden(city).title()
        save_user(user)
        await detect_city_tz(user)
        await vk.send(uid, f"✅ Город: {user['city']}. Время уведомлений подстроено!")
        return

    if act == "set_birthday":
        d = parse_date_or_none(action.get("date") or "")
        if not d:
            return await vk.send(uid, "Уточните, пожалуйста, год рождения (например: 14.03.2009).")
        if (now().date() - d).days / 365.25 < 13:
            return await vk.send(uid, "🔞 Возраст меньше 13 лет — так нельзя (152-ФЗ).")
        user["birthday"] = d.isoformat()
        save_user(user)
        await vk.send(uid, f"✅ День рождения: {fmt_date(d)}.")
        return

    if act == "show_profile":
        ref_link = f"https://vk.me/club{vk.group_id}?ref=ref_{uid}"
        await vk.send(uid,
            f"👤 Ваш профиль:\n\n"
            f"⭐ Баланс: {user.get('stars_balance', 0)} (потрачено {user.get('total_stars_spent', 0)})\n"
            f"🌍 Город: {user.get('city') or '—'}\n"
            f"🎂 ДР: {user.get('birthday') or '—'}\n"
            f"🎓 Класс: {(cls or {}).get('class_name') or 'не в классе'}\n"
            f"☁️ Файлов в облаке: {len(cloud_user_files(uid))}\n"
            f"🔐 В сейфе: {len(vault_files_list(uid))}\n"
            f"👥 Рефералов: {user.get('referrals_count', 0)}\n"
            f"🔗 {ref_link}")
        return

    if act == "show_referrals":
        ref_link = f"https://vk.me/club{vk.group_id}?ref=ref_{uid}"
        await vk.send(uid,
            f"🎁 Ваши рефералы: {user.get('referrals_count', 0)} чел. "
            f"(+{REFERRAL_REWARD_STARS} ⭐ за каждого)\n\n"
            f"Пригласите друзей по ссылке:\n{ref_link}\n\n"
            f"Друг должен перейти по ней, нажать «Написать» и отправить любое сообщение — "
            f"звёзды начислятся автоматически.")
        return

    if act == "show_storage":
        files = cloud_user_files(uid)
        await vk.send(uid,
            f"☁️ Облако: {len(files)} файлов, {_fmt_size(cloud_total_size(uid))}.\n"
            f"🔐 Сейф: {len(vault_files_list(uid))} зашифрованных файлов "
            f"({_fmt_size(vault_total_size(uid))}).")
        return

    if act == "vault_status":
        await vk.send(uid,
            f"🔐 Сейф: {len(vault_files_list(uid))} зашифрованных файлов "
            f"({_fmt_size(vault_total_size(uid))}). Имена скрыты — они зашифрованы.")
        return

    if act == "show_favorites":
        favs = [f for f in cloud_user_files(uid) if (user.get("miniapp_lists") or {})
                .get("favorites", []).__contains__(f["id"])]
        if not favs:
            return await vk.send(uid, "⭐ Избранных файлов нет (отметьте их звёздочкой в веб-облаке).")
        await vk.send(uid, "⭐ Избранные файлы:\n" +
                     "\n".join(f"• {f['name']}" for f in favs[:20]))
        return

    if act == "message_developer":
        text = (action.get("text") or "").strip()
        if not text:
            return await vk.send(uid, "Что передать разработчику? Напишите текст.")
        if DEVELOPER_ID:
            try:
                await vk.send(int(DEVELOPER_ID),
                              f"💬 Сообщение от {await vk.user_name(uid)} (vk{uid}):\n«{text[:1500]}»")
            except Exception:
                pass
        sm = load_data(SUPPORT_MESSAGES_FILE, [], expect="list")
        sm.append({"user": str(uid), "text": clean_forbidden(text)[:1500],
                   "ts": now().strftime("%Y-%m-%d %H:%M")})
        save_data(SUPPORT_MESSAGES_FILE, sm)
        await vk.send(uid, "✅ Сообщение передано разработчику. Ответ придёт сюда.")
        return

    if act == "show_bot_stats":
        if not is_developer:
            return await vk.send(uid, "⛔ Только разработчик.")
        return await dev_stats(user)

    # ---------- Только для разработчика ----------
    if act in ("backup_now",):
        if not is_developer:
            return await vk.send(uid, "⛔ Только разработчик.")
        return await dev_do_backup(user)

    if act == "import_holidays":
        if not is_developer:
            return await vk.send(uid, "⛔ Только разработчик.")
        holidays = {} if action.get("mode") == "replace" else load_holidays()
        for item in (action.get("items") or []):
            d = parse_date_or_none(item.get("date") or "")
            name = (item.get("name") or "").strip()
            if d and name:
                holidays[f"{d.day:02d}-{d.month:02d}"] = clean_forbidden(name)[:60]
        save_data(HOLIDAYS_FILE, holidays)
        log_admin_action(uid, "import_holidays", f"{len(holidays)} шт.")
        await vk.send(uid, f"✅ Праздники сохранены: {len(holidays)} шт. "
                           f"Поздравления разойдутся автоматически.")
        return

    if act == "delete_holiday":
        if not is_developer:
            return await vk.send(uid, "⛔ Только разработчик.")
        holidays = load_holidays()
        key = (action.get("date") or "").strip()
        name = (action.get("name") or "").strip()
        removed = False
        if key:
            d = parse_date_or_none(key)
            k = f"{d.day:02d}-{d.month:02d}" if d else key
            removed = holidays.pop(k, None) is not None
        elif name:
            for k in list(holidays.keys()):
                if name.lower() in holidays[k].lower():
                    holidays.pop(k)
                    removed = True
        save_data(HOLIDAYS_FILE, holidays)
        await vk.send(uid, "🗑 Праздник удалён." if removed else "⚠️ Праздник не найден.")
        return

    if act == "show_holidays_all":
        holidays = load_holidays()
        if not holidays:
            return await vk.send(uid, "🎉 Праздники пока не заданы.")
        lines = ["🎉 Праздники бота:"]
        for k in sorted(holidays.keys(), key=lambda x: (int(x.split("-")[1]), int(x.split("-")[0]))):
            lines.append(f"• {k}: {holidays[k]}")
        await vk.send(uid, "\n".join(lines))
        return

    if act == "broadcast":
        if not is_developer:
            return await vk.send(uid, "⛔ Только разработчик.")
        text = (action.get("text") or "").strip()
        if not text:
            return await vk.send(uid, "⚠️ Пустой текст рассылки.")
        sent = 0
        for uid2, u in all_users().items():
            if u.get("is_blocked") or not u.get("setup_completed"):
                continue
            if await vk.send(int(uid2), f"📢 {clean_forbidden(text)[:3000]}") != 0:
                sent += 1
            await asyncio.sleep(0.05)
        log_admin_action(uid, "agent_broadcast", f"{sent} чел.")
        await vk.send(uid, f"📢 Рассылка: доставлено {sent}.")
        return

    if act == "set_price":
        if not is_developer:
            return await vk.send(uid, "⛔ Только разработчик.")
        key = (action.get("key") or "").strip()
        try:
            value = int(action.get("value"))
        except (TypeError, ValueError):
            return await vk.send(uid, "⚠️ Некорректное значение цены.")
        prices = load_data(PRICES_FILE, {}, expect="dict")
        prices[key] = value
        save_data(PRICES_FILE, prices)
        log_admin_action(uid, "set_price", f"{key}={value}")
        await vk.send(uid, f"💰 Цена «{key}» = {value}.")
        return

    if act == "create_global_button":
        if not is_developer:
            return await vk.send(uid, "⛔ Только разработчик.")
        name = (action.get("name") or "").strip()
        content = (action.get("content") or "").strip()
        if not name or not content:
            return await vk.send(uid, "Нужно название и содержимое кнопки.")
        gbtns = get_global_buttons()
        gbtns.append({"name": clean_forbidden(name)[:40],
                      "content": clean_forbidden(content)[:2000],
                      "type": action.get("type") or ("url" if content.startswith("http") else "text"),
                      "ts": now().strftime("%Y-%m-%d %H:%M")})
        save_data(GLOBAL_BUTTONS_FILE, gbtns)
        log_admin_action(uid, "create_global_button", name)
        await vk.send(uid, f"🌐 Глобальная кнопка «{name}» создана — теперь она у всех!")
        return

    if act == "delete_global_button":
        if not is_developer:
            return await vk.send(uid, "⛔ Только разработчик.")
        name = (action.get("name") or "").strip()
        gbtns = [b for b in get_global_buttons() if b.get("name") != name]
        save_data(GLOBAL_BUTTONS_FILE, gbtns)
        await vk.send(uid, "🗑 Глобальная кнопка удалена.")
        return

    if act in ("block_user", "unblock_user", "grant_stars"):
        if not is_developer:
            return await vk.send(uid, "⛔ Только разработчик.")
        target = await _agent_find_user(user, cls, action.get("user") or "")
        if not target:
            return await vk.send(uid, "⚠️ Пользователь не найден.")
        tu = get_user(target)
        if act == "block_user":
            hours = action.get("hours")
            reason = (action.get("reason") or "").strip()
            tu["is_blocked"] = True
            tu["block_reason"] = clean_forbidden(reason)[:200]
            if hours:
                tu["block_until"] = (now() + timedelta(hours=float(hours))).strftime("%Y-%m-%d %H:%M")
            else:
                tu["block_until"] = None
            save_user(tu)
            log_admin_action(uid, "block_user", f"vk{target} {hours}ч")
            await vk.send(uid, f"🚫 vk{target} заблокирован" + (f" на {hours} ч." if hours else " навсегда."))
        elif act == "unblock_user":
            tu["is_blocked"] = False
            tu["block_until"] = None
            tu["block_reason"] = ""
            save_user(tu)
            log_admin_action(uid, "unblock_user", f"vk{target}")
            await vk.send(uid, f"✅ vk{target} разблокирован.")
        else:
            try:
                amount = int(action.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0
            tu["stars_balance"] = int(tu.get("stars_balance", 0)) + amount
            save_user(tu)
            log_admin_action(uid, "grant_stars", f"vk{target} {amount:+d}")
            await vk.send(uid, f"⭐ vk{target}: баланс {tu['stars_balance']} ⭐ ({amount:+d}).")
            try:
                word = "начислено" if amount >= 0 else "списано"
                await vk.send(int(target), f"⭐ Вам {word} {abs(amount)} звёзд.")
            except Exception:
                pass
        return

    # ---------- Уточнение / прочее ----------
    if act == "clarify":
        await vk.send(uid, (action.get("question") or "Уточните, пожалуйста.").strip()[:500])
        return

    if act == "none":
        await vk.send(uid, (action.get("answer") or "Готово!").strip()[:1500])
        return

    # Неизвестное действие — дефолт в чат ИИ
    answer = await ai_chat_reply(user, str(action)[:1000])
    await vk.send(uid, answer)


# ==================================
# === РОУТЕР ВЛОЖЕНИЙ ===
# ==================================

async def route_attachments(user: dict, message: dict, state: str) -> bool:
    """Обработка фото/файлов/голосовых. True — обработано состоянием."""
    atts = message.get("attachments") or []
    if not atts:
        return False
    uid = int(user["user_id"])
    # В состояниях, где вложения не нужны — они обработаны самими состояниями
    # (ДЗ, сейф, решения, AI). Здесь: файл вне режимов → предложить облако.
    if state in (ST_MAIN, ST_SUB_GATE, ST_REG_INSTR, ST_REG_DISCLAIMER,
                 ST_REG_PDN, ST_REG_PARENT, ST_REG_BIRTHDAY, ST_REG_CITY,
                 ST_CLASS_MANAGEMENT, ST_USER_SETTINGS, ST_TIMER_SET_DATE,
                 ST_TIMER_SET_TIME, ST_TIMER_SET_TEXT):
        first = atts[0]
        t = first.get("type")
        if t == "photo":
            label = "фото"
        elif t == "doc" and (first.get("doc", {}).get("preview") or {}).get("audio_msg"):
            label = "голосовое сообщение"
        elif t == "doc":
            label = f"файл «{first.get('doc', {}).get('title', '?')}»"
        else:
            return False
        await vk.send(uid,
            f"📎 Вижу {label}.\nСохранить в ☁️ Облако? (файл останется доступным "
            f"из чата и веб-облака, когда подключите мини-апп)",
            keyboard=kb_inline([
                [("✅ Сохранить в облако", {"c": "cloud_save_att"}, "positive")],
                [("🔐 В сейф (зашифровать)", {"c": "vault_save_att"})],
                [("❌ Не нужно", {"c": "back_menu"}, "negative")]]))
        # Кладём вложения во временное хранилище (переживает рестарт)
        user["bg_pending"] = [
            {"type": a.get("type"),
             "owner": (a.get(a.get("type"), {}) or {}).get("owner_id"),
             "id": (a.get(a.get("type"), {}) or {}).get("id"),
             "title": (a.get("doc", {}) or {}).get("title", "")}
            for a in atts[:5] if a.get("type") in ("photo", "doc")
        ]
        save_user(user)
        return True
    return False


async def cloud_save_pending(user: dict) -> None:
    """Сохраняет файлы из bg_pending в облако (скачивая их через VK API)."""
    uid = int(user["user_id"])
    pending = user.get("bg_pending") or []
    if not pending:
        return await vk.send(uid, "⚠️ Файлов больше нет (истёк срок хранения вложений ВК). "
                                  "Пришлите файл ещё раз.")
    saved = 0
    for p in pending[:5]:
        if not p.get("owner") or not p.get("id"):
            continue
        att = {p["type"]: {"owner_id": p["owner"], "id": p["id"], "title": p.get("title", "")},
               "type": p["type"]}
        data, name, kind = await vk.download_attachment(att)
        if not data:
            continue
        if cloud_total_size(uid) + len(data) > CLOUD_MAX_TOTAL:
            await vk.send(uid, "⚠️ Лимит облака 200 МБ исчерпан — файл не сохранён.")
            break
        try:
            cloud_add_file(uid, name or f"file_{p['id']}", data, kind or "document", src="chat")
            saved += 1
        except VKApiError:
            await vk.send(uid, "⚠️ Достигнут лимит числа файлов облака.")
            break
    user["bg_pending"] = []
    save_user(user)
    clear_state(user)
    if saved:
        await vk.send(uid, f"☁️ Сохранено в облако: {saved} шт. Файлы видно в ☁️ Облако → 📁 Мои файлы.")
    else:
        await vk.send(uid, "😔 Не удалось скачать файлы (ВК хранит вложения ограниченное время). "
                           "Пришлите файл заново и сохраните сразу.")


async def vault_save_pending(user: dict) -> None:
    """Перенаправляет файлы из bg_pending в сейф (через пароль)."""
    uid = int(user["user_id"])
    if not vault_user_index(uid).get("salt"):
        await vk.send(uid, "🔐 Сейф ещё не создан. Сначала задайте пароль:",
                      keyboard=kb_inline([[("🔑 Задать пароль", {"c": "vault_setpass"}, "positive")]]))
        set_state(user, ST_VAULT_PASSWORD)
        return
    await vk.send(uid, "🔑 Введите пароль сейфа для шифрования файла:",
                  keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
    set_state(user, ST_VAULT_PUT_WAIT)
    user["fsm"]["from_bg"] = True
    save_user(user)


@state_handler(ST_SUPPORT_CHAT)
async def support_chat_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel", "выход"):
        clear_state(user)
        return await send_main_menu(user, "👋 Чат поддержки закрыт.")
    bad = reject_if_forbidden(text)
    if bad:
        await vk.send(uid, bad)
        return True
    code = generate_user_code(uid)
    sm = load_data(SUPPORT_MESSAGES_FILE, [], expect="list")
    sm.append({"user": str(uid), "code": code, "text": clean_forbidden(text.strip())[:2000],
               "ts": now().strftime("%Y-%m-%d %H:%M"), "answered": False})
    save_data(SUPPORT_MESSAGES_FILE, sm)
    if DEVELOPER_ID:
        try:
            await vk.send(int(DEVELOPER_ID),
                          f"💬 ПОДДЕРЖКА · {await vk.user_name(uid)} (vk{uid}, код {code}):\n"
                          f"«{text.strip()[:1500]}»")
        except Exception:
            pass
    await vk.send(uid, "✅ Сообщение отправлено разработчику. Ответ придёт прямо сюда.")
    clear_state(user)
    return await send_main_menu(user)


# ==================================
# === CALLBACK-РОУТЕР (кнопки) ===
# ==================================

async def handle_callback(user: dict, payload: dict, event: dict) -> None:
    """Маршрутизация нажатий callback-кнопок (message_event)."""
    uid = int(user["user_id"])
    cmd = payload.get("c", "")

    # --- Общие ---
    if cmd == "back_menu":
        clear_state(user)
        return await send_main_menu(user)
    if cmd == "menu":
        clear_state(user)
        return await handle_main_menu(user, payload.get("b", "⬅️ Назад в меню"), {})
    if cmd == "noop":
        return None

    # --- Регистрация ---
    if cmd == "reg_next":
        state = user.get("state")
        if state == ST_REG_INSTR:
            return await reg_next_step(user, "disclaimer")
        if state == ST_REG_DISCLAIMER:
            return await reg_next_step(user, "pdn")
        if state == ST_REG_PDN:
            user["pdn_accepted"] = True
            user["pdn_consent_at"] = now().strftime("%Y-%m-%d %H:%M:%S")
            user["pdn_policy_version"] = PDN_POLICY_VERSION
            save_user(user)
            return await reg_next_step(user, "birthday")
        if state == ST_REG_PARENT:
            user["parent_consent"] = True
            save_user(user)
            return await reg_next_step(user, "city")
        return None
    if cmd == "reg_cancel":
        await vk.send(uid, "😢 Жаль. Данные не сохранены. Чтобы начать заново — "
                           "отправьте любое сообщение.")
        return set_state(user, ST_SUB_GATE)
    if cmd == "reg_skip_city":
        return await finish_registration(user)
    if cmd == "check_sub":
        if await is_user_subscribed(uid):
            await _clear_gate_fails(uid)
            _SUB_CACHE.pop(str(uid), None)
            user["subscription_confirmed_at"] = now().strftime("%Y-%m-%d %H:%M:%S")
            save_user(user)
            if user.get("setup_completed"):
                clear_state(user)
                return await send_main_menu(user, "✅ Спасибо за подписку! Бот снова работает.")
            return await start_registration(user)
        # не подписан → лестница доверия
        fails = await _record_gate_fail(uid)
        if fails >= SUB_GATE_FAILS_LIMIT:
            await vk.send(uid, "🤝 Ладно, верю вам на слово — пускаю. "
                               "Подпишитесь, пожалуйста, чуть позже!")
            user["subscription_confirmed_at"] = now().strftime("%Y-%m-%d %H:%M:%S")
            save_user(user)
            if user.get("setup_completed"):
                clear_state(user)
                return await send_main_menu(user)
            return await start_registration(user)
        await vk.send(uid, "❌ Подписка ещё не видна. Подпишитесь на сообщество и "
                           "нажмите кнопку ещё раз.")
        return None
    if cmd == "open_group":
        link_btn = {"action": {"type": "open_link",
                               "link": f"https://vk.com/club{vk.group_id}",
                               "label": "🌐 Открыть сообщество",
                               "payload": json.dumps({"c": "noop"})}}
        await vk.send(uid, "🌐 Откройте сообщество и нажмите «Подписаться»:",
                      keyboard=kb_inline([[link_btn],
                          [("✅ Я подписался(ась)", {"c": "check_sub"}, "positive")]]))
        return None

    # --- Погода ---
    if cmd == "weather_3days":
        return await vk.send(uid, await weather_forecast_text(user, 3))

    # --- ДЗ ---
    if cmd == "hw_add":
        return await hw_add_start(user)
    if cmd == "hw_date":
        d = _iso_date(payload.get("d", ""))
        if d:
            return await hw_add_subject_step(user, d)
        return None
    if cmd == "hw_subj":
        return await hw_add_text_step(user, payload.get("s", "?"))
    if cmd == "hw_subj_custom":
        await vk.send(uid, "✍️ Напишите название предмета:")
        return None
    if cmd == "hw_del":
        return await hw_del_start(user)
    if cmd == "hw_del_date":
        return await hw_del_date(user, payload.get("d", ""))
    if cmd == "hw_del_one":
        cls = get_class_by_user(uid)
        if cls and is_class_admin(uid, cls):
            hw = cls.setdefault("homework", {})
            day = hw.get(payload.get("d", "")) or {}
            day.pop(payload.get("s", ""), None)
            if not day:
                hw.pop(payload.get("d", ""), None)
            save_class(cls)
            log_admin_action(uid, "del_hw", f"{payload.get('s')} {payload.get('d')}")
            await vk.send(uid, f"🗑 ДЗ по «{payload.get('s')}» удалено.")
            return await hw_del_start(user)
        return await vk.send(uid, "⛔ Только админ.")
    if cmd == "hw_del_day":
        cls = get_class_by_user(uid)
        if cls and is_class_admin(uid, cls):
            cls.setdefault("homework", {}).pop(payload.get("d", ""), None)
            save_class(cls)
            await vk.send(uid, "🗑 Все ДЗ на день удалены.")
            return await hw_del_start(user)
        return await vk.send(uid, "⛔ Только админ.")
    if cmd == "hw_del_all":
        cls = get_class_by_user(uid)
        if cls and is_class_admin(uid, cls):
            cls["homework"] = {}
            save_class(cls)
            log_admin_action(uid, "clear_hw", "")
            return await vk.send(uid, "🗑 ВСЁ ДЗ удалено.", keyboard=back_kb())
        return await vk.send(uid, "⛔ Только админ.")

    # --- Звонки / учителя / каникулы / расписание ---
    if cmd == "bells_edit":
        return await bells_edit_start(user)
    if cmd == "bells_clear":
        cls = get_class_by_user(uid)
        if cls and is_class_admin(uid, cls):
            cls["bells"] = []
            cls["bells_by_day"] = {}
            cls["bells_by_date"] = {}
            save_class(cls)
            log_admin_action(uid, "bells_clear", "")
            clear_state(user)
            await vk.send(uid, "🧹 Звонки очищены.")
            return await send_main_menu(user)
        return await vk.send(uid, "⛔ Только админ.")
    if cmd == "teachers_edit":
        return await teachers_edit_start(user)
    if cmd == "holidays_set":
        return await holidays_set_start(user)
    if cmd == "holidays_off":
        cls = get_class_by_user(uid)
        if cls and is_class_admin(uid, cls):
            cls["holidays"] = None
            save_class(cls)
            await vk.send(uid, "✅ Каникулы сняты.")
            await notify_class(cls, uid, "📅 Каникулы закончились — расписание снова работает!")
            return await handle_main_menu(user, "🎉 Каникулы", {})
        return await vk.send(uid, "⛔ Только админ.")
    if cmd == "sched_day":
        return await send_day_schedule_screen(user, payload.get("d", ""))
    if cmd == "adm_edit_schedule":
        return await admin_edit_schedule(user)
    if cmd == "adm_sched_day":
        await vk.send(uid,
                      f"📅 Расписание на {payload.get('d')}.\n"
                      f"Пришлите список уроков (каждый с новой строки): «1. Математика» …\n"
                      f"«пусто» или «-» — убрать уроки.",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_EDIT_SCHEDULE_CONTENT, {"day": payload.get("d")})

    # --- Таймеры / помодоро ---
    if cmd == "tmr_quick":
        return await timer_quick_add(user, int(payload.get("m", 5)))
    if cmd == "tmr_custom":
        return await timer_custom_start(user)
    if cmd == "tmr_list_del":
        return await timer_list_delete(user)
    if cmd == "tmr_del":
        timers = _load_timers()
        lst = timers.get(str(uid)) or []
        timers[str(uid)] = [t for t in lst if t.get("id") != payload.get("i")]
        _save_timers(timers)
        await vk.send(uid, "🗑 Таймер удалён.")
        return await timer_menu(user)
    if cmd == "pomodoro":
        return await pomodoro_menu(user)
    if cmd == "pomo_start":
        return await pomodoro_start(user, int(payload.get("w", 25)), int(payload.get("r", 5)))
    if cmd == "pomo_stop":
        data = _load_pomodoro()
        entry = data.get(str(uid))
        if entry:
            entry["active"] = False
            entry["phase"] = "stopped"
            data[str(uid)] = entry
            save_data(POMODORO_FILE, data)
            await vk.send(uid, f"⏹ Помодоро остановлен. Циклов завершено: {entry.get('cycles', 0)}.")
        return await timer_menu(user)

    # --- Анонимки ---
    if cmd == "anon_to":
        return await anon_compose(user, int(payload.get("id", 0)))
    if cmd == "anon_page":
        return await anon_inbox(user, int(payload.get("p", 0)))
    if cmd == "anon_view":
        msgs = _load_anon().get(str(uid)) or []
        m = next((x for x in msgs if x.get("id") == payload.get("i")), None)
        if not m:
            return await vk.send(uid, "⚠️ Сообщение не найдено.")
        if m.get("sender_viewed"):
            sender = await vk.user_name(m.get("from"))
            return await vk.send(uid, f"👁 Отправитель: {sender} (vk{m.get('from')})")
        price = get_price("view_sender")
        if not stars_spend(user, price, "view_sender"):
            return await vk.send(uid, f"⚠️ Нужно {price} ⭐ (у вас {user.get('stars_balance', 0)}). "
                                      f"Пригласите друзей по ссылке из ⭐ Звезды!")
        m["sender_viewed"] = True
        anon = _load_anon()
        lst = anon.get(str(uid)) or []
        for i, x in enumerate(lst):
            if x.get("id") == m["id"]:
                lst[i] = m
        anon[str(uid)] = lst
        _save_anon(anon)
        sender = await vk.user_name(m.get("from"))
        return await vk.send(uid, f"👁 Отправитель: {sender} (vk{m.get('from')})")
    if cmd == "anon_reply":
        msgs = _load_anon().get(str(uid)) or []
        m = next((x for x in msgs if x.get("id") == payload.get("i")), None)
        if not m:
            return await vk.send(uid, "⚠️ Сообщение не найдено.")
        await vk.send(uid, "✉️ Напишите ответ — доставлю отправителю (он не узнает, что это вы):")
        return set_state(user, ST_ANON_SEND_MESSAGE, {"to": m.get("from")})

    # --- Классы ---
    if cmd == "class_create":
        if user.get("class_code"):
            return await vk.send(uid, "🚪 Сначала выйдите из текущего класса.")
        await vk.send(uid, "🎓 Введите название класса (например: «9А» или «Физмат 11»):",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_CREATE_CLASS_NAME)
    if cmd == "class_join":
        if user.get("class_code"):
            return await vk.send(uid, "🚪 Сначала выйдите из текущего класса.")
        await vk.send(uid, "🔗 Введите код класса (формат: ABC-123):",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_JOIN_CLASS)
    if cmd == "class_users":
        return await class_show_users(user)
    if cmd == "class_leave":
        cls = get_class_by_user(uid)
        if not cls:
            return await send_main_menu(user)
        cls["members"] = [m for m in cls.get("members", []) if str(m) != str(uid)]
        cls["admins"] = [a for a in cls.get("admins", []) if str(a) != str(uid)]
        save_class(cls)
        user["class_code"] = None
        save_user(user)
        clear_state(user)
        await vk.send(uid, f"🚪 Вы вышли из класса «{cls['class_name']}».")
        return await send_main_menu(user)
    if cmd == "account_delete":
        users = load_data(USERS_FILE, {}, expect="dict")
        users.pop(str(uid), None)
        # чистим личные кнопки, таймеры, облако
        pb = load_data(PERSONAL_BUTTONS_FILE, {}, expect="dict")
        pb.pop(str(uid), None)
        save_data(PERSONAL_BUTTONS_FILE, pb)
        timers = _load_timers()
        timers.pop(str(uid), None)
        _save_timers(timers)
        idx = cloud_index()
        for f in (idx.get(str(uid)) or []):
            try:
                if f.get("path") and os.path.exists(f["path"]):
                    os.remove(f["path"])
            except OSError:
                pass
        idx.pop(str(uid), None)
        cloud_save_index(idx)
        vidx = vault_index()
        for f in (vidx.get(str(uid), {}).get("files") or []):
            try:
                os.remove(os.path.join(VAULT_DIR, str(uid), f["id"] + ".dvf"))
            except OSError:
                pass
        vidx.pop(str(uid), None)
        vault_save_index(vidx)
        user_new = dict(DEFAULT_USER)
        user_new["user_id"] = str(uid)
        user_new["joined_date"] = now().strftime("%Y-%m-%d %H:%M")
        users[str(uid)] = user_new
        save_data(USERS_FILE, users)
        _DATA_CACHE.pop(USERS_FILE, None)
        await vk.send(uid, "🔓 Аккаунт удалён. Все данные стёрты. "
                           "Напишите «старт», чтобы начать заново.")
        return None

    # --- Настройки ---
    if cmd == "set_notif":
        user["notifications"] = not user.get("notifications", True)
        save_user(user)
        return await user_settings(user)
    if cmd == "set_times":
        kb = [
            [("🌅 Время утра", {"c": "set_time", "k": "morning_notification_time"}),
             ("🌙 Время вечера", {"c": "set_time", "k": "evening_notification_time"})],
            [("🌅 Текст утра", {"c": "set_text", "k": "morning_text"}),
             ("🌙 Текст вечера", {"c": "set_text", "k": "evening_text"})],
            [("⬅️ Назад", {"c": "menu", "b": "⚙️ Настройки"})],
        ]
        return await vk.send(uid, "🌅/🌙 Время и тексты утренних/вечерних сообщений:", keyboard=kb_inline(kb))
    if cmd == "set_time":
        which = {"morning_notification_time": ST_SET_MORNING_TIME,
                 "evening_notification_time": ST_SET_EVENING_TIME,
                 "weather_notification_time": ST_SET_WEATHER_TIME,
                 "birthday_notification_time": ST_SET_BIRTHDAY_NOTIF_TIME}.get(payload.get("k"))
        if which:
            await vk.send(uid, "🕐 Введите время ЧЧ:ММ (например: 08:30):")
            set_state(user, which)
        return None
    if cmd == "set_text":
        which = {"morning_text": ST_SET_MORNING_TEXT,
                 "evening_text": ST_SET_EVENING_TEXT}.get(payload.get("k"))
        if which:
            await vk.send(uid, "📝 Введите текст (до 200 символов):")
            set_state(user, which)
        return None
    if cmd == "set_weather":
        kb = [
            [("🌦 Утренняя погода", {"c": "set_weather_n"}),
             ("🌍 Город", {"c": "set_city"})],
            [("⬅️ Назад", {"c": "menu", "b": "⚙️ Настройки"})],
        ]
        return await vk.send(uid, "🌦 Погодные настройки:", keyboard=kb_inline(kb))
    if cmd == "set_weather_n":
        user["weather_notifications"] = not user.get("weather_notifications", True)
        save_user(user)
        return await user_settings(user)
    if cmd == "set_city":
        await vk.send(uid, "🌍 Напишите ваш город (например: Москва):",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_CHANGE_CITY)
    if cmd == "set_bday":
        kb = [
            [("🎂 Дата ДР", {"c": "set_bday_date"}),
             ("🔔 Напоминание о ДР", {"c": "set_bday_notif_t"})],
        ]
        if user.get("birthday"):
            age = (now().date() - _iso_date(user["birthday"])).days // 365
            kb.append([(f"👤 Показывать классу: {'да' if user.get('show_birthday_to_class') else 'нет'}",
                        {"c": "set_bday_class"})])
        kb.append([("⬅️ Назад", {"c": "menu", "b": "⚙️ Настройки"})])
        return await vk.send(uid, "🎂 Настройки дня рождения:", keyboard=kb_inline(kb))
    if cmd == "set_bday_date":
        await vk.send(uid, "🎂 Введите дату рождения ДД.ММ.ГГГГ:",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_SET_BIRTHDAY)
    if cmd == "set_bday_notif_t":
        await vk.send(uid, "🕐 Во сколько напоминать о вашем ДР? (ЧЧ:ММ)")
        return set_state(user, ST_SET_BIRTHDAY_NOTIF_TIME)
    if cmd == "set_bday_class":
        user["show_birthday_to_class"] = not user.get("show_birthday_to_class", True)
        save_user(user)
        return await handle_callback(user, {"c": "set_bday"}, event)
    if cmd == "set_buttons":
        kb = [
            [("👁 Скрыть/показать кнопки", {"c": "btn_visibility"}),
             ("✏️ Переименовать", {"c": "btn_rename"})],
            [("🔄 Порядок кнопок", {"c": "btn_reorder"}),
             ("🎨 Раскладка", {"c": "btn_layout"})],
            [("🌈 Цветные кнопки", {"c": "btn_colors"}),
             ("💡 Предложить функцию", {"c": "btn_suggest"})],
            [("⬅️ Назад", {"c": "menu", "b": "⚙️ Настройки"})],
        ]
        return await vk.send(uid, "🎨 Кнопки меню:", keyboard=kb_inline(kb))
    if cmd == "btn_colors":
        user["colored_buttons_enabled"] = not user.get("colored_buttons_enabled", True)
        save_user(user)
        await vk.send(uid, f"🌈 Цветные кнопки: {'включены' if user['colored_buttons_enabled'] else 'выключены'}.")
        return await send_main_menu(user)
    if cmd == "btn_layout":
        layout = payload.get("v") or ({"default": "compact", "compact": "wide", "wide": "default"}.get(user.get("button_layout", "default")))
        user["button_layout"] = layout if layout in ("default", "compact", "wide") else "default"
        save_user(user)
        kb = [
            [("3 в ряд" if user["button_layout"] == "default" else "⬜ 3 в ряд",
              {"c": "btn_layout", "v": "default"}),
             ("⬜ 4 в ряд" if user["button_layout"] != "compact" else "✅ 4 в ряд",
              {"c": "btn_layout", "v": "compact"})],
            [("⬜ 2 в ряд" if user["button_layout"] != "wide" else "✅ 2 в ряд",
              {"c": "btn_layout", "v": "wide"})],
            [("⬅️ Назад", {"c": "menu", "b": "⚙️ Настройки"})],
        ]
        return await vk.send(uid, f"🎨 Раскладка: {user['button_layout']} (по умолчанию 3, "
                                  f"compact — 4, wide — 2 в ряд).", keyboard=kb_inline(kb))
    if cmd == "btn_visibility":
        return await btn_visibility_menu(user)
    if cmd == "toggle_visibility":
        name = payload.get("n", "")
        hidden = list(user.get("hidden_buttons") or [])
        if name in hidden:
            hidden.remove(name)
        else:
            hidden.append(name)
        user["hidden_buttons"] = hidden
        save_user(user)
        return await btn_visibility_menu(user)
    if cmd == "finish_visibility":
        clear_state(user)
        await vk.send(uid, "✅ Настройки кнопок сохранены.")
        return await send_main_menu(user)
    if cmd == "btn_rename":
        await vk.send(uid, "✏️ Напишите: «старое название — новое название»\n"
                           "Например: «⏰ Таймер — ⏰ Будильник»",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_CHANGE_BUTTON_NAME)
    if cmd == "btn_reorder":
        await vk.send(uid, "🔄 Перемещение кнопок.\nНапишите: «название кнопки — позиция»\n"
                           "Например: «🌦 Погода — 1» (поставить первой)",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_REORDER_BUTTONS)
    if cmd == "btn_suggest":
        await vk.send(uid, "💡 Опишите функцию, которую хотите видеть в боте:",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_SUGGEST_FUNCTION)
    if cmd == "set_dnd":
        return await dnd_start(user)
    if cmd == "dnd_on":
        hours = payload.get("h", 1)
        if int(hours) == -1:
            until = now().replace(hour=8, minute=0, second=0, microsecond=0)
            if until < now():
                until += timedelta(days=1)
        else:
            until = now() + timedelta(hours=int(hours))
        user["dnd_until"] = until.strftime("%Y-%m-%d %H:%M")
        save_user(user)
        await vk.send(uid, f"🌙 Тишина до {until.strftime('%H:%M %d.%m')}.")
        return await send_main_menu(user)
    if cmd == "dnd_off":
        user["dnd_until"] = None
        save_user(user)
        await vk.send(uid, "🌅 Режим «Не беспокоить» выключен.")
        return await send_main_menu(user)
    if cmd == "set_webpw":
        await vk.send(uid, "🔑 Придумайте пароль для входа в веб-облако из браузера "
                           "(от 4 символов):",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_WEB_PW_ENTER)
    if cmd == "miniapp_open":
        if MINIAPP_URL:
            link_btn = {"action": {"type": "open_link", "link": MINIAPP_URL,
                                   "label": "☁️ Открыть веб-облако",
                                   "payload": json.dumps({"c": "noop"})}}
            return await vk.send(uid, f"☁️ Веб-облако DEVO+:\n{MINIAPP_URL}",
                                 keyboard=kb_inline([[link_btn],
                                     [("⬅️ Назад в меню", {"c": "back_menu"})]]))
        return await vk.send(uid, "☁️ Мини-апп не подключён (MINIAPP_URL не задан).")

    # --- Профиль / рефералы ---
    if cmd == "show_profile":
        return await agent_execute(user, {"action": "show_profile"})
    if cmd == "show_referrals":
        return await agent_execute(user, {"action": "show_referrals"})

    # --- Личные кнопки ---
    if cmd == "pb_create":
        btns = get_personal_buttons(uid)
        max_btns = int(user.get("max_personal_buttons", 1)) + int(user.get("prepaid_buttons", 0))
        if len(btns) >= max_btns:
            price = get_price("button_base") + get_price("button_increment") * len(btns)
            if not stars_spend(user, price, "personal_button"):
                return await vk.send(uid, f"⚠️ Создание кнопки — {price} ⭐, у вас "
                                          f"{user.get('stars_balance', 0)}. "
                                          f"Зарабатывайте звёзды приглашениями (⭐ Звезды)!")
            await vk.send(uid, f"⭐ Списано {price} за создание кнопки.")
        await vk.send(uid, "🌟 Название кнопки (например: «Шпаргалки»):",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_CREATE_PB_NAME)
    if cmd == "pb_del":
        btns = get_personal_buttons(uid)
        new_btns = [b for b in btns if b.get("name") != payload.get("n")]
        _save_personal_buttons(uid, new_btns)
        await vk.send(uid, f"🗑 Кнопка «{payload.get('n')}» удалена.")
        return await personal_buttons_menu(user)

    # --- Облако / сейф ---
    if cmd == "cloud_list":
        return await cloud_list_page(user, int(payload.get("p", 0)))
    if cmd == "cloud_dl":
        return await cloud_send_file(user, payload.get("i", ""))
    if cmd == "cloud_rm":
        if cloud_delete_file(uid, payload.get("i", "")):
            await vk.send(uid, "🗑 Файл удалён из облака.")
            return await cloud_list_page(user, 0)
        return await vk.send(uid, "⚠️ Файл не найден.")
    if cmd == "cloud_save_att":
        return await cloud_save_pending(user)
    if cmd == "vault_save_att":
        return await vault_save_pending(user)
    if cmd == "vault_setpass":
        await vk.send(uid, "🔑 Придумайте пароль сейфа (от 6 символов). ВАЖНО: забыли "
                           "пароль — файлы не восстановить!",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_VAULT_PASSWORD)
    if cmd == "vault_put":
        return await vault_put_wait_file(user)
    if cmd == "vault_get":
        return await vault_get_flow(user)
    if cmd == "vault_chpass":
        return await vault_chpass_flow(user)
    if cmd == "vault_out":
        # Просим пароль и достаём файл
        await vk.send(uid, "🔑 Введите пароль сейфа:",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_VAULT_GET_PASSWORD, {"put": False, "out_id": payload.get("i")})
    if cmd == "vault_clear":
        idx = vault_index()
        rec = idx.get(str(uid)) or {}
        for f in (rec.get("files") or []):
            try:
                os.remove(os.path.join(VAULT_DIR, str(uid), f["id"] + ".dvf"))
            except OSError:
                pass
        rec["files"] = []
        idx[str(uid)] = rec
        vault_save_index(idx)
        await vk.send(uid, "🧹 Сейф очищен (пароль остался).")
        return await vault_menu(user)

    # --- Решения ---
    if cmd == "sol_add":
        cls = get_class_by_user(uid)
        if not cls:
            return await vk.send(uid, "🎓 Решения — база класса.")
        await vk.send(uid, "📚 Добавление решения.\n\nПредмет? (например: «Алгебра»)")
        set_state(user, ST_SOL_WAIT_FILE, {"step": "subject"})
        return None
    if cmd == "sol_view":
        return await solution_view(user, payload.get("i", ""))
    if cmd == "sol_dl":
        cls = get_class_by_user(uid)
        if not cls:
            return
        s = next((x for x in (_load_solutions().get(cls["class_code"]) or [])
                  if x.get("id") == payload.get("i")), None)
        if not s:
            return await vk.send(uid, "⚠️ Решение не найдено.")
        path = os.path.join(SOLUTIONS_DIR, cls["class_code"], s.get("file", ""))
        try:
            with open(path, "rb") as f:
                await vk.send_file(uid, f.read(), s.get("file", "").split("__", 1)[-1])
        except FileNotFoundError:
            await vk.send(uid, "⚠️ Файл не найден на диске.")
        return None
    if cmd == "sol_del":
        cls = get_class_by_user(uid)
        if not cls:
            return
        sols = _load_solutions()
        lst = sols.get(cls["class_code"]) or []
        s = next((x for x in lst if x.get("id") == payload.get("i")), None)
        if s and (str(uid) == str(s.get("by")) or is_class_admin(uid, cls)):
            sols[cls["class_code"]] = [x for x in lst if x.get("id") != payload.get("i")]
            _save_solutions(sols)
            try:
                os.remove(os.path.join(SOLUTIONS_DIR, cls["class_code"], s.get("file", "")))
            except OSError:
                pass
            await vk.send(uid, "🗑 Решение удалено.")
            return await solutions_menu(user)
        return await vk.send(uid, "⛔ Удалять может автор или админ.")
    if cmd == "sol_comment":
        await vk.send(uid, "💬 Текст комментария:")
        return set_state(user, ST_SOL_COMMENT_WAIT, {"id": payload.get("i")})

    # --- Опросы ---
    if cmd == "poll_vote":
        polls = _load_polls()
        poll = polls.get(payload.get("p"))
        if not poll:
            return await vk.send(uid, "⚠️ Опрос не найден.")
        try:
            opt = int(payload.get("o"))
        except (TypeError, ValueError):
            return None
        if str(uid) in (poll.get("votes") or {}) and poll["votes"][str(uid)] == opt:
            # повторное нажатие — снимаем голос
            poll["votes"].pop(str(uid), None)
            action_txt = "Голос снят."
        else:
            poll.setdefault("votes", {})[str(uid)] = opt
            action_txt = f"✅ Голос за «{poll['options'][opt]}» учтён!"
        _save_polls(polls)
        await vk.send(uid, action_txt)
        return None
    if cmd == "poll_results":
        return await poll_results(user, payload.get("p", ""))

    # --- Дежурные / Важное ---
    if cmd == "vazhno_edit":
        cls = get_class_by_user(uid)
        if not (cls and is_class_admin(uid, cls)):
            return await vk.send(uid, "⛔ Только админ класса.")
        await vk.send(uid, "❗ Текст важного объявления (или «удалить» чтобы снять):",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_VAZHNO_WAIT)
    if cmd == "adm_duty":
        cls = get_class_by_user(uid)
        if not (cls and is_class_admin(uid, cls)):
            return await vk.send(uid, "⛔ Только админ класса.")
        await vk.send(uid,
            "🕐 График дежурств. Пришлите список:\n"
            "«понедельник: Петя, Маша\nвторник: Вася»\n"
            "или «по одному по списку класса» — распределю сам.",
            keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_DUTY_WAIT)
    if cmd == "adm_poll":
        await vk.send(uid, "📊 Вопрос опроса:",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_POLL_WAIT_Q)

    # --- Админ-панель ---
    if cmd == "adm_manage_admins":
        return await admin_manage_admins(user)
    if cmd == "adm_add_admin":
        return await admin_pick_member(user, "adm_add_admin_do")
    if cmd == "adm_add_admin_do":
        cls = get_class_by_user(uid)
        if cls and is_class_admin(uid, cls):
            target = str(payload.get("id"))
            if target not in [str(a) for a in cls["admins"]]:
                cls["admins"].append(target)
                save_class(cls)
                log_admin_action(uid, "add_admin", str(target))
                await vk.send(uid, f"👑 {await vk.user_name(target)} теперь админ!")
                try:
                    await vk.send(int(target), "👑 Вы теперь админ класса! "
                                               "Откройте 👨‍💼 Админскую панель.")
                except Exception:
                    pass
                return await admin_manage_admins(user)
        return None
    if cmd == "adm_del_admin":
        return await admin_pick_member(user, "adm_del_admin_do")
    if cmd == "adm_del_admin_do":
        cls = get_class_by_user(uid)
        if cls and is_class_admin(uid, cls):
            target = str(payload.get("id"))
            if len([a for a in cls["admins"] if str(a) != target]) >= 1:
                cls["admins"] = [a for a in cls["admins"] if str(a) != target]
                save_class(cls)
                log_admin_action(uid, "del_admin", str(target))
                await vk.send(uid, "✅ Админ снят.")
                return await admin_manage_admins(user)
            return await vk.send(uid, "⚠️ В классе должен остаться хотя бы один админ.")
        return None
    if cmd == "adm_class_timers":
        return await admin_class_timers(user)
    if cmd == "ct_add":
        await vk.send(uid, "🕒 Таймер классу. Формат: «25.10.2026 09:00 | Текст» "
                           "(или «завтра 8:00 | Текст»):",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_CLASS_TIMER_INPUT)
    if cmd == "ct_del":
        cls = get_class_by_user(uid)
        if not (cls and is_class_admin(uid, cls)):
            return await vk.send(uid, "⛔ Только админ класса.")
        timers = _load_class_timers()
        lst = timers.get(cls["class_code"]) or []
        if not lst:
            return await admin_class_timers(user)
        kb = layout_rows([(f"🗑 {t.get('when')}: {t.get('text', '')[:30]}",
                           {"c": "ct_del_do", "i": t["id"]}, "negative") for t in lst], 1)
        kb.append([("⬅️ Назад", {"c": "menu", "b": "👨‍💼 Админская панель"})])
        return await vk.send(uid, "🗑 Выберите таймер для удаления:", keyboard=kb_inline(kb))
    if cmd == "ct_del_do":
        cls = get_class_by_user(uid)
        if cls and is_class_admin(uid, cls):
            timers = _load_class_timers()
            timers[cls["class_code"]] = [t for t in (timers.get(cls["class_code"]) or [])
                                         if t.get("id") != payload.get("i")]
            _save_class_timers(timers)
            await vk.send(uid, "🗑 Таймер удалён.")
            return await admin_class_timers(user)
        return None

    # --- Панель разработчика ---
    if cmd in ("dev_stats",):
        return await dev_stats(user)
    if cmd == "dev_broadcast":
        await vk.send(uid, "📢 Текст рассылки всем пользователям:",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_DEV_BROADCAST)
    if cmd == "dev_msg_user":
        await vk.send(uid, "✉️ VK ID пользователя (число):",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_DEV_MESSAGE_USER, {})
    if cmd == "dev_msg_class":
        await vk.send(uid, "🎓 Код класса (например: ABC-123):",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_DEV_CLASS_MESSAGE, {})
    if cmd == "dev_stars":
        await vk.send(uid, "⭐ VK ID пользователя для начисления звёзд:",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_DEV_GRANT_STARS, {})
    if cmd == "dev_prices":
        prices = load_prices()
        rows = []
        names = {"button_base": "Кнопка (база)", "button_increment": "Кнопка (+1)",
                 "unblock": "Разблокировка", "unblock_dev": "Разблок у дев.",
                 "view_sender": "Отправитель анонимки",
                 "anon_keep_month": "Анонимки/мес", "ai_generation": "AI сообщение",
                 "cloud_max_files": "Лимит облака"}
        for k, v in prices.items():
            label = names.get(k, k)
            rows.append((f"💰 {label}: {v} — ✏️", {"c": "dev_price_set", "k": k}))
        kb = layout_rows(rows, 1)
        kb.append([("⬅️ Назад", {"c": "menu", "b": "🛠️ Панель разработчика"})])
        return await vk.send(uid, "💰 Цены и лимиты (нажмите, чтобы изменить):",
                             keyboard=kb_inline(kb))
    if cmd == "dev_price_set":
        await vk.send(uid, f"💰 Новое значение для «{payload.get('k')}» (число; -1 = безлимит):")
        return set_state(user, ST_DEV_SET_PRICES, {"key": payload.get("k")})
    if cmd == "dev_instructions":
        await vk.send(uid, f"📚 Текущая инструкция:\n\n{instructions_text()[:2000]}\n\n"
                           "Пришлите НОВЫЙ текст инструкции целиком:",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_DEV_EDIT_INSTRUCTIONS)
    if cmd == "dev_holidays":
        holidays = load_holidays()
        lines = ["🎉 Праздники бота:"]
        for k in sorted(holidays.keys()):
            lines.append(f"• {k}: {holidays[k]}")
        await vk.send(uid, "\n".join(lines) if holidays else "🎉 Праздники не заданы.",
                      keyboard=kb_inline([
                          [("➕ Добавить (оптом можно)", {"c": "dev_holiday_add"}, "positive")],
                          [("➖ Удалить", {"c": "dev_holiday_del"}, "negative")],
                          [("⬅️ Назад", {"c": "menu", "b": "🛠️ Панель разработчика"})]]))
        return None
    if cmd == "dev_holiday_add":
        await vk.send(uid, "🎉 Праздники для добавления. Формат: «1 января — Новый год» "
                           "(через запятую можно несколько):",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_DEV_HOLIDAY_ADD)
    if cmd == "dev_holiday_del":
        holidays = load_holidays()
        if not holidays:
            return await handle_callback(user, {"c": "dev_holidays"}, event)
        kb = layout_rows([(f"➖ {k}: {v}", {"c": "dev_holiday_del_do", "k": k}, "negative")
                          for k, v in holidays.items()], 1)
        kb.append([("⬅️ Назад", {"c": "dev_holidays"})])
        await vk.send(uid, "➖ Удалить праздник:", keyboard=kb_inline(kb))
        return None
    if cmd == "dev_holiday_del_do":
        holidays = load_holidays()
        removed = holidays.pop(payload.get("k", ""), None)
        save_data(HOLIDAYS_FILE, holidays)
        log_admin_action(uid, "del_holiday", payload.get("k", ""))
        await vk.send(uid, f"🗑 Удалён: {removed or '?'}")
        return await handle_callback(user, {"c": "dev_holidays"}, event)
    if cmd == "dev_global_buttons":
        gbtns = get_global_buttons()
        lines = ["🌐 Глобальные кнопки:"]
        for b in gbtns:
            lines.append(f"• {b.get('name')}")
        if not gbtns:
            lines.append("— нет")
        await vk.send(uid, "\n".join(lines),
                      keyboard=kb_inline([
                          [("➕ Создать", {"c": "dev_gb_add"}, "positive")],
                          [("🗑 Удалить", {"c": "dev_gb_del"}, "negative")],
                          [("⬅️ Назад", {"c": "menu", "b": "🛠️ Панель разработчика"})]]))
        return None
    if cmd == "dev_gb_add":
        await vk.send(uid, "🌐 Глобальная кнопка. Формат:\n«Название | содержимое (текст или https-ссылка)»",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_DEV_USER_MGMT, {"mode": "gb_add"})
    if cmd == "dev_gb_del":
        gbtns = get_global_buttons()
        if not gbtns:
            return await handle_callback(user, {"c": "dev_global_buttons"}, event)
        kb = layout_rows([(f"🗑 {b.get('name')}", {"c": "dev_gb_del_do", "n": b.get("name")}, "negative")
                          for b in gbtns], 1)
        kb.append([("⬅️ Назад", {"c": "dev_global_buttons"})])
        await vk.send(uid, "🗑 Удалить глобальную кнопку:", keyboard=kb_inline(kb))
        return None
    if cmd == "dev_gb_del_do":
        gbtns = [b for b in get_global_buttons() if b.get("name") != payload.get("n")]
        save_data(GLOBAL_BUTTONS_FILE, gbtns)
        log_admin_action(uid, "del_global_button", payload.get("n", ""))
        await vk.send(uid, "🗑 Глобальная кнопка удалена.")
        return await handle_callback(user, {"c": "dev_global_buttons"}, event)
    if cmd == "dev_suggestions":
        suggestions = load_data(SUGGESTIONS_FILE, [], expect="list")
        if not suggestions:
            return await vk.send(uid, "💡 Идей пока нет.",
                                 keyboard=kb_inline([[("⬅️ Назад", {"c": "menu", "b": "🛠️ Панель разработчика"})]]))
        lines = ["💡 Идеи пользователей (последние 15):"]
        for s in suggestions[-15:]:
            lines.append(f"• {await vk.user_name(s.get('user'))}: «{s.get('text', '')[:120]}»")
        await vk.send(uid, "\n".join(lines),
                      keyboard=kb_inline([[("⬅️ Назад", {"c": "menu", "b": "🛠️ Панель разработчика"})]]))
        return None
    if cmd == "dev_support":
        sm = load_data(SUPPORT_MESSAGES_FILE, [], expect="list")
        if not sm:
            return await vk.send(uid, "💬 Входящих нет.",
                                 keyboard=kb_inline([[("⬅️ Назад", {"c": "menu", "b": "🛠️ Панель разработчика"})]]))
        lines = ["💬 Последние сообщения поддержки:"]
        for s in sm[-10:]:
            lines.append(f"• vk{s.get('user')} ({s.get('ts', '')}): «{s.get('text', '')[:150]}»")
        await vk.send(uid, "\n".join(lines),
                      keyboard=kb_inline([[("⬅️ Назад", {"c": "menu", "b": "🛠️ Панель разработчика"})]]))
        return None
    if cmd == "dev_backup":
        return await dev_do_backup(user)
    if cmd == "dev_subgate":
        settings = load_dev_settings()
        settings["sub_gate_enabled"] = not settings.get("sub_gate_enabled", True)
        save_dev_settings(settings)
        state_txt = "ВКЛЮЧЕНА" if settings["sub_gate_enabled"] else "ВЫКЛЮЧЕНА"
        await vk.send(uid, f"🔔 Обязательная подписка на сообщество: {state_txt}.")
        return await developer_panel(user)
    if cmd == "dev_admin_log":
        log = load_data(ADMIN_LOG_FILE, [], expect="list")
        if not log:
            return await vk.send(uid, "👑 Журнал пуст.",
                                 keyboard=kb_inline([[("⬅️ Назад", {"c": "menu", "b": "🛠️ Панель разработчика"})]]))
        lines = ["👑 Журнал действий (последние 15):"]
        for e in log[-15:]:
            lines.append(f"• {e.get('ts', '')} vk{e.get('admin', '?')}: {e.get('action', '')} {e.get('details', '')}")
        await vk.send(uid, "\n".join(lines),
                      keyboard=kb_inline([[("⬅️ Назад", {"c": "menu", "b": "🛠️ Панель разработчика"})]]))
        return None
    if cmd == "dev_delete_class":
        await vk.send(uid, "🗑 Введите код класса для удаления (безвозвратно!):",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_DEV_DELETE_CLASS)
    if cmd == "dev_block":
        await vk.send(uid, "🚫 VK ID пользователя для блокировки:",
                      keyboard=kb_inline([[("⬅️ Отмена", {"c": "back_menu"}, "negative")]]))
        return set_state(user, ST_DEV_BLOCK_USER, {"step": "id"})
    if cmd == "ai_persona":
        mode = payload.get("p")
        if mode in AI_PERSONA_MODES:
            user["ai_persona"] = mode
            save_user(user)
            await vk.send(uid, f"✅ Режим: {AI_PERSONA_MODES[mode]['title']} "
                               f"({AI_PERSONA_MODES[mode]['description']}).")
            return await ai_chat_start(user)
        return None
    if cmd == "ai_clear":
        user_conversations.pop(str(uid), None)
        hist = load_data(AI_HIST_FILE, {}, expect="dict")
        hist.pop(str(uid), None)
        save_data(AI_HIST_FILE, hist)
        await vk.send(uid, "🧹 Контекст диалога очищен.")
        return await ai_chat_start(user)

    # Неизвестная команда — тихо игнорируем
    logger.debug("unknown callback: %s", payload)
    return None


@state_handler(ST_CHANGE_BUTTON_NAME)
async def btn_rename_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    for sep in ("—", "–", "-"):
        if sep in text:
            old, new = [x.strip() for x in text.split(sep, 1)]
            names = get_all_user_button_names(uid)
            target = None
            for n in names:
                if old and old.lower() in n.lower():
                    target = n
                    break
            if not target:
                await vk.send(uid, f"⚠️ Кнопка «{old}» не найдена.")
                return True
            rename_map = user.get("custom_buttons") or {}
            rename_map[target] = clean_forbidden(new)[:40]
            user["custom_buttons"] = rename_map
            save_user(user)
            clear_state(user)
            await vk.send(uid, f"✏️ «{target}» → «{rename_map[target]}».")
            return await send_main_menu(user)
    await vk.send(uid, "⚠️ Формат: «старое название — новое название»")
    return True


@state_handler(ST_REORDER_BUTTONS)
async def btn_reorder_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    for sep in ("—", "–", "-"):
        if sep in text:
            name_part, pos_part = [x.strip() for x in text.split(sep, 1)]
            try:
                pos = max(1, int(pos_part))
            except ValueError:
                pos = None
            if not pos:
                await vk.send(uid, "⚠️ Позиция должна быть числом: «🌦 Погода — 1»")
                return True
            target = None
            for n in get_all_user_button_names(uid):
                if name_part and name_part.lower() in n.lower():
                    target = n
                    break
            if not target:
                await vk.send(uid, f"⚠️ Кнопка «{name_part}» не найдена.")
                return True
            order = [n for n in (user.get("custom_button_order") or []) if n != target]
            order.insert(min(pos - 1, len(order)), target)
            user["custom_button_order"] = order
            save_user(user)
            clear_state(user)
            await vk.send(uid, f"✅ «{target}» теперь на позиции {pos}.")
            return await send_main_menu(user)
    await vk.send(uid, "⚠️ Формат: «название кнопки — позиция»")
    return True


@state_handler(ST_SOL_COMMENT_WAIT)
async def sol_comment_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await solutions_menu(user)
    cls = get_class_by_user(uid)
    if not cls:
        clear_state(user)
        return True
    sols = _load_solutions()
    for s in sols.get(cls["class_code"], []):
        if s.get("id") == user["fsm"].get("id"):
            s.setdefault("comments", []).append({
                "by": str(uid), "text": clean_forbidden(text.strip())[:300],
                "ts": now().strftime("%Y-%m-%d %H:%M")})
            _save_solutions(sols)
            clear_state(user)
            await vk.send(uid, "💬 Комментарий добавлен.")
            return await solution_view(user, s["id"])
    clear_state(user)
    await vk.send(uid, "⚠️ Решение не найдено.")
    return True


@state_handler(ST_DEV_SET_PRICES)
async def dev_price_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if not is_dev(uid):
        clear_state(user)
        return True
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    key = user["fsm"].get("key")
    try:
        value = int(text.strip())
    except ValueError:
        await vk.send(uid, "⚠️ Нужно число (например: 30, или -1 для безлимита).")
        return True
    prices = load_data(PRICES_FILE, {}, expect="dict")
    prices[key] = value
    save_data(PRICES_FILE, prices)
    log_admin_action(uid, "set_price", f"{key}={value}")
    clear_state(user)
    await vk.send(uid, f"💰 Цена «{key}» = {value}.")
    return await developer_panel(user)


@state_handler(ST_DEV_DELETE_CLASS)
async def dev_delete_class_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if not is_dev(uid):
        clear_state(user)
        return True
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    code = text.strip().upper()
    classes = load_data(CLASSES_FILE, {}, expect="dict")
    cls = classes.pop(code, None)
    if not cls:
        await vk.send(uid, "⚠️ Класс не найден.")
        return True
    save_data(CLASSES_FILE, classes)
    # открепляем участников
    users = load_data(USERS_FILE, {}, expect="dict")
    for uid2, u in users.items():
        if u.get("class_code") == code:
            u["class_code"] = None
    save_data(USERS_FILE, users)
    log_admin_action(uid, "delete_class", code)
    clear_state(user)
    await vk.send(uid, f"🗑 Класс «{cls.get('class_name')}» ({code}) удалён. "
                       f"Участников: {len(cls.get('members', []))}.")
    return await developer_panel(user)


@state_handler(ST_DEV_BLOCK_USER)
async def dev_block_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if not is_dev(uid):
        clear_state(user)
        return True
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    step = user["fsm"].get("step", "id")
    if step == "id":
        if not text.strip().lstrip("-").isdigit():
            await vk.send(uid, "⚠️ VK ID — число.")
            return True
        set_state(user, ST_DEV_BLOCK_USER, {"id": text.strip(), "step": "hours"})
        await vk.send(uid, "⏱ На сколько часов заблокировать? (число или «навсегда»). "
                           "Или «разблок» для разблокировки.")
        return True
    target = user["fsm"].get("id")
    tu = get_user(target)
    if text.strip().lower() in ("разблок", "unblock"):
        tu["is_blocked"] = False
        tu["block_until"] = None
        save_user(tu)
        log_admin_action(uid, "unblock_user", str(target))
        clear_state(user)
        await vk.send(uid, f"✅ vk{target} разблокирован.")
        return await developer_panel(user)
    if text.strip().lower() in ("навсегда", "forever"):
        tu["is_blocked"] = True
        tu["block_until"] = None
        save_user(tu)
        log_admin_action(uid, "block_user", f"vk{target} навсегда")
        clear_state(user)
        await vk.send(uid, f"🚫 vk{target} заблокирован навсегда.")
        return await developer_panel(user)
    try:
        hours = float(text.strip())
    except ValueError:
        await vk.send(uid, "⚠️ Нужно число часов, «навсегда» или «разблок».")
        return True
    tu["is_blocked"] = True
    tu["block_until"] = (now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    save_user(tu)
    log_admin_action(uid, "block_user", f"vk{target} {hours}ч")
    clear_state(user)
    await vk.send(uid, f"🚫 vk{target} заблокирован на {hours} ч.")
    return await developer_panel(user)


@state_handler(ST_DEV_CLASS_MESSAGE)
async def dev_class_message_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if not is_dev(uid):
        clear_state(user)
        return True
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    code = text.strip().upper()
    cls = get_class_by_code(code)
    if not cls:
        await vk.send(uid, "⚠️ Класс не найден. Введите код (ABC-123):")
        return True
    set_state(user, ST_DEV_CLASS_MESSAGE, {"code": code, "step": "text"})
    await vk.send(uid, f"✉️ Текст сообщения классу «{cls['class_name']}» "
                       f"({len(cls.get('members', []))} чел.):")
    return True


@state_handler(ST_DEV_USER_MGMT)
async def dev_user_mgmt_state(user: dict, text: str, message: dict) -> bool:
    uid = int(user["user_id"])
    if not is_dev(uid):
        clear_state(user)
        return True
    if text.lower() in ("отмена", "cancel"):
        clear_state(user)
        return await send_main_menu(user, "✅ Отменено.")
    mode = user["fsm"].get("mode")
    if mode == "gb_add":
        # «Название | содержимое»
        if "|" in text:
            name, content = [x.strip() for x in text.split("|", 1)]
            gbtns = get_global_buttons()
            gbtns.append({"name": clean_forbidden(name)[:40],
                          "content": clean_forbidden(content)[:2000],
                          "type": "url" if content.startswith("http") else "text",
                          "ts": now().strftime("%Y-%m-%d %H:%M")})
            save_data(GLOBAL_BUTTONS_FILE, gbtns)
            log_admin_action(uid, "create_global_button", name)
            clear_state(user)
            await vk.send(uid, f"🌐 Глобальная кнопка «{name}» создана!")
            return await developer_panel(user)
        await vk.send(uid, "⚠️ Формат: «Название | содержимое»")
        return True
    # сообщение классу: step text
    code = user["fsm"].get("code")
    cls = get_class_by_code(code or "")
    if cls and user["fsm"].get("step") == "text":
        sent = await notify_class(cls, None, f"✉️ Сообщение от разработчика:\n\n{text.strip()[:2000]}")
        log_admin_action(uid, "dev_class_msg", f"{code} {sent} чел.")
        clear_state(user)
        await vk.send(uid, f"✅ Доставлено: {sent} чел.")
        return await developer_panel(user)
    await vk.send(uid, "⚠️ Что-то пошло не так, начните заново.")
    clear_state(user)
    return await developer_panel(user)


async def btn_visibility_menu(user: dict) -> None:
    uid = int(user["user_id"])
    names = get_all_user_button_names(uid)
    hidden = user.get("hidden_buttons") or []
    rows = []
    for name in names[:30]:
        is_hidden = name in hidden
        icon = "🙈" if is_hidden else "👁"
        rows.append((f"{icon} {name}", {"c": "toggle_visibility", "n": name}))
    kb = layout_rows(rows, 1)
    kb.append([("✅ Готово", {"c": "finish_visibility"}, "positive")])
    kb.append([("⬅️ Назад", {"c": "menu", "b": "⚙️ Настройки"})])
    await vk.send(uid, "👁 Видимость кнопок (👁 видна / 🙈 скрыта). Нажмите, чтобы переключить:",
                  keyboard=kb_inline(kb))
    clear_state(user)

# ==================================
# === ФОНОВЫЙ ТИКЕР УВЕДОМЛЕНИЙ ===
# ==================================

def _notif_log_key(uid, kind, day=None) -> str:
    return f"{kind}:{uid}:{day or now().strftime('%Y-%m-%d')}"


def _notif_already_sent(key: str) -> bool:
    log = load_data(NOTIFICATION_LOG_FILE, {}, expect="dict")
    return key in log


def _notif_mark_sent(key: str) -> None:
    log = load_data(NOTIFICATION_LOG_FILE, {}, expect="dict")
    log[key] = now().strftime("%Y-%m-%d %H:%M:%S")
    if len(log) > 50000:
        cutoff = now() - timedelta(days=3)
        for k in list(log.keys()):
            try:
                if datetime.strptime(log[k], "%Y-%m-%d %H:%M:%S") < cutoff:
                    log.pop(k)
            except ValueError:
                log.pop(k, None)
    save_data(NOTIFICATION_LOG_FILE, log)


def _user_dnd_active(u: dict) -> bool:
    dnd = u.get("dnd_until")
    if not dnd:
        return False
    try:
        return datetime.strptime(dnd, "%Y-%m-%d %H:%M") > now()
    except ValueError:
        return False


async def ticker_tick() -> None:
    """Один проход тикера: утро/вечер/погода/ДР/праздники/таймеры/помодоро/дежурства."""
    try:
        await _tick_timers()
        await _tick_class_timers()
        await _tick_pomodoro()
        await _tick_daily_notifications()
    except Exception as e:
        logger.error("ticker: %s", e, exc_info=True)


async def _tick_timers() -> None:
    """Личные таймеры (+ повторяющиеся)."""
    timers = _load_timers()
    changed = False
    for uid, lst in list(timers.items()):
        u = get_user(uid)
        if u.get("is_blocked"):
            continue
        remaining = []
        for t in lst or []:
            when = t.get("fire_at") or ""
            try:
                fire_at = datetime.strptime(when, "%Y-%m-%d %H:%M")
            except ValueError:
                remaining.append(t)
                continue
            if fire_at <= now():
                try:
                    await vk.send(int(uid), t.get("text", "⏰ Напоминание!"))
                except Exception:
                    pass
                # Повторы
                repeat = t.get("repeat")
                if repeat:
                    nxt = _next_repeat_time(fire_at, repeat)
                    if nxt:
                        t["fire_at"] = nxt.strftime("%Y-%m-%d %H:%M")
                        remaining.append(t)
                changed = True
            else:
                remaining.append(t)
        if len(remaining) != len(lst or []):
            timers[uid] = remaining
            changed = True
    if changed:
        _save_timers(timers)


def _next_repeat_time(fire_at: datetime, repeat: dict):
    """Следующее время срабатывания для повторяющегося таймера."""
    rtype = repeat.get("type")
    if rtype == "daily":
        nxt = fire_at + timedelta(days=1)
        while nxt <= now():
            nxt += timedelta(days=1)
        return nxt
    if rtype == "weekly":
        day = repeat.get("day", "mon")
        if day in WEEKDAY_SHORT:
            idx = WEEKDAY_SHORT.index(day)
            days_ahead = (idx - now().date().weekday()) % 7
            if days_ahead == 0 and now().time() > fire_at.time():
                days_ahead = 7
            d = now().date() + timedelta(days=days_ahead)
            hm = (fire_at.hour, fire_at.minute)
            nxt = datetime(d.year, d.month, d.day, hm[0], hm[1])
            while nxt <= now():
                nxt += timedelta(days=7)
            return nxt
        return None
    if rtype == "days":
        days = [d for d in repeat.get("days", []) if d in WEEKDAY_SHORT]
        if not days:
            return None
        nxt = fire_at + timedelta(days=1)
        while nxt <= now() or WEEKDAY_SHORT[nxt.weekday()] not in days:
            nxt += timedelta(days=1)
            if nxt > now() + timedelta(days=14):
                return None
        return nxt
    if rtype == "skip":
        skip = set(repeat.get("days", []))
        nxt = fire_at + timedelta(days=1)
        while nxt <= now() or WEEKDAY_SHORT[nxt.weekday()] in skip:
            nxt += timedelta(days=1)
            if nxt > now() + timedelta(days=14):
                return None
        return nxt
    return None


async def _tick_class_timers() -> None:
    """Таймеры сообщений классу."""
    timers = _load_class_timers()
    changed = False
    for code, lst in list(timers.items()):
        cls = get_class_by_code(code)
        if not cls:
            continue
        remaining = []
        for t in lst or []:
            try:
                fire_at = datetime.strptime(t.get("when", ""), "%Y-%m-%d %H:%M")
            except ValueError:
                remaining.append(t)
                continue
            if fire_at <= now():
                await notify_class(cls, None, t.get("text", "📢 Объявление"))
                repeat = t.get("repeat")
                if repeat:
                    nxt = _next_repeat_time(fire_at, repeat)
                    if nxt:
                        t["when"] = nxt.strftime("%Y-%m-%d %H:%M")
                        remaining.append(t)
                changed = True
            else:
                remaining.append(t)
        if len(remaining) != len(lst or []):
            timers[code] = remaining
            changed = True
    if changed:
        _save_class_timers(timers)


async def _tick_pomodoro() -> None:
    """Фазы помодоро."""
    data = _load_pomodoro()
    changed = False
    for uid, st in list(data.items()):
        if not st.get("active"):
            continue
        try:
            until = datetime.strptime(st.get("phase_until", ""), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if until > now():
            continue
        phase = st.get("phase", "work")
        if phase == "work":
            st["phase"] = "rest"
            st["cycles"] = int(st.get("cycles", 0)) + 1
            mins = int(st.get("rest", 5))
            await vk.send(int(uid), f"☕ Перерыв {mins} мин! Отдохните — вы молодец! "
                                    f"Циклов: {st['cycles']}.")
        else:
            st["phase"] = "work"
            mins = int(st.get("work", 25))
            await vk.send(int(uid), f"💪 Учёба снова! {mins} мин — вперёд!")
        st["phase_until"] = (now() + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M")
        data[uid] = st
        changed = True
    if changed:
        save_data(POMODORO_FILE, data)


async def _tick_daily_notifications() -> None:
    """Утро/вечер, погода, дни рождения, праздники (раз в минуту)."""
    users = all_users()
    today = now().date()
    for uid, u in users.items():
        if u.get("is_blocked") or not u.get("setup_completed"):
            continue
        if not u.get("notifications") or _user_dnd_active(u):
            continue
        try:
            local = user_now(u)
        except Exception:
            continue
        hhmm = local.strftime("%H:%M")
        # Утро
        if u.get("morning_notification_time") == hhmm:
            key = _notif_log_key(uid, "morning")
            if not _notif_already_sent(key):
                _notif_mark_sent(key)
                await vk.send(int(uid), f"🌅 {u.get('morning_text', 'Доброе утро!')}")
        # Вечер
        if u.get("evening_notification_time") == hhmm:
            key = _notif_log_key(uid, "evening")
            if not _notif_already_sent(key):
                _notif_mark_sent(key)
                await vk.send(int(uid), f"🌙 {u.get('evening_text', 'Спокойной ночи!')}")
        # Погода
        if (u.get("weather_notifications") and u.get("weather_notification_time") == hhmm
                and u.get("city")):
            key = _notif_log_key(uid, "weather")
            if not _notif_already_sent(key):
                _notif_mark_sent(key)
                text = await weather_now_text(u)
                await vk.send(int(uid), text)
        # Персональное напоминание о ДР
        if (u.get("birthday_personal_notification") and u.get("birthday")
                and u.get("birthday_notification_time") == hhmm):
            key = _notif_log_key(uid, "bday")
            if not _notif_already_sent(key):
                _notif_mark_sent(key)
                days = days_to_birthday(u["birthday"], today)
                if days == 0:
                    await vk.send(int(uid), "🎉 С ДНЁМ РОЖДЕНИЯ! Здоровья, счастья и пятёрок! 🎂🥳")
                elif days <= 7:
                    await vk.send(int(uid), f"🎂 Через {days} дн. — ваш день рождения!")


async def _tick_birthdays_and_holidays() -> None:
    """Раз в сутки: поздравления именинникам в классе + праздники всем (10:00)."""
    today = now().date()
    holidays = load_holidays()
    holiday_today = holidays.get(today.strftime("%d-%m"))
    users = all_users()
    # Праздники
    if holiday_today:
        for uid, u in users.items():
            if u.get("is_blocked") or not u.get("setup_completed"):
                continue
            if not u.get("holiday_notifications") or _user_dnd_active(u):
                continue
            key = _notif_log_key(uid, "holiday", today.strftime("%Y-%m-%d"))
            if not _notif_already_sent(key):
                _notif_mark_sent(key)
                await vk.send(int(uid), f"🎉 Сегодня {holiday_today}! Праздничного настроения! 🎊")
    # Дни рождения в классах
    classes = load_data(CLASSES_FILE, {}, expect="dict")
    for code, cls in classes.items():
        for m in cls.get("members", []):
            mu = users.get(str(m)) or {}
            if not mu.get("birthday") or not mu.get("show_birthday_to_class", True):
                continue
            if days_to_birthday(mu["birthday"], today) != 0:
                continue
            key = _notif_log_key(m, "bday_class", today.strftime("%Y-%m-%d"))
            if _notif_already_sent(key):
                continue
            _notif_mark_sent(key)
            name = await vk.user_name(m)
            for m2 in cls.get("members", []):
                if str(m2) == str(m):
                    continue
                u2 = users.get(str(m2)) or {}
                if u2.get("is_blocked") or _user_dnd_active(u2):
                    continue
                key2 = _notif_log_key(m2, f"bday_note_{m}", today.strftime("%Y-%m-%d"))
                if not _notif_already_sent(key2):
                    _notif_mark_sent(key2)
                    try:
                        await vk.send(int(m2), f"🎂 Сегодня день рождения у {name}! "
                                               f"Не забудьте поздравить 🎉")
                    except Exception:
                        pass


async def ticker_loop() -> None:
    """Главный цикл тикера: каждые 30 сек — таймеры, раз в минуту — уведомления."""
    last_minute = -1
    last_day = -1
    last_drift = 0.0
    while True:
        try:
            await asyncio.sleep(30)
            await ticker_tick()
            minute = now().minute
            day = now().day
            # раз в минуту — дневные уведомления уже внутри тикера (идемпотентны по логу)
            if day != last_day:
                last_day = day
                await _tick_birthdays_and_holidays()
            if time.time() - last_drift > 3600:
                last_drift = time.time()
                await asyncio.to_thread(_refresh_time_drift)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("ticker_loop: %s", e, exc_info=True)
            await asyncio.sleep(10)

# ==================================
# === ВЕБ-СЕРВЕР: HEALTH + МИНИ-АПП + API ===
# ==================================

# Веб-сессии мини-аппа: {token: {"uid":..., "ts":...}}
_web_sessions: dict = {}


def _web_session_create(uid) -> str:
    token = secrets.token_urlsafe(24)
    _web_sessions[token] = {"uid": str(uid), "ts": time.time()}
    if len(_web_sessions) > 10000:
        cutoff = time.time() - 86400 * 7
        for k in list(_web_sessions.keys())[:5000]:
            v = _web_sessions.get(k)
            if v and v.get("ts", 0) < cutoff:
                _web_sessions.pop(k, None)
    return token


def _web_session_get(token: str):
    if not token:
        return None
    ent = _web_sessions.get(token)
    if not ent:
        return None
    if time.time() - ent.get("ts", 0) > 86400 * 7:
        _web_sessions.pop(token, None)
        return None
    ent["ts"] = time.time()
    return ent


def _vk_sign_valid(params: dict) -> int:
    """Проверка подписи запуска VK Mini App → vk_user_id | None."""
    if not (VKAPP_ID and VKAPP_SECRET):
        return None
    if str(params.get("vk_app_id")) != str(VKAPP_ID):
        return None
    sign = params.pop("sign", None)
    if not sign:
        return None
    sorted_qs = "&".join(
        f"{k}={params[k]}" for k in sorted(params.keys())
        if k.startswith("vk_"))
    expected = hmac_mod.new(
        VKAPP_SECRET.encode(), sorted_qs.encode(), hashlib.md5).hexdigest()
    if not hmac_mod.compare_digest(expected, str(sign)):
        return None
    uid = params.get("vk_user_id")
    return int(uid) if uid and str(uid).lstrip("-").isdigit() else None


@web.middleware
async def _cors_middleware(request, handler):
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


def _api_uid(request) -> str:
    """uid из Bearer-токена или VK-подписи."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        ent = _web_session_get(auth[7:].strip())
        if ent:
            return ent["uid"]
    return ""


async def handle_health(request):
    return _json({"ok": True, "build": VK_BOT_BUILD, "time": now().isoformat()})


async def handle_miniapp(request):
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "miniapp", "index.html")
    if not os.path.exists(html_path):
        return web.Response(text="Mini App не найден", status=404)
    with open(html_path, "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html",
                            headers={"Cache-Control": "no-store"})


async def handle_sw_js(request):
    sw = ("self.addEventListener('install', e => self.skipWaiting());\n"
          "self.addEventListener('activate', e => self.clients.claim());\n"
          "self.addEventListener('fetch', e => {});\n")
    return web.Response(text=sw, content_type="application/javascript")


# ---------- API мини-аппа ----------

async def api_web_login(request):
    """Вход по VK ID + паролю веб-облака."""
    try:
        body = await request.json()
    except Exception:
        return _json({"message": "bad request"}, 400)
    uid = str(body.get("user_id", "")).strip()
    password = str(body.get("password", "")).strip()
    u = get_user(uid)
    stored = u.get("web_password")
    if not stored:
        return _json({"message": "Сначала задайте пароль в боте: "
                                 "⚙️ Настройки → 🔑 Пароль веб-облака"}, 403)
    if hashlib.sha256(password.encode()).hexdigest() != stored:
        return _json({"message": "Неверный пароль"}, 403)
    token = _web_session_create(uid)
    return _json({"token": token, "name": u.get("first_name", "")})


async def api_vk_login(request):
    """Вход по подписи запуска VK Mini App (без пароля)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    params = dict(body.get("params") or {})
    uid = _vk_sign_valid(params)
    if not uid:
        return _json({"message": "Подпись VK не прошла проверку"}, 403)
    token = _web_session_create(uid)
    u = get_user(uid)
    return _json({"token": token, "name": u.get("first_name", "")})


async def api_web_me(request):
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    u = get_user(uid)
    return _json({
        "id": uid, "name": u.get("first_name", ""),
        "stars": u.get("stars_balance", 0),
        "city": u.get("city"),
        "build": VK_BOT_BUILD,
    })


async def api_files(request):
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    files = cloud_user_files(uid)
    payload = [{"id": f["id"], "name": f["name"], "kind": f.get("kind", "document"),
                "size": int(f.get("size", 0)), "ts": f.get("ts", ""),
                "vault": False, "safe": False, "plain": False,
                "src": f.get("src", "web")} for f in files]
    return _json({"files": payload, "bot": "DEVORKS+ VK", "build": VK_BOT_BUILD})


async def api_file_patch(request):
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    fid = request.match_info["fid"]
    try:
        body = await request.json()
    except Exception:
        return _json({"message": "bad request"}, 400)
    new_name = str(body.get("name", "")).strip()
    if not new_name:
        return _json({"message": "Пустое имя"}, 400)
    if cloud_rename_file(uid, fid, new_name):
        f = cloud_get_file(uid, fid)
        return _json({"ok": True, "file": {"id": fid, "name": f.get("name")}})
    return _json({"message": "not found"}, 404)


async def api_file_delete(request):
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    fid = request.match_info["fid"]
    if cloud_delete_file(uid, fid):
        return _json({"ok": True})
    return _json({"message": "not found"}, 404)


async def api_file_link(request):
    """Одноразовая ссылка на скачивание файла."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    fid = request.match_info["fid"]
    f = cloud_get_file(uid, fid)
    if not f:
        return _json({"message": "not found"}, 404)
    tokens = load_data(SHARE_TOKENS_FILE, {}, expect="dict")
    token = secrets.token_urlsafe(16)
    tokens[token] = {"fid": fid, "uid": str(uid), "ts": time.time(),
                     "uses": 0, "max_uses": 3}
    save_data(SHARE_TOKENS_FILE, tokens)
    disp = request.query.get("disp", "attachment")
    return _json({"link": f"/api/dl/{token}?disp={disp}", "name": f["name"]})


async def api_dl(request):
    """Скачивание по одноразовому токену."""
    token = request.match_info["token"]
    tokens = load_data(SHARE_TOKENS_FILE, {}, expect="dict")
    ent = tokens.get(token)
    if not ent or time.time() - ent.get("ts", 0) > 3600 or ent.get("uses", 0) >= ent.get("max_uses", 3):
        return web.Response(text="Ссылка недействительна", status=404)
    ent["uses"] = int(ent.get("uses", 0)) + 1
    tokens[token] = ent
    save_data(SHARE_TOKENS_FILE, tokens)
    # Временный файл (ZIP) по прямому пути
    if ent.get("path") and os.path.exists(ent["path"]):
        with open(ent["path"], "rb") as fh:
            data = fh.read()
        name = ent.get("name") or "archive.zip"
        try:
            os.remove(ent["path"])
        except OSError:
            pass
        return web.Response(body=data,
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})
    f = cloud_get_file(ent.get("uid"), ent.get("fid"))
    if not f or not f.get("path") or not os.path.exists(f["path"]):
        return web.Response(text="Файл не найден", status=404)
    with open(f["path"], "rb") as fh:
        data = fh.read()
    disp = request.query.get("disp", "attachment")
    return web.Response(
        body=data,
        headers={"Content-Disposition": f'{disp}; filename="{f["name"]}"'},
    )


async def api_to_chat(request):
    """Отправка файла из облака в чат с ботом."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    fid = request.match_info["fid"]
    f = cloud_get_file(uid, fid)
    if not f:
        return _json({"message": "not found"}, 404)
    try:
        with open(f["path"], "rb") as fh:
            data = fh.read()
        ok = await vk.send_file(int(uid), data, f["name"])
        return _json({"ok": bool(ok)}) if ok else _json({"message": "Не удалось отправить"}, 500)
    except FileNotFoundError:
        return _json({"message": "Файл не найден на диске"}, 404)


async def api_settings(request):
    """Синхронизация настроек мини-аппа."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    u = get_user(uid)
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return _json({"message": "bad request"}, 400)
        cur = u.get("miniapp_settings") or {}
        cur.update({k: v for k, v in (body or {}).items() if k != "theme_ping"})
        u["miniapp_settings"] = cur
        save_user(u)
        return _json({"ok": True, "settings": cur})
    return _json({"settings": u.get("miniapp_settings") or {}})


async def api_lists(request):
    """Списки мини-аппа: папки/избранное/описания."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    u = get_user(uid)
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return _json({"message": "bad request"}, 400)
        cur = u.get("miniapp_lists") or {}
        cur.update({k: v for k, v in (body or {}).items()
                    if k in ("folders", "favorites", "descriptions", "hidden",
                             "deletedDemo", "albums")})
        u["miniapp_lists"] = cur
        save_user(u)
        return _json({"ok": True, "lists": cur})
    return _json({"lists": u.get("miniapp_lists") or {}})


async def api_upload_init(request):
    """Начало загрузки файла в облако (chunked)."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    try:
        body = await request.json()
    except Exception:
        return _json({"message": "bad request"}, 400)
    name = clean_forbidden(os.path.basename(str(body.get("name", "file"))))[:120]
    size = int(body.get("size") or 0)
    encrypt = bool(body.get("encrypt"))
    upload_id = secrets.token_hex(6)
    state = load_data(UPLOADS_STATE_FILE, {}, expect="dict")
    state[upload_id] = {"uid": str(uid), "name": name, "size": size,
                        "encrypt": encrypt, "ts": time.time(), "chunks": []}
    save_data(UPLOADS_STATE_FILE, state)
    return _json({"uploadId": upload_id, "encrypt": False})


async def api_upload_chunk(request):
    """Приём блока файла (байты в теле)."""
    upload_id = request.query.get("uploadId", "")
    state = load_data(UPLOADS_STATE_FILE, {}, expect="dict")
    up = state.get(upload_id)
    if not up or time.time() - up.get("ts", 0) > 86400:
        return _json({"message": "upload not found"}, 404)
    try:
        seq = int(request.query.get("seq", "0"))
    except ValueError:
        seq = 0
    data = await request.read()
    chunk_dir = os.path.join(FILES_DIR, "uploads", upload_id)
    os.makedirs(chunk_dir, exist_ok=True)
    with open(os.path.join(chunk_dir, f"{seq:05d}"), "wb") as f:
        f.write(data)
    up["chunks"] = sorted(set(up.get("chunks", []) + [seq]))
    up["ts"] = time.time()
    state[upload_id] = up
    save_data(UPLOADS_STATE_FILE, state)
    return _json({"ok": True, "received": seq, "total": len(up["chunks"])})


async def api_upload_complete(request):
    """Склейка блоков → файл в облаке."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    try:
        body = await request.json()
    except Exception:
        return _json({"message": "bad request"}, 400)
    upload_id = str(body.get("uploadId", ""))
    state = load_data(UPLOADS_STATE_FILE, {}, expect="dict")
    up = state.get(upload_id)
    if not up or up.get("uid") != str(uid):
        return _json({"message": "upload not found"}, 404)
    chunk_dir = os.path.join(FILES_DIR, "uploads", upload_id)
    buf = io.BytesIO()
    if os.path.isdir(chunk_dir):
        for fn in sorted(os.listdir(chunk_dir)):
            with open(os.path.join(chunk_dir, fn), "rb") as f:
                buf.write(f.read())
    data = buf.getvalue()
    name = body.get("name") or up.get("name") or "file"
    try:
        rec = cloud_add_file(uid, name, data, src="web")
    except VKApiError:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        state.pop(upload_id, None)
        save_data(UPLOADS_STATE_FILE, state)
        return _json({"message": "Лимит файлов облака исчерпан"}, 400)
    except Exception as e:
        logger.error("upload complete: %s", e)
        return _json({"message": "Ошибка сохранения"}, 500)
    shutil.rmtree(chunk_dir, ignore_errors=True)
    state.pop(upload_id, None)
    save_data(UPLOADS_STATE_FILE, state)
    return _json({"ok": True, "file": {"id": rec["id"], "name": rec["name"],
                                        "size": rec["size"], "ts": rec["ts"]}})


async def api_upload_abort(request):
    """Отмена загрузки."""
    try:
        body = await request.json()
    except Exception:
        return _json({"ok": True})
    upload_id = str(body.get("uploadId", ""))
    state = load_data(UPLOADS_STATE_FILE, {}, expect="dict")
    if upload_id in state:
        state.pop(upload_id, None)
        save_data(UPLOADS_STATE_FILE, state)
    shutil.rmtree(os.path.join(FILES_DIR, "uploads", upload_id), ignore_errors=True)
    return _json({"ok": True})


async def api_upload_status(request):
    """Статус загрузки (для докачки)."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    upload_id = request.query.get("uploadId", "")
    state = load_data(UPLOADS_STATE_FILE, {}, expect="dict")
    up = state.get(upload_id)
    if not up or up.get("uid") != str(uid):
        return _json({"chunks": [], "name": "", "size": 0})
    return _json({"chunks": up.get("chunks", []), "name": up.get("name"),
                  "size": up.get("size", 0)})


async def api_upload_rename(request):
    """Переименовать загружаемый файл (до complete)."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    try:
        body = await request.json()
    except Exception:
        return _json({"message": "bad request"}, 400)
    upload_id = str(body.get("uploadId", ""))
    state = load_data(UPLOADS_STATE_FILE, {}, expect="dict")
    up = state.get(upload_id)
    if up and up.get("uid") == str(uid):
        up["name"] = clean_forbidden(os.path.basename(str(body.get("name", up.get("name", "file")))))[:120]
        state[upload_id] = up
        save_data(UPLOADS_STATE_FILE, state)
    return _json({"ok": True})


async def api_upload_closed(request):
    """Веб-клиент закрылся — незаконченная загрузка сохранится, докачается потом."""
    return _json({"ok": True})


async def api_upload_queue_send(request):
    """«Загрузить через ВК» — просим пользователя прислать файл в чат."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    try:
        await vk.send(int(uid),
                  "📤 Пришлите файл прямо в этот чат — я предложу сохранить его в облако.")
    except Exception:
        pass
    return _json({"ok": True})


async def api_download_queue(request):
    """Очередь отправки файлов в чат (заглушка статуса мини-аппа)."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    return _json({"queue": [], "active": False})


async def api_download_queue_cancel(request):
    return _json({"ok": True})


async def api_safe_unlock(request):
    """Проверка пароля сейфа из мини-аппа."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    try:
        body = await request.json()
    except Exception:
        return _json({"message": "bad request"}, 400)
    password = str(body.get("password", ""))
    _, ok = vault_check_password(uid, password)
    if not ok:
        return _json({"message": "Неверный пароль", "code": "wrong_password"}, 403)
    return _json({"ok": True})


async def api_file_safe(request):
    """to_safe / from_safe: перемещение файла облако ↔ сейф."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    kind = request.match_info["kind"]
    fid = request.match_info["fid"]
    if kind not in ("to_safe", "from_safe"):
        return _json({"message": "bad request"}, 400)
    if not HAVE_CRYPTO:
        return _json({"message": "Сейф недоступен (нет cryptography)"}, 500)
    try:
        body = await request.json()
    except Exception:
        body = {}
    password = str(body.get("password", ""))
    key, ok = vault_check_password(uid, password)
    if not ok:
        return _json({"message": "Неверный пароль", "code": "wrong_password"}, 403)
    if kind == "to_safe":
        f = cloud_get_file(uid, fid)
        if not f or not f.get("path") or not os.path.exists(f["path"]):
            return _json({"message": "Файл не найден"}, 404)
        with open(f["path"], "rb") as fh:
            data = fh.read()
        payload = json.dumps({"name": f["name"], "kind": f.get("kind"),
                              "data": base64.b64encode(data).decode(),
                              "ts": now().strftime("%Y-%m-%d %H:%M:%S")},
                             ensure_ascii=False).encode()
        blob = vault_encrypt_blob(key, payload)
        vid = secrets.token_hex(6)
        user_dir = os.path.join(VAULT_DIR, str(uid))
        os.makedirs(user_dir, exist_ok=True)
        with open(os.path.join(user_dir, vid + ".dvf"), "wb") as fh:
            fh.write(blob)
        idx = vault_index()
        rec = idx.setdefault(str(uid), {})
        rec.setdefault("files", []).append({"id": vid, "size": len(data),
                                            "ts": now().strftime("%Y-%m-%d %H:%M:%S")})
        vault_save_index(idx)
        cloud_delete_file(uid, fid)
        return _json({"ok": True})
    # from_safe: мини-апп просит «достать» — доставляем в чат бота
    for vf in vault_files_list(uid):
        if vf.get("id") == fid:
            try:
                with open(os.path.join(VAULT_DIR, str(uid), fid + ".dvf"), "rb") as fh:
                    payload = json.loads(vault_decrypt_blob(fh.read(), key))
                data = base64.b64decode(payload.get("data", ""))
                await vk.send_file(int(uid), data, payload.get("name", "file"))
                return _json({"ok": True, "delivered": "chat"})
            except FileNotFoundError:
                return _json({"message": "Файл не найден"}, 404)
            except Exception as e:
                logger.error("from_safe: %s", e)
                return _json({"message": "Ошибка расшифровки"}, 500)
    return _json({"message": "Не найдено в сейфе"}, 404)


async def api_storage(request):
    """Статус «хранилища» для мини-аппа (диск бота)."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    files = cloud_user_files(uid)
    return _json({
        "mode": "bot_disk",
        "title": "Облако бота (диск сервера)",
        "files": len(files),
        "size": cloud_total_size(uid),
        "max_files": get_price("cloud_max_files"),
        "max_size": CLOUD_MAX_TOTAL,
    })


async def api_storage_settings(request):
    return _json({"mode": "bot_disk"})


async def api_storage_plain(request):
    return _json({"plain": False})


async def api_storage_connect(request):
    return _json({"message": "Хранилище бота подключено всегда"}, 400)


async def api_storage_disconnect(request):
    return _json({"message": "Нельзя отключить хранилище бота"}, 400)


async def api_zip_selected(request):
    """ZIP выбранных файлов → одноразовая ссылка."""
    uid = _api_uid(request)
    if not uid:
        return _json({"message": "unauthorized"}, 401)
    try:
        body = await request.json()
    except Exception:
        return _json({"message": "bad request"}, 400)
    ids = [str(x) for x in (body.get("ids") or [])]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        count = 0
        for fid in ids:
            f = cloud_get_file(uid, fid)
            if f and f.get("path") and os.path.exists(f["path"]):
                with open(f["path"], "rb") as fh:
                    zf.writestr(f["name"], fh.read())
                count += 1
    if not count:
        return _json({"message": "Файлы не найдены"}, 404)
    data = buf.getvalue()
    rec = {"name": f"devo_files_{now().strftime('%Y%m%d_%H%M')}.zip", "path": None}
    # временный файл в облаке не создаём — отдаём токен на раздачу из памяти
    tmp_path = os.path.join(FILES_DIR, "tmp", f"{secrets.token_hex(6)}.zip")
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
    with open(tmp_path, "wb") as f:
        f.write(data)
    tokens = load_data(SHARE_TOKENS_FILE, {}, expect="dict")
    token = secrets.token_urlsafe(16)
    tokens[token] = {"fid": None, "uid": str(uid), "ts": time.time(), "uses": 0,
                     "max_uses": 3, "path": tmp_path, "name": rec["name"]}
    save_data(SHARE_TOKENS_FILE, tokens)
    return _json({"link": f"/api/dl/{token}", "name": rec["name"], "count": count})


async def api_unzip(request):
    return _json({"message": "Распаковка ZIP доступна после скачивания"}, 400)


def create_web_app() -> web.Application:
    app = web.Application(middlewares=[_cors_middleware],
                          client_max_size=64 * 1024 * 1024)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_miniapp)
    app.router.add_get("/miniapp", handle_miniapp)
    app.router.add_get("/sw.js", handle_sw_js)
    # Веб-сессии
    app.router.add_post("/api/web_login", api_web_login)
    app.router.add_post("/api/vk_login", api_vk_login)
    app.router.add_get("/api/web_me", api_web_me)
    # Файлы
    app.router.add_get("/api/files", api_files)
    app.router.add_patch("/api/files/{fid}", api_file_patch)
    app.router.add_delete("/api/files/{fid}", api_file_delete)
    app.router.add_get("/api/files/{fid}/link", api_file_link)
    app.router.add_post("/api/files/{fid}/to_chat", api_to_chat)
    app.router.add_post("/api/files/{fid}/{kind}", api_file_safe)
    app.router.add_post("/api/files/zip_selected", api_zip_selected)
    app.router.add_post("/api/files/unzip", api_unzip)
    # Загрузки
    app.router.add_post("/api/upload/init", api_upload_init)
    app.router.add_post("/api/upload/chunk", api_upload_chunk)
    app.router.add_post("/api/upload/complete", api_upload_complete)
    app.router.add_post("/api/upload/abort", api_upload_abort)
    app.router.add_get("/api/upload/status", api_upload_status)
    app.router.add_post("/api/upload/rename", api_upload_rename)
    app.router.add_post("/api/upload/closed", api_upload_closed)
    app.router.add_post("/api/upload/queue_send", api_upload_queue_send)
    # Очередь скачивания
    app.router.add_get("/api/download/queue", api_download_queue)
    app.router.add_post("/api/download/queue/cancel", api_download_queue_cancel)
    # Сейф
    app.router.add_post("/api/safe/unlock", api_safe_unlock)
    # Настройки/списки
    app.router.add_get("/api/settings", api_settings)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_get("/api/lists", api_lists)
    app.router.add_post("/api/lists", api_lists)
    # Хранилище
    app.router.add_get("/api/storage", api_storage)
    app.router.add_get("/api/storage/settings", api_storage_settings)
    app.router.add_get("/api/storage/plain", api_storage_plain)
    app.router.add_post("/api/storage/connect", api_storage_connect)
    app.router.add_post("/api/storage/disconnect", api_storage_disconnect)
    # Одноразовые ссылки
    app.router.add_get("/api/dl/{token}", api_dl)
    return app


# ==================================
# === LONG POLL (получение событий ВК) ===
# ==================================

async def resolve_group_id() -> int:
    """Определяем id сообщества: env VK_GROUP_ID → groups.getById (свой токен)."""
    if VK_GROUP_ID:
        try:
            return abs(int(VK_GROUP_ID))
        except ValueError:
            pass
    if not VK_TOKEN:
        return 0
    # Для токена сообщества groups.getById без параметров возвращает своё сообщество
    async with aiohttp.ClientSession() as http:
        async with http.post(VK_API_URL + "groups.getById",
                             data={"access_token": VK_TOKEN, "v": VK_API_VERSION,
                                   "fields": ""}) as r:
            data = await r.json(content_type=None)
    resp = data.get("response") or {}
    groups = resp.get("groups") if isinstance(resp, dict) else resp
    if isinstance(groups, list) and groups:
        return int(groups[0].get("id", 0))
    return 0


async def longpoll_loop() -> None:
    """Bots Long Poll: получаем message_new и message_event."""
    await vk.start()
    server = key = ""
    ts = 0
    fails = 0
    while True:
        try:
            if not server:
                resp = await vk.call("groups.getLongPollServer", group_id=vk.group_id)
                server = resp.get("server", "")
                key = resp.get("key", "")
                ts = int(resp.get("ts", 0))
                logger.info("Long Poll подключён (group %s).", vk.group_id)
            url = (f"{server}?act=a_check&key={key}&ts={ts}&wait=25"
                   f"&mode=2&version=3")
            async with vk.http.get(url) as r:
                data = await r.json(content_type=None)
            if "failed" in data:
                code = int(data.get("failed", 0))
                logger.warning("Long Poll failed=%s — переподключаюсь.", code)
                if code in (1,):
                    ts = int(data.get("ts", ts))
                    continue
                server = ""
                await asyncio.sleep(1)
                continue
            ts = int(data.get("ts", ts))
            for update in data.get("updates", []):
                try:
                    await dispatch_update(update)
                except Exception as e:
                    logger.error("dispatch_update: %s\n%s", e, traceback.format_exc())
            fails = 0
        except asyncio.CancelledError:
            break
        except Exception as e:
            fails += 1
            logger.error("longpoll: %s (попытка %s)", e, fails)
            server = ""
            await asyncio.sleep(min(30, 2 ** min(fails, 5)))


async def dispatch_update(update: dict) -> None:
    """Обработка одного события longpoll."""
    etype = update.get("type")
    if etype == "message_new":
        msg = (update.get("object") or {}).get("message") or {}
        await on_message_new(msg)
    elif etype == "message_event":
        ev = update.get("object") or {}
        await on_message_event(ev)
    elif etype == "message_allow":
        # пользователь разрешил сообщения — включаем уведомления обратно
        uid = (update.get("object") or {}).get("user_id")
        if uid:
            u = get_user(uid)
            if not u.get("notifications"):
                u["notifications"] = True
                save_user(u)
    elif etype == "message_deny":
        uid = (update.get("object") or {}).get("user_id")
        if uid:
            _on_send_denied(uid, 901)
    # остальные типы событий игнорируем


async def on_message_new(msg: dict) -> None:
    """Входящее сообщение от пользователя."""
    try:
        from_id = int(msg.get("from_id") or msg.get("peer_id") or 0)
        if not from_id or from_id < 1:
            return
        # Сообщения из бесед (peer_id > 2e9) — не поддерживаем, только личные
        peer = int(msg.get("peer_id") or from_id)
        if peer > 2000000000:
            return
        text = msg.get("text") or ""
        # payload от текстовой кнопки
        button_payload = {}
        raw_payload = msg.get("payload")
        if isinstance(raw_payload, str) and raw_payload.strip():
            try:
                button_payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                button_payload = {}
        elif isinstance(raw_payload, dict):
            button_payload = raw_payload
        # ref-метка (рефералы/классы по ссылке vk.me)
        ref = msg.get("ref") or ""
        if ref and not text.strip():
            text = "старт"
        # Антиспам
        if is_user_spamming(from_id):
            return
        user = get_user(from_id)
        # Блокировка
        blocked, reason = get_user_block(from_id)
        if blocked:
            if text.strip().lower() in ("/start", "старт", "начать"):
                await vk.send(from_id, f"🚫 Вы заблокированы. Причина: {reason}")
            return
        user["last_activity"] = now().strftime("%Y-%m-%d %H:%M")
        save_user(user)
        # Нажатие текстовой кнопки меню (payload)
        if button_payload.get("c") == "menu" and button_payload.get("b"):
            # если пользователь переименовал кнопку — payload хранит исходное имя,
            # но текст кнопки приходит в text; приоритет — text
            label = text.strip() or button_payload["b"]
            return await handle_main_menu(user, label, msg)
        if button_payload.get("c") and button_payload.get("c") != "menu":
            return await handle_callback(user, button_payload, msg)
        # Первое сообщение незнакомца — старт
        if not user.get("setup_completed") and not user.get("state", "").startswith("REG_"):
            if text.strip().lower() in ("", "старт", "начать", "/start", "start", "hi", "привет"):
                return await start_registration(user, ref)
            return await start_registration(user, ref)
        await route_message(user, text, msg)
    except Exception as e:
        logger.error("on_message_new: %s\n%s", e, traceback.format_exc())


async def on_message_event(ev: dict) -> None:
    """Нажатие callback-кнопки (message_event)."""
    try:
        user_id = int(ev.get("user_id") or 0)
        peer_id = int(ev.get("peer_id") or user_id)
        event_id = str(ev.get("event_id") or "")
        raw_payload = ev.get("payload")
        payload = {}
        if isinstance(raw_payload, str) and raw_payload.strip():
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                payload = {}
        elif isinstance(raw_payload, dict):
            payload = raw_payload
        if not user_id:
            return
        # Обязательно закрываем «крутилку» на кнопке
        await vk.answer_event(event_id, user_id, peer_id)
        user = get_user(user_id)
        blocked, reason = get_user_block(user_id)
        if blocked:
            return
        user["last_activity"] = now().strftime("%Y-%m-%d %H:%M")
        save_user(user)
        if not user.get("setup_completed") and not user.get("state", "").startswith("REG_"):
            return await start_registration(user, "")
        if not payload.get("c"):
            return
        await handle_callback(user, payload, ev)
    except Exception as e:
        logger.error("on_message_event: %s\n%s", e, traceback.format_exc())


# ==================================
# === ГЛАВНАЯ ФУНКЦИЯ ===
# ==================================

async def main() -> None:
    global vk
    if not VK_TOKEN:
        raise SystemExit(
            "❌ Переменная окружения VK_TOKEN не задана (токен сообщества ВК\n"
            "с правами: Сообщения, Фотографии, Документы, Управление).\n"
            "Установите её в настройках хостинга или в .env.")
    if not GROQ_API_KEY:
        logger.warning("GROQ_API_KEY не задан — DEVORKS+ai будет недоступен.")
    if not DEVELOPER_ID:
        logger.warning("DEVELOPER_ID не задан — панель разработчика будет недоступна.")
    if not DEEPSEEK_API_KEY:
        logger.warning("DEEPSEEK_API_KEY не задан — AI Agent будет недоступен.")
    if not MINIAPP_URL:
        logger.info("MINIAPP_URL не задан — кнопки веб-облака скрыты.")
    if not (VKAPP_ID and VKAPP_SECRET):
        logger.info("VKAPP_ID/VKAPP_SECRET не заданы — вход в мини-апп по VK-подписи "
                    "выключен (остаётся вход по VK ID + паролю).")

    group_id = await resolve_group_id()
    if not group_id:
        raise SystemExit("❌ Не удалось определить id сообщества. Задайте VK_GROUP_ID.")
    vk = VKClient(VK_TOKEN, group_id)
    await vk.start()
    logger.info("DEVORKS+ VK %s запускается: сообщество %s.", VK_BOT_BUILD, group_id)

    # Веб-сервер (keep-alive + мини-апп + API)
    port = int(_env("PORT", "8080"))
    runner = web.AppRunner(create_web_app())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Веб-сервер: http://0.0.0.0:%s (health: /health, мини-апп: /)", port)

    # Тикер уведомлений
    ticker = asyncio.create_task(ticker_loop())

    # Long Poll (основной цикл)
    try:
        await longpoll_loop()
    finally:
        ticker.cancel()
        await vk.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
