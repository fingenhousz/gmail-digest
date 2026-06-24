# gmail-digest

Fetches newsletters from Gmail every morning, summarizes them with Claude, and sends the digest to WhatsApp via CallMeBot.

## How it works

1. Connects to Gmail via IMAP and fetches emails from the last 24h in your `Newsletters` label
2. Summarizes them with Claude (claude-sonnet-4-6)
3. Sends the digest to your WhatsApp via CallMeBot

Runs automatically every day at 6:00 UTC (7:00 AM Paris in winter, 8:00 AM in summer) via GitHub Actions.

## Setup

### 1. Gmail App Password

Gmail IMAP requires an App Password (not your regular password):

1. Enable 2-Step Verification on your Google account
2. Go to myaccount.google.com/apppasswords
3. Create a new app password named "gmail-digest"
4. Save the 16-character password

### 2. Gmail label

Create a label named `Newsletters` in Gmail and set up filters to route your newsletters there.

### 3. CallMeBot WhatsApp

1. Add `+34 644 59 78 23` to your WhatsApp contacts
2. Send this exact message: `I allow callmebot to send me messages`
3. You will receive your API key by reply

### 4. GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|--------|-------------|
| `GMAIL_USER` | Your Gmail address (`you@gmail.com`) |
| `GMAIL_APP_PASSWORD` | The 16-character App Password from step 1 |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `CALLMEBOT_PHONE` | Your number in international format (`+33612345678`) |
| `CALLMEBOT_APIKEY` | Your CallMeBot API key from step 3 |

### 5. Test

Once secrets are set, trigger a run from **Actions → Gmail Newsletter Digest → Run workflow**.