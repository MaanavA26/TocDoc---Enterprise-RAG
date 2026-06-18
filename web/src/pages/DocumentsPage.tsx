import { useEffect, useState } from "react";
import { useApi } from "../api/ApiContext";
import type { DocumentDetailResponse } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { EmptyState, ErrorState, Spinner } from "../components/StateBlocks";

function DocumentDetail({
  documentId,
  onClose,
}: {
  documentId: string;
  onClose: () => void;
}) {
  const { client, botTag } = useApi();
  const { data, loading, error, reload } = useAsync<DocumentDetailResponse>(
    () => client.getDocument(botTag, documentId),
    [botTag, documentId],
  );

  return (
    <div className="panel detail-panel">
      <div className="panel-header">
        <h3>Document detail</h3>
        <button type="button" className="btn btn-ghost" onClick={onClose}>
          Close
        </button>
      </div>
      {loading && <Spinner />}
      {error && <ErrorState error={error} onRetry={reload} />}
      {data && (
        <dl className="detail-list">
          <dt>Document ID</dt>
          <dd>
            <code>{data.document_id}</code>
          </dd>
          <dt>Source path</dt>
          <dd>{data.source_path ?? "—"}</dd>
          <dt>Source type</dt>
          <dd>{data.source_type ?? "—"}</dd>
          <dt>FR tag</dt>
          <dd>{data.fr_tag ?? "—"}</dd>
          <dt>Chunk count</dt>
          <dd>{data.chunk_count}</dd>
          <dt>Ingestion timestamps</dt>
          <dd>
            {data.ingestion_timestamps.length > 0
              ? data.ingestion_timestamps.join(", ")
              : "—"}
          </dd>
          <dt>Sample chunks</dt>
          <dd>
            {data.sample_chunks.length > 0 ? (
              <ul className="chunk-list">
                {data.sample_chunks.map((c) => (
                  <li key={c.id}>
                    <code>{c.id}</code>
                    {c.chunk_index !== null ? ` (index ${c.chunk_index})` : ""}
                  </li>
                ))}
              </ul>
            ) : (
              "—"
            )}
          </dd>
        </dl>
      )}
    </div>
  );
}

export default function DocumentsPage() {
  const { client, botTag } = useApi();
  const [selected, setSelected] = useState<string | null>(null);

  // Clear the open detail panel when the bot_tag scope changes — otherwise the
  // previously selected document_id immediately refetches under the new scope.
  useEffect(() => {
    setSelected(null);
  }, [botTag]);

  const { data, loading, error, reload } = useAsync(
    () => client.listDocuments(botTag),
    [botTag],
  );

  return (
    <section>
      <div className="page-header">
        <h2>Documents</h2>
        <div className="page-actions">
          <span className="scope-badge">
            bot_tag: <code>{botTag}</code>
          </span>
          <button type="button" className="btn" onClick={reload}>
            Refresh
          </button>
        </div>
      </div>
      <p className="page-hint">
        Listing is scoped to the bot_tag set in the header. Change it to filter a
        different tenant/workspace.
      </p>

      {loading && <Spinner />}
      {error && <ErrorState error={error} onRetry={reload} />}
      {data && data.documents.length === 0 && (
        <EmptyState message={`No documents indexed for bot_tag "${botTag}".`} />
      )}
      {data && data.documents.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Document ID</th>
              <th>Source type</th>
              <th>Chunks</th>
              <th>Last ingested</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {data.documents.map((doc) => (
              <tr key={doc.document_id}>
                <td className="mono-cell">{doc.document_id}</td>
                <td>{doc.source_type ?? "—"}</td>
                <td>{doc.chunk_count}</td>
                <td>{doc.last_ingested_at ?? "—"}</td>
                <td>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => setSelected(doc.document_id)}
                  >
                    View
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selected && (
        <DocumentDetail documentId={selected} onClose={() => setSelected(null)} />
      )}
    </section>
  );
}
