"""
track_applications.py

End-of-day job application tracker.

What it does:
1. Searches Gmail for likely application-related emails (confirmations,
   rejections, next-round/assessment invites) received since the last
   successful run (tracked in state/last_run.json).
2. Sends each candidate email's subject + sender + body to Gemini, which
   classifies it and extracts structured fields (company, role, platform).
3. Confirmations are appended as new "Applied" rows; rejections and
   next-round emails update the status of the matching existing row (or add
   a new row if the original application was never logged).
4. Updates the "last run" timestamp and the set of processed Gmail message IDs.
5. On days with updates, emails a summary to NOTIFY_EMAIL (optional), plus a
   separate reminder for each application that moved to a next round.

Environment variables required (set as GitHub Actions secrets):
- GMAIL_CLIENT_ID
- GMAIL_CLIENT_SECRET
- GMAIL_REFRESH_TOKEN        (reuse the same OAuth app you already use for
                               sending Gmail notifications in the job-application-agent)
- GEMINI_API_KEY
- START_DATE (optional)        YYYY-MM-DD; scan emails from this date instead
                               of the last run (set from the Run workflow form)
- RESET_DATA (optional)        "RESET" clears all tracked applications first
- GMAIL_APP_PASSWORD, NOTIFY_EMAIL (optional) -- send the daily summary from
                               the tracked mailbox to NOTIFY_EMAIL

Files this script reads/writes, in TRACKER_DATA_DIR (default: next to this
script; the workflow uses a checkout of the `data` branch):
- applications.csv           the running log
- applications_by_company.md the same, grouped by company
- state/last_run.json        {"last_run_utc": "...", "processed_ids": [...]}
"""

import os
import re
import json
import csv
import base64
import smtplib
import sys
import time
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
# The workflow checks out the `data` branch into its own folder and points
# TRACKER_DATA_DIR at it, so the data never lands on the code branch.
DATA_DIR = Path(os.environ.get("TRACKER_DATA_DIR") or REPO_ROOT).resolve()
CSV_PATH = DATA_DIR / "applications.csv"
STATE_PATH = DATA_DIR / "state" / "last_run.json"
SUMMARY_PATH = DATA_DIR / "applications_by_company.md"

CSV_HEADERS = [
    "company",
    "role",
    "job_id",
    "skills_required",
    "date_applied",
    "platform",
    "status",
    "email_subject",
    "gmail_message_id",
    "status_updated",
    "next_step_details",
    # Added later; older rows have them blank.
    "next_step_type",
    "next_step_due",
    "first_response_date",
    "work_mode",
    "location",
    "employment_type",
    "salary",
]

STATUS_APPLIED = "Applied"
STATUS_REJECTED = "Rejected"
STATUS_NEXT_ROUND = "Next Round"
STATUS_OFFER = "Offer"

# Details about the job that any email may mention; filled in when a row
# doesn't have them yet.
JOB_DETAIL_FIELDS = (
    "role", "job_id", "skills_required", "platform",
    "work_mode", "location", "employment_type", "salary",
)
# Two confirmations for the same company and role this close together are one
# application (e.g. LinkedIn's "application sent" plus the company's own email).
DUPLICATE_WINDOW_DAYS = 3

# Keywords that decide which emails are recorded. Matching is case-insensitive
# and looks at the subject and body. Edit these lists to tune what gets picked up.
CONFIRMATION_PHRASES = [
    "thank you",
    "thank you for applying",
    "thanks for applying",
    "your application has been received",
    "we have received your application",
    "we've received your application",
]

REJECTION_PHRASES = [
    "unfortunately",
    "large volume",
    "unable",
    "not moving forward",
    "not be moving forward",
    "we have decided",
    "other candidate",
    "other candidates",
    "move forward with other candidates",
    "decided to pursue other candidates",
]

NEXT_STEP_PHRASES = [
    "assessment",
    "survey",
    "next round",
    "next step",
    "coding challenge",
    "online test",
    "hackerrank",
    "codesignal",
    "interview",
]

OFFER_PHRASES = [
    "offer letter",
    "pleased to offer",
    "happy to offer",
    "excited to offer",
    "job offer",
]

# Words that tie an email to a job application, so generic emails that just
# say "thank you" or "unfortunately" aren't sent to the LLM.
JOB_CONTEXT_TERMS = [
    "application", "applied", "applying", "position", "candidacy", "candidate",
]


def _gmail_or(terms: list[str]) -> str:
    # Gmail matches phrases anywhere, so drop a phrase that already contains a
    # shorter one in the list ("thank you for applying" is covered by "thank you").
    # Fewer terms keeps each search under Gmail's per-minute query-cost quota.
    # Whole-word check, since Gmail doesn't treat "candidate" as matching "candidates".
    terms = [
        t for t in terms
        if not any(o != t and re.search(rf"\b{re.escape(o)}\b", t) for o in terms)
    ]
    return "(" + " OR ".join(f'"{t}"' for t in terms) + ")"


# Gmail rejects one big search with every keyword ("Quota exceeded for quota
# metric 'Total Query Cost'"), so run a small search per group and merge the
# results. Bare terms (no "subject:" prefix) also match the body; each keyword
# must appear with a job word. The LLM does the precise classification afterward.
GMAIL_QUERIES = [
    '(subject:"application" OR subject:"applied")',
] + [
    f"{_gmail_or(phrases)} {_gmail_or(JOB_CONTEXT_TERMS)}"
    for phrases in (CONFIRMATION_PHRASES, REJECTION_PHRASES, NEXT_STEP_PHRASES, OFFER_PHRASES)
]

# Applied to every search. Confirmations/rejections land in Primary or Updates;
# Promotions/Social and job-alert digests are mostly noise that would use up
# the Gemini free-tier quota. (LinkedIn's "application sent" emails come from
# jobs-noreply@, not jobalerts-noreply@, so they are kept.)
GMAIL_EXCLUDE = (
    "-category:promotions -category:social "
    "-from:jobalerts-noreply@linkedin.com -from:alert@indeed.com"
)

# Tried in order; when the first is overloaded (503) or rate-limited, the
# lighter model usually still has capacity.
GEMINI_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]

EXTRACTION_PROMPT = """You are helping track the status of job applications from emails \
(e.g. from LinkedIn, Indeed, Greenhouse, Lever, Workday, iCIMS, SmartRecruiters, Ashby, \
a company's own careers page, etc.).

Email metadata:
Date received: {email_date}
From: {sender}
Subject: {subject}
Body: {snippet}

Keyword matches found by a simple text scan (hints only; use your judgment):
- confirmation keywords: {confirmation_hits}
- rejection keywords: {rejection_hits}
- next-step keywords: {next_step_hits}
- offer keywords: {offer_hits}

Classify the email as exactly one email_type:
- "application_confirmation": confirms a job application was submitted/received \
(typically "Thank you for applying", "We have received your application").
- "rejection": the company is not moving forward with the candidate (typically \
"Unfortunately...", "due to the large volume of applications", "we are unable to...", \
"not moving forward", "decided to pursue other candidates").
- "next_step": the candidate is being taken forward, e.g. an online assessment, \
coding challenge, survey/questionnaire to complete, or an interview invitation/scheduling.
- "offer": the company is offering the candidate the job.
- "other": anything else (newsletters, job-alert digests, recommendations, marketing, \
account/security emails, or emails not about a specific application by the candidate).

Keywords are only hints: "thank you" also appears in rejections, and "unable" or \
"survey" can appear in unrelated emails. Decide based on the overall meaning.

Extract the company and role the email is about. For "next_step", put the action \
required and any assessment/survey/scheduling link in next_step_details (copy links \
exactly as they appear). Some emails include a requisition/job ID (e.g. "Job ID: 12345", \
"Req #R-00123") and/or required skills; extract them if present, otherwise use null. \
Also extract, only when the email states them:
- work_mode: "Remote", "Hybrid" or "On-site".
- location: city and state/country of the job.
- employment_type: "Full-time", "Part-time", "Contract", "Internship" or "Temporary".
- salary: the pay or range exactly as written (e.g. "$85,000-$95,000/yr").
- next_step_type (only for "next_step"): "Assessment" (test, coding challenge, \
recorded/video screening), "Survey" (questionnaire, form, work-authorization or EEO \
questions), "Interview" (call or interview to attend or schedule), or "Other".
- next_step_due (only for "next_step"): the deadline to complete it, or the date of a \
scheduled interview, as YYYY-MM-DD. Work out relative deadlines ("within 4 days", \
"by Friday") from the date received.

Do not guess or invent values that are not actually present in the text.

Respond with ONLY a JSON object, no other text, in this exact shape:
{{
  "email_type": "application_confirmation" | "rejection" | "next_step" | "offer" | "other",
  "company": "string or null",
  "role": "string or null",
  "platform": "string or null (e.g. LinkedIn, Indeed, Greenhouse, Workday, Company Careers Page)",
  "job_id": "string or null",
  "skills_required": "comma-separated string or null",
  "next_step_details": "string or null",
  "next_step_type": "Assessment" | "Survey" | "Interview" | "Other" | null,
  "next_step_due": "YYYY-MM-DD or null",
  "work_mode": "Remote" | "Hybrid" | "On-site" | null,
  "location": "string or null",
  "employment_type": "Full-time" | "Part-time" | "Contract" | "Internship" | "Temporary" | null,
  "salary": "string or null"
}}
"""


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

REQUIRED_ENV_VARS = [
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
    "GEMINI_API_KEY",
]


def check_env():
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        sys.exit(
            f"ERROR: missing required environment variable(s): {', '.join(missing)}. "
            "Add them under Settings -> Secrets and variables -> Actions."
        )


def get_gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    try:
        creds.refresh(Request())
    except RefreshError as e:
        sys.exit(
            f"ERROR: Gmail OAuth refresh failed: {e}\n"
            "Check the GMAIL_* secrets. Most often (invalid_grant) the refresh "
            "token has expired or been revoked: if your "
            "Google Cloud OAuth consent screen is in 'Testing' mode, refresh tokens "
            "expire after 7 days -- set it to 'In production' (Google Auth Platform "
            "-> Audience -> Publish app), then run get_refresh_token.py locally and "
            "update the GMAIL_REFRESH_TOKEN secret."
        )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _execute_with_retry(request, attempts: int = 4):
    """Execute a Gmail API request, backing off on rate-limit errors."""
    for attempt in range(attempts):
        try:
            # num_retries covers 5xx responses and dropped connections.
            return request.execute(num_retries=3)
        except HttpError as e:
            rate_limited = e.resp.status == 429 or (
                e.resp.status == 403 and "rateLimitExceeded" in str(e)
            )
            if not rate_limited or attempt == attempts - 1:
                raise
            wait = 30 * (attempt + 1)
            print(f"[debug] Gmail rate limit; retrying in {wait}s")
            time.sleep(wait)


def search_candidate_messages(service, since_utc: datetime):
    # Gmail search only supports day granularity for "after:", so we filter
    # more precisely afterward using the message's internalDate.
    after_str = since_utc.strftime("%Y/%m/%d")

    message_ids = []
    for terms in GMAIL_QUERIES:
        query = f"{terms} {GMAIL_EXCLUDE} after:{after_str}"
        request = service.users().messages().list(userId="me", q=query, maxResults=100)
        while request is not None:
            response = _execute_with_retry(request)
            message_ids.extend(m["id"] for m in response.get("messages", []))
            request = service.users().messages().list_next(request, response)
    # The searches overlap; keep the first occurrence of each ID.
    return list(dict.fromkeys(message_ids))


def _extract_plain_text(payload: dict) -> str:
    """Walk a Gmail message payload and return the best plain-text body found."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")

    for part in payload.get("parts", []) or []:
        text = _extract_plain_text(part)
        if text:
            return text
    return ""


def fetch_message_summary(service, msg_id: str):
    msg = _execute_with_retry(
        service.users().messages().get(userId="me", id=msg_id, format="full")
    )
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    internal_date_ms = int(msg["internalDate"])

    body_text = _extract_plain_text(msg["payload"]) or msg.get("snippet", "")
    # Cap length so we don't blow up prompt size on huge HTML-derived emails.
    body_text = body_text[:4000]

    return {
        "id": msg_id,
        "sender": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "snippet": body_text,
        "internal_date": datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc),
    }


# ---------------------------------------------------------------------------
# Gemini extraction
# ---------------------------------------------------------------------------

def keyword_hits(email: dict) -> dict[str, list[str]]:
    """Code-level scan for the configured keywords. Passed to the LLM as hints
    and logged for debugging; the LLM makes the final call since keywords like
    "thank you" also show up in rejections."""
    haystack = f"{email['subject']} {email['snippet']}".lower()
    return {
        "confirmation": [p for p in CONFIRMATION_PHRASES if p in haystack],
        "rejection": [p for p in REJECTION_PHRASES if p in haystack],
        "next_step": [p for p in NEXT_STEP_PHRASES if p in haystack],
        "offer": [p for p in OFFER_PHRASES if p in haystack],
    }


EMAIL_TYPE_TO_STATUS = {
    "application_confirmation": STATUS_APPLIED,
    "rejection": STATUS_REJECTED,
    "next_step": STATUS_NEXT_ROUND,
    "offer": STATUS_OFFER,
}


def gh_annotation(level: str, title: str, message: str):
    """Show a message on the GitHub Actions run page (no-op formatting outside CI)."""
    message = message.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::{level} title={title}::{message}")


class LLMUnavailableError(Exception):
    """Gemini could not be reached; the email should be retried next run."""


def generate_with_retry(client: "genai.Client", prompt: str, attempts: int = 4):
    """Call Gemini, falling back across GEMINI_MODELS and backing off on
    rate-limit / overload errors (common on the free tier)."""
    for attempt in range(attempts):
        for model in GEMINI_MODELS:
            try:
                return client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        max_output_tokens=2000,
                        response_mime_type="application/json",
                    ),
                )
            except genai_errors.ClientError as e:
                # 429: rate-limited; 404: model alias not available to this key.
                if e.code not in (404, 429):
                    raise
                last_error = e
            except (genai_errors.ServerError, httpx.HTTPError) as e:
                # httpx.HTTPError covers dropped connections mid-request.
                last_error = e
            print(f"  [debug] Gemini {model} error ({last_error})")
        if attempt == attempts - 1:
            break
        wait = 15 * (attempt + 1)
        print(f"  [debug] all Gemini models failed; retrying in {wait}s")
        time.sleep(wait)
    raise LLMUnavailableError(str(last_error))


def extract_with_llm(client: "genai.Client", email: dict) -> dict | None:
    hits = keyword_hits(email)
    prompt = EXTRACTION_PROMPT.format(
        email_date=email["internal_date"].strftime("%Y-%m-%d (%A)"),
        sender=email["sender"],
        subject=email["subject"],
        snippet=email["snippet"],
        confirmation_hits=", ".join(hits["confirmation"]) or "none",
        rejection_hits=", ".join(hits["rejection"]) or "none",
        next_step_hits=", ".join(hits["next_step"]) or "none",
        offer_hits=", ".join(hits["offer"]) or "none",
    )
    response = generate_with_retry(client, prompt)
    text = (response.text or "").strip()
    print(f"  [debug] subject={email['subject']!r}")
    print(f"  [debug] keyword_hits={hits}")
    print(f"  [debug] gemini raw response: {text!r}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  [debug] JSON parse failed: {e}")
        return None

    if data.get("email_type") not in EMAIL_TYPE_TO_STATUS:
        print(f"  [debug] Gemini classified this as {data.get('email_type')!r}; ignoring")
        return None

    return data


# ---------------------------------------------------------------------------
# State + CSV
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_run_utc": None, "processed_ids": []}


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def load_rows() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(newline="") as f:
        # Older CSVs lack the newer columns; fill them in as blank.
        return [{h: row.get(h) or "" for h in CSV_HEADERS} for row in csv.DictReader(f)]


def save_rows(rows: list[dict]):
    """Write applications.csv grouped by company (then by date_applied)."""
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda r: (r["company"].lower(), r["date_applied"]))
    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def _norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def _loosely_equal(a: str, b: str) -> bool:
    # "TransUnion" vs "TransUnion LLC", "Data Analyst" vs "Data Analyst Intern".
    return bool(a and b) and (a in b or b in a)


def find_matching_row(rows: list[dict], company: str | None, role: str | None) -> dict | None:
    """Find the logged application a status email refers to: same company,
    preferring a matching role, most recently applied."""
    company_n, role_n = _norm(company), _norm(role)
    same_company = [r for r in rows if _loosely_equal(_norm(r["company"]), company_n)]
    if not same_company:
        return None
    same_role = [r for r in same_company if _loosely_equal(_norm(r["role"]), role_n)]
    candidates = same_role or same_company
    return max(candidates, key=lambda r: r["date_applied"])


def find_duplicate(rows: list[dict], company: str | None, role: str | None, day: str) -> dict | None:
    """A row this confirmation repeats: same company, same role (or both
    blank), applied within DUPLICATE_WINDOW_DAYS."""
    company_n, role_n = _norm(company), _norm(role)
    for r in rows:
        if not _loosely_equal(_norm(r["company"]), company_n):
            continue
        r_role = _norm(r["role"])
        if (role_n or r_role) and not _loosely_equal(r_role, role_n):
            continue
        try:
            gap = abs((datetime.fromisoformat(day) - datetime.fromisoformat(r["date_applied"])).days)
        except ValueError:
            continue
        if gap <= DUPLICATE_WINDOW_DAYS:
            return r
    return None


def _clean_date(value) -> str:
    """Keep a YYYY-MM-DD the model returned; drop anything else."""
    value = (value or "").strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return ""
    return value


def _fill_details(row: dict, extracted: dict):
    for field in JOB_DETAIL_FIELDS:
        if not row[field] and extracted.get(field):
            row[field] = str(extracted[field]).strip()


def record_email(rows: list[dict], email: dict, extracted: dict) -> tuple[str, dict]:
    """Apply one classified email to rows.
    Returns ("added" | "updated" | "duplicate", row)."""
    status = EMAIL_TYPE_TO_STATUS[extracted["email_type"]]
    email_date = email["internal_date"].strftime("%Y-%m-%d")
    is_next_step = status == STATUS_NEXT_ROUND

    if status == STATUS_APPLIED:
        row = find_duplicate(rows, extracted.get("company"), extracted.get("role"), email_date)
        if row is not None:
            _fill_details(row, extracted)
            return "duplicate", row
    else:
        row = find_matching_row(rows, extracted.get("company"), extracted.get("role"))
        if row is not None:
            row["status"] = status
            row["status_updated"] = email_date
            row["first_response_date"] = row["first_response_date"] or email_date
            row["next_step_details"] = (extracted.get("next_step_details") or "") if is_next_step else row["next_step_details"]
            row["next_step_type"] = (extracted.get("next_step_type") or "Other") if is_next_step else row["next_step_type"]
            # A deadline only matters while the next step is open.
            row["next_step_due"] = _clean_date(extracted.get("next_step_due")) if is_next_step else ""
            _fill_details(row, extracted)
            return "updated", row

    row = {field: "" for field in CSV_HEADERS}
    _fill_details(row, extracted)
    row.update(
        {
            # For a status email with no logged application, the email date is
            # the best available stand-in for when it was applied.
            "date_applied": email_date,
            "company": extracted.get("company") or "",
            "status": status,
            "email_subject": email["subject"],
            "gmail_message_id": email["id"],
            "status_updated": email_date,
            "first_response_date": "" if status == STATUS_APPLIED else email_date,
            "next_step_details": (extracted.get("next_step_details") or "") if is_next_step else "",
            "next_step_type": (extracted.get("next_step_type") or "Other") if is_next_step else "",
            "next_step_due": _clean_date(extracted.get("next_step_due")) if is_next_step else "",
        }
    )
    rows.append(row)
    return "added", row


def write_grouped_summary():
    """Write applications_by_company.md: one section per company, listing each
    role/job_id/skills/status/date, for a quick human-readable grouped view."""
    rows = load_rows()
    by_company: dict[str, list[dict]] = {}
    for row in rows:
        by_company.setdefault(row["company"] or "Unknown", []).append(row)

    lines = ["# Applications by Company", ""]
    for company in sorted(by_company, key=str.lower):
        entries = by_company[company]
        lines.append(f"## {company} ({len(entries)})")
        for r in entries:
            job_id_part = f" — Job ID: {r['job_id']}" if r["job_id"] else ""
            lines.append(f"- **{r['role'] or 'Unknown role'}**{job_id_part}")
            status = r["status"]
            if r["status_updated"] and status != STATUS_APPLIED:
                status += f" (updated {r['status_updated']})"
            via = f" via {r['platform']}" if r["platform"] else ""
            lines.append(f"  - Applied: {r['date_applied']}{via} — Status: {status}")
            if r["next_step_details"]:
                due = f" (due {r['next_step_due']})" if r["next_step_due"] else ""
                lines.append(f"  - Next step: {r['next_step_details']}{due}")
            job = ", ".join(x for x in (r["work_mode"], r["employment_type"], r["location"], r["salary"]) if x)
            if job:
                lines.append(f"  - Job: {job}")
            if r["skills_required"]:
                lines.append(f"  - Skills: {r['skills_required']}")
        lines.append("")

    SUMMARY_PATH.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Email summary
# ---------------------------------------------------------------------------

STATUS_HEADINGS = [
    (STATUS_APPLIED, "Applied"),
    (STATUS_NEXT_ROUND, "Next round"),
    (STATUS_OFFER, "Offer"),
    (STATUS_REJECTED, "Rejected"),
]


def build_summary_email(updates: list[dict], rows: list[dict]) -> tuple[str, str]:
    """Return (subject, body) for the run's updates, grouped by status."""
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    lines = [f"Job tracker update - {today}", ""]
    for status, heading in STATUS_HEADINGS:
        group = [r for r in updates if r["status"] == status]
        if not group:
            continue
        lines.append(f"{heading} ({len(group)}):")
        for r in group:
            lines.append(f"  - {r['company'] or 'Unknown company'} - {r['role'] or 'Unknown role'}")
            if r["next_step_details"]:
                lines.append(f"      Next step: {r['next_step_details']}")
        lines.append("")

    totals = " / ".join(
        f"{sum(r['status'] == status for r in rows)} {heading.lower()}"
        for status, heading in STATUS_HEADINGS
    )
    lines.append(f"All applications: {totals}")
    url = dashboard_url()
    if url:
        lines.append(f"Dashboard: {url}")

    subject = f"Job tracker: {len(updates)} update(s) - {today}"
    return subject, "\n".join(lines)


def dashboard_url() -> str | None:
    """The GitHub Pages address of index.html for this repo, when run in Actions."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def _pretty_date(day: str) -> str:
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return day or "unknown"


def build_reminder_email(row: dict) -> tuple[str, str]:
    """Return (subject, body) for one application that moved to a next round
    or got an offer."""
    what = row["role"] or "an application"
    company = row["company"] or "a company"
    role_part = f" - {row['role']}" if row["role"] else ""
    if row["status"] == STATUS_OFFER:
        lines = [
            f"{company} has made you an offer for {what}. Congratulations!",
            "",
            f"Received: {_pretty_date(row['status_updated'])}",
            "Check the original email for the details and any reply-by date.",
        ]
        url = dashboard_url()
        if url:
            lines += ["", f"All your applications: {url}"]
        return f"Offer from {company}{role_part}", "\n".join(lines)

    lines = [
        f"{company} has moved your application for {what} to the next round.",
        "",
        f"What to do: {row['next_step_details'] or 'See the email from ' + company + ' for details.'}",
    ]
    if row["next_step_type"]:
        lines.append(f"Type: {row['next_step_type']}")
    if row["next_step_due"]:
        label = "Interview date" if row["next_step_type"] == "Interview" else "Due by"
        lines.append(f"{label}: {_pretty_date(row['next_step_due'])}")
    link = re.search(r"https?://[^\s)>\]]+", row["next_step_details"] or "")
    if link:
        lines.append(f"Link: {link.group(0)}")
    lines += [
        "",
        f"Received: {_pretty_date(row['status_updated'])}",
        f"Applied: {_pretty_date(row['date_applied'])}" + (f" via {row['platform']}" if row["platform"] else ""),
    ]
    if not row["next_step_due"]:
        note = (
            "Check the original email for the interview date and time."
            if row["next_step_type"] == "Interview"
            else "Assessments and surveys often expire within a few days, so check the "
            "original email for a deadline."
        )
        lines += ["", note]
    url = dashboard_url()
    if url:
        lines += ["", f"All your applications: {url}"]
    due = f" (due {_pretty_date(row['next_step_due'])})" if row["next_step_due"] and row["next_step_type"] != "Interview" else ""
    return f"Action needed: next round at {company}{role_part}{due}", "\n".join(lines)


def send_email(sender: str, subject: str, body: str) -> str | None:
    """Send one email via Gmail SMTP using an app password. Returns None on
    success, otherwise why it wasn't sent. Never raises, so email problems
    don't fail the run."""
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("NOTIFY_EMAIL")
    if not (password and recipient):
        return "not configured (GMAIL_APP_PASSWORD / NOTIFY_EMAIL)"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            # Google shows app passwords in groups of four; spaces are optional.
            smtp.login(sender, password.replace(" ", ""))
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        return str(e)
    return None


def send_notifications(sender: str, updates: list[dict], rows: list[dict]):
    """The daily summary, plus one reminder per application that moved to a
    next round in this run."""
    if not os.environ.get("GMAIL_APP_PASSWORD") or not os.environ.get("NOTIFY_EMAIL"):
        print("[debug] email not configured (GMAIL_APP_PASSWORD / NOTIFY_EMAIL)")
        return

    error = send_email(sender, *build_summary_email(updates, rows))
    if error:
        gh_annotation("warning", "Email not sent", f"Could not send the summary: {error}")
    else:
        gh_annotation("notice", "Email sent", f"Summary of {len(updates)} update(s) sent")

    reminders = [r for r in updates if r["status"] in (STATUS_NEXT_ROUND, STATUS_OFFER)]
    failed = []
    for row in reminders:
        error = send_email(sender, *build_reminder_email(row))
        if error:
            failed.append(f"{row['company']}: {error}")
    if reminders:
        sent = len(reminders) - len(failed)
        gh_annotation("notice", "Next-round reminders", f"Sent {sent} of {len(reminders)} reminder(s)")
    if failed:
        gh_annotation("warning", "Reminder not sent", "; ".join(failed))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_start_date(value: str) -> datetime:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        sys.exit(f"ERROR: start date {value!r} is not a date like 2026-10-01 (YYYY-MM-DD).")


def main():
    check_env()
    start_date = os.environ.get("START_DATE", "").strip()
    since_override = parse_start_date(start_date) if start_date else None

    if os.environ.get("RESET_DATA", "").strip().upper() == "RESET":
        # Fresh start, e.g. after copying this repo as a template.
        save_rows([])
        save_state({"last_run_utc": None, "processed_ids": []})
        gh_annotation("notice", "Reset", "Cleared all tracked applications; starting fresh.")

    state = load_state()
    processed_ids = set(state.get("processed_ids", []))

    if since_override:
        # Already-processed emails are still skipped, so this never double-counts.
        since = since_override
    elif state.get("last_run_utc"):
        since = datetime.fromisoformat(state["last_run_utc"])
    else:
        # First run with no start date: look back 7 days.
        since = (datetime.now(timezone.utc) - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)

    gmail = get_gmail_service()
    mailbox = gmail.users().getProfile(userId="me").execute()["emailAddress"]
    gh_annotation("notice", "Mailbox", f"Scanning {mailbox}")
    llm = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    run_started = datetime.now(timezone.utc)
    candidate_ids = search_candidate_messages(gmail, since)
    changes = 0
    llm_failed = False
    print(f"[debug] since={since.isoformat()}")

    emails = []
    for msg_id in candidate_ids:
        if msg_id in processed_ids:
            print(f"[debug] {msg_id}: skipped, already in processed_ids")
            continue

        email = fetch_message_summary(gmail, msg_id)
        if email["internal_date"] <= since:
            print(
                f"[debug] {msg_id}: skipped, internal_date={email['internal_date'].isoformat()} "
                f"<= since={since.isoformat()}"
            )
            continue
        emails.append(email)

    # Oldest first, so a confirmation is logged before a later rejection or
    # next-round email for the same application arrives in the same run.
    emails.sort(key=lambda e: e["internal_date"])
    rows = load_rows()

    def persist():
        # Saved after every email so a crash mid-run keeps the work done so
        # far; the workflow commits these files even when this step fails.
        save_rows(rows)
        write_grouped_summary()
        state["processed_ids"] = list(processed_ids)[-2000:]  # cap growth
        save_state(state)

    updates = []
    for email in emails:
        msg_id = email["id"]
        try:
            extracted = extract_with_llm(llm, email)
        except LLMUnavailableError as e:
            # Leave it unprocessed and stop here; last_run_utc is held back
            # below so this email is picked up again on the next run.
            gh_annotation(
                "warning",
                "Gemini unavailable",
                f"Stopped before {msg_id}; remaining emails will be retried next run. Error: {e}",
            )
            llm_failed = True
            break
        processed_ids.add(msg_id)  # mark as seen regardless, to avoid reprocessing

        if extracted is not None:
            action, row = record_email(rows, email, extracted)
            print(f"[debug] {msg_id}: {action} {row['company']} / {row['role'] or '?'} -> {row['status']}")
            # A row updated twice in one run (e.g. applied, then assessment)
            # is reported once, with its latest status. A repeat confirmation
            # only fills in details, so it isn't news.
            if action != "duplicate" and not any(u is row for u in updates):
                updates.append(row)
                changes += 1
        persist()

    # On a Gemini outage keep the old window so skipped emails are retried;
    # processed_ids prevents the ones already handled from being re-logged.
    if not llm_failed:
        state["last_run_utc"] = run_started.isoformat()
    # Always rewrite, so older CSVs pick up the newer columns.
    persist()

    if updates:
        send_notifications(mailbox, updates, rows)

    gh_annotation(
        "notice",
        "Summary",
        f"Scanned {len(candidate_ids)} candidate emails, recorded {changes} application update(s).",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        gh_annotation("error", "Tracker crashed", traceback.format_exc()[-3000:])
        raise
