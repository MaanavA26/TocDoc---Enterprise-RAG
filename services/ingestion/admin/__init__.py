"""Admin API package — read-only endpoints for index management.

This package exposes a FastAPI router (`admin.routes.router`) that provides
operator endpoints for inspecting indexed documents within a `bot_tag` scope.

Wired into `services/ingestion/app.py` via:

    from admin.routes import router as admin_router
    app.include_router(admin_router, prefix="/admin")

PR-1 (this change) ships read-only endpoints only:
    GET /admin/documents
    GET /admin/documents/{document_id}
    GET /admin/index/stats

Destructive endpoints (DELETE document, DELETE bot, POST reindex) are
deferred to a follow-up PR — see docs/architect_phase_2/01_ADMIN_API_SPEC.md.
"""
