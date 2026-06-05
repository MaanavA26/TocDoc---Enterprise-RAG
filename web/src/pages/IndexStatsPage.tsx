import { useApi } from "../api/ApiContext";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, Spinner } from "../components/StateBlocks";

function CountTable({ title, counts }: { title: string; counts: Record<string, number> }) {
  const entries = Object.entries(counts);
  return (
    <div className="panel">
      <h3>{title}</h3>
      {entries.length === 0 ? (
        <p className="page-hint">No data.</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Count</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([key, val]) => (
              <tr key={key}>
                <td>{key}</td>
                <td>{val}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function IndexStatsPage() {
  const { client, botTag } = useApi();
  const { data, loading, error, reload } = useAsync(
    () => client.getIndexStats(botTag),
    [botTag],
  );

  return (
    <section>
      <div className="page-header">
        <h2>Index Stats</h2>
        <div className="page-actions">
          <span className="scope-badge">
            bot_tag: <code>{botTag}</code>
          </span>
          <button type="button" className="btn" onClick={reload}>
            Refresh
          </button>
        </div>
      </div>

      {loading && <Spinner />}
      {error && <ErrorState error={error} onRetry={reload} />}
      {data && (
        <>
          <div className="stat-cards">
            <div className="stat-card">
              <span className="stat-value">{data.document_count}</span>
              <span className="stat-label">Documents</span>
            </div>
            <div className="stat-card">
              <span className="stat-value">{data.chunk_count}</span>
              <span className="stat-label">Chunks</span>
            </div>
          </div>
          <div className="panel-grid">
            <CountTable title="By source type" counts={data.source_types} />
            <CountTable title="By FR mode" counts={data.fr_modes} />
          </div>
        </>
      )}
    </section>
  );
}
