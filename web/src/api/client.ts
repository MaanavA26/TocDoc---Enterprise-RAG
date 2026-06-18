// Typed fetch wrapper for the TocDoc ingestion admin API.
//
// Every request injects the configured base URL and the X-Admin-Token header.
// On a non-2xx response the structured error envelope
// `{ error: { code, message, request_id, errors } }` is parsed into a typed
// `ApiError`. Network failures and non-JSON bodies degrade gracefully.
//
// Base-URL contract: `baseUrl` points at the ingestion service ROOT
// (e.g. http://localhost:8000). This client appends the `/admin` prefix and
// the route path. Trailing slashes on baseUrl are tolerated.

import type {
  ConnectorRunListResponse,
  ConnectorRunStatusResponse,
  ConnectorSyncResponse,
  DeleteDocumentResponse,
  DeleteTenantResponse,
  DocumentDetailResponse,
  DocumentListResponse,
  ErrorBody,
  ErrorEnvelope,
  IndexStatsResponse,
  SourceType,
} from "./types";

export interface ApiClientConfig {
  baseUrl: string;
  adminToken: string;
}

/** A typed error carrying the parsed envelope fields when available. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly fieldErrors: ErrorBody["errors"];

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string | null,
    fieldErrors: ErrorBody["errors"],
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.fieldErrors = fieldErrors;
  }
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== "object" || value === null) return false;
  const err = (value as { error?: unknown }).error;
  return (
    typeof err === "object" &&
    err !== null &&
    typeof (err as { code?: unknown }).code === "string" &&
    typeof (err as { message?: unknown }).message === "string"
  );
}

function normalizeBaseUrl(baseUrl: string): string {
  return baseUrl.replace(/\/+$/, "");
}

export class AdminApiClient {
  private readonly baseUrl: string;
  private readonly adminToken: string;
  private readonly fetchImpl: typeof fetch;

  constructor(config: ApiClientConfig, fetchImpl: typeof fetch = fetch) {
    this.baseUrl = normalizeBaseUrl(config.baseUrl);
    this.adminToken = config.adminToken;
    // Bind so a bare `fetch` reference doesn't lose its `this` (Illegal invocation).
    this.fetchImpl = fetchImpl.bind(globalThis);
  }

  private async request<T>(
    method: string,
    path: string,
    query?: Record<string, string | number | boolean | undefined>,
  ): Promise<T> {
    const url = new URL(`${this.baseUrl}/admin${path}`);
    if (query) {
      for (const [key, val] of Object.entries(query)) {
        if (val !== undefined) url.searchParams.set(key, String(val));
      }
    }

    let resp: Response;
    try {
      resp = await this.fetchImpl(url.toString(), {
        method,
        headers: {
          "X-Admin-Token": this.adminToken,
          Accept: "application/json",
        },
      });
    } catch (cause) {
      // Network-level failure (DNS, CORS, offline). No envelope to parse.
      throw new ApiError(
        0,
        "NETWORK_ERROR",
        cause instanceof Error && cause.message
          ? `Network request failed: ${cause.message}`
          : "Network request failed. Check the API base URL and that the service is reachable.",
        null,
        null,
      );
    }

    const requestIdHeader = resp.headers.get("X-Request-ID");

    if (resp.status === 204) {
      return undefined as T;
    }

    let parsed: unknown = null;
    const text = await resp.text();
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = null;
      }
    }

    if (!resp.ok) {
      if (isErrorEnvelope(parsed)) {
        const body = parsed.error;
        throw new ApiError(
          resp.status,
          body.code,
          body.message,
          body.request_id ?? requestIdHeader,
          body.errors ?? null,
        );
      }
      // Non-enveloped error body (should not happen against this API, but be safe).
      throw new ApiError(
        resp.status,
        "HTTP_ERROR",
        `Request failed with status ${resp.status}`,
        requestIdHeader,
        null,
      );
    }

    return parsed as T;
  }

  // ----- Documents -----

  listDocuments(botTag: string): Promise<DocumentListResponse> {
    return this.request<DocumentListResponse>("GET", "/documents", {
      bot_tag: botTag,
    });
  }

  getDocument(botTag: string, documentId: string): Promise<DocumentDetailResponse> {
    return this.request<DocumentDetailResponse>(
      "GET",
      `/documents/${encodeURIComponent(documentId)}`,
      { bot_tag: botTag },
    );
  }

  // ----- Index stats -----

  getIndexStats(botTag: string): Promise<IndexStatsResponse> {
    return this.request<IndexStatsResponse>("GET", "/index/stats", {
      bot_tag: botTag,
    });
  }

  // ----- Connectors -----

  triggerConnectorSync(sourceType: SourceType): Promise<ConnectorSyncResponse> {
    return this.request<ConnectorSyncResponse>(
      "POST",
      `/connectors/${encodeURIComponent(sourceType)}/sync`,
    );
  }

  listConnectorRuns(limit = 50): Promise<ConnectorRunListResponse> {
    return this.request<ConnectorRunListResponse>("GET", "/connectors/runs", {
      limit,
    });
  }

  getConnectorRun(runId: string): Promise<ConnectorRunStatusResponse> {
    return this.request<ConnectorRunStatusResponse>(
      "GET",
      `/connectors/runs/${encodeURIComponent(runId)}`,
    );
  }

  // ----- Danger zone (destructive) -----

  deleteDocument(botTag: string, documentId: string): Promise<DeleteDocumentResponse> {
    return this.request<DeleteDocumentResponse>(
      "DELETE",
      `/documents/${encodeURIComponent(documentId)}`,
      { bot_tag: botTag },
    );
  }

  deleteTenant(botTag: string, confirm: boolean): Promise<DeleteTenantResponse> {
    return this.request<DeleteTenantResponse>(
      "DELETE",
      `/bots/${encodeURIComponent(botTag)}/documents`,
      { confirm },
    );
  }
}
