import type { ApiError } from "../api/client";

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state-block" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="state-block state-empty" role="status">
      {message}
    </div>
  );
}

/**
 * Surfaces the structured error envelope: code, message, request_id and any
 * per-field validation errors. Includes an optional retry button.
 */
export function ErrorState({
  error,
  onRetry,
}: {
  error: ApiError;
  onRetry?: () => void;
}) {
  return (
    <div className="state-block state-error" role="alert">
      <div className="error-header">
        <strong>{error.code}</strong>
        {error.status > 0 && <span className="error-status">HTTP {error.status}</span>}
      </div>
      <p className="error-message">{error.message}</p>
      {error.fieldErrors && error.fieldErrors.length > 0 && (
        <ul className="error-fields">
          {error.fieldErrors.map((fe, i) => (
            <li key={i}>
              <code>{fe.loc.join(".")}</code>: {fe.msg}
            </li>
          ))}
        </ul>
      )}
      {error.requestId && (
        <p className="error-request-id">
          Request ID: <code>{error.requestId}</code>
        </p>
      )}
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}
