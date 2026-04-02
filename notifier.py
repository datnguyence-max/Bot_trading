"""
Telegram Notifier — Gửi thông báo qua Telegram Bot
"""

import requests
import logging

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.enabled = bool(token and chat_id and
                            token != "YOUR_BOT_TOKEN_HERE" and
                            chat_id != "YOUR_CHAT_ID_HERE")

        if not self.enabled:
            log.warning("Telegram chưa cấu hình — thông báo sẽ bị bỏ qua")

    def send(self, message: str) -> bool:
        if not self.enabled:
            return False
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
            if resp.status_code == 200:
                log.debug("Telegram: gửi thành công")
                return True
            else:
                log.warning(f"Telegram lỗi {resp.status_code}: {resp.text}")
                return False
        except Exception as e:
            log.error(f"Telegram exception: {e}")
            return False

    def send_photo(self, photo_path: str, caption: str = "") -> bool:
        if not self.enabled:
            return False
        try:
            with open(photo_path, "rb") as f:
                resp = requests.post(
                    f"{self.base_url}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption},
                    files={"photo": f},
                    timeout=15
                )
            return resp.status_code == 200
        except Exception as e:
            log.error(f"Telegram send_photo lỗi: {e}")
            return False
