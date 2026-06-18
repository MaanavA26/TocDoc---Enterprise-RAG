# TocDoc Admin Dashboard

A Vite + React + TypeScript single-page app for operating the TocDoc
**ingestion service admin API**. It provides a management UI for browsing
indexed documents, viewing index stats, triggering and monitoring connector
syncs, and performing scoped destructive operations.

The dashboard is a pure client: it makes authenticated calls to the ingestion
service's `/admin/*` endpoints and ships with no secrets baked in.

## Features

- **Documents** — list indexed documents for a `bot_tag` scope and view
  per-document detail (source, chunk count, ingestion timestamps, sample chunks).
- **Index Stats** — document/chunk totals plus per-source-type and per-FR-mode
  breakdowns for the scope.
- **Connectors** — trigger a sync for a source type (`blob` / `sharepoint`) and
  watch the recent-runs list with live-ish polling (faster while a run is
  active).
- **Danger Zone** — delete a single document, or delete every document for a
  `bot_tag`. The tenant delete mirrors the API's `confirm=true` guard and adds a
  re-type-the-tag confirmation in the UI.

Loading, empty, and error states are handled throughout. API errors are
rendered from the service's structured error envelope
(`{ error: { code, message, request_id, errors } }`), including the request ID
for correlation and any per-field validation details.

## Prerequisites

- Node.js **20.19+** or **22.13+** (required by eslint 9 / typescript-eslint 8; CI uses 22.13).
- A reachable ingestion service exposing the admin API, and a valid admin token
  (`X-Admin-Token`).

## Quick start

```bash
cd web
npm install
npm run dev      # http://localhost:5173
```

Open the app, expand **Connection settings**, and enter:

- **API base URL** — the ingestion service root, e.g. `http://localhost:8000`.
  Do **not** include `/admin`; the client appends it (see "API base URL contract"
  below).
- **X-Admin-Token** — the static admin token configured server-side via
  `ADMIN_API_TOKEN`.
- **Default bot_tag** — the tenant/workspace scope all pages operate within
  (must match `^[A-Za-z0-9_-]{1,128}$`).

These values are stored in `sessionStorage` only — they survive navigation but
are cleared when the tab closes, and are never written to disk or committed.

## Configuring the API URL / token via env (optional)

For local convenience you can pre-fill the connection settings at build time.
Copy `.env.example` to `.env.local` (gitignored) and set:

```dotenv
VITE_ADMIN_API_BASE_URL=http://localhost:8000
VITE_ADMIN_TOKEN=your-admin-token
```

UI-entered values always take precedence over these defaults. **Never commit a
real token or a client-specific URL.**

## API base URL contract

The configured base URL points at the **service root** (e.g.
`http://localhost:8000`). The API client appends the `/admin` prefix and the
route path, so it calls endpoints such as:

- `GET  /admin/documents?bot_tag=…`
- `GET  /admin/documents/{document_id}?bot_tag=…`
- `GET  /admin/index/stats?bot_tag=…`
- `POST /admin/connectors/{source_type}/sync`
- `GET  /admin/connectors/runs` and `GET /admin/connectors/runs/{run_id}`
- `DELETE /admin/documents/{document_id}?bot_tag=…`
- `DELETE /admin/bots/{bot_tag}/documents?confirm=true`

The admin API is defined in `services/ingestion/admin/routes.py` and its
response models in `services/ingestion/admin/models.py`. The TypeScript types in
`src/api/types.ts` mirror those Pydantic models.

> CORS: when the SPA and the API are on different origins, the ingestion
> service must allow the dashboard origin and the `X-Admin-Token` header.

## Scripts

| Command             | What it does                                   |
| ------------------- | ---------------------------------------------- |
| `npm run dev`       | Start the Vite dev server.                     |
| `npm run build`     | Type-check (`tsc -b`) and produce a `dist/`.   |
| `npm run preview`   | Serve the production build locally.            |
| `npm run typecheck` | Type-check via `tsc -b` (build mode, no emit).  |
| `npm run lint`      | ESLint over the project.                       |
| `npm test`          | Run the vitest suite once.                     |

## Tests

Tests use **vitest** with **@testing-library/react** and a `jsdom` environment.
No test makes a real network call — `fetch` is mocked. Coverage includes:

- `src/api/client.test.ts` — the API client: header/URL/query construction,
  base-URL normalization, error-envelope parsing into a typed `ApiError`
  (including `request_id` and per-field validation errors), network-error
  wrapping, and the `confirm=true` tenant-delete guard.
- `src/pages/DangerZonePage.test.tsx` — the Danger Zone confirm flow: the tenant
  delete stays disabled until the box is checked and the bot_tag is re-typed
  correctly, calls `deleteTenant(tag, true)`, and treats an idempotent
  zero-chunk document delete as success.

## Project layout

```
web/
  src/
    api/        # typed client, response types, React context
    components/ # settings bar, shared loading/empty/error blocks
    hooks/      # useAsync data-fetching hook
    pages/      # Documents, Index Stats, Connectors, Danger Zone
    App.tsx     # shell: connection gate + tab navigation
```

## Notes

- This is a read/operate console; it does not store any tenant data itself.
- Connector run state on the server is in-process and resets on service restart
  (reflected in the UI's empty-state copy).
