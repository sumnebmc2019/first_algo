import requests


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_ids):
        if isinstance(chat_ids, (list, tuple, set)):
            self.chat_ids = list(chat_ids)
        else:
            self.chat_ids = [chat_ids]
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send(self, text: str, reply_to_message_id: int | None = None):
        """
        Send a message to all chat_ids.
        Returns: dict {chat_id: message_id} for successful sends.
        """
        url = f"{self.base_url}/sendMessage"
        results: dict[int, int] = {}
        for chat_id in self.chat_ids:
            try:
                payload = {
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                    "parse_mode": "HTML",
                }
                if reply_to_message_id is not None:
                    payload["reply_to_message_id"] = reply_to_message_id
                resp = requests.post(url, data=payload, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if data.get("ok") and "result" in data:
                    msg_id = data["result"]["message_id"]
                    results[chat_id] = msg_id
            except Exception as e:
                print(f"[TELEGRAM ERROR] chat_id={chat_id} error={e}")
        return results
