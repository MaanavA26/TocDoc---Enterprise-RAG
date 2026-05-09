# Phase 2 Decision Record — `bot_tag` Scope, Naming, and Product Role

## Decision needed

TocDoc currently uses `bot_tag` as the scoping field for ingestion and retrieval. The team needs to decide whether to keep it, rename it, or remove it.

## Architect decision

Do not remove `bot_tag` unless an equivalent or stronger isolation primitive replaces it everywhere.

For the next development phase, keep `bot_tag` as the internal scoping field. Treat it as the current tenant/bot/workspace boundary until a cleaner product naming model is introduced.

Recommended future product-facing name:

```text
workspace_id
```

Recommended internal transition path:

1. Keep current `bot_tag` field in Azure Search to avoid breaking existing indexed data.
2. Introduce API alias `workspace_id` in future public APIs.
3. Map `workspace_id` to `bot_tag` internally during transition.
4. Later, during a planned index schema migration, rename the underlying field if worth the cost.

## What is `bot_tag`?

`bot_tag` identifies the logical scope that owns a set of indexed chunks.

In simple terms:

```text
bot_tag = the boundary that says which documents this bot/workspace/user group is allowed to search.
```

Examples:

```text
client_a_hr
client_a_legal
client_b_finance
```

If two teams upload documents into the same Azure Search index, `bot_tag` prevents one team’s QnA request from retrieving another team’s documents.

## Why do we need it?

### 1. Tenant or workspace isolation

Enterprise RAG systems need strict retrieval boundaries. Without a scoping field, all indexed chunks are part of the same retrieval pool.

That creates the risk that:
- HR users retrieve finance documents
- one client retrieves another client’s documents
- a demo bot answers from the wrong dataset
- admin delete operations remove data outside the intended scope

### 2. Safe admin operations

Admin APIs require a scope.

For example:
- list documents for one bot/workspace
- delete one document inside one bot/workspace
- delete all documents for one bot/workspace
- show stats for one bot/workspace

Without `bot_tag` or a replacement, these operations become dangerous.

### 3. Repeatable client deployment

Even in a dedicated client deployment, a single client may have multiple document domains:
- HR policy bot
- legal contract bot
- product manual bot
- finance compliance bot

`bot_tag` allows these to share infrastructure while keeping retrieval separated.

### 4. Future UI and connector routing

Connectors will eventually need to know where documents belong.

Examples:
- SharePoint site A maps to workspace A
- Blob container B maps to workspace B
- manual uploads from admin UI map to selected workspace

`bot_tag` is the current routing key for that ownership model.

## Should we remove it?

No, not now.

Removing `bot_tag` would weaken the product unless replaced by a stronger model such as:
- `tenant_id`
- `workspace_id`
- `bot_id`
- `project_id`

The field name is imperfect, but the capability is essential.

## Should we rename it?

Eventually yes, but not as an urgent Phase 2 blocker.

`bot_tag` sounds implementation-specific. For product APIs and UI, `workspace_id` or `collection_id` is clearer.

Recommended naming:

| Layer | Recommended name | Notes |
|---|---|---|
| Current Azure Search field | `bot_tag` | Keep for backward compatibility |
| Public API request field | `workspace_id` | Add later as alias |
| UI label | Workspace | Product-friendly |
| Documentation explanation | Workspace / bot scope | Helps bridge old and new terminology |

## Required enforcement rules

Any future implementation must enforce the scope in all these places:

### Ingestion

Every indexed chunk must include `bot_tag`.

Reject ingestion requests if scope is missing or invalid.

### Retrieval

Every Azure Search query must filter by `bot_tag`.

A QnA request without a valid scope should fail fast.

### Admin APIs

Every admin operation must be scoped by `bot_tag` or future `workspace_id`.

Destructive operations must never run across all scopes by default.

### Connectors

Every connector mapping must write documents into exactly one scope unless explicitly designed otherwise.

### Observability

Logs should include scope metadata where safe:

```json
{
  "bot_tag": "client_a_hr",
  "request_id": "...",
  "event": "retrieval_completed"
}
```

## Validation rules

Recommended validation regex:

```text
^[A-Za-z0-9_-]{1,128}$
```

Reject values containing:
- quotes
- spaces
- semicolons
- OData operators
- path traversal patterns
- extremely long values

This reduces OData filter injection risk and keeps scope identifiers clean.

## Admin API impact

For Phase 2 Admin APIs:

- require `bot_tag` query parameter for every read operation
- require `bot_tag` for every delete operation
- do not add cross-tenant delete/list operations yet
- do not allow `bot_tag=*`

## Product recommendation

Keep `bot_tag` internally for now.

Add docs explaining it as:

```text
bot_tag is TocDoc’s current workspace/tenant scoping key. It ensures ingestion, retrieval, and admin operations only operate within the intended document collection.
```

Later, introduce `workspace_id` as the external/public name while maintaining backward compatibility with `bot_tag`.

## Open questions for later

1. Should one client deployment support many workspaces?
2. Should workspace membership be tied to Azure AD groups?
3. Should the QnA API infer workspace from user claims instead of accepting it from request payload?
4. Should workspace metadata live in a database instead of only Azure Search fields?
5. Should admin APIs allow super-admin cross-workspace operations?

These are important, but they should not block Phase 2 operability.

## Final decision for developers

For the next PRs:

- Do not remove `bot_tag`.
- Validate it strictly.
- Use it in every admin query and delete operation.
- Include it in observability logs.
- Document it as the current workspace/tenant boundary.
- Do not rename the Azure Search field yet.

Co-Authored by Maanav's Mac-Air
