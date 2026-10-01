import os
import urllib.parse
import urllib.request
import json
from pathlib import Path


BASE = Path.home() / "smart-electricity"
ENV_FILE = BASE / "config" / "telegram.env"


def load_config():
    if not ENV_FILE.exists():
        raise RuntimeError(
            f"Telegram configuration not found: {ENV_FILE}"
        )

    config = {}

    with ENV_FILE.open() as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            key, sep, value = line.partition("=")

            if sep:
                config[key.strip()] = value.strip()

    token = config.get("TELEGRAM_BOT_TOKEN")
    chat_id = config.get("TELEGRAM_CHAT_ID")

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is missing")

    return token, chat_id


def send_telegram(message):
    """
    Send a Telegram notification.

    Raises an exception if Telegram does not confirm delivery.
    """

    token, chat_id = load_config()

    url = (
        "https://api.telegram.org/bot"
        + token
        + "/sendMessage"
    )

    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
        }
    ).encode()

    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=10
    ) as response:
        result = json.loads(
            response.read().decode()
        )

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram rejected message: {result}"
        )

    return True


if __name__ == "__main__":
    send_telegram(
        "⚡ Smart Electricity\n\n"
        "✅ Telegram notification module test successful.\n\n"
        "FoxESS control has NOT been changed."
    )

    print("Telegram test notification sent successfully.")
