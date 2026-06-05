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
- Lines beginning with ``:`` are comments (used as keep-alive heartbeats) and
  are ignored.
- Non-``data`` fields (``event:``, ``id:``, ``retry:``) are parsed but ignored
  by :func:`iter_sse_data` — only the data payload is yielded.

Sentinel handling: the OpenAI-style ``[DONE]`` marker is NOT part of the SSE
spec. :func:`iter_sse_data` treats a data payload equal to ``[DONE]`` as an
end-of-stream sentinel and stops *without* yielding it, so callers get a clean
token stream. This is asserted by the unit tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

# OpenAI-style end-of-stream sentinel. Not part of the SSE spec; treated as a
# terminator and never yielded to the caller.
DONE_SENTINEL = "[DONE]"


class SSEDecoder:
    """Incremental, line-driven SSE decoder shared by the sync and async paths.

    Feed one decoded line at a time with :meth:`feed`; it returns the completed
    event's ``data`` payload (or ``None`` if the line did not complete an event).
    Call :meth:`flush` once the stream ends to drain a trailing event that was
    not terminated by a blank line.

    The single ``done`` flag records whether the :data:`DONE_SENTINEL` was seen,
    so a driving loop can stop and never yield the sentinel itself. Keeping the
    parse state here lets both ``iter_lines`` (sync) and ``aiter_lines`` (async)
    reuse identical framing rules without duplicating logic.
    """

    def __init__(self) -> None:
        self._data_lines: list[str] = []
        self.done = False

    def feed(self, raw: str) -> str | None:
        """Consume one line; return a completed event payload or ``None``.

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
        if field != "data":
            # event:/id:/retry: and any unknown field — parsed but not yielded.
            return None

        # A single leading space after the colon is part of the framing, not data.
        if value.startswith(" "):
            value = value[1:]
        self._data_lines.append(value)
        return None

    def flush(self) -> str | None:
        """Dispatch a trailing event not terminated by a blank line, if any."""
        return self._dispatch()

    def _dispatch(self) -> str | None:
        if not self._data_lines:
            return None
        payload = "\n".join(self._data_lines)
        self._data_lines = []
        if payload == DONE_SENTINEL:
            self.done = True
            return None
        return payload


def iter_sse_data(lines: Iterable[str]) -> Iterator[str]:
    """Yield the ``data`` payload of each SSE event from an iterable of lines.

    Args:
        lines: Decoded text lines (without trailing newlines, as produced by
            ``httpx.Response.iter_lines``).

    Yields:
        The concatenated ``data`` payload for each complete event, in order.
        Comment/heartbeat lines and non-``data`` fields are skipped. A ``data``
        payload equal to :data:`DONE_SENTINEL` terminates the stream and is not
        yielded.
    """
    decoder = SSEDecoder()
    for raw in lines:
        payload = decoder.feed(raw)
        if payload is not None:
            yield payload
        if decoder.done:
            return
    payload = decoder.flush()
    if payload is not None:
        yield payload
