"""Minimal Server-Sent-Events (SSE) parser for the streaming QnA helper.

A dependency-light, transport-agnostic parser for the SSE wire format
(https://html.spec.whatwg.org/multipage/server-sent-events.html). It operates on
an iterable of already-decoded text *lines* so it can be unit-tested against a
canned stream with no live server and no httpx coupling.

What it implements (the subset the QnA streaming contract needs):

- An event is terminated by a blank line.
- A ``data:`` field contributes its value to the current event; an optional
  single leading space after the colon is stripped (per the spec).
- Multiple ``data:`` lines within one event are joined with ``"\\n"``.
- An ``event:`` field sets the event *type* for the current block; it defaults
  to ``"message"`` and resets after each dispatched event (per the spec).
- Lines beginning with ``:`` are comments (used as keep-alive heartbeats) and
  are ignored.
- Other non-``data`` fields (``id:``, ``retry:``) are parsed but ignored.

The decoder surfaces ``(event_type, data)`` pairs so the QnA streaming helper can
distinguish answer tokens (default/``message``) from the out-of-band
``event: citation`` payload and a terminal ``event: error`` — the server
multiplexes all three onto one stream (see ``services/qna/app.py`` /qna/stream).

Sentinel handling: the OpenAI-style ``[DONE]`` marker is NOT part of the SSE
spec. The driving loop treats a data payload equal to ``[DONE]`` as an
end-of-stream sentinel and stops *without* yielding it, so callers get a clean
event stream. This is asserted by the unit tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

# OpenAI-style end-of-stream sentinel. Not part of the SSE spec; treated as a
# terminator and never yielded to the caller.
DONE_SENTINEL = "[DONE]"

# Default SSE event type when no `event:` field is present (per the spec).
DEFAULT_EVENT = "message"

# Tagged event types the QnA stream multiplexes alongside answer tokens
# (see services/qna/app.py /qna/stream wire format).
CITATION_EVENT = "citation"
ERROR_EVENT = "error"


class SSEDecoder:
    """Incremental, line-driven SSE decoder shared by the sync and async paths.

    Feed one decoded line at a time with :meth:`feed`; it returns the completed
    event as an ``(event_type, data)`` pair (or ``None`` if the line did not
    complete an event). Call :meth:`flush` once the stream ends to drain a
    trailing event that was not terminated by a blank line.

    The single ``done`` flag records whether the :data:`DONE_SENTINEL` was seen,
    so a driving loop can stop and never yield the sentinel itself. Keeping the
    parse state here lets both ``iter_lines`` (sync) and ``aiter_lines`` (async)
    reuse identical framing rules without duplicating logic.
    """

    def __init__(self) -> None:
        self._data_lines: list[str] = []
        self._event_type: str | None = None
        self.done = False

    def feed(self, raw: str) -> tuple[str, str] | None:
        """Consume one line; return a completed ``(event_type, data)`` or ``None``.

        Returns ``None`` when the line is a field/comment that does not yet
        complete an event, or when the completed event is the
        :data:`DONE_SENTINEL` (which instead sets :attr:`done`).
        """
        # Normalize a trailing CR (CRLF streams) without touching interior text.
        line = raw.rstrip("\r")

        if line == "":
            return self._dispatch()

        if line.startswith(":"):
            # Comment / keep-alive heartbeat — ignore.
            return None

        field, _, value = line.partition(":")
        # A single leading space after the colon is part of the framing, not data.
        if value.startswith(" "):
            value = value[1:]

        if field == "data":
            self._data_lines.append(value)
        elif field == "event":
            # The event type applies to the current block; it resets on dispatch.
            self._event_type = value
        # id:/retry: and any unknown field — parsed but not retained.
        return None

    def flush(self) -> tuple[str, str] | None:
        """Dispatch a trailing event not terminated by a blank line, if any."""
        return self._dispatch()

    def _dispatch(self) -> tuple[str, str] | None:
        if not self._data_lines:
            # No data: a lone `event:`/comment block carries nothing to yield.
            # Still reset the event type so it does not leak into the next block.
            self._event_type = None
            return None
        payload = "\n".join(self._data_lines)
        event_type = self._event_type or DEFAULT_EVENT
        self._data_lines = []
        self._event_type = None
        if payload == DONE_SENTINEL:
            self.done = True
            return None
        return (event_type, payload)


def iter_sse_events(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    """Yield ``(event_type, data)`` for each SSE event from an iterable of lines.

    Args:
        lines: Decoded text lines (without trailing newlines, as produced by
            ``httpx.Response.iter_lines``).

    Yields:
        An ``(event_type, data)`` pair for each complete event, in order.
        ``event_type`` defaults to ``"message"`` when no ``event:`` field is
        present. Comment/heartbeat lines and ``id:``/``retry:`` fields are
        skipped. A ``data`` payload equal to :data:`DONE_SENTINEL` terminates
        the stream and is not yielded.
    """
    decoder = SSEDecoder()
    for raw in lines:
        event = decoder.feed(raw)
        if event is not None:
            yield event
        if decoder.done:
            return
    event = decoder.flush()
    if event is not None:
        yield event


def iter_sse_data(lines: Iterable[str]) -> Iterator[str]:
    """Yield the ``data`` payload of each default/``message`` SSE event.

    Backward-compatible token view over :func:`iter_sse_events`: only
    default/``message`` events are surfaced (answer tokens), so tagged events
    such as ``event: citation`` / ``event: error`` are not mixed into the token
    stream. Callers that need those tagged events should use
    :func:`iter_sse_events` directly.
    """
    for event_type, payload in iter_sse_events(lines):
        if event_type == DEFAULT_EVENT:
            yield payload
