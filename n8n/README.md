# n8n workflows (optional)

Import each JSON in n8n (Workflows → Import from file). Set two environment variables for n8n:
- `SAARTHI_URL`: where Saarthi runs, e.g. `http://localhost:8080` or your tunnel URL
- `SAARTHI_KEY`: same value as `API_SHARED_SECRET` in Saarthi's `.env`

| Workflow | When | What it does |
|---|---|---|
| daily_report.json | 8 PM IST | CEO Agent sends each owner the day's spend, trips, incidents, expiring documents |
| document_expiry.json | 9 AM IST | Owner gets a list of documents expiring within 30 days |
| expenses_to_sheets.json | hourly | Every confirmed entry appended to a Google Sheet: diesel/expenses the driver spoke, FASTag tolls logged automatically from GPS crossings (estimated, then replaced with actual), freight received |

**FASTag SMS automation (optional):** install any SMS-forwarder app on the phone that receives FASTag debit SMS and point it to
`POST {SAARTHI_URL}/api/fastag/sms` with header `X-Saarthi-Key` and JSON `{"text": "<sms body>"}`. Each debit is matched to the
truck's GPS toll crossing; a debit with no crossing is flagged to the owner as a possible wrong charge.
Monthly statements can also be uploaded as CSV on the office dashboard.

Run n8n free on your laptop: `npx n8n` (or Docker). Saarthi works without n8n; these just automate reports.
