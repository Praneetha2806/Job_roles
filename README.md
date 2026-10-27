# Job Application Tracker

Reads job-application emails from Gmail every night and keeps track of every
application you have sent: confirmations, rejections, and next-round
invitations (assessments, surveys, interviews). It runs free on GitHub Actions
and the Gemini free tier, with no server to maintain.

**Live example:** https://praneetha2806.github.io/Job_roles/

Each run:

- updates `applications.csv` and `applications_by_company.md` in this repo,
- emails you a summary on days with updates, and a separate "Action
  needed" reminder for each application that moves to a next round
  (optional), and
- refreshes your dashboard at `https://<your-username>.github.io/<repo-name>/`
  (once GitHub Pages is on).

## Use it for your own job search

About 30 minutes, once. You need a GitHub account and the Gmail account your
job emails go to.

1. **Copy the repo.** On this repo's GitHub page, click **Use this template →
   Create a new repository** (or **Fork**). Choose a name and whether it is
   public or private (see Privacy below).
2. **Set up Google and the secrets:** follow [Setup](#setup) steps 1–5
   below.
3. **Start fresh.** In your copy, go to **Actions**, enable workflows if asked,
   then **Track Job Applications → Run workflow** and fill in:
   - **reset:** `RESET` (removes the example applications that came with the
     copy)
   - **start_date:** the first day to track, e.g. two weeks ago, as
     `YYYY-MM-DD`
4. **Turn on the dashboard:** see [Dashboard](#dashboard).
5. **Optional:** change the nightly time for your time zone (see
   [Setup](#setup) step 6), and edit the keyword lists for the wording your
   emails use (see [How it works](#how-it-works)).

From then on it runs every night by itself.

> **Privacy:** in a public repo, anyone can see `applications.csv` and the
> dashboard, so the companies and roles you applied to are visible. To keep
> them private, make the repo private (Settings → General → Danger Zone →
> Change visibility); the tracker works the same. GitHub Pages on a private
> repo needs a paid GitHub plan; without it, open `applications_by_company.md`
> on GitHub instead.

## What's in the repo

| File | What it is |
|---|---|
| `track_applications.py` | The tracker: searches Gmail, classifies emails with Gemini, updates the CSV, sends the summary email |
| `.github/workflows/track-applications.yml` | Runs the tracker nightly and commits the results |
| `applications.csv` | Every tracked application, one row each |
| `applications_by_company.md` | The same data grouped by company, readable on GitHub |
| `index.html` | The dashboard, published with GitHub Pages |
| `state/last_run.json` | Where the next run starts, and which emails were already processed |
| `get_refresh_token.py` | One-time helper you run on your own computer to create the Gmail token |
| `requirements.txt` | Python packages the workflow installs |

## How it works

1. **Search Gmail.** The tracker looks at emails received since the last run.
   It runs a few small searches: one for subjects containing "application" or
   "applied", and one per keyword group below, each requiring a job word
   (application, applied, applying, position, candidate, candidacy) as well.
   It skips Gmail's Promotions and Social tabs and LinkedIn/Indeed job-alert
   digests, which are mostly noise.
2. **Classify each email with Gemini.** Each email's sender, subject and body
   are sent to Google Gemini (free tier), along with which keywords matched
   as hints. Gemini decides whether it is a confirmation, a rejection, a next
   step, or something else, and extracts the company, role, platform, job ID,
   skills and next-step details. Keywords alone are not enough, because
   "thank you" also appears in rejections.
3. **Update the CSV.**

   | Email type | Keywords it looks for | What happens in `applications.csv` |
   |---|---|---|
   | Confirmation | thank you, thank you for applying, thanks for applying, we have received your application | New row with status `Applied` |
   | Rejection | unfortunately, large volume, unable, not moving forward, we have decided, other candidate(s) | That application's row changes to `Rejected` |
   | Next step | assessment, survey, next round, next step, interview, coding challenge, online test, HackerRank, CodeSignal | That application's row changes to `Next Round`, and the link is saved |

   A rejection or next-step email is matched to an existing row by company,
   preferring the same role, then the most recent application. If no
   application was logged for that company, a new row is added with that
   status. Emails are handled oldest first, so a confirmation is logged
   before a later rejection for the same job.
4. **Save and commit.** Progress is saved after every email. The workflow
   commits the CSV, summary and state back to the repo, even if a run fails
   partway, so finished work is never lost.
5. **Email you** (optional, only on days with updates):
   - a summary of new applications, next rounds with their links,
     rejections, and overall totals, and
   - a separate **"Action needed: next round at <company>"** reminder for each
     application that moved to a next round, with what to do, the link, and
     the dates.

### When a company replies to an earlier application

Nothing to do on your side. The next nightly run finds the reply, Gemini
reads it, and the tracker finds that application's existing row in
`applications.csv` (same company, preferring the same role) and changes its
status, instead of adding a new row. The run commits the updated CSV, and
the dashboard shows the change the next time it is opened, after GitHub Pages
redeploys (a minute or two). Replies are picked up at the nightly run, or
sooner if you run the workflow by hand.

To change the keywords, edit `CONFIRMATION_PHRASES`, `REJECTION_PHRASES` and
`NEXT_STEP_PHRASES` near the top of `track_applications.py`.

### `applications.csv` columns

| Column | Meaning |
|---|---|
| `company`, `role` | From the email (role may be blank if the email doesn't say) |
| `job_id`, `skills_required` | Only filled in when the email contains them; never guessed |
| `date_applied` | Date of the confirmation email (or of the first status email, if no confirmation was found) |
| `platform` | Where you applied, e.g. LinkedIn, Workday, Greenhouse |
| `status` | `Applied`, `Next Round` or `Rejected` |
| `email_subject`, `gmail_message_id` | The email the row was created from |
| `status_updated` | Date of the latest rejection or next-step email |
| `next_step_details` | What to do next and the link, for next rounds |

## Dashboard

`index.html` shows:

- totals: applications, waiting to hear, next rounds, rejections, response rate
- applications per day as a weekday × week matrix, with weekly totals
- applications by company (sortable and searchable)
- rejections so far, with days from applying to the rejection
- next rounds, with the link from each email

It reads `applications.csv` each time it is opened, so it is up to date
after every run. To publish it: Settings → Pages → Build and deployment →
Source **Deploy from a branch**, Branch **main**, folder **/ (root)** →
Save. After a minute or two it is at
`https://<your-username>.github.io/<repo-name>/`.

## Setup

Needed once for a new copy, or to move to a different Gmail account. Your
secrets stay in your own GitHub repo and Google account; nothing is shared
with the repo you copied from.

### 1. Google Cloud project (for Gmail access)

Signed in as the Gmail account to track, at https://console.cloud.google.com:

1. Create a project (for example `job-tracker`) and select it.
2. **APIs & Services → Library** → search **Gmail API** → **Enable**.
3. **Google Auth Platform → Branding:** app name `Job Tracker`, your email as
   the support and developer contact, and:
   - Application home page: `https://github.com/<you>/<repo>`
   - Application privacy policy link: `https://github.com/<you>/<repo>/blob/main/README.md`
   - Authorized domains: `github.com`
   - No logo (a logo forces a Google review).
4. **Google Auth Platform → Audience:** user type **External**, then
   **Publish app** so the status is **In production**. While it says
   *Testing*, Google expires the token every 7 days and the workflow fails.
5. **Google Auth Platform → Clients → Create client:** type **Desktop app**.
   Copy the **Client ID** and **Client secret**.

### 2. Gmail token

On your own computer:

```
pip install google-auth-oauthlib google-api-python-client
python get_refresh_token.py <CLIENT_ID> <CLIENT_SECRET>
```

Choose the Gmail account to track. At "Google hasn't verified this app",
click **Advanced → Go to Job Tracker (unsafe)** (it's your own app), then
**Continue**. The script prints `Authorized mailbox: …` and the refresh
token. To switch to another Gmail account later, run this again with that
account and update the `GMAIL_REFRESH_TOKEN` secret.

### 3. Gemini API key

At https://aistudio.google.com/apikey → **Create API key**. The free tier
needs no credit card.

### 4. Email summary (optional)

1. Turn on **2-Step Verification**, with a phone number, for the tracked
   Gmail account (https://myaccount.google.com/signinoptions/twosv).
2. Create an **App Password** named `Job Tracker` at
   https://myaccount.google.com/apppasswords and copy the 16 characters.

The summary and next-round reminders are sent from the tracked Gmail account
to `NOTIFY_EMAIL`. Leave these secrets out to turn both off.

### 5. Repo secrets

Settings → Secrets and variables → Actions → **New repository secret**:

| Secret | Value | Required |
|---|---|---|
| `GMAIL_CLIENT_ID` | Client ID from step 1 | Yes |
| `GMAIL_CLIENT_SECRET` | Client secret from step 1 | Yes |
| `GMAIL_REFRESH_TOKEN` | Token from step 2 | Yes |
| `GEMINI_API_KEY` | Key from step 3 | Yes |
| `GMAIL_APP_PASSWORD` | App Password from step 4 | For emails |
| `NOTIFY_EMAIL` | Address that should receive the summary and reminders | For emails |

### 6. Run it

The workflow runs every night at 06:55 UTC: 11:55 PM Pacific during daylight
saving time, 10:55 PM in winter. For another time zone, change the `cron`
line in `.github/workflows/track-applications.yml`; it is in UTC (for
example `"30 18 * * *"` is midnight in India). To run it now: **Actions →
Track Job Applications → Run workflow**. The first time, set **start_date**
to the first day you want tracked; otherwise it looks back 7 days.

## Day-to-day

- **See what a run did:** open the run in the Actions tab. The summary at the
  top shows the mailbox it read, how many emails it checked and recorded,
  whether the email was sent, and any warning or crash.
- **Re-scan from an earlier date:** **Run workflow** with **start_date** set
  (e.g. `2026-10-01`). Emails already processed are skipped, so nothing is
  counted twice.
- **Start over:** **Run workflow** with **reset** set to `RESET`. This deletes
  every tracked application; the old data stays in the repo's git history.
- **Gemini's free tier allows about 500 requests a day.** A normal day is well
  under that. When catching up on a long period, a run may stop at the limit;
  the next run continues where it left off.

## Troubleshooting

| What you see | Cause and fix |
|---|---|
| Run fails with "Gmail OAuth refresh failed" / `invalid_grant` | The token expired or was revoked. Make sure the app is **In production** (Setup 1.4), create a new token (Setup 2) and update `GMAIL_REFRESH_TOKEN`. |
| `invalid_client` / "The OAuth client was not found" when creating the token | The Client ID is wrong or incomplete (it ends in `.apps.googleusercontent.com`), the ID and secret are swapped, or the client was just created (wait 5 minutes). |
| "Access blocked: … has not completed the Google verification process" (`access_denied`) | The app is still in *Testing*. Publish it (Setup 1.4). If **Publish app** is greyed out, fill in the home page and privacy policy links on the Branding page first (Setup 1.3). |
| "The setting you are looking for is not available" on the App passwords page | 2-Step Verification is off, or uses only a passkey or security key. Add a phone number as a second step (Setup 4.1). |
| Warning "Gemini unavailable" on a run | Gemini was busy or the daily limit was reached. Nothing is lost; the next run retries those emails. |
| Warning "Email not sent" or "Reminder not sent" | Check the `GMAIL_APP_PASSWORD` and `NOTIFY_EMAIL` secrets. A new App Password is needed if 2-Step Verification was turned off and on again. |
| Workflow shows "disabled" | GitHub turns off scheduled workflows after 60 days without repo activity. **Actions → Track Job Applications → Enable workflow**. |
| Run fails with "start date … is not a date like 2026-10-01" | Type the start date as `YYYY-MM-DD`. |
| The dashboard or CSV shows someone else's applications | Your copy still has the example data. Run the workflow once with **reset** set to `RESET`. |
| Dashboard shows "Couldn't load your applications" | GitHub Pages is off or still deploying. Check Settings → Pages, wait a minute, and refresh. |

## Ideas for later

- Merge duplicate rows when both LinkedIn and the company's own system confirm
  the same application.
- Add an "Interview" status separate from assessments and surveys.
- Add filters to the dashboard (date range, platform).
