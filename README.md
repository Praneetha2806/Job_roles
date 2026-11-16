# Job Application Tracker

Reads job-application emails from Gmail once a day and keeps track of every
application you have sent: confirmations, rejections, and next-round
invitations (assessments, surveys, interviews). It runs free on GitHub Actions
and the Gemini free tier, with no server to maintain.

**Live example:** https://praneetha2806.github.io/Job_roles/

Each run:

- updates `applications.csv` and `applications_by_company.md` on the repo's
  `data` branch,
- emails you a summary on days with updates, and one "Action needed"
  email per next round with the date it arrived and the deadline
  (optional), and
- refreshes your dashboard at `https://<your-username>.github.io/<repo-name>/`
  (once GitHub Pages is on).

## Use it for your own job search

About 30 minutes, once. You need a GitHub account and the Gmail account your
job emails go to.

1. **Copy the repo.** On this repo's GitHub page, click **Use this template →
   Create a new repository** (leave "Include all branches" unticked), or
   **Fork** (leave "Copy the main branch only" ticked). Choose a name and
   whether it is public or private (see Privacy below). Your copy has only
   the code; the original owner's applications live on their `data` branch
   and are not copied.
2. **Set up Google and the secrets:** follow [Setup](#setup) steps 1–5
   below.
3. **First run.** In your copy, go to **Actions**, enable workflows if asked,
   then **Track Job Applications → Run workflow**, set **start_date** to the
   first day to track (e.g. two weeks ago, as `YYYY-MM-DD`), and run it. It
   creates your own `data` branch.
4. **Turn on the dashboard:** see [Dashboard](#dashboard).
5. **Optional:** change the daily time for your time zone (see
   [Setup](#setup) step 6), and edit the keyword lists for the wording your
   emails use (see [How it works](#how-it-works)).

From then on it runs once a day by itself (see [Run it](#6-run-it) for
when).

> **Privacy:** in a public repo, anyone can open the `data` branch and the
> dashboard, so the companies and roles you applied to are visible. To keep
> them private, make the repo private (Settings → General → Danger Zone →
> Change visibility); the tracker works the same. GitHub Pages on a private
> repo needs a paid GitHub plan; without it, open `applications_by_company.md`
> on the `data` branch instead.

## What's in the repo

| File | What it is |
|---|---|
| `track_applications.py` | The tracker: searches Gmail, classifies emails with Gemini, updates the CSV, sends the summary email |
| `.github/workflows/track-applications.yml` | Runs the tracker once a day and commits the results to the `data` branch |
| `.github/workflows/publish-dashboard.yml` | Publishes the dashboard to GitHub Pages after every run |
| `index.html` | The dashboard |
| `get_refresh_token.py` | One-time helper you run on your own computer to create the Gmail token |
| `requirements.txt` | Python packages the workflow installs, capped below their next major version (see [Library versions](#library-versions)) |
| `LICENSE` | MIT license: anyone may use, copy and change this code |

The data lives on a separate **`data` branch**, so copies of the code start
empty and every run is a commit you can look back through:

| File on `data` | What it is |
|---|---|
| `applications.csv` | Every tracked application, one row each |
| `applications_by_company.md` | The same data grouped by company, readable on GitHub |
| `state/last_run.json` | Where the next run starts, and which emails were already processed |

## How it works

1. **Search Gmail.** The tracker looks at emails received since the last run.
   It runs a few small searches: one for subjects containing "application" or
   "applied", and one per keyword group below, each requiring a job word
   (application, applied, applying, position, candidate, candidacy) as well.
   It skips Gmail's Promotions and Social tabs and LinkedIn/Indeed job-alert
   digests, which are mostly noise.
2. **Classify each email with Gemini.** Each email's sender, subject and body
   are sent to Google Gemini (free tier: `gemini-flash-latest`, falling back
   to `gemini-flash-lite-latest` when it is busy), along with which keywords
   matched as hints. Gemini decides whether it is a confirmation, a rejection, a next
   step, or something else, and extracts the company, role, platform, job ID,
   skills and next-step details. Keywords alone are not enough, because
   "thank you" also appears in rejections. An email about several
   applications at once is recorded as several applications.
3. **Update the CSV.**

   | Email type | Keywords it looks for | What happens in `applications.csv` |
   |---|---|---|
   | Confirmation | thank you, thank you for applying, thanks for applying, we have received your application | New row with status `Applied` |
   | Rejection | unfortunately, large volume, unable, not moving forward, we have decided, other candidate(s) | That application's row changes to `Rejected` |
   | Next step | assessment, survey, next round, next step, interview, coding challenge, online test, HackerRank, CodeSignal | That application's row changes to `Next Round`, with the step type, due date and link |
   | Offer | offer letter, pleased to offer, job offer | That application's row changes to `Offer` |

   A rejection or next-step email is matched to an existing row at the same
   company with the same role (titles match when one contains the other or
   they share most words, so "Sr Data Analyst" matches "Senior Data
   Analyst"). If the email names no role, the most recent application at
   that company is used. If no matching application was logged, a new row
   is added with that status. Emails are handled oldest first, so a confirmation is logged
   before a later rejection for the same job. A second confirmation for the
   same company and role within 3 days (for example LinkedIn's "application
   sent" plus the company's own email) only fills in missing details instead
   of adding a duplicate row.
4. **Save and commit.** Progress is saved after every email. The workflow
   commits the CSV, summary and state to the `data` branch even if a run
   fails, is cancelled or times out, so finished work is never lost. A run
   stops taking new emails after 40 minutes and leaves the rest for the next
   run. An email that can't be processed is skipped with a warning instead of
   stopping the run.
5. **Email you** (optional, only on days with updates):
   - a summary of new applications, next rounds with their links,
     rejections, and overall totals, and
   - one **"Action needed: next round at <company>"** email per next round,
     and
   - an **"Offer from <company>"** email for each offer.

   The next-round email starts with the two dates that matter, then the
   details:

   ```
   Subject: Action needed: next round at Nimbus - Data Analyst (due Oct 31, 2026)

   Email received: Oct 27, 2026
   Deadline: Oct 31, 2026      ("Interview date" for interviews; "not stated in the email" when unknown)

   What to do: Complete CodeSignal assessment
   Type: Assessment
   Link: https://...
   Applied: Oct 27, 2026 via LinkedIn
   ```

   It is sent **once per step**. Another one is sent only when something new
   happens: the step type changes (e.g. assessment to interview), the link
   points to a different site, an interview is scheduled or moved, or a
   deadline changes. Follow-up and reminder emails about a step you were
   already told about only fill in missing details (such as a deadline), and
   "thanks for completing your assessment" emails are ignored. The daily
   summary lists each next round the same way, with the date received and
   the deadline.

### When a company replies to an earlier application

Nothing to do on your side. The next daily run finds the reply, Gemini
reads it, and the tracker finds that application's existing row in
`applications.csv` (same company, preferring the same role) and changes its
status, instead of adding a new row. The run commits the updated CSV to the
`data` branch, and the dashboard is republished a minute or two later. Replies
are picked up at the next daily run, or sooner if you run the workflow by
hand.

To change the keywords, edit `CONFIRMATION_PHRASES`, `REJECTION_PHRASES`,
`NEXT_STEP_PHRASES` and `OFFER_PHRASES` near the top of `track_applications.py`.

### `applications.csv` columns

| Column | Meaning |
|---|---|
| `company`, `role` | From the email (role may be blank if the email doesn't say) |
| `job_id`, `skills_required` | Only filled in when the email contains them; never guessed |
| `date_applied` | Date of the confirmation email (or of the first status email, if no confirmation was found) |
| `platform` | Where you applied, e.g. LinkedIn, Workday, Greenhouse |
| `status` | `Applied`, `Next Round`, `Offer` or `Rejected` |
| `email_subject`, `gmail_message_id` | The email the row was created from |
| `status_updated` | Date of the latest rejection or next-step email |
| `next_step_details` | What to do next and the link, for next rounds |
| `next_step_type` | `Assessment`, `Survey`, `Interview` or `Other` |
| `next_step_due` | Deadline to complete the step, or the interview date (`YYYY-MM-DD`), when the email gives one |
| `first_response_date` | Date of the first reply of any kind, used for reply-time measures |
| `work_mode` | `Remote`, `Hybrid` or `On-site`, when mentioned |
| `location` | Job location, when mentioned |
| `employment_type` | `Full-time`, `Part-time`, `Contract`, `Internship` or `Temporary`, when mentioned |
| `salary` | Pay or range as written in the email, when mentioned |

The last seven columns were added later, so applications logged before then
have them blank; they fill in for new emails.

## Dashboard

`index.html` shows, top to bottom:

- **Totals:** applications, reply rate, next rounds, offers, rejections, and
  applications with no reply after 2 weeks.
- **Needs your attention:** open next rounds and offers, soonest deadline
  first, with the step type, due date (highlighted when 3 days or less) and
  the link.
- **How your search is going:**
  - a funnel from applied to reply, next round and offer, with the median
    days to a reply
  - applications still waiting, by how long ago you applied (0–7, 8–14,
    15–30, 30+ days)
  - applications sent and replies received per week
  - applications per day as a weekday × week matrix
- **What gets replies:** reply rate and next rounds by platform and by role
  type (role titles grouped by keywords such as "Data Engineer" or
  "Analyst"), plus work mode and employment type when emails mention them.
- **All applications:** by company (sortable and searchable), and every
  rejection with days from applying.

The **Publish dashboard** workflow republishes it after every tracker run,
with the latest `applications.csv` from the `data` branch. To turn it on:
Settings → Pages → Build and deployment → Source **GitHub Actions**. Then
**Actions → Publish dashboard → Run workflow** once (later runs are
automatic). After a minute or two it is at
`https://<your-username>.github.io/<repo-name>/`. Until Pages is on, the
workflow skips publishing and leaves a note on its run.

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

The workflow is scheduled once a day at **06:55 UTC** (10:55 PM Pacific in
winter, 11:55 PM in summer), set by the `cron` line in
`.github/workflows/track-applications.yml`. Cron times are in UTC; for
example `"30 18 * * *"` is midnight in India.

GitHub starts scheduled runs **late when its servers are busy**, and this
one has usually started 7–9 hours late (around 6–7:30 AM Pacific). That is
fine for a daily tracker: each run picks up everything since the last one,
and starting after midnight Pacific means Gemini's daily limit has just
reset. If you need it sooner, run it by hand: **Actions → Track Job
Applications → Run workflow**. The first time, set **start_date** to the
first day you want tracked; otherwise it looks back 7 days.

## Day-to-day

- **See what a run did:** open the run in the Actions tab. The messages at
  the top of the run page say what happened:

  | Message | Meaning |
  |---|---|
  | Mailbox | The Gmail account it read |
  | Summary | How many emails it checked and how many updates it recorded |
  | Email sent / Next-round reminders | The summary email, and how many next-round or offer emails went out |
  | Gemini unavailable | Gemini was busy or out of daily quota; the rest is retried next run |
  | Time limit reached | It stopped after 40 minutes; the next run continues |
  | Skipped emails | Emails it couldn't process, with the reason; the rest were recorded |
  | Gmail sign-in failed / Missing secrets / Tracker crashed | The run stopped; see Troubleshooting |
- **Re-scan from an earlier date:** **Run workflow** with **start_date** set
  (e.g. `2026-10-01`). Emails already processed are skipped, so nothing is
  counted twice.
- **Start over:** **Run workflow** with **reset** set to `RESET`. This deletes
  every tracked application; the old data stays in the `data` branch's
  history.
- **See how your data changed:** the `data` branch's commit history has one
  commit per run (`Log applications for <date>`), so you can see what changed
  on any day.
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
| Run fails with only "Process completed with exit code 1" and no other message | The tracker couldn't start, usually because a Python library changed. This happened when google-genai 3.0 came out; `requirements.txt` now caps versions to prevent it. Open the run's **Run application tracker** step log to see the error. |
| Warning "Gemini unavailable" mentioning `404 NOT_FOUND` | Google retired or renamed a model. Change `GEMINI_MODELS` near the top of `track_applications.py` to current model names. |
| Workflow shows "disabled" | GitHub turns off scheduled workflows after 60 days without repo activity. **Actions → Track Job Applications → Enable workflow**. |
| Run fails with "start date … is not a date like 2026-10-01" | Type the start date as `YYYY-MM-DD`. |
| The dashboard or CSV shows someone else's applications | Your copy included the original `data` branch. Run the workflow once with **reset** set to `RESET`. |
| Dashboard shows "Couldn't load your applications" or the Pages address shows 404 | Check that Settings → Pages → Source is **GitHub Actions**, then run **Actions → Publish dashboard → Run workflow**. |

## Library versions

`requirements.txt` allows newer minor versions but not the next major
version of each library (for example `google-genai>=2.33.0,<4`), so a
breaking release can't stop the daily run without warning. To move to a new
major version, raise the cap, run the workflow by hand, and check the run
page for errors.

## Ideas for later

- Add filters to the dashboard (date range, platform, role type).
- Re-read older emails to fill in the newer columns for past applications.
- A weekly digest of open next rounds whose due date is near.
- A way to mark a next round as done, so the dashboard's "to do" list only
  shows steps you haven't finished.

## License

[MIT](LICENSE): free to use, copy, change and share, with no warranty.
