# Phase P3 — Agentic AI Layer (LangGraph)

> **Prerequisite:** P0-3 (global state removal) must be done before introducing LangGraph,
> as the agentic layer inherits the same request-scoping contract.
> Full P0 completion is recommended before starting P3 work.
>
> **LangGraph compatibility note:** Current codebase uses `langchain==0.3.25`.
> LangGraph requires `langchain-core>=0.3.0` — the 0.3.x series satisfies this.
> Pin `langgraph>=0.2.0,<0.3.0` when introducing it.
> Do NOT upgrade to `langchain==0.4.x` — it breaks `MarkdownHeaderTextSplitter` in ingestion.

---

## Architecture overview

```
User Query
    │
    ▼
┌─────────────────────────────────────────────────┐
│                 Agent Router (P3-1)             │
│   LangGraph StateGraph — supervisor node        │
│                                                 │
│  Greeting? ──→ Direct LLM (no retrieval)        │
│  Factual?  ──→ Standard RAG (current pipeline)  │
│  Synthesis?──→ Map-Reduce Agent (P3-2)          │
│  Multi-hop?──→ ReAct Agent (P3-3)               │
└──────────────────────┬──────────────────────────┘
                       │
           ┌───────────┼────────────┐
           ▼           ▼            ▼
    Standard RAG  Map-Reduce    ReAct Agent
    (existing)    Summarizer    (new)
                  (new)
                       │
                       ▼
              ┌─────────────────┐
              │  Self-Critique  │ (P3-4) — all paths pass through
              │  Verifier Node  │
              └────────┬────────┘
                       │
                       ▼
               Final Answer + Citations
```

---

## P3-1 | Agent Router
**Status:** `PLANNED`

### LangGraph state schema
```python
from langgraph.graph import StateGraph, END
from typing import TypedDict, Optional, List

class AgentState(TypedDict):
    query: str
    history: List[dict]
    bot_tag: str
    fr_mode: str
    route: Optional[str]           # "standard" | "map_reduce" | "react" | "greeting"
    retrieved_chunks: List[dict]
    partial_answers: List[str]     # for map-reduce
    final_answer: Optional[str]
    citations: dict
    confidence: Optional[float]
    error: Optional[str]
```

### Router node
A lightweight LLM call (GPT-4o-mini, structured output) that classifies the query:
```python
ROUTER_PROMPT = """
Classify this query into exactly one category:
- greeting: social/chit-chat, no document knowledge needed
- factual: single-document or single-concept lookup
- synthesis: requires combining information across multiple documents or sections
- multihop: requires chaining multiple lookups to arrive at an answer

Query: {query}
History (last 2 turns): {history}

Reply with JSON: {"route": "<category>", "reason": "<one sentence>"}
"""
```

### Files to create
- `services/qna/src/agents/__init__.py`
- `services/qna/src/agents/state.py` — `AgentState` TypedDict
- `services/qna/src/agents/router.py` — router node + graph wiring
- `services/qna/src/agents/tools.py` — shared tools (search, embed)

### Integration with existing pipeline
The existing `generate_answer()` in `qna_pipeline.py` becomes one node in the graph
(the "standard" route). All other routes are new nodes that call the same underlying
Azure services (search, embed, LLM) but with different orchestration patterns.

---

## P3-2 | Map-Reduce Summarization Agent
**Status:** `PLANNED`

The killer feature: answers synthesis questions by reading the ENTIRE document corpus,
not just top-k chunks. Used for: "Summarize all contracts", "What risks appear across
all uploaded vendor reports?", "Compare the payment terms in these three SOWs."

### Architecture
```
Query
  │
  ▼
[Retrieve ALL chunks for bot_tag + fr_mode, no top-k limit]
  │
  ▼
[Map node — parallel] ──────────────────────────────────────────┐
  For each chunk (asyncio.gather):                              │
    LLM: "Given this chunk and the query, extract any          │
          relevant content. If not relevant, reply SKIP."       │
  Returns list of (chunk_id, extracted_content | SKIP)         │
                                                               │
[Filter node] ── drops SKIP responses                          │
  │                                                            │
  ▼                                                            │
[Reduce node]                                                  │
  LLM: "Synthesize these extracted passages into a            │
        coherent answer for the query. Cite sources."         │
  │                                                            │
  ▼                                                            │
Final answer with multi-document citations ◄───────────────────┘
```

### Implementation notes
- Map phase: `asyncio.gather(*[map_chunk(chunk, query) for chunk in all_chunks])`
- Batch map calls in groups of 20 to avoid Azure OpenAI rate limits
- Max total chunks: configurable via `MAP_REDUCE_MAX_CHUNKS` env var (default: 200)
- If chunk count exceeds max, fall back to standard top-k retrieval with a warning log
- Map model: GPT-4o-mini (cheap, parallel-friendly)
- Reduce model: GPT-4o (higher quality synthesis)

### Files to create
- `services/qna/src/agents/map_reduce.py`

### New env vars
```
MAP_REDUCE_MAX_CHUNKS=200
MAP_REDUCE_BATCH_SIZE=20
AZURE_OPENAI_REDUCE_MODEL=gpt-4o   # separate model for reduce step
```

---

## P3-3 | ReAct Multi-Hop Reasoning Agent
**Status:** `PLANNED`

For questions that require chaining multiple retrievals:
- "Which approved vendors also appear in flagged supplier reports?"
- "What changed between the Q1 and Q2 compliance reports?"

### Architecture
```
Query
  │
  ▼
[ReAct loop — max 5 iterations]
  │
  ├── Thought: what do I need to find next?
  ├── Action: search_documents(query=..., filter=...)
  ├── Observation: <search results>
  └── [repeat until FINISH or max iterations]
  │
  ▼
[Final answer with evidence chain]
```

### Tools available to the ReAct agent
```python
@tool
async def search_documents(query: str, filter_hint: str = "") -> str:
    """Search the document index. Returns top-5 chunks as formatted text."""
    ...

@tool
def extract_entities(text: str, entity_type: str) -> list[str]:
    """Extract named entities (names, dates, amounts) from text."""
    ...
```

### Implementation notes
- Use LangGraph's built-in `create_react_agent` with custom tools
- Inject `bot_tag` and `fr_mode` into the search tool via closure (not as agent-visible params)
- Cap iterations at 5; if reached, return best partial answer with a caveat
- Log the full reasoning trace for observability

### Files to create
- `services/qna/src/agents/react_agent.py`

---

## P3-4 | Self-Critique / Verifier Node
**Status:** `PLANNED`

All three answer paths (standard, map-reduce, react) pass through this node before
returning to the user. It catches hallucination before it reaches the client.

```python
VERIFIER_PROMPT = """
You are a fact-checking assistant. Given:
- An answer
- The source chunks used to produce it

Identify any claims in the answer that are NOT supported by the source chunks.

Reply with JSON:
{
  "verdict": "VERIFIED" | "HALLUCINATION_DETECTED",
  "unsupported_claims": ["list of specific claims not in sources"],
  "confidence": 0.0-1.0
}
"""
```

### Behavior on `HALLUCINATION_DETECTED`:
1. Log the hallucinated claims (for quality monitoring)
2. Re-run answer generation with added instruction: "Only use information directly stated in the provided sources."
3. If the second attempt also fails verification, return the answer with a disclaimer flag in the response metadata

### Files to create
- `services/qna/src/agents/verifier.py`

### Response model addition
```python
class QnASuccessResponse(BaseModel):
    answer: str
    citations: list[CitationMap]
    request_id: str
    verified: bool = True   # False if verifier found issues
    confidence: Optional[float] = None
```

---

## P3-5 | Conversation Memory (Cosmos DB)
**Status:** `PLANNED`

Replace the in-request history array with durable per-session history stored in Azure Cosmos DB.

### Schema
```
Container: tocdoc-sessions
Partition key: /session_id

Document:
{
  "id": "<session_id>",
  "session_id": "<uuid>",
  "bot_tag": "<tenant>",
  "user": "<email>",
  "created_at": "ISO8601",
  "updated_at": "ISO8601",
  "ttl": 86400,           // auto-expire after 24 hours
  "history": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

### API changes
- QnA endpoint accepts optional `session_id` in request
- If `session_id` provided: load history from Cosmos, append new turn, save back
- If `session_id` not provided: create new session, return `session_id` in response
- History window for LLM context: last 6 turns (configurable via `MAX_HISTORY_TURNS`)

### New env vars
```
COSMOS_DB_ENDPOINT=https://<account>.documents.azure.com
COSMOS_DB_KEY=<key>
COSMOS_DB_DATABASE=tocdoc
COSMOS_DB_SESSIONS_CONTAINER=tocdoc-sessions
MAX_HISTORY_TURNS=6
```

---

## P3-6 | SSE Streaming Responses
**Status:** `PLANNED`

Eliminates the 5-15 second wait for LLM responses. Users see tokens appear as they're generated.

### FastAPI implementation
```python
from fastapi.responses import StreamingResponse
import asyncio

@router.post("/answer/stream")
async def answer_stream(request: QnARequest, ...):
    async def generate():
        async for chunk in azure.openai_client.stream_chat(...):
            yield f"data: {json.dumps({'delta': chunk.content})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

### Notes
- Keep the existing non-streaming `/answer` endpoint as-is for backward compatibility
- Add a new `/answer/stream` endpoint
- Streaming is not compatible with the self-critique verifier (verifier needs full answer)
  — for the streaming endpoint, skip verification or run it post-stream as a background task
- Requires `openai>=1.0` with native streaming support (check requirements.txt)
