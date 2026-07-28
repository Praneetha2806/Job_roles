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
   - Run a one-time local script to authorize and print a refresh token
     (happy to write that helper script too if you need it).

3. **Add repo secrets** (Settings → Secrets and variables → Actions):
   - `GMAIL_CLIENT_ID`
   - `GMAIL_CLIENT_SECRET`
   - `GMAIL_REFRESH_TOKEN`
   - `ANTHROPIC_API_KEY`

4. Commit and push. The workflow runs automatically at ~23:55 PT daily, or
   trigger it manually from the Actions tab ("Run workflow") to test it.

## How it works

- Searches Gmail for emails since the last run whose subject looks
  application-related (broad net).
- Sends each candidate's subject/sender/full email body to Claude, which
  decides if it's a real submission confirmation (filters out rejections,
  interview invites, job-alert digests) and extracts `company`, `role`,
  `job_id`, `skills_required`, `platform`.
- Appends new rows to `applications.csv`, dedupes by Gmail message ID via
  `state/last_run.json`.
- Job ID and skills are only filled in when the email actually contains them
  — Claude is instructed not to guess, so these are often blank.
- After each run with new rows, `applications.csv` is re-sorted so entries
  are grouped by company (then by date), and `applications_by_company.md` is
  regenerated as a readable, grouped-by-company summary.
- Commits the updated CSV and summary back to the repo each run.

## Extending

- Add a `status` update step later (e.g. detect rejection/interview emails
  and update the matching row) if you want the CSV to track full lifecycle,
  not just submissions.
- Point a Google Sheet or a small dashboard at the CSV via the raw GitHub URL
  if you want a live view instead of opening the file.
