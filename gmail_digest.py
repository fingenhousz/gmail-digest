"""
Gmail Newsletter Digest → WhatsApp via CallMeBot
Fetches newsletters from the last 24h via IMAP, summarizes with Claude, sends to WhatsApp.
"""

import os
import imaplib
import email
import re
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.header import decode_header

import anthropic

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
CALLMEBOT_PHONE = os.environ["CALLMEBOT_PHONE"]
CALLMEBOT_APIKEY = os.environ["CALLMEBOT_APIKEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_LABEL = os.environ.get("GMAIL_LABEL", "Newsletters")


def decode_str(s):
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            result.append(part)
    return "".join(result)


def get_text_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                    break
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        raw = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                        body = re.sub(r"<[^>]+>", " ", raw)
                        break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
    return body


def fetch_newsletters():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)

    # Gmail labels appear as IMAP folders
    mail.select(f'"{GMAIL_LABEL}"')

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%d-%b-%Y")
    _, data = mail.search(None, f'(SINCE "{since}")')

    email_ids = data[0].split()
    if not email_ids:
        mail.logout()
        return []

    emails = []
    for eid in email_ids[-20:]:  # Max 20 emails
        _, msg_data = mail.fetch(eid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

        subject = decode_str(msg.get("Subject", "(pas de sujet)"))
        sender = decode_str(msg.get("From", "Inconnu"))
        body = get_text_body(msg)

        emails.append({
            "subject": subject,
            "sender": sender,
            "body": body[:4000],
        })

    mail.logout()
    return emails


def summarize_with_claude(emails):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    emails_text = "\n\n---\n\n".join(
        f"De: {e['sender']}\nSujet: {e['subject']}\n\n{e['body']}"
        for e in emails
    )

    prompt = f"""Tu es un assistant qui crée des digests de newsletters concis et utiles.

Voici {len(emails)} newsletter(s) reçue(s) au cours des dernières 24h :

{emails_text}

Crée un digest WhatsApp en français avec :
- Un titre court avec la date d'aujourd'hui
- Pour chaque newsletter : 2-3 bullet points des infos les plus importantes
- Emoji pertinents pour la lisibilité mobile
- Maximum 1500 caractères au total

Format souhaité :
📰 *Digest du [date]*

*[Nom newsletter]*
• Point clé 1
• Point clé 2

*[Nom newsletter]*
• ...
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text


def send_whatsapp(message):
    encoded = urllib.parse.quote(message)
    url = (
        f"https://api.callmebot.com/whatsapp.php"
        f"?phone={CALLMEBOT_PHONE}&text={encoded}&apikey={CALLMEBOT_APIKEY}"
    )
    with urllib.request.urlopen(url, timeout=15) as response:
        print(f"CallMeBot response: {response.status}")


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching newsletters via IMAP...")
    emails = fetch_newsletters()

    if not emails:
        print("No newsletters in the last 24h — skipping WhatsApp message.")
        return

    print(f"Found {len(emails)} newsletter(s). Summarizing with Claude...")
    digest = summarize_with_claude(emails)

    print("Sending to WhatsApp...")
    print("---\n" + digest + "\n---")
    send_whatsapp(digest)
    print("Done.")


if __name__ == "__main__":
    main()
