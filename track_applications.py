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

Environment variables required (set as GitHub Actions secrets):
- GMAIL_CLIENT_ID
- GMAIL_CLIENT_SECRET
- GMAIL_REFRESH_TOKEN        (reuse the same OAuth app you already use for
                               sending Gmail notifications in the job-application-agent)
- GEMINI_API_KEY

Files this script reads/writes (relative to repo root):
- applications.csv           the running log
- state/last_run.json        {"last_run_utc": "...", "processed_ids": [...]}
"""

import os
import json
import csv
import base64
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
CSV_PATH = REPO_ROOT / "applications.csv"
STATE_PATH = REPO_ROOT / "state" / "last_run.json"

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
]

STATUS_APPLIED = "Applied"
STATUS_REJECTED = "Rejected"
STATUS_NEXT_ROUND = "Next Round"

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

# Words that tie an email to a job application, so generic emails that just
# say "thank you" or "unfortunately" aren't sent to the LLM.
JOB_CONTEXT_TERMS = [
    "application", "applied", "applying", "position", "candidacy", "candidate",
]


def _gmail_or(terms: list[str]) -> str:
    return "(" + " OR ".join(f'"{t}"' for t in terms) + ")"


# Either an application-ish subject line, or a keyword plus job context anywhere
# in the email. Bare terms (no "subject:" prefix) also match the body. The LLM
# does the precise classification afterward.
GMAIL_QUERY_TERMS = (
    '(subject:"application" OR subject:"applied" OR subject:"your application" OR ('
    + _gmail_or(CONFIRMATION_PHRASES + REJECTION_PHRASES + NEXT_STEP_PHRASES)
    + " "
    + _gmail_or(JOB_CONTEXT_TERMS)
    + "))"
)

GEMINI_MODEL = "gemini-flash-latest"

EXTRACTION_PROMPT = """You are helping track the status of job applications from emails \
(e.g. from LinkedIn, Indeed, Greenhouse, Lever, Workday, iCIMS, SmartRecruiters, Ashby, \
a company's own careers page, etc.).

Email metadata:
From: {sender}
Subject: {subject}
Body: {snippet}

Keyword matches found by a simple text scan (hints only; use your judgment):
- confirmation keywords: {confirmation_hits}
- rejection keywords: {rejection_hits}
- next-step keywords: {next_step_hits}

Classify the email as exactly one email_type:
- "application_confirmation": confirms a job application was submitted/received \
(typically "Thank you for applying", "We have received your application").
- "rejection": the company is not moving forward with the candidate (typically \
"Unfortunately...", "due to the large volume of applications", "we are unable to...", \
"not moving forward", "decided to pursue other candidates").
- "next_step": the candidate is being taken forward, e.g. an online assessment, \
coding challenge, survey/questionnaire to complete, or an interview invitation/scheduling.
- "other": anything else (newsletters, job-alert digests, recommendations, marketing, \
account/security emails, or emails not about a specific application by the candidate).

Keywords are only hints: "thank you" also appears in rejections, and "unable" or \
"survey" can appear in unrelated emails. Decide based on the overall meaning.

Extract the company and role the email is about. For "next_step", put the action \
required and any assessment/survey/scheduling link in next_step_details (copy links \
exactly as they appear). Some emails include a requisition/job ID (e.g. "Job ID: 12345", \
"Req #R-00123") and/or required skills; extract them if present, otherwise use null. \
Do not guess or invent values that are not actually present in the text.

Respond with ONLY a JSON object, no other text, in this exact shape:
{{
  "email_type": "application_confirmation" | "rejection" | "next_step" | "other",
  "company": "string or null",
  "role": "string or null",
  "platform": "string or null (e.g. LinkedIn, Indeed, Greenhouse, Workday, Company Careers Page)",
  "job_id": "string or null",
  "skills_required": "comma-separated string or null",
  "next_step_details": "string or null"
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


def search_candidate_messages(service, since_utc: datetime):
    # Gmail search only supports day granularity for "after:", so we filter
    # more precisely afterward using the message's internalDate.
    after_str = since_utc.strftime("%Y/%m/%d")
    query = f"{GMAIL_QUERY_TERMS} after:{after_str}"

    message_ids = []
    request = service.users().messages().list(userId="me", q=query, maxResults=100)
    while request is not None:
        response = request.execute()
        message_ids.extend(m["id"] for m in response.get("messages", []))
        request = service.users().messages().list_next(request, response)
    return message_ids


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
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="full")
        .execute()
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
    }


EMAIL_TYPE_TO_STATUS = {
    "application_confirmation": STATUS_APPLIED,
    "rejection": STATUS_REJECTED,
    "next_step": STATUS_NEXT_ROUND,
}


def gh_annotation(level: str, title: str, message: str):
    """Show a message on the GitHub Actions run page (no-op formatting outside CI)."""
    message = message.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::{level} title={title}::{message}")


class LLMUnavailableError(Exception):
    """Gemini could not be reached; the email should be retried next run."""


def generate_with_retry(client: "genai.Client", prompt: str, attempts: int = 4):
    """Call Gemini, backing off on rate-limit / server errors (free tier is
    easily rate-limited when several emails arrive in one day)."""
    for attempt in range(attempts):
        try:
            return client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=2000,
                    response_mime_type="application/json",
                ),
            )
        except genai_errors.ClientError as e:
            if e.code != 429:
                raise
            last_error = e
        except genai_errors.ServerError as e:
            last_error = e
        if attempt == attempts - 1:
            break
        wait = 15 * (attempt + 1)
        print(f"  [debug] Gemini error ({last_error}); retrying in {wait}s")
        time.sleep(wait)
    raise LLMUnavailableError(str(last_error))


def extract_with_llm(client: "genai.Client", email: dict) -> dict | None:
    hits = keyword_hits(email)
    prompt = EXTRACTION_PROMPT.format(
        sender=email["sender"],
        subject=email["subject"],
        snippet=email["snippet"],
        confirmation_hits=", ".join(hits["confirmation"]) or "none",
        rejection_hits=", ".join(hits["rejection"]) or "none",
        next_step_hits=", ".join(hits["next_step"]) or "none",
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
    """Find the logged application a rejection/next-step email refers to:
    same company, preferring a matching role, most recently applied."""
    company_n, role_n = _norm(company), _norm(role)
    same_company = [r for r in rows if _loosely_equal(_norm(r["company"]), company_n)]
    if not same_company:
        return None
    same_role = [r for r in same_company if _loosely_equal(_norm(r["role"]), role_n)]
    candidates = same_role or same_company
    return max(candidates, key=lambda r: r["date_applied"])


def record_email(rows: list[dict], email: dict, extracted: dict) -> str:
    """Apply one classified email to rows. Returns a short description."""
    status = EMAIL_TYPE_TO_STATUS[extracted["email_type"]]
    email_date = email["internal_date"].strftime("%Y-%m-%d")

    if status != STATUS_APPLIED:
        row = find_matching_row(rows, extracted.get("company"), extracted.get("role"))
        if row is not None:
            row["status"] = status
            row["status_updated"] = email_date
            row["next_step_details"] = extracted.get("next_step_details") or ""
            # Fill in anything the confirmation email didn't have.
            for field in ("role", "job_id", "skills_required", "platform"):
                if not row[field] and extracted.get(field):
                    row[field] = extracted[field]
            return f"updated {row['company']} / {row['role'] or '?'} -> {status}"

    rows.append(
        {
            "company": extracted.get("company") or "",
            "role": extracted.get("role") or "",
            "job_id": extracted.get("job_id") or "",
            "skills_required": extracted.get("skills_required") or "",
            # For a status email with no logged application, the email date is
            # the best available stand-in for when it was applied.
            "date_applied": email_date,
            "platform": extracted.get("platform") or "",
            "status": status,
            "email_subject": email["subject"],
            "gmail_message_id": email["id"],
            "status_updated": email_date,
            "next_step_details": extracted.get("next_step_details") or "",
        }
    )
    return f"added {extracted.get('company') or '?'} / {extracted.get('role') or '?'} as {status}"


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
                lines.append(f"  - Next step: {r['next_step_details']}")
            if r["skills_required"]:
                lines.append(f"  - Skills: {r['skills_required']}")
        lines.append("")

    (REPO_ROOT / "applications_by_company.md").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    check_env()
    state = load_state()
    processed_ids = set(state.get("processed_ids", []))

    if state.get("last_run_utc"):
        since = datetime.fromisoformat(state["last_run_utc"])
    else:
        # first run: look back 1 day
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

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

        if extracted is None:
            continue

        print(f"[debug] {msg_id}: {record_email(rows, email, extracted)}")
        changes += 1

    # Always rewrite, so older CSVs pick up the newer columns.
    save_rows(rows)
    write_grouped_summary()

    # On a Gemini outage keep the old window so skipped emails are retried;
    # processed_ids prevents the ones already handled from being re-logged.
    if not llm_failed:
        state["last_run_utc"] = run_started.isoformat()
    state["processed_ids"] = list(processed_ids)[-2000:]  # cap growth
    save_state(state)

    gh_annotation(
        "notice",
        "Summary",
        f"Scanned {len(candidate_ids)} candidate emails, recorded {changes} application update(s).",
    )


if __name__ == "__main__":
    main()
