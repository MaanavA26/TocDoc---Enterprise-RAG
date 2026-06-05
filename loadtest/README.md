# Load / performance test harness

A [Locust](https://locust.io/)-based load-test suite for the TocDoc RAG product.
It exercises the latency-sensitive `POST /qna` endpoint, the read-only admin
endpoints, and (opt-in) the mutating `POST /upload` endpoint against a **deployed**
instance.

> **This harness needs a reachable deployment.** It does not stand up the
> services itself and makes no live calls during unit tests. You point it at a
> running staging/canary host via environment variables (below). With no target
> configured, only the unit tests run.

## Layout

| Path | Purpose |
| --- | --- |
| `locustfile.py` | Locust user classes and weighted tasks (the actual load). |
| `helpers.py` | Pure request-building / response-validation helpers (no Locust, no I/O). |
| `config.py` | Lazy, env-driven config (base URL, creds, paths, tags). |
| `tests/` | `pytest` unit tests for `helpers.py` + `config.py` — run without a live target. |
| `scenarios/` | Ready-made Locust config profiles: `smoke`, `ramp`, `soak`. |
| `requirements.txt` | Pinned `locust` + `pytest`. |

## Install

Use a virtualenv and your configured package index:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r loadtest/requirements.txt --index-url <your-index-url>
```

## Configure the target and credentials

Everything is read from the environment **at run time** — nothing is committed.

| Variable | Required | Meaning |
| --- | --- | --- |
| `TOCDOC_BASE_URL` | yes (for a run) | Base URL of the deployed instance, e.g. `https://staging.example`. Also passed as `--host`. |
| `TOCDOC_TOKEN` | yes (for `/qna`) | Bearer JWT sent as `Authorization: Bearer <token>`. |
| `TOCDOC_ADMIN_TOKEN` | yes (for admin/upload) | Value sent as the `X-Admin-Token` header. |
| `TOCDOC_BOT_TAG` | no (default `loadtest`) | `bot_tag` / tenant scope to exercise. |
| `TOCDOC_FR_TAG` | no (default `read`) | `fr_tag` / `fr_mode` to exercise. |
| `TOCDOC_QNA_PATH` | no (default `/qna`) | Override if a proxy mounts the route elsewhere. |
| `TOCDOC_UPLOAD_PATH` | no (default `/upload`) | Override the upload route path. |
| `TOCDOC_ADMIN_DOCS_PATH` | no (default `/admin/documents`) | Override the admin documents-list path. |
| `TOCDOC_ADMIN_STATS_PATH` | no (default `/admin/index/stats`) | Override the admin stats path. |
| `TOCDOC_ENABLE_UPLOAD` | no (default off) | Set truthy to enable the **mutating** upload task. |
| `TOCDOC_UPLOAD_FILEPATH` | only if upload enabled | Server-side absolute path the upload task references. |

> Uploads write to the target index. The upload task is disabled unless
> `TOCDOC_ENABLE_UPLOAD` is truthy **and** `TOCDOC_UPLOAD_FILEPATH` is set, so a
> plain smoke/ramp pass never mutates the index by accident. The upload task
> sends only query params (no multipart file), so `TOCDOC_UPLOAD_FILEPATH` must
> point at a **server-side folder** (the endpoint's folder-upload mode); a single
> file path would be rejected.

```bash
export TOCDOC_BASE_URL="https://<your-deployment>"
export TOCDOC_TOKEN="<jwt>"
export TOCDOC_ADMIN_TOKEN="<admin-token>"
```

## Run headless (CI / scripted)

```bash
locust -f loadtest/locustfile.py --headless \
    -u 10 -r 2 -t 5m --host "$TOCDOC_BASE_URL"
```

- `-u` total users, `-r` spawn rate (users/sec), `-t` run time.
- `--csv loadtest/results/run --csv-full-history` writes stats + per-interval
  history for charting.

## Run with the web UI

```bash
locust -f loadtest/locustfile.py --host "$TOCDOC_BASE_URL"
```

Then open <http://localhost:8089>, set the user count and spawn rate, and start.
The **Charts** tab shows RPS and response-time percentiles live; the
**Statistics** tab shows per-endpoint p50/p95/p99.

## Scenario profiles

Pre-baked profiles live in `scenarios/` (Locust `--config` files):

```bash
locust --config loadtest/scenarios/smoke.conf --host "$TOCDOC_BASE_URL"   # quick reachability check (5u / 1m)
locust --config loadtest/scenarios/ramp.conf  --host "$TOCDOC_BASE_URL"   # climb to 50u over 10m, find the knee
locust --config loadtest/scenarios/soak.conf  --host "$TOCDOC_BASE_URL"   # hold 20u for 2h, surface leaks/drift
```

`ramp` and `soak` write per-interval CSV history under `loadtest/results/`
(gitignored).

## Reading the results

Locust reports per request **type** (e.g. `POST /qna`, `GET /admin/documents`):

- **RPS** — requests/sec; the sustained throughput at the current concurrency.
- **p50 / median** — typical latency; what most users feel.
- **p95** — tail latency; 1 in 20 requests is at least this slow. This is the
  number to set SLOs against for an interactive Q&A endpoint.
- **p99** — worst-case tail; watch this during `ramp` to spot the latency knee
  (the concurrency where p99 starts climbing steeply) and during `soak` to spot
  drift over time.
- **# fails / failure %** — validated failures (non-200, empty `answer`, etc.).
  `429` back-pressure on `/upload` is intentionally **not** counted as a
  failure (it is expected under load) but is surfaced in the failure reason.

The weighted task mix (QnA-heavy, admin-light, upload opt-in) approximates a
production traffic shape; adjust the `weight` values in `locustfile.py` to match
your own.

## Unit tests (no deployment needed)

The request-building and response-validation logic is factored into `helpers.py`
and `config.py` so it can be tested without Locust or a live host:

```bash
pytest loadtest/tests -q
```

This is what CI runs to guard the harness; the actual load run requires a
reachable deployment and is not run in CI.
