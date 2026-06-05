#!/usr/bin/env bash
#
# 05_curl.sh — Raw curl calls against the TocDoc HTTP API.
#
# What this shows:
#   The on-the-wire contract behind the SDK: the exact paths, headers, and JSON
#   bodies for a QnA query, a streaming QnA query, and an admin read. These
#   mirror what the Python SDK sends, so the same TOCDOC_* env vars apply.
#
# Paths (mirror the SDK's URL derivation):
#   QnA query   ->  $TOCDOC_BASE_URL/qna
#   QnA stream  ->  $TOCDOC_BASE_URL/qna/stream   (Server-Sent Events)
#   Admin read  ->  $TOCDOC_BASE_URL/admin/documents?bot_tag=...
#
# Note: the /qna/stream call mirrors the SDK's `stream_ask`; that SSE route is
# forward-looking and not (yet) in the documented REST surface (docs/API.md), so
# it needs a deployment that serves it.
#
# Note: the QnA endpoints and the admin endpoints live on DIFFERENT services
# (QnA vs ingestion). If they sit behind one gateway, one base URL works for
# both; if they are on separate hosts, point TOCDOC_BASE_URL at the right
# service for each call (the admin calls need the ingestion host).
#
# Environment variables (placeholders — never hardcode real tokens):
#   TOCDOC_BASE_URL     Base URL of the service (e.g. https://your-host/qna).
#   TOCDOC_TOKEN        Bearer token for the QnA routes.
#   TOCDOC_ADMIN_TOKEN  Static admin token (X-Admin-Token) for the admin routes.
#
# Usage:
#   export TOCDOC_BASE_URL="https://your-host/qna"
#   export TOCDOC_TOKEN="eyJ..."
#   export TOCDOC_ADMIN_TOKEN="..."
#   bash examples/05_curl.sh

set -euo pipefail

: "${TOCDOC_BASE_URL:?set TOCDOC_BASE_URL (e.g. https://your-host/qna)}"
: "${TOCDOC_TOKEN:?set TOCDOC_TOKEN (QnA bearer token)}"
: "${TOCDOC_ADMIN_TOKEN:?set TOCDOC_ADMIN_TOKEN (X-Admin-Token)}"

BOT_TAG="acme"

# ---------------------------------------------------------------------------
# 1. POST /qna — ask a grounded question (Bearer JWT auth).
# ---------------------------------------------------------------------------
echo "== POST /qna =="
curl --fail-with-body -sS \
  -X POST "${TOCDOC_BASE_URL}/qna" \
  -H "Authorization: Bearer ${TOCDOC_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{
    "session_id": "sess-abc-123",
    "bot_tag": "'"${BOT_TAG}"'",
    "fr_tag": "read",
    "bot": [
      { "user_query": "What is the refund policy?" }
    ]
  }'
echo

# ---------------------------------------------------------------------------
# 2. POST /qna/stream — same body, streamed back as Server-Sent Events.
#    `Accept: text/event-stream` selects the streaming response; -N disables
#    curl output buffering so tokens print as they arrive.
# ---------------------------------------------------------------------------
echo "== POST /qna/stream (SSE) =="
curl --fail-with-body -sS -N \
  -X POST "${TOCDOC_BASE_URL}/qna/stream" \
  -H "Authorization: Bearer ${TOCDOC_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "session_id": "sess-abc-123",
    "bot_tag": "'"${BOT_TAG}"'",
    "fr_tag": "read",
    "bot": [
      { "user_query": "Summarize the onboarding guide." }
    ]
  }'
echo

# ---------------------------------------------------------------------------
# 3. GET /admin/documents — admin read (X-Admin-Token auth, NOT the bearer token).
#    Admin routes live on the ingestion service.
# ---------------------------------------------------------------------------
echo "== GET /admin/documents =="
curl --fail-with-body -sS \
  -X GET "${TOCDOC_BASE_URL}/admin/documents?bot_tag=${BOT_TAG}" \
  -H "X-Admin-Token: ${TOCDOC_ADMIN_TOKEN}" \
  -H "Accept: application/json"
echo
