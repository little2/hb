import os
from dotenv import load_dotenv
import json

load_dotenv(dotenv_path=".news.env")
DB_DSN = os.getenv("DB_DSN")
BOT_TOKEN = os.getenv("BOT_TOKEN")
AES_KEY = os.getenv("AES_KEY", "")

BOT_MODE = os.getenv("BOT_MODE", "polling").lower()
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH")
WEBAPP_HOST = os.getenv("WEBAPP_HOST")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", 10000))

s_raw = os.getenv("SWITCHBOT_CONFIGURATION")
s_conf = json.loads(s_raw)
SWITCHBOT_CHAT_ID: int = s_conf["chat_id"]
SWITCHBOT_THREAD_ID: int = s_conf["thread_id"]
SWITCHBOT_TOKEN: str = s_conf["switchbot_token"]

x_raw = os.getenv("X_CONFIGURATION")
x_conf = json.loads(x_raw)
X_MAN_BOT_ID: int = x_conf["x_man_bot_id"]
X_MAN_BOT_PHONE: str = x_conf["x_man_bot_phone"]
X_MAN_BOT_USERNAME: str = x_conf["x_man_bot_username"]