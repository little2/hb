import os
from dotenv import load_dotenv
import json
import lz_var

load_dotenv(dotenv_path=".news.env")
DB_DSN = os.getenv("DB_DSN")
BOT_TOKEN = os.getenv("BOT_TOKEN")
AES_KEY = os.getenv("AES_KEY", "")


def _load_json_env(env_name: str) -> dict:
	raw = os.getenv(env_name, "")
	if not raw:
		return {}

	try:
		value = json.loads(raw)
	except Exception as e:
		print(f"⚠️ {env_name} parse failed: {e}; fallback to defaults", flush=True)
		return {}

	if not isinstance(value, dict):
		print(f"⚠️ {env_name} is not a JSON object, fallback to defaults", flush=True)
		return {}

	return value

BOT_MODE = os.getenv("BOT_MODE", "polling").lower()
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH")
WEBAPP_HOST = os.getenv("WEBAPP_HOST")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", 10000))

s_conf = _load_json_env("SWITCHBOT_CONFIGURATION")
SWITCHBOT_CHAT_ID: int = int(s_conf.get("chat_id") or 0)
SWITCHBOT_THREAD_ID: int = int(s_conf.get("thread_id") or 0)
SWITCHBOT_TOKEN: str = str(s_conf.get("switchbot_token") or "")

x_conf = _load_json_env("X_CONFIGURATION")
X_MAN_BOT_ID: int = int(x_conf.get("x_man_bot_id") or 0)
X_MAN_BOT_PHONE: str = str(x_conf.get("x_man_bot_phone") or "")
X_MAN_BOT_USERNAME: str = str(x_conf.get("x_man_bot_username") or "")

bot_raw = os.getenv("BOT_CONFIGURATION")
bot_conf = _load_json_env("BOT_CONFIGURATION")

lz_var.publish_bot_name = str(bot_conf.get("publish_bot_name") or "")
lz_var.uploader_bot_name = str(bot_conf.get("uploader_bot_name") or "")
lz_var.guider_bot_name = str(bot_conf.get("guider_bot_name") or "")

