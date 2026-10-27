# Application Tracker

Auto-detects submitted job applications from Gmail confirmation emails and
logs them to `applications.csv`, run daily by GitHub Actions.

## Setup

1. Drop these files into your existing `job-application-agent` repo (or a new
   one) — `track_applications.py`, `requirements.txt`, `applications.csv`,
   `.github/workflows/track-applications.yml`.

2. **Gmail credentials.** If your existing agent already sends Gmail
   notifications via OAuth, reuse the same client ID/secret/refresh token —
   just make sure the refresh token's scope includes
   `https://www.googleapis.com/auth/gmail.readonly` (add it and
   re-consent once if it doesn't). Otherwise:
   - Create an OAuth client in Google Cloud Console (Desktop app type).
   - Run `pip install google-auth-oauthlib google-api-python-client` then
     `python get_refresh_token.py <CLIENT_ID> <CLIENT_SECRET>` locally,
     sign in with the Gmail account to track, and copy the printed refresh
     token. To switch accounts later, just repeat this with the other
     account and update the secret.
   - **Publish the OAuth consent screen** (Google Auth Platform → Audience →
     "Publish app", status "In production"). While it is in "Testing",
     Google expires refresh tokens after 7 days and the workflow fails with
     `invalid_grant`. A personal unverified app works fine for your own
     account; just click through the "unverified app" warning once.

3. **Add repo secrets** (Settings → Secrets and variables → Actions):
   - `GMAIL_CLIENT_ID`
   - `GMAIL_CLIENT_SECRET`
   - `GMAIL_REFRESH_TOKEN`
   - `GEMINI_API_KEY` (get one free at aistudio.google.com/apikey — no credit
     card required; this uses the genuinely free Gemini tier, not a trial
     credit that expires)

4. Commit and push. The workflow runs automatically at ~23:55 PT daily, or
   trigger it manually from the Actions tab ("Run workflow") to test it.

## How it works

- Searches Gmail for emails since the last run that look application-related:
  an application-ish subject, or one of the keywords below together with a
  job word (application, applied, applying, position, candidate, candidacy).
- Sends each candidate's subject/sender/full email body to an LLM (Google
  Gemini, free tier), with the keyword matches as hints. It classifies the
  email and extracts `company`, `role`, `job_id`, `skills_required`,
  `platform` and, for next-round emails, `next_step_details` (assessment,
  survey or scheduling link).
- Records it in `applications.csv`:

  | Email type | Example keywords | What happens |
  |---|---|---|
  | Confirmation | thank you, thank you for applying | New row, status `Applied` |
  | Rejection | unfortunately, large volume, unable, not moving forward | Matching row → `Rejected` |
  | Next step | assessment, survey, next round, interview | Matching row → `Next Round`, link saved |

  Rejection/next-step emails are matched to an existing row by company
  (preferring the same role, then the most recent application). If no
  application was logged for that company, a new row is added with that
  status. `status_updated` records the date of the latest status email.
- The keyword lists are at the top of `track_applications.py`
  (`CONFIRMATION_PHRASES`, `REJECTION_PHRASES`, `NEXT_STEP_PHRASES`) —
  edit them to tune what gets picked up.
- Dedupes by Gmail message ID via `state/last_run.json`.
- Job ID and skills are only filled in when the email actually contains them
  — the model is instructed not to guess, so these are often blank.
- After each run, `applications.csv` is re-sorted so entries
  are grouped by company (then by date), and `applications_by_company.md` is
  regenerated as a readable, grouped-by-company summary.
- Commits the updated CSV and summary back to the repo each run.

## Troubleshooting

- **Run fails with "Gmail OAuth refresh failed" / `invalid_grant`** — the
  refresh token expired or was revoked. Publish the consent screen (see
  above), generate a new token with `get_refresh_token.py`, and update the
  `GMAIL_REFRESH_TOKEN` secret.
- **Workflow shows "disabled"** — GitHub turns off scheduled workflows after
  60 days without repo activity. Re-enable it from the Actions tab
  (Track Job Applications → "Enable workflow").
- **Gemini rate limits** — the script retries with backoff. If Gemini is
  still unavailable, it stops early and keeps the previous time window, so
  remaining emails are picked up on the next run.

## Extending

- Point a Google Sheet or a small dashboard at the CSV via the raw GitHub URL
  if you want a live view instead of opening the file.
