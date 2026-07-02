"""
Gmail Newsletter Digest -> WhatsApp via CallMeBot
Fetches unread newsletters via IMAP, summarizes with Claude, sends to WhatsApp.
"""

import os
import re
import imaplib
import email
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from email.header import decode_header

import anthropic
from bs4 import BeautifulSoup

GMAIL_USER = os.environ["GMAIL_USER"].strip()
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].strip()
CALLMEBOT_PHONE = os.environ["CALLMEBOT_PHONE"].strip()
CALLMEBOT_APIKEY = os.environ["CALLMEBOT_APIKEY"].strip()
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()
GMAIL_LABEL = os.environ.get("GMAIL_LABEL", "Newsletters").strip()

APOSTROPHE_RE = re.compile("[‘’‚‛ʼʻ′‵]")


def normalize_apostrophes(text):
    return APOSTROPHE_RE.sub("'", text)


def decode_str(s):
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            result.append(part)
    return "".join(result)


def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "img", "a"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    return "\n".join(lines)


def get_text_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
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
                        body = html_to_text(raw)
                        break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            ct = msg.get_content_type()
            raw = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
            body = html_to_text(raw) if ct == "text/html" else raw
    return body


def sender_key(sender):
    """Normalize sender address to deduplicate (e.g. multiple Free Press emails)."""
    match = re.search(r"<(.+?)>", sender)
    addr = match.group(1) if match else sender
    return addr.lower().strip()


def fetch_newsletters():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select(f'"{GMAIL_LABEL}"')

    _, data = mail.search(None, "UNSEEN")
    email_ids = data[0].split()
    if not email_ids:
        mail.logout()
        return []

    emails = []
    seen_senders = set()
    for eid in reversed(email_ids[-20:]):  # most recent first
        _, msg_data = mail.fetch(eid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        subject = decode_str(msg.get("Subject", "(pas de sujet)"))
        sender = decode_str(msg.get("From", "Inconnu"))
        key = sender_key(sender)
        if key in seen_senders:
            mail.store(eid, "+FLAGS", "\\Seen")
            print(f"  Skipping duplicate from {sender}")
            continue
        seen_senders.add(key)
        body = get_text_body(msg)
        emails.append({"subject": subject, "sender": sender, "body": body[:8000]})
        mail.store(eid, "+FLAGS", "\\Seen")

    mail.logout()
    return emails


def summarize_with_claude(emails):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    emails_text = "\n\n---\n\n".join(
        f"De: {e['sender']}\nSujet: {e['subject']}\n\n{e['body']}"
        for e in emails
    )

    prompt = f"""Tu es un assistant qui cree des digests de newsletters percutants et analytiques.

Voici {len(emails)} newsletter(s) non lues :

{emails_text}

Cree un digest en francais. Pour CHAQUE newsletter, genere un bloc avec :
- Titre : *[Emoji] [Nom newsletter]* (emoji pertinent au contenu du jour)
- 2-3 bullet points

REGLES pour chaque bullet point :
1. Commence par le SO WHAT : l'implication concrete, ce que ca change, pourquoi ca compte
2. Appuie avec les faits et chiffres concrets qui le justifient
3. Le bullet doit etre autonome : si tu mentionnes une personne ou entreprise, introduis-la brievement la premiere fois (ex: "Sam Altman, CEO d'OpenAI, ...")
4. Jamais de "il", "elle", "ils" sans antecedent dans le meme bullet
5. Phrases courtes. Apostrophes droites uniquement (').

Bon exemple : "L'IA accelere le remplacement des cols blancs : McKinsey estime 12M de postes automatisables d'ici 2030 aux US, concentres sur la compta et le droit."
Mauvais exemple : "Elle a lance un nouveau produit qui pourrait changer les choses."

Separe chaque newsletter par une ligne contenant uniquement "---SPLIT---".
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )

    text = message.content[0].text
    return normalize_apostrophes(text)


def send_whatsapp(message):
    encoded = urllib.parse.quote(message)
    url = (
        f"https://api.callmebot.com/whatsapp.php"
        f"?phone={CALLMEBOT_PHONE}&text={encoded}&apikey={CALLMEBOT_APIKEY}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            print(f"  CallMeBot: {response.status} ({len(message)} chars)")
    except urllib.error.HTTPError as e:
        print(f"  CallMeBot error: {e.code} — skipping this message")


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching newsletters via IMAP...")
    emails = fetch_newsletters()

    if not emails:
        print("No unread newsletters — skipping.")
        return

    print(f"Found {len(emails)} newsletter(s). Summarizing with Claude...")
    digest = summarize_with_claude(emails)

    blocks = [b.strip() for b in digest.split("---SPLIT---") if b.strip()]
    print(f"Sending {len(blocks)} messages to WhatsApp...")

    date_str = datetime.now().strftime("%d %B %Y")
    header = f"\U0001f4f0 *Digest du {date_str}* — {len(blocks)} newsletters"
    send_whatsapp(header)

    for i, block in enumerate(blocks):
        time.sleep(10)
        print(f"\n[{i+1}/{len(blocks)}] {block[:60]}...")
        send_whatsapp(block)

    print("\nDone.")


if __name__ == "__main__":
    main()
