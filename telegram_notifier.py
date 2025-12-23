# telegram_notifier.py

import requests


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, text: str):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            resp = requests.post(url, data=payload, timeout=5)
            resp.raise_for_status()
        except Exception as e:
            # For now, just print; you can log this later
            print(f"[TELEGRAM ERROR] {e}")
