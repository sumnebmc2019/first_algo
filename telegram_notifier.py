import requests


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_ids):
        if isinstance(chat_ids, (list, tuple, set)):
            self.chat_ids = list(chat_ids)
        else:
            self.chat_ids = [chat_ids]
        self.bot_token = bot_token

    def send(self, text: str):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        for chat_id in self.chat_ids:
            try:
                resp = requests.post(
                    url,
                    data={"chat_id": chat_id, "text": text},
                    timeout=5,
                )
                resp.raise_for_status()
            except Exception as e:
                print(f"[TELEGRAM ERROR] chat_id={chat_id} error={e}")
