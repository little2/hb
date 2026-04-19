# config.py
import os
from dotenv import load_dotenv
import json

# 读取 rely.env（本机/Render 都可不放这个文件；Render 用 env vars）
load_dotenv(dotenv_path=".env")


config = {}
# 嘗試載入 JSON 並合併參數
try:
    configuration_json = json.loads(os.getenv('CONFIGURATION', '') or '{}')
    if isinstance(configuration_json, dict):
        config.update(configuration_json)  # 將 JSON 鍵值對合併到 config 中
except Exception as e:
    print(f"⚠️ 無法解析 CONFIGURATION：{e}")


def _load_json_env(env_name: str) -> dict:
    raw = os.getenv(env_name, '')
    if not raw:
        return {}

    try:
        value = json.loads(raw)
    except Exception as e:
        print(f"⚠️ 無法解析 {env_name}：{e}")
        return {}

    if not isinstance(value, dict):
        print(f"⚠️ {env_name} 不是 JSON object，改用預設值")
        return {}

    return value

# --- Bot runtime mode ---
# polling | webhook
BOT_MODE = os.getenv("BOT_MODE", "polling").lower()

# --- Telegram bot token ---
BOT_TOKEN = config.get('bot_token', os.getenv('BOT_TOKEN', ''))

# --- Redis (Render Key Value) ---
REDIS_URL = os.getenv("REDIS_URL", "")

# --- MySQL (env keys: MYSQL_DB_*) ---
MYSQL_HOST      = config.get('db_host', os.getenv('MYSQL_DB_HOST', 'localhost'))
MYSQL_USER      = config.get('db_user', os.getenv('MYSQL_DB_USER', ''))
MYSQL_PASSWORD  = config.get('db_password', os.getenv('MYSQL_DB_PASSWORD', ''))
MYSQL_DB        = config.get('db_name', os.getenv('MYSQL_DB_NAME', ''))
MYSQL_DB_PORT   = int(config.get('db_port', os.getenv('MYSQL_DB_PORT', 3306)))

# 优先走 UNIX socket（你项目要求：Localhost via UNIX socket）
MYSQL_UNIX_SOCKET = os.getenv("MYSQL_UNIX_SOCKET", "")

# --- Webhook (only used if BOT_MODE == "webhook") ---
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "").rstrip("/")     # e.g. https://xxx.onrender.com
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram/webhook")
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", os.getenv("PORT", "10000")))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")             # recommended

# --- Hongbao rules ---
MIN_UNIT = int(os.getenv("MIN_UNIT", "1"))
MAX_COUNT = int(os.getenv("MAX_COUNT", "50"))
MAX_TOTAL_AMOUNT = int(os.getenv("MAX_TOTAL_AMOUNT", "667"))
DEFAULT_EXPIRE_MINUTES = int(os.getenv("DEFAULT_EXPIRE_MINUTES", "180"))

# --- Optional throttles / safety ---
GROUP_NOTICE_THROTTLE = os.getenv("GROUP_NOTICE_THROTTLE", "1") == "1"
GROUP_NOTICE_PER_SEC = int(os.getenv("GROUP_NOTICE_PER_SEC", "2"))
DM_BLOCK_TTL_SEC = int(os.getenv("DM_BLOCK_TTL_SEC", "60"))

X_MAN_BOT_ID = int(os.getenv("X_MAN_BOT_ID", 0))

# -- 

TARGET_CHAT_ID = config.get("target_chat_id", int(os.getenv("TARGET_CHAT_ID", "0")))
TARGET_MESSAGE_THREAD_ID = config.get("target_message_thread_id", int(os.getenv("TARGET_MESSAGE_THREAD_ID", "0")))

REVIEW_CHAT_ID = config.get("review_chat_id", int(os.getenv("REVIEW_CHAT_ID", "0")))
REVIEW_MESSAGE_THREAD_ID = config.get("review_message_thread_id", int(os.getenv("REVIEW_MESSAGE_THREAD_ID", "0")))

s_conf = _load_json_env("SWITCHBOT_CONFIGURATION")
SWITCHBOT_CHAT_ID: int = int(s_conf.get("chat_id") or 0)
SWITCHBOT_THREAD_ID: int = int(s_conf.get("thread_id") or 0)
SWITCHBOT_TOKEN: str = str(s_conf.get("switchbot_token") or "")

version = "0.1.1"