# Phase 2 Workstream B — Observability Specification

## Objective

Add the minimum production observability needed to support TocDoc after client deployment.

A delivery engineer should be able to trace every ingestion and QnA request end-to-end, understand latency, identify failures, and explain which retrieval sources contributed to an answer.

## Backlog mapping

- `docs/productization_backlog/09_OBSERVABILITY_Add_telemetry_audit_logs_and_operational_metrics.md`
- `docs/productization_backlog/06_API_Harden_error_contracts_request_validation_and_response_schema.md`
- `docs/productization_backlog/13_QUALITY_Expand_test_strategy_coverage_and_release_gates.md`

## Required principles

- Use structured logs, preferably JSON-compatible dictionaries.
- Every request must have a request/correlation ID.
- Logs must include `bot_tag` where available.
- Never log raw document content, full prompts, full answers, access tokens, or secrets.
- Log enough metadata to debug behavior without leaking sensitive content.

## Request/correlation ID

Add middleware to both services:
- ingestion service
- QnA service

Behavior:
- If request header `X-Request-ID` exists, reuse it.
- Otherwise generate a UUID.
- Add it to response header `X-Request-ID`.
- Make it available to route handlers and service code.
- Include it in every structured log.

Suggested log field:

```json
{
  "request_id": "uuid",
  "service": "qna",
  "event": "request_started",
  "path": "/qna/ask",
  "method": "POST"
}
```

## QnA observability events

### Event: request started

Fields:
- `request_id`
- `service=qna`
- `event=request_started`
- `path`
- `method`
- `bot_tag` if already parsed

### Event: auth result

Fields:
- `request_id`
- `service=qna`
- `event=auth_success` or `auth_failure`
- `failure_type` if failed, such as `missing_token`, `invalid_audience`, `expired_token`, `jwks_unavailable`

Do not log token values.

### Event: query rephrased

Fields:
- `request_id`
- `event=query_rephrased`
- `history_turns_used`
- `latency_ms`

Do not log full conversation history by default.

Optional debug-only field:
- `rephrased_query_preview`, capped at 200 characters.

### Event: retrieval completed

Fields:
- `request_id`
- `event=retrieval_completed`
- `bot_tag`
- `fr_tag`
- `retrieved_chunk_count`
- `top_k`
- `latency_ms`
- `source_document_ids`
- `source_paths`

Do not log full chunk text.

### Event: answer generated

Fields:
- `request_id`
- `event=answer_generated`
- `model`
- `latency_ms`
- `citation_count`
- `answer_length_chars`

Do not log full answer unless explicitly enabled for development.

### Event: request failed

Fields:
- `request_id`
- `event=request_failed`
- `error_class`
- `error_category`
- `http_status`
- `safe_message`

## Ingestion observability events

### Event: ingestion started

Fields:
- `request_id`
- `service=ingestion`
- `event=ingestion_started`
- `bot_tag`
- `fr_mode`
- `source_type`
- `source_path`

### Event: document parsed

Fields:
- `request_id`
- `event=document_parsed`
- `parser=azure_document_intelligence`
- `latency_ms`
- `page_count` if available
- `content_length_chars`

### Event: chunking completed

Fields:
- `request_id`
- `event=chunking_completed`
- `chunk_count`
- `chunking_mode`
- `max_tokens`
- `overlap_tokens`
- `latency_ms`

### Event: embeddings completed

Fields:
- `request_id`
- `event=embeddings_completed`
- `embedding_model`
- `embedding_count`
- `latency_ms`

### Event: index upsert completed

Fields:
- `request_id`
- `event=index_upsert_completed`
- `document_id`
- `bot_tag`
- `chunk_count`
- `deleted_stale_chunks`
- `latency_ms`

### Event: ingestion failed

Fields:
- `request_id`
- `event=ingestion_failed`
- `document_id` if known
- `bot_tag`
- `error_class`
- `error_category`
- `safe_message`
- `stage`

Suggested failure stages:
- `validation`
- `file_read`
- `document_intelligence`
- `chunking`
- `embedding`
- `search_indexing`
- `unknown`

## Metrics to derive from logs

The first implementation can rely on structured logs that Application Insights can query. Do not introduce a large metrics framework unless needed.

Required derived metrics:
- ingestion success count
- ingestion failure count by stage
- average ingestion latency
- QnA request count
- QnA failure count by category
- average retrieval latency
- average answer generation latency
- average retrieved chunks per request

## Error contract alignment

When returning HTTP errors, include request ID:

```json
{
  "error": {
    "code": "SEARCH_INDEX_UNAVAILABLE",
    "message": "Search index is currently unavailable.",
    "request_id": "uuid"
  }
}
```

Do not expose internal stack traces to clients.

## Implementation guidance

Suggested shared utility:

```text
services/common/observability/
  request_context.py
  logging.py
```

If creating a shared package is too much for one PR, duplicate minimal middleware in both services but keep the same field names.

Preferred approach:
- create middleware for request ID
- add helper function `log_event(logger, event_name, **fields)`
- add timers around major stages
- keep logs stdout-compatible for Container Apps / App Insights

## Testing requirements

Add tests for:
- `X-Request-ID` is reused when provided
- request ID is generated when absent
- response includes `X-Request-ID`
- logs include request ID
- retrieval logs do not include full chunk text
- auth failure logs do not include token value
- ingestion failure logs include stage and safe message

## Acceptance criteria

This workstream is accepted when:
- every request has a request ID
- request ID appears in response headers
- ingestion and QnA have structured stage-level logs
- failures are categorized
- no secrets/tokens/raw document content are logged
- deployment docs mention how to query logs in Azure Container Apps / Application Insights

## Non-goals

- Full UI observability dashboard
- Vendor-specific tracing lock-in beyond Azure-native log compatibility
- Complex OpenTelemetry rollout in the first PR
- Logging full prompts, documents, or answers

## Architect note

Observability is not optional anymore. Once TocDoc is deployed in a client environment, debugging blind is the fastest way to lose trust. This workstream should start immediately after or in parallel with read-only admin APIs.

Co-Authored by Maanav's Mac-Air
