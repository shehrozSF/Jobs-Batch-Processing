# Batch Processing of Job Postings

Upload a batch of job postings. Each one becomes a job on a Redis queue;
workers send it to an LLM (`gpt-oss-120b` on Cerebras), validate the answer
against a strict schema, and store the result in Postgres. A live page shows
progress, failures and cost while the batch runs.

```
POST /api/batches ──► Postgres (batch + jobs) ──► Redis queue (job ids only)
                                                        │
                       ┌────────────────────────────────┘
                       ▼
               worker × N ── shared rate limiter (Redis) ──► Cerebras API
                       │
                       ▼
     validate ──► ok: save result + tokens + cost
              └─► invalid: one repair turn with the errors ──► still invalid: dead letter
```

## Results

A full run over the scraped corpus (249 unique postings), two workers sharing
one rate limiter:

| | |
|---|---|
| Succeeded | **249 / 249**, none dead-lettered |
| Fixed by the repair turn | **20** (8%) failed validation first time; all repaired |
| Model calls | 269 (249 + 20 repairs) |
| Tokens | 374k in, 140k out (~1,500 in / ~560 out per posting) |
| Cost | **$0.24**; the logged cost matched the vendor's invoice to within a cent |
| Transient API errors | 1, recovered by backoff and retry |
| Wall time | 1 h 48 min, paced at 2.5 requests/minute |

The pace is deliberate. Cerebras' developer tier allows 5 requests/minute but
only 150/hour, which averages 2.5/minute. Spacing calls evenly gets the same
throughput as bursting, without ever being rate-limited.

## What it handles

| Situation | What happens |
|---|---|
| Model returns JSON that breaks a rule | The errors are sent back once for a repair turn; if it still fails, the job is **dead-lettered** |
| Rate limit, 5xx, timeout | Retried with **exponential backoff + jitter**, up to `BATCH_MAX_ATTEMPTS` |
| Several workers running | One **Redis-based rate limiter** spaces calls across all of them, so the fleet stays under the vendor's limit |
| No billing (402), bad key, daily quota spent | The **batch halts**: remaining jobs pause instead of each making a doomed request |
| A fix is in (prompt, billing, quota reset) | `POST /api/batches/{id}/retry` sends every dead, paused and stranded job back through |
| Redis is lost | Only job ids live there; postings and results are in Postgres |
| A job is delivered twice | Ignored unless it is still `queued` |
| A worker dies mid-job (crash, Ctrl+C) | The job is stuck in `running`; after 20 minutes, retry picks it up too |

Every job records its model calls, input/output tokens and cost in USD,
including repair turns and retries, so a batch's cost is exact.

## Run it

Needs Python 3.12+ and Docker (for Postgres and Redis only). The app itself
runs in your terminals.

**Once:**

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env               # then set BATCH_API_KEY
```

**Each session**, three terminals from the repo root:

```powershell
docker compose up -d                                       # Postgres (5433) + Redis (6379)

.venv\Scripts\python -m uvicorn app.main:app --reload      # terminal 1: API + UI at http://localhost:8000
.venv\Scripts\python -m app.worker                         # terminal 2: worker
.venv\Scripts\python scripts\submit_corpus.py --limit 10   # terminal 3: submit a batch
```

- **On Windows**, run everything as `python -m ...`. Application Control
  blocks the `.exe` launchers in `.venv\Scripts` (e.g. `uvicorn.exe`).
- **More parallelism:** start the worker in more terminals. They share one
  rate limiter, so the total pace stays the same; extra workers help when
  calls are slow, not to get past the limit.
- **After editing `.env`**, restart the worker (and the API, if you changed a
  URL). Settings are read once at startup; `--reload` only watches `.py` files.
- **If a terminal stops:** without the API (terminal 1), workers keep
  processing and saving results; you just can't submit or view batches until
  it's back. Without a worker (terminal 2), jobs wait in `queued`. A worker
  killed mid-job leaves that job in `running`; after 20 minutes the
  Retry button sends it back through.
- **Stop:** Ctrl+C in each terminal, then `docker compose stop`. Data stays
  in the `pgdata` volume.

<details>
<summary>Running the API and workers in containers instead</summary>

```bash
docker compose --profile full up --build --scale worker=4
```

Don't run this alongside the terminals: the container API takes port 8000 and
its workers share your queue.
</details>

The full corpus takes about 1 h 45 min at the default 2.5 requests/minute. If
your account has higher limits, raise `BATCH_REQUESTS_PER_MINUTE` in `.env`
and restart the workers (settings are read once at startup).

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/batches` | `{"name": ..., "postings": [{"source_key": ..., "body": ...}]}` |
| `GET` | `/api/batches/{id}` | counts by status, % done, tokens, cost, halt reason |
| `GET` | `/api/batches/{id}/jobs?status=dead` | jobs with their result or error |
| `POST` | `/api/batches/{id}/retry` | requeue dead, paused and stranded jobs |
| `GET` | `/` and `/batches/{id}` | upload form and live progress page (HTMX) |

## Tests

```powershell
.venv\Scripts\python -m pytest
```

They run on in-memory SQLite and fake Redis, so they need no API key, Docker
or network. A scripted fake LLM client covers the retry, repair, dead-letter
and halt paths.

## Layout

```
app/
  config.py    settings from .env (BATCH_ prefix)
  db.py        Batch and Job tables, job statuses
  schema.py    JobPosting / Salary and the validation rules
  llm.py       Cerebras client; sorts errors into retryable vs fatal
  extract.py   prompt, validation, one repair turn
  limiter.py   rate limiter shared by all workers
  tasks.py     one job: retries, backoff, dead letter, batch halt
  queue.py     RQ wiring
  worker.py    worker process (Windows-compatible)
  service.py   create batch, progress, requeue
  main.py      FastAPI routes and pages
scripts/submit_corpus.py   send data/raw/ as one batch
tests/       39 tests; no network, Docker or API key
```

## Data

- `data/raw/`: 251 job postings scraped in September 2026 from LinkedIn,
  Indeed (Pakistan and US) and Rozee.pk, one `.txt` per posting, plus
  `index.jsonl` with each board's metadata. Each file is a provenance header,
  a `---` line, then the posting text; only the text is sent to the model.
  Two postings are exact duplicates listed in two cities, so 249 are unique.
- `data/eval/salary_eval.jsonl`: 117 salary labels taken from the boards' own
  parsed pay fields, with per-row results from an earlier regex-vs-LLM
  comparison in `data/eval/runs/`.
