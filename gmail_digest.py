"""
Gmail Newsletter Digest -> WhatsApp via CallMeBot
Fetches unread newsletters via IMAP, summarizes with Claude, sends to WhatsApp.
"""

import os
import re
import sys
import imaplib
import email
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime

import anthropic
from bs4 import BeautifulSoup

MAX_NEWSLETTER_AGE = timedelta(hours=36)

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
    # Cap is generous: stale emails are marked \Seen and skipped before ever
    # reaching Claude, so a large backlog (e.g. after the age filter was
    # added) drains in one run instead of 20-at-a-time across many triggers.
    for eid in reversed(email_ids[-500:]):  # most recent first
        _, msg_data = mail.fetch(eid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        subject = decode_str(msg.get("Subject", "(pas de sujet)"))
        sender = decode_str(msg.get("From", "Inconnu"))

        date_header = msg.get("Date")
        if date_header:
            try:
                sent_at = parsedate_to_datetime(date_header)
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - sent_at
                if age > MAX_NEWSLETTER_AGE:
                    mail.store(eid, "+FLAGS", "\\Seen")
                    print(f"  Skipping stale email from {sender} (sent {age} ago)")
                    continue
            except (TypeError, ValueError):
                pass  # unparseable date — don't drop the email over it

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
3. Le bullet doit etre 100% autoporteur : un lecteur qui n'a vu aucun autre message doit tout comprendre. Introduis toute personne/entreprise/produit la premiere fois qu'il apparait (ex: "Sam Altman, CEO d'OpenAI, ..."), meme si ca semble evident ou deja connu
4. Jamais de "il", "elle", "ils", "ce produit", "cette annonce" sans antecedent explicite dans le meme bullet
5. Phrases courtes. Apostrophes droites uniquement (')
6. Ignore les actualites que la newsletter recycle ou rappelle (retrospectives, "cette semaine on a parle de...", references a des annonces anterieures) : ne retiens que ce qui est presente comme une information nouvelle du jour

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


# CallMeBot returns HTTP 200 even on failure (invalid apikey, exhausted message
# quota, unauthorized number...) with the error only in the HTML body — so the
# body MUST be checked, not just the status code.
CALLMEBOT_OK_MARKERS = ("message queued", "added into the queue", "message sent")
CALLMEBOT_FAIL_MARKERS = (
    "<b>0</b> messages left",
    "have 0 messages left",
    "apikey is invalid",
    "invalid parameter",
    "not registered",
    "error:",
)

TAG_RE = re.compile(r"<[^>]+>")


def send_whatsapp(message):
    """Send a WhatsApp message via CallMeBot.

    Returns True only if CallMeBot's response body confirms the message was
    queued for delivery. Any other outcome is logged loudly and returns False.
    """
    encoded = urllib.parse.quote(message)
    url = (
        f"https://api.callmebot.com/whatsapp.php"
        f"?phone={CALLMEBOT_PHONE}&text={encoded}&apikey={CALLMEBOT_APIKEY}"
    )
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  CallMeBot: FAILED — network error: {e}")
        return False

    lower = body.lower()
    ok = status in (200, 210) and any(
        m in lower for m in CALLMEBOT_OK_MARKERS
    ) and not any(m in lower for m in CALLMEBOT_FAIL_MARKERS)

    if ok:
        print(f"  CallMeBot: {status} OK — message queued ({len(message)} chars)")
    else:
        snippet = " ".join(TAG_RE.sub(" ", body).split())[:300]
        print(f"  CallMeBot: FAILED (HTTP {status}) — {snippet}")
    return ok


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
    failures = 0 if send_whatsapp(header) else 1

    for i, block in enumerate(blocks):
        time.sleep(10)
        tagged_block = f"\U0001f4f0 {block}"
        print(f"\n[{i+1}/{len(blocks)}] {block[:60]}...")
        if not send_whatsapp(tagged_block):
            failures += 1

    if failures:
        print(
            f"\nERROR: {failures}/{len(blocks) + 1} WhatsApp messages were NOT "
            "delivered by CallMeBot (see responses above). Common causes: "
            "message quota exhausted, invalid apikey, or the phone number is "
            "no longer authorized (re-send 'I allow callmebot to send me "
            "messages' to the CallMeBot WhatsApp bot)."
        )
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
