import requests


class TelegramNotifier:
    """
    Simple Telegram notifier using the Bot API and requests.

    - Designed for backtests: fails fast on network issues.
    - Never raises on HTTP/network errors; just logs and returns None.
    """

    def __init__(self, bot_token, chat_ids=None, timeout=3):
        # timeout is per request, in seconds (small on purpose for BT)
        self.bot_token = bot_token
        self.chat_ids = [str(c) for c in (chat_ids or [])]
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        print(
            f"[DEBUG][TG] TelegramNotifier init: chat_ids={self.chat_ids}, "
            f"timeout={self.timeout}"
        )

    def send_to_chat(
        self,
        chat_id,
        text,
        reply_to_message_id=None,
        parse_mode="HTML",
        disable_web_page_preview=True,
    ):
        """
        Send a message to a single chat.

        Returns message_id on success, or None on any error.
        """
        chat_id = str(chat_id)
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id

        try:
            resp = requests.post(
                self.base_url,
                data=payload,
                timeout=self.timeout,
            )

            # HTTP status errors
            try:
                resp.raise_for_status()
            except Exception as e:
                print(f"[TELEGRAM ERROR] chat_id={chat_id} http_error={e}")
                return None

            # JSON parse
            try:
                data = resp.json()
            except Exception as e:
                print(f"[TELEGRAM ERROR] chat_id={chat_id} json_error={e}")
                return None

            if not data.get("ok"):
                print(f"[TELEGRAM ERROR] chat_id={chat_id} api_error={data}")
                return None

            msg_id = data["result"]["message_id"]
            print(f"[DEBUG][TG] SENT chat_id={chat_id} message_id={msg_id}")
            return msg_id

        except requests.exceptions.ReadTimeout as e:
            # Short, single-line log; do not re-raise
            print(f"[TELEGRAM ERROR] chat_id={chat_id} timeout={e}")
            return None
        except requests.exceptions.RequestException as e:
            # Any other requests-level error (connection, SSL, etc.)
            print(f"[TELEGRAM ERROR] chat_id={chat_id} request_error={e}")
            return None
        except Exception as e:
            # Catch-all safety net so backtest never crashes from Telegram
            print(f"[TELEGRAM ERROR] chat_id={chat_id} unexpected_error={e}")
            return None

    def send(
        self,
        text,
        reply_to_message_id=None,
        parse_mode="HTML",
        disable_web_page_preview=True,
    ):
        """
        Broadcast a message to all configured chats using the same reply_to_message_id.
        Backtest uses a wrapper for per-chat reply IDs.
        """
        results = {}
        if not self.chat_ids:
            print("[DEBUG][TG] send() called but chat_ids is empty")
            return results

        for cid in self.chat_ids:
            msg_id = self.send_to_chat(
                chat_id=cid,
                text=text,
                reply_to_message_id=reply_to_message_id,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            results[cid] = msg_id

        return results
