"""
get_refresh_token.py

One-time LOCAL helper (do not run in GitHub Actions) that opens a browser,
asks you to sign in to Gmail, and prints a refresh token to store in the
GMAIL_REFRESH_TOKEN repo secret.

Usage:
    pip install google-auth-oauthlib
    python get_refresh_token.py <CLIENT_ID> <CLIENT_SECRET>

Your OAuth client must be of type "Desktop app". Set the consent screen to
"In production" first (Google Auth Platform -> Audience -> Publish app);
while it is in "Testing", Google expires refresh tokens after 7 days and the
daily workflow starts failing.
"""

import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: python get_refresh_token.py <CLIENT_ID> <CLIENT_SECRET>")
    client_id, client_secret = sys.argv[1], sys.argv[2]

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        SCOPES,
    )
    # prompt="consent" forces Google to issue a fresh refresh token.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    print("\nGMAIL_REFRESH_TOKEN:\n")
    print(creds.refresh_token)


if __name__ == "__main__":
    main()
