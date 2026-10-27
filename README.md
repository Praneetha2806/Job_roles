# Tracker data

This branch holds the data written by the **Track Job Applications** workflow.
The code lives on the `main` branch.

| File | What it is |
|---|---|
| `applications.csv` | Every tracked application, one row each |
| `applications_by_company.md` | The same data grouped by company |
| `state/last_run.json` | Where the next run starts, and which emails were already processed |

Keeping the data here means anyone who copies the code from `main` starts
with an empty tracker. Every nightly run adds a commit, so the history of this
branch is the history of your applications.
