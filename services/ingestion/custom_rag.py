import hashlib
import logging
import os
import sys
import time
from datetime import datetime
from types import SimpleNamespace

import fitz
import tiktoken
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from dotenv import load_dotenv
from langchain_community.document_loaders import AzureAIDocumentIntelligenceLoader
from langchain_text_splitters import MarkdownHeaderTextSplitter
from loaders import PDF_EXTENSION, extract_text, get_extension
from observability import log_event

load_dotenv()

# Read-mode token-chunking parameters. Hoisted to module constants so the
# `chunking_completed` stage event can report them without magic numbers
# drifting from the call site.
_READ_MAX_TOKENS = 500
_READ_OVERLAP_TOKENS = 50

# Azure Cognitive Search caps a single indexing batch at 1000 actions, so
# delete_by_source_path deletes in batches no larger than this. The id list
# itself is unbounded — paginated above the 1000 per-page read cap. The same
# cap bounds the merge_or_upload batch size (H3).
_DELETE_BATCH_SIZE = 1000


def _escape_odata(value: str) -> str:
    """Escape a string literal for an OData filter (single quote → doubled).

    This is the SINK-side injection defense (C1). `bot_tag` is also validated at
    the /upload route against a strict pattern, but escaping here too is
    defense-in-depth and protects every other caller (connectors,
    delete_by_source_path) regardless of upstream validation. Mirrors
    `SearchAdminService._escape_odata`.
    """
    return value.replace("'", "''")


def _document_tag_filter(document_id: str, bot_tag: str) -> str:
    """Build the OData filter selecting all chunks for one (document_id, bot_tag).

    Both values are OData-escaped at this single chokepoint so no caller can
    interpolate an unescaped quote into the filter that drives delete_documents
    (C1). Deliberately omits `fr_mode` so a re-upload under a different mode can
    clear every prior mode's chunks (M3 / L-Ing1 prune).
    """
    return f"document_id eq '{_escape_odata(document_id)}' and bot_tag eq '{_escape_odata(bot_tag)}'"


# Logging handlers — stdout always, file logging only if LOG_FILE is set
log_handlers = [logging.StreamHandler(sys.stdout)]
log_file = os.getenv("LOG_FILE")  # Not set in containers; set locally if desired
if log_file:
    log_handlers.append(logging.FileHandler(log_file))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=log_handlers,
)

logger = logging.getLogger(__name__)


class rag:
    def __init__(self):
        logger.info("RAG instance initialized")
        self.embedding_client = None
        self.search_client = None

    async def chunk_token(self, content):
        logger.debug(f"Calculating token count for content of length: {len(content)} characters")
        try:
            encoding = tiktoken.get_encoding("cl100k_base")  # for GPT-4, GPT-3.5-turbo
            tokens = encoding.encode(content)
            token_count = len(tokens)
            logger.debug(f"Token count calculated: {token_count} tokens")
            return token_count
        except Exception as e:
            logger.error(f"Error calculating token count: {str(e)}", exc_info=True)
            raise

    async def get_embedding(self, txt):
        logger.debug(f"Generating embedding for text of length: {len(txt)} characters")
        try:
            from langchain_openai import AzureOpenAIEmbeddings

            if not self.embedding_client:
                logger.info("Initializing Azure OpenAI Embeddings client")
                self.embedding_client = AzureOpenAIEmbeddings(
                    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                    api_key=os.getenv("AZURE_OPENAI_KEY"),
                    api_version=os.getenv("AZURE_OPENAI_VERSION"),
                    model=os.getenv("AZURE_OPENAI_EMBEDDING_MODEL"),
                )
                logger.info("Azure OpenAI Embeddings client initialized successfully")

            start_time = time.time()
            embedding = self.embedding_client.embed_query(txt)
            end_time = time.time()

            logger.debug(f"Embedding generated successfully in {end_time - start_time:.2f} seconds")
            logger.debug(f"Embedding dimensions: {len(embedding)}")

            return embedding

        except Exception as e:
            logger.error(f"Error generating embedding: {str(e)}", exc_info=True)
            raise

    async def create_search_index(self):
        # M6: memoize the SearchClient. __init__ sets self.search_client = None;
        # build it once (running the index-existence check / creation a single
        # time) and reuse it across every upload()/delete call so the underlying
        # HTTP transport / connection pool is not rebuilt-and-leaked per request.
        if self.search_client is not None:
            return self.search_client

        index_name = os.getenv("INDEX_NAME")

        logger.info(f"Creating or retrieving search index: {index_name}")

        try:
            logger.info("Initializing Azure Search Index client")
            index_client = SearchIndexClient(
                endpoint=os.getenv("AZURE_SEARCH_ENDPOINT"),
                credential=AzureKeyCredential(os.getenv("AZURE_SEARCH_KEY")),
            )
            logger.info("Azure Search Index client initialized successfully")

            logger.info("Checking for existing indexes")
            existing_indexes = [idx.name for idx in index_client.list_indexes()]
            logger.info(f"Found {len(existing_indexes)} existing indexes")

            if index_name in existing_indexes:
                logger.info(f"Index '{index_name}' already exists. Skipping creation.")
                search_client = SearchClient(
                    endpoint=os.getenv("AZURE_SEARCH_ENDPOINT"),
                    index_name=index_name,
                    credential=AzureKeyCredential(os.getenv("AZURE_SEARCH_KEY")),
                )
                logger.info("Search client initialized for existing index")
                self.search_client = search_client
                return search_client

            logger.info(f"Creating new index: {index_name}")
            embedding_dimensions = 1536
            logger.debug(f"Embedding dimensions set to: {embedding_dimensions}")

            # Define fields
            logger.debug("Defining search index fields")
            fields = [
                SimpleField(
                    name="id",
                    type=SearchFieldDataType.String,
                    key=True,
                    filterable=True,
                ),
                SimpleField(
                    name="bot_tag",
                    type=SearchFieldDataType.String,
                    filterable=True,
                ),
                SimpleField(
                    name="fr_tag",
                    type=SearchFieldDataType.String,
                    filterable=True,
                ),
                SearchableField(
                    name="filename",
                    type=SearchFieldDataType.String,
                    searchable=True,
                    filterable=True,
                ),
                SearchableField(
                    name="filepath",
                    type=SearchFieldDataType.String,
                    searchable=True,
                ),
                SearchableField(
                    name="chunk_size",
                    type=SearchFieldDataType.String,
                    searchable=False,
                ),
                SearchableField(
                    name="page_number",
                    type=SearchFieldDataType.String,
                    searchable=True,
                ),
                SearchableField(
                    name="section_header",
                    type=SearchFieldDataType.String,
                    searchable=True,
                    filterable=True,
                ),
                SimpleField(
                    name="sub_section",
                    type=SearchFieldDataType.String,
                    filterable=True,
                ),
                SimpleField(
                    name="source",
                    type=SearchFieldDataType.String,
                    filterable=True,
                ),
                SimpleField(
                    name="document_id",
                    type=SearchFieldDataType.String,
                    filterable=True,
                ),
                SimpleField(
                    name="ingestion_timestamp",
                    type=SearchFieldDataType.String,
                    filterable=False,
                ),
                SimpleField(
                    name="source_type",
                    type=SearchFieldDataType.String,
                    filterable=True,
                ),
                SearchableField(
                    name="source_path",
                    type=SearchFieldDataType.String,
                    searchable=False,
                ),
                SearchableField(
                    name="content",
                    type=SearchFieldDataType.String,
                    searchable=True,
                    retrievable=True,
                ),
                SearchField(
                    name="content_vector",
                    type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True,
                    retrievable=True,
                    vector_search_dimensions=embedding_dimensions,
                    vector_search_profile_name="myHnswProfile",
                ),
            ]
            logger.debug(f"Defined {len(fields)} fields for the index")

            # Configure vector search
            logger.debug("Configuring vector search settings")
            vector_search = VectorSearch(
                profiles=[
                    VectorSearchProfile(
                        name="myHnswProfile",
                        algorithm_configuration_name="myHnsw",
                    )
                ],
                algorithms=[HnswAlgorithmConfiguration(name="myHnsw")],
            )

            # Configure semantic search
            logger.debug("Configuring semantic search settings")
            semantic_search = SemanticSearch(
                configurations=[
                    SemanticConfiguration(
                        name="mySemanticConfig",
                        prioritized_fields=SemanticPrioritizedFields(
                            title_field=SemanticField(field_name="section_header"),
                            content_fields=[SemanticField(field_name="content")],
                            keywords_fields=[
                                SemanticField(field_name="filename"),
                                SemanticField(field_name="page_number"),
                            ],
                        ),
                    )
                ]
            )

            # Create the search index
            logger.info("Creating search index with vector and semantic search configurations")
            index = SearchIndex(
                name=index_name, fields=fields, vector_search=vector_search, semantic_search=semantic_search
            )

            # Create the index
            start_time = time.time()
            index_client.create_index(index)
            end_time = time.time()

            logger.info(f"Index '{index_name}' created successfully in {end_time - start_time:.2f} seconds")

            # Return search client for the created index
            search_client = SearchClient(
                endpoint=os.getenv("AZURE_SEARCH_ENDPOINT"),
                index_name=index_name,
                credential=AzureKeyCredential(os.getenv("AZURE_SEARCH_KEY")),
            )
            logger.info("Search client initialized for new index")

            self.search_client = search_client
            return search_client

        except Exception as e:
            logger.error(f"Error creating search index: {str(e)}", exc_info=True)
            raise

    async def upload(self, file, tag, fr_mode, file_path, source_type="upload", request_id=None):
        # `source_type` is the provenance stamp written to every chunk
        # (and the ingestion_started event). It defaults to "upload" so the
        # /upload route and folder-batch path keep their exact prior behavior;
        # connectors pass "blob"/"sharepoint". The connector layer NEVER mints
        # chunk IDs or writes the index — it only feeds bytes into upload(),
        # so P0-4 deterministic IDs and P0-5 chunking stay enforced in one place.
        logger.info(f"Starting upload process - tag: {tag}, fr_mode: {fr_mode}, file_path: {file_path}")

        # `stage` tracks where we are in the pipeline so a failure event can
        # report the precise phase that broke. `document_id` may not be known
        # yet when an early-stage failure occurs.
        stage = "validation"
        document_id = None

        log_event(
            logger,
            "ingestion_started",
            request_id=request_id,
            service="ingestion",
            bot_tag=tag,
            fr_mode=fr_mode,
            source_type=source_type,
            source_path=file_path,
        )

        try:
            filename = file.filename
            logger.info(f"Processing file: {filename}")

            # Read file content
            stage = "file_read"
            logger.debug("Reading file content")
            file_content = await file.read()
            logger.info(f"File content read successfully - size: {len(file_content)} bytes")

            document_id = hashlib.sha256(file_content).hexdigest()[:16]
            logger.info(f"Document ID (content hash): {document_id}")

            # Dispatch by file extension. PDF keeps its exact PyMuPDF + Azure
            # Document Intelligence path (below); every other supported format is
            # routed through the pluggable loader registry, which extracts PLAIN
            # TEXT only. Both paths converge on `docs_string` / `total_pages` and
            # feed the SAME chunk → embed → index pipeline, so deterministic chunk
            # IDs (P0-4) and chunking (P0-5) stay untouched. An unknown extension
            # is a clean 4xx upstream; here it raises UnsupportedFormatError from
            # the registry as a defense-in-depth backstop.
            extension = get_extension(filename)
            is_pdf = extension == PDF_EXTENSION

            if is_pdf:
                # Get total pages using fitz (PDF path — unchanged).
                logger.debug("Analyzing PDF structure with fitz")
                doc = fitz.open(stream=file_content, filetype="pdf")
                total_pages = doc.page_count
                doc.close()
                logger.info(f"PDF analysis complete - total pages: {total_pages}")
            else:
                # Non-PDF: page/slide count comes from the registry loader below.
                total_pages = 0

            # Initialize search client
            logger.info("Initializing search client")
            search_client = await self.create_search_index()

            # Enumerate prior chunks for this document+tenant so we can prune
            # stale ones AFTER a successful upsert (L-Ing1: upsert-then-prune
            # avoids the transient zero-chunk window the old delete-then-rebuild
            # had). We only READ here; the delete happens post-upsert below.
            # M3: drain ALL matching ids via `.by_page()` (no `top` cap) so a
            # document that previously produced >1000 chunks — e.g. under a
            # different fr_mode — is fully cleared, not orphaned. The filter
            # omits fr_mode deliberately so re-uploading under read↔layout
            # clears the other mode's chunks too.
            stale_ids: list[str] = []
            try:
                stale_ids = self._drain_chunk_ids(search_client, _document_tag_filter(document_id, tag))
                logger.info(
                    f"Found {len(stale_ids)} prior chunk(s) for "
                    f"document_id={document_id!r}, bot_tag={tag!r} (pruned after upsert)"
                )
            except Exception as cleanup_err:
                # Enumeration is best-effort: a failure here must not block the
                # upsert. Log the exception CLASS only (never str(e), which may
                # carry index/path detail).
                logger.warning(f"Stale chunk enumeration failed (non-fatal): {type(cleanup_err).__name__}")

            # Load document text. PDF → Azure Document Intelligence (unchanged);
            # other supported formats → the loader registry (plain-text extract).
            if is_pdf:
                stage = "document_intelligence"
                logger.info(f"Loading document with Azure AI Document Intelligence - mode: {fr_mode}")
                start_time = time.time()

                loader = AzureAIDocumentIntelligenceLoader(
                    bytes_source=file_content,
                    api_key=os.getenv("DOC_INTELLIGENCE_KEY"),
                    api_endpoint=os.getenv("DOC_INTELLIGENCE_ENDPOINT"),
                    api_model=f"prebuilt-{fr_mode}",
                )

                docs = loader.load()
                end_time = time.time()
                _parser_name = "azure_document_intelligence"
            else:
                # Registry path: extract plain text, then wrap it in the minimal
                # shim the chunking branches read (`docs[0].page_content`). A
                # SimpleNamespace is enough — only `.page_content` is consumed.
                stage = "text_extraction"
                logger.info(f"Extracting text via loader registry - extension: {extension}")
                start_time = time.time()

                extracted = extract_text(filename, file_content)
                docs = [SimpleNamespace(page_content=extracted.text)]
                total_pages = extracted.page_count
                end_time = time.time()
                _parser_name = extracted.parser

            logger.info(f"Document loaded successfully in {end_time - start_time:.2f} seconds")
            logger.info(f"Loaded {len(docs)} document(s)")

            _content_length_chars = sum(len(d.page_content or "") for d in docs)
            if docs:
                logger.debug(f"First document content length: {len(docs[0].page_content)} characters")

            log_event(
                logger,
                "document_parsed",
                request_id=request_id,
                parser=_parser_name,
                latency_ms=round((end_time - start_time) * 1000, 2),
                page_count=total_pages,
                content_length_chars=_content_length_chars,
            )

            # Prepare documents for upload
            stage = "chunking"
            _chunking_start = time.time()
            azure_docs = []
            token_list = []
            # Defaults reported for the chunking event; refined per mode below.
            _chunking_mode = fr_mode
            _chunk_max_tokens = None
            _chunk_overlap_tokens = None

            # Cumulative wall-clock spent in embedding calls. Chunking and
            # embedding are interleaved per-chunk in this pipeline, so we time
            # them separately by accumulating embedding latency and subtracting
            # it from the chunking-stage total below.
            _embedding_latency_ms = 0.0
            _embedding_count = 0

            if fr_mode == "layout":
                logger.info("Processing document in layout mode with header-based splitting")
                _chunking_mode = "markdown_header"

                # Use header-based splitting for layout mode
                headers_to_split_on = [
                    ("#", "Header_1"),
                    ("##", "Header_2"),
                    ("###", "Header_3"),
                ]

                logger.debug(f"Headers to split on: {headers_to_split_on}")
                text_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on)
                docs_string = docs[0].page_content

                logger.debug("Splitting text by headers")
                start_time = time.time()
                splits = text_splitter.split_text(docs_string)
                end_time = time.time()

                logger.info(f"Text split into {len(splits)} chunks in {end_time - start_time:.2f} seconds")

                for i, chunk in enumerate(splits):
                    logger.debug(f"Processing chunk {i + 1}/{len(splits)}")
                    content = chunk.page_content

                    if not content.strip():
                        logger.debug(f"Skipping empty chunk {i + 1}")
                        continue

                    section_name = chunk.metadata.get("Header_1", "")
                    logger.debug(f"Chunk {i + 1} - section: {section_name}, content length: {len(content)}")

                    # Get embedding for content
                    stage = "embedding"
                    logger.debug(f"Generating embedding for chunk {i + 1}")
                    _emb_start = time.perf_counter()
                    content_vector = await self.get_embedding(content)
                    _embedding_latency_ms += (time.perf_counter() - _emb_start) * 1000
                    _embedding_count += 1
                    stage = "chunking"

                    # Calculate token count
                    token_count = await self.chunk_token(content)
                    token_list.append(token_count)

                    azure_docs.append(
                        {
                            "id": f"{tag}_{document_id}_{fr_mode}_{i:05d}",
                            "bot_tag": tag,
                            "fr_tag": f"fr_{fr_mode}",
                            "filename": filename,
                            "filepath": file_path,
                            "chunk_size": str(token_count),
                            "section_header": section_name,
                            "sub_section": chunk.metadata.get("Header_2", ""),
                            "source": filename,
                            "content": content,
                            "content_vector": content_vector,
                            "document_id": document_id,
                            "ingestion_timestamp": datetime.utcnow().isoformat() + "Z",
                            "source_type": source_type,
                            "source_path": file_path,
                        }
                    )

                    logger.debug(f"Chunk {i + 1} processed successfully")

            elif fr_mode == "read":
                logger.info("Processing document in read mode with token-based splitting")
                _chunking_mode = "token"
                _chunk_max_tokens = _READ_MAX_TOKENS
                _chunk_overlap_tokens = _READ_OVERLAP_TOKENS

                # Use token-based splitting for read mode
                docs_string = docs[0].page_content
                logger.debug(f"Document content length: {len(docs_string)} characters")

                logger.debug("Splitting text by tokens")
                start_time = time.time()
                chunks = await self._chunk_text_by_tokens(
                    docs_string, max_tokens=_READ_MAX_TOKENS, overlap=_READ_OVERLAP_TOKENS
                )
                end_time = time.time()

                logger.info(f"Text split into {len(chunks)} chunks in {end_time - start_time:.2f} seconds")

                for i, chunk in enumerate(chunks):
                    logger.debug(f"Processing chunk {i + 1}/{len(chunks)}")
                    content = chunk.strip()

                    if not content:
                        logger.debug(f"Skipping empty chunk {i + 1}")
                        continue

                    logger.debug(f"Chunk {i + 1} content length: {len(content)} characters")

                    # Get embedding for content
                    stage = "embedding"
                    logger.debug(f"Generating embedding for chunk {i + 1}")
                    _emb_start = time.perf_counter()
                    content_vector = await self.get_embedding(content)
                    _embedding_latency_ms += (time.perf_counter() - _emb_start) * 1000
                    _embedding_count += 1
                    stage = "chunking"

                    # Calculate token count
                    token_count = await self.chunk_token(content)
                    token_list.append(token_count)

                    azure_docs.append(
                        {
                            "id": f"{tag}_{document_id}_{fr_mode}_{i:05d}",
                            "bot_tag": tag,
                            "fr_tag": f"fr_{fr_mode}",
                            "filename": filename,
                            "filepath": file_path,
                            "chunk_size": str(token_count),
                            "section_header": "",  # Empty for read mode
                            "sub_section": "",  # Empty for read mode
                            "source": filename,
                            "content": content,
                            "content_vector": content_vector,
                            "document_id": document_id,
                            "ingestion_timestamp": datetime.utcnow().isoformat() + "Z",
                            "source_type": source_type,
                            "source_path": file_path,
                        }
                    )

                    logger.debug(f"Chunk {i + 1} processed successfully")

            # Stage events: chunking + embedding completed. Chunking latency is
            # the total chunk-loop wall time minus the embedding wall time
            # (the two are interleaved), floored at 0 for safety.
            _chunking_total_ms = (time.time() - _chunking_start) * 1000
            _chunking_only_ms = max(0.0, _chunking_total_ms - _embedding_latency_ms)
            log_event(
                logger,
                "chunking_completed",
                request_id=request_id,
                chunk_count=len(azure_docs),
                chunking_mode=_chunking_mode,
                max_tokens=_chunk_max_tokens,
                overlap_tokens=_chunk_overlap_tokens,
                latency_ms=round(_chunking_only_ms, 2),
            )
            log_event(
                logger,
                "embeddings_completed",
                request_id=request_id,
                embedding_model=os.getenv("AZURE_OPENAI_EMBEDDING_MODEL"),
                embedding_count=_embedding_count,
                latency_ms=round(_embedding_latency_ms, 2),
            )

            # Upload documents to the index
            if azure_docs:
                stage = "search_indexing"
                logger.info(f"Uploading {len(azure_docs)} documents to search index")
                start_time = time.time()

                # H3: batch at the 1000-action cap and inspect every
                # IndexingResult. Azure Search returns 207 on partial success
                # WITHOUT raising — failed chunks must be surfaced, not reported
                # as success.
                failed_keys = self._upsert_in_batches(search_client, azure_docs)
                end_time = time.time()

                logger.info(f"Documents uploaded in {end_time - start_time:.2f} seconds")
                logger.debug(f"Upsert failed_keys: {len(failed_keys)}")

                # L-Ing1: prune stale chunks ONLY after a (here, attempted)
                # upsert, and only the ids that the fresh write did NOT just
                # (re)create. Deterministic IDs mean an identical-content
                # re-upload overwrites in place; the set difference deletes only
                # the genuine residue (e.g. a previous fr_mode's chunks, or the
                # tail of a now-shorter document) — never the chunks we just
                # wrote, and never leaving a zero-chunk window.
                new_ids = {doc["id"] for doc in azure_docs}
                to_delete = [cid for cid in stale_ids if cid not in new_ids]
                deleted_stale_chunks = 0
                if to_delete:
                    try:
                        for d_start in range(0, len(to_delete), _DELETE_BATCH_SIZE):
                            d_batch = to_delete[d_start : d_start + _DELETE_BATCH_SIZE]
                            search_client.delete_documents(documents=[{"id": cid} for cid in d_batch])
                        deleted_stale_chunks = len(to_delete)
                        logger.info(
                            f"Pruned {deleted_stale_chunks} stale chunk(s) for "
                            f"document_id={document_id!r}, bot_tag={tag!r}"
                        )
                    except Exception as prune_err:
                        # Pruning residue is non-fatal: the new content is
                        # already indexed; orphans are self-healed on re-run.
                        logger.warning(f"Stale chunk prune failed (non-fatal): {type(prune_err).__name__}")

                # H3: if any chunk failed to index, report a DEGRADED status with
                # the failed keys — never claim "successful" for a partial write.
                upsert_status = "successful" if not failed_keys else "degraded"

                log_event(
                    logger,
                    "index_upsert_completed",
                    request_id=request_id,
                    level=logging.ERROR if failed_keys else logging.INFO,
                    document_id=document_id,
                    bot_tag=tag,
                    chunk_count=len(azure_docs),
                    failed_chunks=len(failed_keys),
                    failed_keys=failed_keys if failed_keys else None,
                    status=upsert_status,
                    deleted_stale_chunks=deleted_stale_chunks,
                    latency_ms=round((end_time - start_time) * 1000, 2),
                )

                # Calculate character statistics for each chunk
                char_list = [len(doc["content"]) for doc in azure_docs]

                # Create summary statistics
                stats = {
                    "status": upsert_status,
                    "filename": filename,
                    "total_pages": total_pages,
                    "total_chunks": len(azure_docs),
                    "failed_chunks": len(failed_keys),
                    "failed_keys": failed_keys,
                    "max_token": max(token_list),
                    "min_token": min(token_list),
                    "avg_token": sum(token_list) / len(token_list),
                    "max_character": max(char_list),
                    "min_character": min(char_list),
                    "avg_character": sum(char_list) / len(char_list),
                }

                logger.info(f"Upload completed - status={upsert_status} - stats: {stats}")
                return stats
            else:
                # L-Ing1: an empty parse must NOT prune the prior chunks — that
                # would zero out a document on a transient bad parse. Leave the
                # existing chunks in place (self-healing on a good re-run).
                logger.warning("No documents to upload; leaving existing chunks intact")
                return "No documents to upload"

        except Exception as e:
            logger.error(f"Error in upload process: {str(e)}", exc_info=True)
            # Structured failure event. `stage` pinpoints the failing phase.
            # safe_message is the exception CLASS only — NOT str(e), which may
            # carry document paths or content. The exception is re-raised so
            # the route's error envelope (P0-6) still owns the HTTP response.
            log_event(
                logger,
                "ingestion_failed",
                request_id=request_id,
                level=logging.ERROR,
                document_id=document_id,
                bot_tag=tag,
                error_class=type(e).__name__,
                error_category="ingestion_error",
                safe_message=f"Ingestion failed during {stage} stage",
                stage=stage,
            )
            raise

    async def delete_by_source_path(self, source_path: str, bot_tag: str) -> int:
        """Delete every chunk indexed at one (source_path, bot_tag).

        This is the **edited-file cleanup** mechanism (ADR section B), distinct
        from the document_id-keyed stale-delete inside upload() (section A).
        When a file's content changes at a stable source_path, its sha256 — and
        thus its document_id — changes, so the document_id-keyed delete matches
        nothing and the old version's chunks orphan. Connectors call this before
        upload() so an edited file fully replaces its prior chunks under the
        same source_path.

        Both filter values are OData-escaped (single quote → doubled). source_path
        is not regex-validated upstream (unlike bot_tag), so escaping it is the
        primary injection defense here.

        Errors propagate: this is correctness-critical, not best-effort cleanup,
        so failures are NOT swallowed (contrast the document_id stale-delete in
        upload(), which warns-and-continues).

        Returns:
            The number of chunks deleted.
        """
        filter_expr = (
            f"source_path eq '{_escape_odata(source_path)}' and bot_tag eq '{_escape_odata(bot_tag)}'"
        )

        search_client = await self.create_search_index()

        # Drain ALL matching ids first (across every page), THEN delete. We never
        # delete while the search pager is open: deleting mutates the index, which
        # can invalidate continuation tokens mid-walk. `_drain_chunk_ids` does not
        # pass `top` — in the azure-search-documents SDK it maps to OData $top,
        # which can be interpreted as a hard total cap and silently truncate
        # results; the `.by_page()` continuation-token walk visits every match.
        ids = self._drain_chunk_ids(search_client, filter_expr)

        for start in range(0, len(ids), _DELETE_BATCH_SIZE):
            batch = ids[start : start + _DELETE_BATCH_SIZE]
            search_client.delete_documents(documents=[{"id": cid} for cid in batch])

        logger.info(
            f"delete_by_source_path removed {len(ids)} chunks for "
            f"source_path={source_path!r}, bot_tag={bot_tag!r}"
        )
        return len(ids)

    @staticmethod
    def _drain_chunk_ids(search_client, filter_expr: str) -> list[str]:
        """Collect EVERY chunk id matching ``filter_expr`` across all pages.

        Mirrors the paginated drain in ``delete_by_source_path`` (M3): no ``top``
        (which the SDK maps to OData $top and can silently cap the total), and a
        ``.by_page()`` continuation-token walk so documents that previously
        produced >1000 chunks are fully enumerated instead of orphaned.
        """
        result = search_client.search(
            search_text="*",
            filter=filter_expr,
            select=["id"],
        )
        return [doc["id"] for page in result.by_page() for doc in page if doc.get("id")]

    @staticmethod
    def _upsert_in_batches(search_client, azure_docs: list[dict]) -> list[str]:
        """Batch merge_or_upload at the 1000-action cap and check every result.

        H3: Azure Search returns HTTP 207 on partial success WITHOUT raising, and
        the per-document ``IndexingResult`` list was previously only logged at
        debug — so failed chunks were silently reported as success. Here we batch
        at ``_DELETE_BATCH_SIZE`` and inspect each result, collecting the keys of
        any chunk where ``not r.succeeded``.

        Returns:
            The list of chunk ids that FAILED to index (empty on full success).
        """
        failed_keys: list[str] = []
        for start in range(0, len(azure_docs), _DELETE_BATCH_SIZE):
            batch = azure_docs[start : start + _DELETE_BATCH_SIZE]
            results = search_client.merge_or_upload_documents(documents=batch)
            for r in results:
                if not getattr(r, "succeeded", False):
                    failed_keys.append(getattr(r, "key", "<unknown>"))
        return failed_keys

    async def _chunk_text_by_tokens(self, text: str, max_tokens: int = 500, overlap: int = 50) -> list[str]:
        """Chunk text by real token count using tiktoken (cl100k_base encoding).

        Args:
            text: Input text to chunk.
            max_tokens: Maximum tokens per chunk (actual tiktoken count, not words).
            overlap: Number of tokens to overlap between consecutive chunks.

        Returns:
            List of text chunks, each bounded by max_tokens real tokens.
        """
        logger.debug(f"Chunking text by real tokens - max_tokens: {max_tokens}, overlap: {overlap}")

        try:
            encoding = tiktoken.get_encoding("cl100k_base")
            token_ids = encoding.encode(text)

            if not token_ids:
                logger.debug("No tokens found in text")
                return []

            logger.debug(f"Total tokens in text: {len(token_ids)}")

            chunks = []
            start = 0

            while start < len(token_ids):
                end = min(start + max_tokens, len(token_ids))
                chunk_token_ids = token_ids[start:end]
                chunk_text = encoding.decode(chunk_token_ids)

                if chunk_text.strip():
                    chunks.append(chunk_text.strip())

                if end >= len(token_ids):
                    break

                start = start + max_tokens - overlap
                if start <= 0:
                    start = max_tokens

            logger.info(f"Token-aware chunking complete: {len(chunks)} chunks from {len(token_ids)} tokens")
            return chunks

        except Exception as e:
            logger.error(f"Error in token-aware chunking: {str(e)}", exc_info=True)
            raise
