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
DEFAULT_EXPIRE_MINUTES = int(os.getenv("DEFAULT_EXPIRE_MINUTES", "180"))

# --- Optional throttles / safety ---
GROUP_NOTICE_THROTTLE = os.getenv("GROUP_NOTICE_THROTTLE", "1") == "1"
GROUP_NOTICE_PER_SEC = int(os.getenv("GROUP_NOTICE_PER_SEC", "2"))
DM_BLOCK_TTL_SEC = int(os.getenv("DM_BLOCK_TTL_SEC", "60"))

X_MAN_BOT_ID = int(os.getenv("X_MAN_BOT_ID", 0))