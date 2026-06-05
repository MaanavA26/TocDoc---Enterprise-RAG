import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";
import { ApiError } from "../api/client";

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: ApiError | null;
}

function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err;
  return new ApiError(
    0,
    "UNKNOWN",
    err instanceof Error ? err.message : "Unexpected error",
    null,
    null,
  );
}

/**
 * Run an async loader and expose {data, loading, error}. The loader is
 * re-run whenever `deps` change (and on mount unless `enabled` is false).
 * A ref guards against setting state after unmount / superseded calls.
 */
export function useAsync<T>(
  loader: () => Promise<T>,
  deps: DependencyList,
  enabled = true,
): AsyncState<T> & { reload: () => void } {
  const [state, setState] = useState<AsyncState<T>>({
    data: null,
    loading: false,
    error: null,
  });
  const callIdRef = useRef(0);

  const run = useCallback(() => {
    if (!enabled) return;
    const callId = ++callIdRef.current;
    setState((s) => ({ ...s, loading: true, error: null }));
    loader()
      .then((data) => {
        if (callIdRef.current === callId) {
          setState({ data, loading: false, error: null });
        }
      })
      .catch((err: unknown) => {
        if (callIdRef.current === callId) {
          setState({ data: null, loading: false, error: toApiError(err) });
        }
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps.concat(enabled));

  useEffect(() => {
    run();
    return () => {
      // Invalidate in-flight call on unmount / dep change.
      callIdRef.current++;
    };
  }, [run]);

  return { ...state, reload: run };
}
