// Typed models mirroring services/ingestion/admin/models.py and errors.py.
// Field nullability matches the Pydantic Optional[...] declarations exactly.

export interface DocumentSummary {
  document_id: string;
  source_path: string | null;
  source_type: string | null;
  fr_tag: string | null;
  chunk_count: number;
  first_ingested_at: string | null;
  last_ingested_at: string | null;
}

export interface DocumentListResponse {
  bot_tag: string;
  count: number;
  documents: DocumentSummary[];
}

export interface ChunkSample {
  id: string;
  chunk_index: number | null;
}

export interface DocumentDetailResponse {
  bot_tag: string;
  document_id: string;
  source_path: string | null;
  source_type: string | null;
  fr_tag: string | null;
  chunk_count: number;
  ingestion_timestamps: string[];
  sample_chunks: ChunkSample[];
}

export interface IndexStatsResponse {
  bot_tag: string;
  document_count: number;
  chunk_count: number;
  source_types: Record<string, number>;
  fr_modes: Record<string, number>;
}

export interface DeleteDocumentResponse {
  bot_tag: string;
  document_id: string;
  deleted_chunks: number;
  status: string;
}

export interface DeleteTenantResponse {
  bot_tag: string;
  deleted_chunks: number;
  deleted_documents: number;
  status: string;
}

export interface ConnectorSyncResponse {
  run_id: string;
  source_type: string;
  status: string;
}

export interface ConnectorRunError {
  error_class: string;
  safe_message: string;
}

export type ConnectorRunStatus = "started" | "completed" | "failed";

export interface ConnectorRunStatusResponse {
  run_id: string;
  status: ConnectorRunStatus | string;
  source_type: string;
  bot_tag: string;
  started_at: string | null;
  finished_at: string | null;
  processed_count: number;
  failed_count: number;
  error: ConnectorRunError | null;
}

export interface ConnectorRunListResponse {
  count: number;
  runs: ConnectorRunStatusResponse[];
}

// Source types the trigger endpoint accepts (mirrors _SUPPORTED_SOURCE_TYPES).
export const SUPPORTED_SOURCE_TYPES = ["blob", "sharepoint"] as const;
export type SourceType = (typeof SUPPORTED_SOURCE_TYPES)[number];

// Structured error envelope: { error: { code, message, request_id?, errors? } }
export interface ErrorFieldDetail {
  loc: (string | number)[];
  type: string;
  msg: string;
}

export interface ErrorBody {
  code: string;
  message: string;
  request_id?: string | null;
  errors?: ErrorFieldDetail[] | null;
}

export interface ErrorEnvelope {
  error: ErrorBody;
}
