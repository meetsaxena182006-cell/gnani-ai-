# n8n workflows (optional)

Import each JSON in n8n (Workflows → Import from file). Set two environment variables for n8n:
- `SAARTHI_URL`: where Saarthi runs, e.g. `http://localhost:8080` or your tunnel URL
- `SAARTHI_KEY`: same value as `API_SHARED_SECRET` in Saarthi's `.env`

| Workflow | When | What it does |
|---|---|---|
| daily_report.json | 8 PM IST | CEO Agent sends each owner the day's spend, trips, incidents, expiring documents |
| document_expiry.json | 9 AM IST | Owner gets a list of documents expiring within 30 days |
| expenses_to_sheets.json | hourly | Confirmed expenses appended to a Google Sheet (connect your Google credential and paste the sheet URL) |

Run n8n free on your laptop: `npx n8n` (or Docker). Saarthi works without n8n; these just automate reports.
