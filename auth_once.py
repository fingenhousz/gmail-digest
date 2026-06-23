"""
Run this ONCE locally to authorize Gmail access and generate token.json.
After running, copy the content of token.json into your GitHub Secret GMAIL_TOKEN.
"""

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0)

with open("token.json", "w") as f:
    f.write(creds.to_json())

print("token.json created successfully.")
print("\nCopy this into your GitHub Secret GMAIL_TOKEN:")
print(creds.to_json())
