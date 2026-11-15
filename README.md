# Scholarship Scout

**A local-first scholarship matching prototype for students.**

Scholarship Scout runs a sequential 17-agent pipeline using the local
`qwen3:1.7b` model through Ollama. Seven stages call the model; the
other ten use lightweight Python checks. The standalone script can post
approved scholarship data and a short summary to an n8n webhook.

> **Project status:** This is a local-first prototype, not a production
> scholarship search service. It reads recent RSS listings from Opportunity
> Desk and Scholars4Dev and fetches a bounded number of their public pages.
> Eligibility is not independently verified; confirm every opportunity with
> its official provider.

## 17-agent architecture

The stages run in order in `scholarship_scout.py`:

1. **Live scholarship finder (`agent_live_scraper`)** — Reads recent RSS
    entries from Opportunity Desk and Scholars4Dev, fetches up to eight
    matching listing pages per source concurrently, and extracts descriptions,
    deadlines, source names, URLs, and eligibility clues. Expired listings are
    excluded. A failed source feed does not prevent other sources from being used.
2. **Scout (`agent_scout`)** — Prioritizes live listing titles using the
    requested study level and field. Results are restricted to feed entries.
3. **Extractor (`agent_extractor`)** — Copies age, grade, and country clues
    parsed from each source page; the page remains the source of truth.
4. **Validator (`agent_validator`) — AI + Python** — Uses the model as a
    suggestion, while Python removes records that conflict with its explicit
    age, grade, or study-level rules. The model cannot override those checks.
5. **Scam detector (`agent_scam_detector`) — AI + Python** — Uses the model
    alongside deterministic checks for fees, payment details, and suspicious
    links. Flagged records are removed.
6. **Profile matcher (`agent_profile_matcher`) — Python** — Drops listings
    matching its excluded age or education terms and records with explicit age
    or grade ranges that do not fit. Remaining listings are ranked by subject
    and opportunity type. Country restrictions that appear to conflict are
    marked `Check eligibility`; otherwise, listings default to `Likely match`.
    This is a text heuristic, not verified eligibility.
7. **Spam filter (`agent_spam_filter`) — Python** — Removes records with likely
    gibberish, repeated text, or duplicate descriptions.
8. **Auditor (`agent_auditor`) — AI + Python** — Checks for prompt injection
    and must return exactly `STATUS: CLEAN` to proceed.
9. **Antivirus (`agent_antivirus`) — Python** — Uses only `MALWARE_PATTERN`
    as a hard gate. If no pattern is found, it prints a warning and passes
    the records; it does not ask the model to reject the batch.
10. **Record integrity (`agent_record_integrity`) — Python** — Confirms each
    result still matches the live source data.
11. **Eligibility guard (`agent_eligibility_guard`) — Python** — Reapplies the
    deterministic age, grade, and education-term exclusions.
12. **Deadline checker (`agent_deadline_checker`) — Python** — Uses dates
    parsed from the live listing; emits `null` when no supported date is found.
13. **QA (`agent_qa`) — Python** — Checks JSON structure, live source values, deadline shape,
    and selected injection/malware patterns. Delivery stops unless it returns
    `QA PASSED`.
14. **Contact extractor (`agent_contact_extractor`) — Python** — Extracts emails and
    HTTPS or `www` links explicitly present in scholarship text; it does not
    invent or independently verify contact details.
15. **Summarizer (`agent_summarizer`) — AI** — Writes a short summary, which is
    checked for unsupported claims; a factual Python summary is used if needed.
16. **Motivator (`agent_motivator`) — Python** — Adds a hardcoded encouragement
    sentence and prints `Agent 16: Motivator`.
17. **Manager (`agent_manager`) — AI + Python** — Formats the final scholarship
    JSON with the model, verifies it exactly matches approved data, and posts
    the payload to the configured webhook when enabled.

All stages are sequential. The model-calling stages are Scout, Extractor,
Validator, Scam Detector, Auditor, Summarizer, and Manager.
`agent_web_verifier` and `agent_translator` have been removed. The Manager
handles final formatting; there is no separate formatter stage. QA runs before
contact extraction, summarization, motivation, and delivery. A failed gate
exits without posting to n8n.

Malformed Ollama responses are retried up to two times with a larger bounded
output limit before the pipeline reports an error.

## Security & Privacy

- **Local inference and profile privacy:** Profile details are sent to the
  local Scout server and local Ollama only. They are not sent to listing
  sites; only public RSS and listing pages are requested. The profile is kept
  in the browser tab's session storage and is not saved by the Python server.
- **Zero-Trust gating:** Model output is treated as untrusted. Names and
  extracted fields are constrained to fetched source pages, eligibility is
  checked in Python, and selected prompt-injection/malicious-content checks
  run before the manager can send data. The antivirus uses a regex-only
  malware gate, and final JSON QA must return `QA PASSED`.
- **Webhook boundary:** The default destination is
  `http://localhost:5678/webhook/scholarships`. The manager sends a JSON object
  containing `scholarships`, `summary`, `contacts`, and `motivation`. A locally
  running n8n workflow may forward or store data elsewhere depending on its
  configuration.
- **Limits:** These pattern checks and a small language model are not a
  guarantee that data is safe or accurate. They are not a substitute for a
  full antivirus product or security review. Verify scholarship details with
  official sources and review the n8n workflow before using real student data.
- Missing age or education requirements do not stop a listing from being
  marked `Likely match`. Country conflicts detected from listing text or
  extracted country metadata are labeled `Check eligibility`. All results
  still require confirmation with the official provider.

## Requirements

- Python 3
- [Ollama](https://ollama.com/) with the `qwen3:1.7b` model
- n8n is optional for the browser inbox; it is used only when running the
  standalone script with its webhook delivery enabled.

The initial Ollama model download requires an internet connection. After
installation, model inference runs locally.

## Run locally

### 1. Install the Python dependencies

From the repository directory:

```powershell
py -m pip install ollama requests beautifulsoup4 lxml
```

The webhook client uses Python's standard library. On systems without the `py`
launcher, use `python -m pip install ollama requests beautifulsoup4 lxml`
or `python3 -m pip install ollama requests beautifulsoup4 lxml`.

### 2. Download the model and start Ollama

```powershell
ollama pull qwen3:1.7b
```

Make sure the Ollama service is running before continuing.

### 3. Start the inbox and local API

From the project directory, start the server:

```powershell
py server.py
```

Open `http://127.0.0.1:8000` in your browser and choose **New scan**. The
server binds to the local machine only. Scans run asynchronously; the inbox
polls for status and displays results or pipeline errors. Do not open the HTML
directly as a file, because it needs the local API.

The first time you open the dashboard in a browser tab, it asks for age,
country, current study level, optional class/year and subject, and preferred
opportunity type. Your profile stays in that tab's session storage; use **My
profile** to change it. Click **Find live matches** to search recent listings.
Your profile is sent only to this local server and local Ollama, not to the
listing site.

The API provides `GET /api/health`, `GET /api/status`, `GET /api/system`,
`GET /api/saved`, `POST /api/save`, and `POST /api/scan` with a validated
`profile` object. Saving a card persists the scholarship locally with status
`Interested` in `data/saved_scholarships.json`; the Saved view reloads those
records after a restart. Clicking Save again removes it. The system endpoint
reports live free/total storage for the project drive and available/total
physical RAM; the dashboard refreshes these readings every 15 seconds. Only
one scan runs at a time. The browser scan does not post data to n8n.

The live finder fetches at most sixteen recent matching pages (up to eight per
source), using up to four parallel requests. This keeps memory use bounded for
8 GB laptops. It shows each listing's source and application link when found
and skips expired opportunities. Missing age or education rules can still
result in a “Likely match”; this is a text-based signal, not verified
eligibility.

### 4. Optional standalone CLI and n8n delivery

To post a scan to n8n instead, start n8n locally and create a Webhook node that
accepts `POST` requests at `scholarships`. Activate the workflow as appropriate
for your n8n setup, then run:

```powershell
py scholarship_scout.py
```

The destination is configured by `N8N_WEBHOOK_URL`. The script prints the
payload before posting and raises an error if the webhook is unavailable or
rejects the request.

The webhook payload has this shape:

```json
{
  "scholarships": [
    {
      "name": "Example scholarship",
      "eligible_ages": "10-14",
      "eligible_grades": "6-8",
      "eligible_countries": ["Worldwide"],
      "is_verified": false,
      "application_deadline": "December 8, 2026",
      "source_url": "https://opportunitydesk.org/example-listing/",
      "application_url": "https://provider.example/apply",
      "match_status": "Likely match",
      "match_reasons": ["No conflicting age or education restriction found"]
    }
  ],
  "summary": "This scan found one opportunity that passed the pipeline checks. Confirm eligibility and application details with the official provider."
}
```

## Personalize the prototype

When running the standalone script, enter the student's age, grade, country,
and field of study when prompted. The browser inbox accepts the profile from
its profile form.

Other configuration constants near the top of `scholarship_scout.py` include:

- `SCHOLARSHIPS` is an empty legacy catalog; live scans use the configured RSS
  feeds.
- `MODEL` selects the Ollama model tag.
- `N8N_WEBHOOK_URL` sets the webhook destination.

## Inbox UI

[`static/index.html`](./static/index.html) is served by `server.py` and uses
the API to start scans, retrieve status, and render results. It includes a
responsive dark glass-style dashboard with local storage and RAM indicators.
The pipeline uses Ollama locally, reads RSS feeds from Opportunity Desk and
Scholars4Dev, and fetches up to sixteen recent public listing pages. Cards link
to the source and show explicit match notes. Results remain unverified; confirm
details with the official provider. The old demo catalog and DuckDuckGo
dependency are not used by the live finder.

## Current limitations

- **Limited search:** The finder reads two RSS feeds and fetches up to eight
  associated listing pages from each; it does not search the entire web.
- **Eligibility uses page text:** Age, country, and study-level clues are
  parsed with simple text rules. Missing age or education rules default to
  `Likely match`, which can produce false positives; verify each result and
  check ambiguous or conflicting country rules manually.
- **Summary quality:** The small model can produce inaccurate text. Review
  summaries before relying on them.
- **Deadlines:** The checker only extracts dates written in its supported
  formats from the fetched listing data. Missing dates remain `null`.
- **Local setup required:** Python, Ollama, and the model must be installed and
  running for inbox scans. n8n is only needed for standalone CLI webhook
  delivery. The RSS feed and listing pages require internet access.

## Automated n8n delivery

Import [`workflows\scholarship-noon.json`](./workflows/scholarship-noon.json)
into n8n and activate it. The workflow starts the Python script daily at 11:58
AM Asia/Karachi time, receives its POST at the `scholarships` webhook, and
holds results until noon when they arrive early. Results arriving at or after
noon continue immediately. The webhook response returns the results to the
Python script after the hold.

The Execute Command node runs `py scholarship_scout.py`; configure its working
directory to the project root on your host. In Docker, the host project must
be mounted into the container, Python, Ollama, the `ollama` package, and the
model must be available there, and the Execute Command node must be enabled.
Otherwise, run the Python script from the host instead of relying on this node.

## Contributing

Contributions that add reliable scholarship sources, make eligibility criteria
transparent, or improve local setup are welcome. Please keep student privacy
in mind and clearly distinguish verified opportunities from sample or
unverified listings.

## Developer profile and hardware

- Profile used for the example scan: age 12 years; arts-focused student.
- Test hardware: Windows 11 Pro laptop, Intel Core i3-10110U CPU at 2.10 GHz,
  about 8 GB RAM.

There is still room to improve the software and data quality, but this laptop
is at its practical limit for heavier local workloads. Future improvements
should prioritize better sources, more accurate eligibility checks, and
efficiency rather than requiring more powerful hardware.
