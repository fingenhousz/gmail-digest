"""
Poll Telegram for button clicks on the digest ("En dis plus") and reply with
the full newsletter text. Runs on a short interval (via a separate workflow)
since GitHub Actions has no way to receive Telegram's webhook push directly.
"""

import os
import json
import urllib.request
import urllib.parse
import urllib.error

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

OFFSET_FILE = "telegram_offset.json"
PENDING_EXPANSIONS_FILE = "pending_expansions.json"
API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def load_offset():
    if not os.path.exists(OFFSET_FILE):
        return 0
    try:
        with open(OFFSET_FILE, encoding="utf-8") as f:
            return json.load(f).get("offset", 0)
    except (json.JSONDecodeError, OSError):
        return 0


def save_offset(offset):
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)


def load_pending_expansions():
    if not os.path.exists(PENDING_EXPANSIONS_FILE):
        return {}
    try:
        with open(PENDING_EXPANSIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def api_call(method, params):
    data = urllib.parse.urlencode(params).encode("utf-8")
    try:
        with urllib.request.urlopen(f"{API}/{method}", data=data, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="ignore"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", errors="ignore"))


def send_message(text):
    for start in range(0, len(text), 4096):
        api_call("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[start:start + 4096],
        })


def answer_callback(callback_query_id, text, alert=False):
    api_call("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": "true" if alert else "false",
    })


def main():
    offset = load_offset()
    result = api_call("getUpdates", {"offset": offset, "timeout": 0})
    if not result.get("ok"):
        print(f"getUpdates failed: {result}")
        return

    updates = result.get("result", [])
    print(f"{len(updates)} update(s) since offset {offset}")
    if not updates:
        return

    expansions = load_pending_expansions()

    for update in updates:
        offset = max(offset, update["update_id"] + 1)
        cq = update.get("callback_query")
        if not cq:
            continue

        data = cq.get("data", "")
        cq_id = cq["id"]
        if not data.startswith("expand:"):
            continue

        eid = data.split(":", 1)[1]
        entry = expansions.get(eid)
        if not entry:
            answer_callback(cq_id, "Ce bouton a expire (plus de 7 jours).", alert=True)
            continue

        answer_callback(cq_id, "Texte complet envoye ci-dessous.")
        # Plain text (no parse_mode): raw newsletter bodies often contain
        # unbalanced */_ characters that would break Telegram's Markdown parser.
        header = f"\U0001f4d6 {entry['sender']} — {entry['subject']} ({entry['date']})\n\n"
        send_message(header + entry["body"])
        print(f"  Expanded newsletter {eid} ({entry['subject'][:50]})")

    save_offset(offset)


if __name__ == "__main__":
    main()
