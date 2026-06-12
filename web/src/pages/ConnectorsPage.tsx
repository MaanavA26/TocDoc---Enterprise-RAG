import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { useApi } from "../api/ApiContext";
import {
  SUPPORTED_SOURCE_TYPES,
  type ConnectorRunListResponse,
  type SourceType,
} from "../api/types";
import { EmptyState, ErrorState, Spinner } from "../components/StateBlocks";

const POLL_INTERVAL_MS = 4000;

function statusClass(status: string): string {
  if (status === "completed") return "badge badge-ok";
  if (status === "failed") return "badge badge-err";
  return "badge badge-pending";
}

function formatTs(ts: string | null): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString();
}

export default function ConnectorsPage() {
  const { client } = useApi();
  const [sourceType, setSourceType] = useState<SourceType>(SUPPORTED_SOURCE_TYPES[0]);
  const [triggering, setTriggering] = useState(false);
  const [triggerError, setTriggerError] = useState<ApiError | null>(null);
  const [lastTriggered, setLastTriggered] = useState<string | null>(null);

  const [runs, setRuns] = useState<ConnectorRunListResponse | null>(null);
  const [listError, setListError] = useState<ApiError | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const loadedOnce = useRef(false);

  const loadRuns = useCallback(async () => {
    try {
      const data = await client.listConnectorRuns(50);
      setRuns(data);
      setListError(null);
    } catch (err) {
      setListError(
        err instanceof ApiError
          ? err
          : new ApiError(0, "UNKNOWN", "Failed to load runs", null, null),
      );
    } finally {
      if (!loadedOnce.current) {
        loadedOnce.current = true;
        setInitialLoading(false);
      }
    }
  }, [client]);

  // Initial load + live-ish polling while any run is non-terminal.
  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  useEffect(() => {
    if (!autoRefresh) return;
    const hasActive = runs?.runs.some((r) => r.status === "started") ?? false;
    // Poll continuously when a run is active; otherwise a slower keep-fresh tick.
    const interval = hasActive ? POLL_INTERVAL_MS : POLL_INTERVAL_MS * 3;
    const id = setInterval(() => {
      void loadRuns();
    }, interval);
    return () => clearInterval(id);
  }, [autoRefresh, runs, loadRuns]);

  const handleTrigger = useCallback(async () => {
    setTriggering(true);
    setTriggerError(null);
    setLastTriggered(null);
    try {
      const resp = await client.triggerConnectorSync(sourceType);
      setLastTriggered(resp.run_id);
      await loadRuns();
    } catch (err) {
      setTriggerError(
        err instanceof ApiError
          ? err
          : new ApiError(0, "UNKNOWN", "Failed to trigger sync", null, null),
      );
    } finally {
      setTriggering(false);
    }
  }, [client, sourceType, loadRuns]);

  return (
    <section>
      <div className="page-header">
        <h2>Connectors</h2>
        <label className="checkbox-inline">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
          />
          Auto-refresh
        </label>
      </div>

      <div className="panel">
        <h3>Trigger a sync</h3>
        <p className="page-hint">
          The target bot_tag and source location are bound to the connector's
          server-side env config — the dashboard only selects the source type.
        </p>
        <div className="trigger-row">
          <select
            value={sourceType}
            onChange={(e) => setSourceType(e.target.value as SourceType)}
            aria-label="Source type"
          >
            {SUPPORTED_SOURCE_TYPES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleTrigger}
            disabled={triggering}
          >
            {triggering ? "Triggering…" : "Trigger sync"}
          </button>
        </div>
        {lastTriggered && (
          <p className="success-note">
            Sync accepted. Run ID: <code>{lastTriggered}</code>
          </p>
        )}
        {triggerError && <ErrorState error={triggerError} />}
      </div>

      <div className="panel">
        <div className="panel-header">
          <h3>Recent runs</h3>
          <button type="button" className="btn btn-ghost" onClick={() => void loadRuns()}>
            Refresh now
          </button>
        </div>
        {initialLoading && <Spinner />}
        {listError && <ErrorState error={listError} onRetry={() => void loadRuns()} />}
        {!initialLoading && runs && runs.runs.length === 0 && (
          <EmptyState message="No connector runs yet (state is in-process and resets on service restart)." />
        )}
        {runs && runs.runs.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>Run ID</th>
                <th>Source</th>
                <th>bot_tag</th>
                <th>Status</th>
                <th>Processed</th>
                <th>Failed</th>
                <th>Started</th>
                <th>Finished</th>
              </tr>
            </thead>
            <tbody>
              {runs.runs.map((run) => (
                <tr key={run.run_id}>
                  <td className="mono-cell">{run.run_id}</td>
                  <td>{run.source_type}</td>
                  <td>{run.bot_tag}</td>
                  <td>
                    <span className={statusClass(run.status)}>{run.status}</span>
                    {run.error && (
                      <div className="run-error" title={run.error.error_class}>
                        {run.error.safe_message}
                      </div>
                    )}
                  </td>
                  <td>{run.processed_count}</td>
                  <td>{run.failed_count}</td>
                  <td>{formatTs(run.started_at)}</td>
                  <td>{formatTs(run.finished_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
