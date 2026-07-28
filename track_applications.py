"""
track_applications.py

End-of-day job application tracker.

What it does:
1. Searches Gmail for likely application-confirmation emails received since the
   last successful run (tracked in state/last_run.json).
2. Sends each candidate email's subject + sender + snippet to Claude, which
   decides whether it's really an application confirmation and extracts
   structured fields (company, role, platform).
3. Appends new, deduplicated rows to applications.csv.
4. Updates the "last run" timestamp and the set of processed Gmail message IDs.

Environment variables required (set as GitHub Actions secrets):
- GMAIL_CLIENT_ID
- GMAIL_CLIENT_SECRET
- GMAIL_REFRESH_TOKEN        (reuse the same OAuth app you already use for
                               sending Gmail notifications in the job-application-agent)
- ANTHROPIC_API_KEY

Files this script reads/writes (relative to repo root):
- applications.csv           the running log
- state/last_run.json        {"last_run_utc": "...", "processed_ids": [...]}
"""

import os
import json
import csv
import base64
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import anthropic

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
]

# Search is intentionally broad; Claude does the real filtering afterward.
GMAIL_QUERY_TERMS = (
    '(subject:"application" OR subject:"applied" OR subject:"thank you for applying" '
    'OR subject:"application received" OR subject:"your application")'
)

CLAUDE_MODEL = "claude-sonnet-4-6"

EXTRACTION_PROMPT = """You are helping classify and extract data from an email that MIGHT be an \
automated confirmation that a job application was submitted (e.g. from LinkedIn, Indeed, \
Greenhouse, Lever, Workday, iCIMS, SmartRecruiters, a company's own careers page, etc.).

Email metadata:
From: {sender}
Subject: {subject}
Snippet: {snippet}

Decide if this is genuinely an application-submission confirmation (NOT a rejection, \
NOT an interview invite, NOT a newsletter, NOT a job recommendation digest).

Some confirmation emails include a requisition/job ID (e.g. "Job ID: 12345", \
"Req #R-00123") and/or a list of required skills or qualifications pulled from the \
posting. Extract these if present in the subject or snippet; otherwise use null. \
Do not guess or invent a job ID or skills that are not actually present in the text.

Respond with ONLY a JSON object, no other text, in this exact shape:
{{
  "is_application_confirmation": true or false,
  "company": "string or null",
  "role": "string or null",
  "platform": "string or null (e.g. LinkedIn, Indeed, Greenhouse, Workday, Company Careers Page)",
  "job_id": "string or null",
  "skills_required": "comma-separated string or null"
}}
"""


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


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
# Claude extraction
# ---------------------------------------------------------------------------

def extract_with_claude(client: "anthropic.Anthropic", email: dict) -> dict | None:
    prompt = EXTRACTION_PROMPT.format(
        sender=email["sender"], subject=email["subject"], snippet=email["snippet"]
    )
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not data.get("is_application_confirmation"):
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


def ensure_csv():
    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="") as f:
            csv.writer(f).writerow(CSV_HEADERS)


def append_row(row: dict):
    with CSV_PATH.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writerow(row)


def sort_csv_by_company():
    """Re-sort applications.csv so rows are grouped by company (then by date_applied)."""
    with CSV_PATH.open(newline="") as f:
        rows = list(csv.DictReader(f))

    rows.sort(key=lambda r: (r["company"].lower(), r["date_applied"]))

    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def write_grouped_summary():
    """Write applications_by_company.md: one section per company, listing each
    role/job_id/skills/status/date, for a quick human-readable grouped view."""
    with CSV_PATH.open(newline="") as f:
        rows = list(csv.DictReader(f))

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
            lines.append(f"  - Applied: {r['date_applied']} via {r['platform']} — Status: {r['status']}")
            if r["skills_required"]:
                lines.append(f"  - Skills: {r['skills_required']}")
        lines.append("")

    (REPO_ROOT / "applications_by_company.md").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    processed_ids = set(state.get("processed_ids", []))

    if state.get("last_run_utc"):
        since = datetime.fromisoformat(state["last_run_utc"])
    else:
        # first run: look back 1 day
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    ensure_csv()

    gmail = get_gmail_service()
    claude = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    candidate_ids = search_candidate_messages(gmail, since)
    new_rows = 0

    for msg_id in candidate_ids:
        if msg_id in processed_ids:
            continue

        email = fetch_message_summary(gmail, msg_id)
        if email["internal_date"] <= since:
            continue

        extracted = extract_with_claude(claude, email)
        processed_ids.add(msg_id)  # mark as seen regardless, to avoid reprocessing

        if extracted is None:
            continue

        append_row(
            {
                "company": extracted.get("company") or "",
                "role": extracted.get("role") or "",
                "job_id": extracted.get("job_id") or "",
                "skills_required": extracted.get("skills_required") or "",
                "date_applied": email["internal_date"].strftime("%Y-%m-%d"),
                "platform": extracted.get("platform") or "",
                "status": "Applied",
                "email_subject": email["subject"],
                "gmail_message_id": msg_id,
            }
        )
        new_rows += 1

    if new_rows:
        sort_csv_by_company()
        write_grouped_summary()

    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    state["processed_ids"] = list(processed_ids)[-2000:]  # cap growth
    save_state(state)

    print(f"Scanned {len(candidate_ids)} candidate emails, logged {new_rows} new application(s).")


if __name__ == "__main__":
    main()
