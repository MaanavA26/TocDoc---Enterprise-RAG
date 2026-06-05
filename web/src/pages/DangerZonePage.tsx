import { useState } from "react";
import { ApiError } from "../api/client";
import { useApi } from "../api/ApiContext";
import type { DeleteDocumentResponse, DeleteTenantResponse } from "../api/types";
import { ErrorState } from "../components/StateBlocks";

function asApiError(err: unknown): ApiError {
  return err instanceof ApiError
    ? err
    : new ApiError(0, "UNKNOWN", "Unexpected error", null, null);
}

function DeleteDocumentCard() {
  const { client, botTag } = useApi();
  const [documentId, setDocumentId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<DeleteDocumentResponse | null>(null);

  const handleDelete = async () => {
    if (!documentId.trim()) return;
    const ok = window.confirm(
      `Delete ALL chunks for document "${documentId}" in bot_tag "${botTag}"? This cannot be undone.`,
    );
    if (!ok) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const resp = await client.deleteDocument(botTag, documentId.trim());
      setResult(resp);
    } catch (err) {
      setError(asApiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel danger-card">
      <h3>Delete a document</h3>
      <p className="page-hint">
        Removes every chunk for one document within the current bot_tag scope (
        <code>{botTag}</code>). Idempotent: deleting a non-existent document
        succeeds with <code>deleted_chunks: 0</code>.
      </p>
      <div className="trigger-row">
        <input
          type="text"
          placeholder="document_id"
          value={documentId}
          onChange={(e) => setDocumentId(e.target.value)}
          aria-label="Document ID to delete"
        />
        <button
          type="button"
          className="btn btn-danger"
          onClick={handleDelete}
          disabled={busy || !documentId.trim()}
        >
          {busy ? "Deleting…" : "Delete document"}
        </button>
      </div>
      {result && (
        <p className="success-note">
          Deleted document <code>{result.document_id}</code> —{" "}
          {result.deleted_chunks} chunk(s) removed (status: {result.status}).
        </p>
      )}
      {error && <ErrorState error={error} />}
    </div>
  );
}

function DeleteTenantCard() {
  const { client, botTag } = useApi();
  // Mirror the API's confirm=true guard with an explicit checkbox + typed name.
  const [confirmChecked, setConfirmChecked] = useState(false);
  const [typedTag, setTypedTag] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<DeleteTenantResponse | null>(null);

  const armed = confirmChecked && typedTag.trim() === botTag;

  const handleDelete = async () => {
    if (!armed) return;
    const ok = window.confirm(
      `Delete ALL documents for the ENTIRE bot_tag "${botTag}"? This removes every indexed chunk for this tenant and cannot be undone.`,
    );
    if (!ok) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      // confirm=true mirrors the server-side guard; the server still re-checks.
      const resp = await client.deleteTenant(botTag, true);
      setResult(resp);
      setConfirmChecked(false);
      setTypedTag("");
    } catch (err) {
      setError(asApiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel danger-card">
      <h3>Delete all documents for a bot_tag</h3>
      <p className="page-hint">
        Deletes every document and chunk for bot_tag <code>{botTag}</code>. The
        server requires <code>confirm=true</code>; this UI additionally requires
        you to re-type the bot_tag and tick the confirmation box.
      </p>
      <label className="confirm-input">
        Re-type the bot_tag to confirm:
        <input
          type="text"
          value={typedTag}
          onChange={(e) => setTypedTag(e.target.value)}
          placeholder={botTag}
          aria-label="Re-type bot_tag to confirm tenant deletion"
        />
      </label>
      <label className="checkbox-inline">
        <input
          type="checkbox"
          checked={confirmChecked}
          onChange={(e) => setConfirmChecked(e.target.checked)}
        />
        I understand this permanently deletes all data for this bot_tag.
      </label>
      <div className="trigger-row">
        <button
          type="button"
          className="btn btn-danger"
          onClick={handleDelete}
          disabled={busy || !armed}
        >
          {busy ? "Deleting…" : "Delete entire bot_tag"}
        </button>
      </div>
      {result && (
        <p className="success-note">
          Deleted bot_tag <code>{result.bot_tag}</code> —{" "}
          {result.deleted_documents} document(s), {result.deleted_chunks}{" "}
          chunk(s) removed (status: {result.status}).
        </p>
      )}
      {error && <ErrorState error={error} />}
    </div>
  );
}

export default function DangerZonePage() {
  const { botTag } = useApi();
  return (
    <section>
      <div className="page-header">
        <h2>Danger Zone</h2>
        <span className="scope-badge">
          bot_tag: <code>{botTag}</code>
        </span>
      </div>
      <p className="danger-banner" role="note">
        Destructive operations. All actions are scoped to the current bot_tag and
        cannot be undone.
      </p>
      <DeleteDocumentCard />
      <DeleteTenantCard />
    </section>
  );
}
