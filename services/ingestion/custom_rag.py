from langchain import hub
import fitz
from azure.search.documents.indexes import SearchIndexClient
from langchain_openai import AzureChatOpenAI
from langchain_community.document_loaders import AzureAIDocumentIntelligenceLoader
from langchain_openai import AzureOpenAIEmbeddings
from langchain.schema import StrOutputParser
from langchain.schema.runnable import RunnablePassthrough
from langchain.text_splitter import MarkdownHeaderTextSplitter
from langchain.vectorstores.azuresearch import AzureSearch
from langchain_openai import AzureOpenAI
from openai import AzureOpenAI
from dotenv import load_dotenv
load_dotenv()
import hashlib
import os
import uuid
import tiktoken
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SearchFieldDataType,
    SimpleField,
    SearchableField,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticSearch,
    SemanticField,
    SemanticPrioritizedFields
)
import numpy as np
from langchain_community.document_loaders import AzureAIDocumentIntelligenceLoader
from langchain.text_splitter import MarkdownHeaderTextSplitter
from dotenv import load_dotenv
import logging
import sys
import time
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('rag.log')
    ]
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
                    model=os.getenv("AZURE_OPENAI_EMBEDDING_MODEL")
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
        index_name = os.getenv("INDEX_NAME")

        logger.info(f"Creating or retrieving search index: {index_name}")
        
        try:
            logger.info("Initializing Azure Search Index client")
            index_client = SearchIndexClient(
                endpoint=os.getenv("AZURE_SEARCH_ENDPOINT"),
                credential=AzureKeyCredential(os.getenv("AZURE_SEARCH_KEY"))
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
                    credential=AzureKeyCredential(os.getenv("AZURE_SEARCH_KEY"))
                )
                logger.info("Search client initialized for existing index")
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
                    name='bot_tag',
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
                algorithms=[
                    HnswAlgorithmConfiguration(
                        name="myHnsw"
                    )
                ],
            )

            # Configure semantic search
            logger.debug("Configuring semantic search settings")
            semantic_search = SemanticSearch(
                configurations=[
                    SemanticConfiguration(
                        name="mySemanticConfig",
                        prioritized_fields=SemanticPrioritizedFields(
                            title_field=SemanticField(field_name="section_header"),
                            content_fields=[
                                SemanticField(field_name="content")
                            ],
                            keywords_fields=[
                                SemanticField(field_name="filename"),
                                SemanticField(field_name="page_number")
                            ]
                        )
                    )
                ]
            )

            # Create the search index
            logger.info("Creating search index with vector and semantic search configurations")
            index = SearchIndex(
                name=index_name,
                fields=fields,
                vector_search=vector_search,
                semantic_search=semantic_search
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
                credential=AzureKeyCredential(os.getenv("AZURE_SEARCH_KEY"))
            )
            logger.info("Search client initialized for new index")
            
            return search_client
            
        except Exception as e:
            logger.error(f"Error creating search index: {str(e)}", exc_info=True)
            raise
    
    async def upload(self, file, tag, fr_mode, file_path):
        logger.info(f"Starting upload process - tag: {tag}, fr_mode: {fr_mode}, file_path: {file_path}")
        
        try:
            filename = file.filename
            logger.info(f"Processing file: {filename}")
            
            # Read file content
            logger.debug("Reading file content")
            file_content = await file.read()
            logger.info(f"File content read successfully - size: {len(file_content)} bytes")
            
            # Get total pages using fitz
            logger.debug("Analyzing PDF structure with fitz")
            doc = fitz.open(stream=file_content, filetype="pdf")
            total_pages = doc.page_count
            doc.close()
            logger.info(f"PDF analysis complete - total pages: {total_pages}")
            
            # Initialize search client
            logger.info("Initializing search client")
            search_client = await self.create_search_index()
            
            # Load document using Azure AI Document Intelligence
            logger.info(f"Loading document with Azure AI Document Intelligence - mode: {fr_mode}")
            start_time = time.time()
            
            loader = AzureAIDocumentIntelligenceLoader(
                bytes_source=file_content,
                api_key=os.getenv("DOC_INTELLIGENCE_KEY"),
                api_endpoint=os.getenv("DOC_INTELLIGENCE_ENDPOINT"),
                api_model=f"prebuilt-{fr_mode}"
            )
            
            docs = loader.load()
            end_time = time.time()
            
            logger.info(f"Document loaded successfully in {end_time - start_time:.2f} seconds")
            logger.info(f"Loaded {len(docs)} document(s)")
            
            if docs:
                logger.debug(f"First document content length: {len(docs[0].page_content)} characters")
            
            # Prepare documents for upload
            azure_docs = []
            token_list = []
            
            if fr_mode == "layout":
                logger.info("Processing document in layout mode with header-based splitting")
                
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
                    logger.debug(f"Processing chunk {i+1}/{len(splits)}")
                    content = chunk.page_content
                    
                    if not content.strip():
                        logger.debug(f"Skipping empty chunk {i+1}")
                        continue
                    
                    section_name = chunk.metadata.get("Header_1", "")
                    logger.debug(f"Chunk {i+1} - section: {section_name}, content length: {len(content)}")
                    
                    # Get embedding for content
                    logger.debug(f"Generating embedding for chunk {i+1}")
                    content_vector = await self.get_embedding(content)
                    
                    # Calculate token count
                    token_count = await self.chunk_token(content)
                    token_list.append(token_count)
                    
                    azure_docs.append({
                        "id": str(uuid.uuid4()),
                        "bot_tag": tag,
                        "fr_tag": f"fr_{fr_mode}",
                        "filename": filename,
                        "filepath": file_path,
                        "chunk_size": str(token_count),
                        "section_header": section_name,
                        "sub_section": chunk.metadata.get("Header_2", ""),
                        "source": filename,
                        "content": content,
                        "content_vector": content_vector
                    })
                    
                    logger.debug(f"Chunk {i+1} processed successfully")
            
            elif fr_mode == "read":
                logger.info("Processing document in read mode with token-based splitting")
                
                # Use token-based splitting for read mode
                docs_string = docs[0].page_content
                logger.debug(f"Document content length: {len(docs_string)} characters")
                
                logger.debug("Splitting text by tokens")
                start_time = time.time()
                chunks = await self._chunk_text_by_tokens(docs_string, max_tokens=500, overlap=50)
                end_time = time.time()
                
                logger.info(f"Text split into {len(chunks)} chunks in {end_time - start_time:.2f} seconds")
                
                for i, chunk in enumerate(chunks):
                    logger.debug(f"Processing chunk {i+1}/{len(chunks)}")
                    content = chunk.strip()
                    
                    if not content:
                        logger.debug(f"Skipping empty chunk {i+1}")
                        continue
                    
                    logger.debug(f"Chunk {i+1} content length: {len(content)} characters")
                    
                    # Get embedding for content
                    logger.debug(f"Generating embedding for chunk {i+1}")
                    content_vector = await self.get_embedding(content)
                    
                    # Calculate token count
                    token_count = await self.chunk_token(content)
                    token_list.append(token_count)
                    
                    azure_docs.append({
                        "id": str(uuid.uuid4()),
                        "bot_tag": tag,
                        "fr_tag": f"fr_{fr_mode}",
                        "filename": filename,
                        "filepath": file_path,
                        "chunk_size": str(token_count),
                        "section_header": "",  # Empty for read mode
                        "sub_section": "",     # Empty for read mode
                        "source": filename,
                        "content": content,
                        "content_vector": content_vector
                    })
                    
                    logger.debug(f"Chunk {i+1} processed successfully")
            
            # Upload documents to the index
            if azure_docs:
                logger.info(f"Uploading {len(azure_docs)} documents to search index")
                start_time = time.time()
                
                result = search_client.upload_documents(documents=azure_docs)
                end_time = time.time()
                
                logger.info(f"Documents uploaded successfully in {end_time - start_time:.2f} seconds")
                logger.debug(f"Upload result: {result}")
                
                # Calculate character statistics for each chunk
                char_list = [len(doc["content"]) for doc in azure_docs]
                
                # Create summary statistics
                stats = {
                    "status": "successful",
                    "filename": filename,
                    "total_pages": total_pages,
                    "total_chunks": len(azure_docs),
                    "max_token": max(token_list),
                    "min_token": min(token_list),
                    "avg_token": sum(token_list) / len(token_list),
                    "max_character": max(char_list),
                    "min_character": min(char_list),
                    "avg_character": sum(char_list) / len(char_list)
                }
                
                logger.info(f"Upload completed successfully - stats: {stats}")
                return stats
            else:
                logger.warning("No documents to upload")
                return "No documents to upload"
                
        except Exception as e:
            logger.error(f"Error in upload process: {str(e)}", exc_info=True)
            raise

    async def _chunk_text_by_tokens(self, text: str, max_tokens: int = 500, overlap: int = 50):
        """Chunks text based on token count with overlap."""
        logger.debug(f"Chunking text by tokens - max_tokens: {max_tokens}, overlap: {overlap}")
        
        try:
            chunks = []
            words = text.split()
            
            if not words:
                logger.debug("No words found in text")
                return chunks
            
            logger.debug(f"Total words in text: {len(words)}")
            
            start_idx = 0
            chunk_count = 0
            
            while start_idx < len(words):
                chunk_count += 1
                logger.debug(f"Processing chunk {chunk_count} starting at word {start_idx}")
                
                # Determine the end index for this chunk
                end_idx = min(start_idx + max_tokens, len(words))
                
                # Create chunk from start_idx to end_idx
                chunk_words = words[start_idx:end_idx]
                chunk = " ".join(chunk_words)
                
                if chunk.strip():
                    chunks.append(chunk.strip())
                    logger.debug(f"Chunk {chunk_count} created - length: {len(chunk)} characters, words: {len(chunk_words)}")
                
                # Move start_idx for next chunk, accounting for overlap
                # If this is the last chunk, break
                if end_idx >= len(words):
                    logger.debug(f"Reached end of text at chunk {chunk_count}")
                    break
                    
                # Move start index forward by (max_tokens - overlap)
                start_idx = start_idx + max_tokens - overlap
                
                # Ensure we don't go backwards
                if start_idx <= 0:
                    start_idx = max_tokens
                    
                logger.debug(f"Next chunk will start at word {start_idx}")
            
            logger.info(f"Text chunking completed - created {len(chunks)} chunks")
            return chunks
            
        except Exception as e:
            logger.error(f"Error in text chunking: {str(e)}", exc_info=True)
            raise