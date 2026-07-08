"""
Gmail Newsletter Digest -> Telegram
Fetches unread newsletters via IMAP, summarizes with Claude, sends to Telegram.
"""

import os
import re
import sys
import json
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
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()

# Fetch by sender directly against INBOX instead of relying on a Gmail label
# applied by a user-side filter. The "Newsletters" label filter silently
# stopped matching anything (broken from:(from:x OR from:y ...) syntax /
# possible sender-address drift on Substack's side) and stayed broken for
# weeks with zero visible error — this removes that single point of failure.
# Update this list if a newsletter's sending address changes or a new one is
# added; it doesn't require touching any Gmail setting.
NEWSLETTER_SENDERS = [
    "exponentialview", "jonathanhaidt", "gadallon", "nouveaudepart",
    "nicolascolin", "philippecorbe", "lenny", "linguasinica",
    "mariedolle", "bariweiss", "lewrapup", "therundown", "Benedict Evans",
]

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


SUGGESTED_SENDERS_FILE = "suggested_senders.json"


def load_suggested_senders():
    if not os.path.exists(SUGGESTED_SENDERS_FILE):
        return set()
    try:
        with open(SUGGESTED_SENDERS_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def save_suggested_senders(senders):
    with open(SUGGESTED_SENDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(senders), f, ensure_ascii=False, indent=2)


# Domains used by individual-newsletter platforms — deliberately narrower
# than "has a List-Unsubscribe header", which matches almost any marketing
# or transactional email and floods the suggestion with noise (confirmed:
# an earlier version using that alone produced a Telegram message so long
# it exceeded the 4096-char API limit). This targets "same type as what's
# already tracked" — most current NEWSLETTER_SENDERS are on Substack.
NEWSLETTER_PLATFORM_DOMAINS = (
    "substack.com", "beehiiv.com", "ghost.io", "convertkit.com", "buttondown.email",
)

MAX_SUGGESTED_CANDIDATES = 15


def scan_for_new_newsletter_candidates():
    """Suggestion-only detection: never auto-adds a sender to NEWSLETTER_SENDERS.
    Flags recent inbox mail from a known newsletter-platform domain whose
    sender isn't already tracked, so Florian can decide whether to add it.
    Each sender is suggested at most once (tracked in suggested_senders.json)."""
    already_suggested = load_suggested_senders()

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select("INBOX")

    since = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%d-%b-%Y")
    _, data = mail.search(None, f"(SINCE {since})")
    ids = data[0].split()

    candidates = {}
    for eid in ids[-300:]:
        _, msg_data = mail.fetch(eid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        header_bytes = msg_data[0][1] if msg_data and msg_data[0] else b""
        if not header_bytes:
            continue
        msg = email.message_from_bytes(header_bytes)
        sender = decode_str(msg.get("From", ""))
        key = sender_key(sender)
        if not key or key in already_suggested:
            continue
        if not any(domain in key for domain in NEWSLETTER_PLATFORM_DOMAINS):
            continue
        if any(s.lower() in key for s in NEWSLETTER_SENDERS):
            continue  # already tracked
        candidates[key] = (sender, decode_str(msg.get("Subject", "(pas de sujet)")))

    mail.logout()
    return candidates, already_suggested


def build_from_or_query(senders):
    """Build a nested IMAP '(OR FROM "a" (OR FROM "b" FROM "c"))' expression."""
    terms = [f'FROM "{s}"' for s in senders]
    query = terms[-1]
    for t in reversed(terms[:-1]):
        query = f'(OR {t} {query})'
    return query


def fetch_newsletters():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select("INBOX")

    # IMAP SINCE is day-granularity only — this is a coarse pre-filter to
    # keep the fetch small; the precise 36h cutoff is enforced per-message
    # below via the actual Date header.
    since = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%d-%b-%Y")
    query = f'(SINCE {since}) (UNSEEN) {build_from_or_query(NEWSLETTER_SENDERS)}'
    _, data = mail.search(None, query)
    email_ids = data[0].split()
    print(f"  {len(email_ids)} unread newsletter email(s) matched in INBOX")
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
        sent_at = None
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
                sent_at = None  # unparseable date — don't drop the email over it

        key = sender_key(sender)
        if key in seen_senders:
            mail.store(eid, "+FLAGS", "\\Seen")
            print(f"  Skipping duplicate from {sender}")
            continue
        seen_senders.add(key)
        body = get_text_body(msg)
        date_str = sent_at.strftime("%d/%m/%Y") if sent_at else "date inconnue"
        emails.append({"subject": subject, "sender": sender, "body": body[:8000], "date": date_str})
        mail.store(eid, "+FLAGS", "\\Seen")

    mail.logout()
    return emails


def summarize_with_claude(emails):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    emails_text = "\n\n---\n\n".join(
        f"De: {e['sender']}\nDate d'envoi: {e['date']}\nSujet: {e['subject']}\n\n{e['body']}"
        for e in emails
    )

    prompt = f"""Tu es un assistant qui cree des digests de newsletters percutants et analytiques.

Voici {len(emails)} newsletter(s) non lues :

{emails_text}

Cree un digest en francais. Pour CHAQUE newsletter, genere un bloc avec :
- Titre : *[Emoji] [Nom newsletter] ([Date d'envoi])* (emoji pertinent au contenu du jour, date d'envoi au format JJ/MM/AAAA fournie ci-dessus)
- 2-3 bullet points

REGLES pour chaque bullet point :
1. Commence par le SO WHAT : l'implication concrete, ce que ca change, pourquoi ca compte
2. Appuie avec les faits et chiffres concrets qui le justifient
3. Le bullet doit etre 100% autoporteur : un lecteur qui n'a vu aucun autre message doit tout comprendre. Introduis toute personne/entreprise/produit la premiere fois qu'il apparait (ex: "Sam Altman, CEO d'OpenAI, ..."), meme si ca semble evident ou deja connu
4. Jamais de "il", "elle", "ils", "ce produit", "cette annonce" sans antecedent explicite dans le meme bullet
5. Phrases courtes. Apostrophes droites uniquement (')
6. Ignore les actualites que la newsletter recycle ou rappelle (retrospectives, "cette semaine on a parle de...", references a des annonces anterieures) : ne retiens que ce qui est presente comme une information nouvelle du jour
7. Si le bullet s'appuie sur une etude, un rapport ou une enquete cite dans la newsletter (ex: "une etude de McKinsey montre..."), precise sa date de publication entre parentheses juste apres l'avoir mentionnee (ex: "une etude de McKinsey (mars 2026) montre..."). Si la date de l'etude n'est pas indiquee dans le texte source, ne l'invente pas — omets simplement la parenthese.

Bon exemple : "L'IA accelere le remplacement des cols blancs : une etude McKinsey (janvier 2026) estime 12M de postes automatisables d'ici 2030 aux US, concentres sur la compta et le droit."
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


def send_telegram(message):
    """Send a message via the Telegram bot.

    Telegram's API gives a proper JSON {"ok": bool, ...} response with a
    real HTTP status — unlike CallMeBot, no HTML-body-sniffing needed.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }).encode("utf-8")
    try:
        with urllib.request.urlopen(url, data=data, timeout=30) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  Telegram: FAILED — network error: {e}")
        return False

    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        print(f"  Telegram: FAILED (HTTP {status}) — invalid response: {body[:300]}")
        return False

    if result.get("ok"):
        print(f"  Telegram: {status} OK — message sent ({len(message)} chars)")
        return True
    print(f"  Telegram: FAILED (HTTP {status}) — {result.get('description', body[:300])}")
    return False


def send_new_newsletter_suggestion():
    """Suggestion-only: scan for candidate newsletters and, if any are new,
    send one notification listing them. Never modifies NEWSLETTER_SENDERS —
    Florian decides whether to add them."""
    try:
        candidates, already_suggested = scan_for_new_newsletter_candidates()
    except Exception as e:
        print(f"  Newsletter-candidate scan failed (non-fatal): {e}")
        return
    if not candidates:
        return
    items = list(candidates.items())
    shown, rest = items[:MAX_SUGGESTED_CANDIDATES], items[MAX_SUGGESTED_CANDIDATES:]
    lines = "\n".join(f'- {sender} (sujet recent : "{subject}")' for _, (sender, subject) in shown)
    if rest:
        lines += f"\n... et {len(rest)} autre(s) (relance le scan apres avoir traite ceux-ci)"
    suggestion = (
        "\U0001f50d *Nouvelle(s) newsletter(s) potentielle(s) detectee(s)*, "
        "pas encore suivie(s) :\n" + lines +
        "\n\nDis-le a Claude si tu veux que je les ajoute au suivi."
    )
    if send_telegram(suggestion):
        # Only mark the shown candidates as suggested — the truncated "rest"
        # should resurface on the next run instead of being silently dropped.
        save_suggested_senders(already_suggested | {key for key, _ in shown})


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching newsletters via IMAP...")
    emails = fetch_newsletters()

    print("Scanning for new newsletter candidates (suggestion only)...")
    send_new_newsletter_suggestion()

    if not emails:
        print("No unread newsletters — skipping.")
        return

    print(f"Found {len(emails)} newsletter(s). Summarizing with Claude...")
    digest = summarize_with_claude(emails)

    blocks = [b.strip() for b in digest.split("---SPLIT---") if b.strip()]
    print(f"Sending {len(blocks)} messages to Telegram...")

    date_str = datetime.now().strftime("%d %B %Y")
    header = f"\U0001f4f0 *Digest du {date_str}* — {len(blocks)} newsletters"
    failures = 0 if send_telegram(header) else 1

    for i, block in enumerate(blocks):
        time.sleep(3)
        tagged_block = f"\U0001f4f0 {block}"
        print(f"\n[{i+1}/{len(blocks)}] {block[:60]}...")
        if not send_telegram(tagged_block):
            failures += 1

    if failures:
        print(
            f"\nERROR: {failures}/{len(blocks) + 1} Telegram messages were NOT "
            "delivered (see responses above). Common causes: invalid bot "
            "token, wrong chat ID, or the bot was blocked."
        )
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
