"""
Gmail Newsletter Digest → WhatsApp via CallMeBot
Fetches newsletters from the last 24h, summarizes with Claude, sends to WhatsApp.
"""

import os
import base64
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from email import message_from_bytes

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import anthropic

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CALLMEBOT_PHONE = os.environ["CALLMEBOT_PHONE"]
CALLMEBOT_APIKEY = os.environ["CALLMEBOT_APIKEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_LABEL = os.environ.get("GMAIL_LABEL", "Newsletters")


def get_gmail_service():
    creds = None

    # GitHub Actions: credentials from env vars
    if os.environ.get("GMAIL_TOKEN"):
        token_data = json.loads(os.environ["GMAIL_TOKEN"])
        creds_data = json.loads(os.environ["GMAIL_CREDENTIALS"])

        with open("/tmp/credentials.json", "w") as f:
            json.dump(creds_data, f)

        creds = Credentials(
            token=token_data["token"],
            refresh_token=token_data["refresh_token"],
            token_uri=token_data["token_uri"],
            client_id=token_data["client_id"],
            client_secret=token_data["client_secret"],
            scopes=token_data["scopes"],
        )

    # Local dev: use token.json file
    elif os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds:
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_email_body(payload):
    """Extract plain text body from email payload."""
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")

    for part in payload.get("parts", []):
        if part["mimeType"] == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    # Fallback: try HTML parts
    for part in payload.get("parts", []):
        if part["mimeType"] == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                raw = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                # Very basic HTML strip
                import re
                return re.sub(r"<[^>]+>", " ", raw)

    return ""


def fetch_newsletters(service):
    """Fetch emails from the Newsletters label from the last 24h."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y/%m/%d")
    query = f"after:{since}"

    # Get label ID
    labels_result = service.users().labels().list(userId="me").execute()
    label_id = None
    for label in labels_result.get("labels", []):
        if label["name"].lower() == GMAIL_LABEL.lower():
            label_id = label["id"]
            break

    if not label_id:
        print(f"Label '{GMAIL_LABEL}' not found in Gmail.")
        return []

    results = service.users().messages().list(
        userId="me", labelIds=[label_id], q=query, maxResults=20
    ).execute()

    messages = results.get("messages", [])
    emails = []

    for msg in messages:
        msg_data = service.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in msg_data["payload"]["headers"]}
        subject = headers.get("Subject", "(pas de sujet)")
        sender = headers.get("From", "Inconnu")
        body = get_email_body(msg_data["payload"])

        emails.append({
            "subject": subject,
            "sender": sender,
            "body": body[:4000],  # Cap at 4000 chars per email
        })

    return emails


def summarize_with_claude(emails):
    """Summarize all newsletters into a WhatsApp-friendly digest."""
    if not emails:
        return None

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
    """Send message via CallMeBot WhatsApp API."""
    encoded = urllib.parse.quote(message)
    url = (
        f"https://api.callmebot.com/whatsapp.php"
        f"?phone={CALLMEBOT_PHONE}&text={encoded}&apikey={CALLMEBOT_APIKEY}"
    )
    with urllib.request.urlopen(url, timeout=15) as response:
        status = response.status
        print(f"CallMeBot response: {status}")
    return status


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching newsletters...")
    service = get_gmail_service()
    emails = fetch_newsletters(service)

    if not emails:
        print("No newsletters in the last 24h — skipping WhatsApp message.")
        return

    print(f"Found {len(emails)} newsletter(s). Summarizing with Claude...")
    digest = summarize_with_claude(emails)

    if not digest:
        print("Nothing to summarize.")
        return

    print("Sending to WhatsApp...")
    print("---\n" + digest + "\n---")
    send_whatsapp(digest)
    print("Done.")


if __name__ == "__main__":
    main()
