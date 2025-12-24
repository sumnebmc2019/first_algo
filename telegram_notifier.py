# telegram_notifier.py

import requests


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_ids):
        """
        chat_ids: str or list of str/int
        """
        self.bot_token = bot_token
        if isinstance(chat_ids, (list, tuple, set)):
            self.chat_ids = list(chat_ids)
        else:
            self.chat_ids = [chat_ids]

    def send(self, text: str):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        for chat_id in self.chat_ids:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }
            try:
                resp = requests.post(url, data=payload, timeout=5)
                resp.raise_for_status()
            except Exception as e:
                print(f"[TELEGRAM ERROR] chat_id={chat_id} error={e}")
