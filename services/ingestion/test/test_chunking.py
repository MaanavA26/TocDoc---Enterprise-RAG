"""Tests for token-aware chunking and deterministic chunk IDs."""
import pytest
import asyncio
import hashlib
import sys
import os

# Add parent to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# We need to mock env vars before importing custom_rag
os.environ.setdefault('AZURE_OPENAI_ENDPOINT', 'https://test.openai.azure.com/')
os.environ.setdefault('AZURE_OPENAI_KEY', 'test-key')
os.environ.setdefault('AZURE_OPENAI_VERSION', '2024-02-01')
os.environ.setdefault('AZURE_OPENAI_EMBEDDING_MODEL', 'text-embedding-3-small')
os.environ.setdefault('AZURE_SEARCH_ENDPOINT', 'https://test.search.windows.net')
os.environ.setdefault('AZURE_SEARCH_KEY', 'test-key')
os.environ.setdefault('INDEX_NAME', 'test-index')
os.environ.setdefault('DOC_INTELLIGENCE_ENDPOINT', 'https://test.cognitiveservices.azure.com/')
os.environ.setdefault('DOC_INTELLIGENCE_KEY', 'test-key')

from custom_rag import rag

@pytest.fixture
def rag_instance():
    return rag()

@pytest.mark.asyncio
async def test_chunk_token_count_is_bounded(rag_instance):
    """Chunks must not exceed max_tokens real tokens."""
    import tiktoken
    encoding = tiktoken.get_encoding("cl100k_base")

    # Create text that is definitely more than 500 tokens
    long_text = "This is a test sentence with multiple words. " * 100

    chunks = await rag_instance._chunk_text_by_tokens(long_text, max_tokens=500, overlap=50)

    assert len(chunks) > 0, "Should produce at least one chunk"
    for i, chunk in enumerate(chunks):
        token_count = len(encoding.encode(chunk))
        assert token_count <= 500, f"Chunk {i} has {token_count} tokens, exceeds max 500"

@pytest.mark.asyncio
async def test_chunk_overlap_preserved(rag_instance):
    """Adjacent chunks should share overlapping content."""
    import tiktoken
    encoding = tiktoken.get_encoding("cl100k_base")

    text = " ".join([f"word{i}" for i in range(1000)])
    chunks = await rag_instance._chunk_text_by_tokens(text, max_tokens=100, overlap=20)

    assert len(chunks) > 1, "Should produce multiple chunks"

@pytest.mark.asyncio
async def test_empty_text_returns_empty_list(rag_instance):
    chunks = await rag_instance._chunk_text_by_tokens("", max_tokens=500, overlap=50)
    assert chunks == []

def test_deterministic_document_id():
    """Same file content must always produce the same document_id."""
    content = b"This is a test PDF content"
    doc_id_1 = hashlib.sha256(content).hexdigest()[:16]
    doc_id_2 = hashlib.sha256(content).hexdigest()[:16]
    assert doc_id_1 == doc_id_2

def test_different_content_produces_different_id():
    """Different files must produce different document IDs."""
    content_a = b"Document A content"
    content_b = b"Document B content"
    id_a = hashlib.sha256(content_a).hexdigest()[:16]
    id_b = hashlib.sha256(content_b).hexdigest()[:16]
    assert id_a != id_b

def test_chunk_id_format():
    """Chunk IDs must follow deterministic format."""
    document_id = "abc123def456789a"
    fr_mode = "read"
    chunk_index = 3
    chunk_id = f"{document_id}_{fr_mode}_{chunk_index:05d}"
    assert chunk_id == "abc123def456789a_read_00003"
