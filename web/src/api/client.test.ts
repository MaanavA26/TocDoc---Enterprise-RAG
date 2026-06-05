import { describe, expect, it, vi } from "vitest";
import { AdminApiClient, ApiError } from "./client";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(body === undefined ? "" : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

const config = { baseUrl: "http://api.test", adminToken: "secret-token" };

describe("AdminApiClient", () => {
  it("injects base URL, /admin prefix, query params and X-Admin-Token header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(200, { bot_tag: "acme", count: 0, documents: [] }),
    );
    const client = new AdminApiClient(config, fetchMock as unknown as typeof fetch);

    await client.listDocuments("acme");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.test/admin/documents?bot_tag=acme");
    expect(init.method).toBe("GET");
    expect(init.headers["X-Admin-Token"]).toBe("secret-token");
  });

  it("normalizes a trailing slash on the base URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(200, { bot_tag: "acme", document_count: 0, chunk_count: 0, source_types: {}, fr_modes: {} }),
    );
    const client = new AdminApiClient(
      { ...config, baseUrl: "http://api.test/" },
      fetchMock as unknown as typeof fetch,
    );

    await client.getIndexStats("acme");
    expect(fetchMock.mock.calls[0][0]).toBe("http://api.test/admin/index/stats?bot_tag=acme");
  });

  it("parses the structured error envelope into a typed ApiError", async () => {
    const envelope = {
      error: {
        code: "NOT_FOUND",
        message: "Document not found in this scope",
        request_id: "req-123",
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(404, envelope, { "X-Request-ID": "req-123" }),
    );
    const client = new AdminApiClient(config, fetchMock as unknown as typeof fetch);

    await expect(client.getDocument("acme", "doc-1")).rejects.toMatchObject({
      status: 404,
      code: "NOT_FOUND",
      message: "Document not found in this scope",
      requestId: "req-123",
    });
  });

  it("surfaces per-field validation errors (422) on the ApiError", async () => {
    const envelope = {
      error: {
        code: "VALIDATION_ERROR",
        message: "Request validation failed",
        request_id: "req-422",
        errors: [{ loc: ["query", "bot_tag"], type: "string_pattern_mismatch", msg: "bad tag" }],
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(422, envelope));
    const client = new AdminApiClient(config, fetchMock as unknown as typeof fetch);

    try {
      await client.listDocuments("bad tag");
      throw new Error("expected rejection");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      const apiErr = err as ApiError;
      expect(apiErr.code).toBe("VALIDATION_ERROR");
      expect(apiErr.fieldErrors).toHaveLength(1);
      expect(apiErr.fieldErrors?.[0].msg).toBe("bad tag");
    }
  });

  it("falls back to X-Request-ID header when the body lacks request_id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(503, { error: { code: "UPSTREAM_UNAVAILABLE", message: "down" } }, {
        "X-Request-ID": "hdr-id",
      }),
    );
    const client = new AdminApiClient(config, fetchMock as unknown as typeof fetch);

    await expect(client.getIndexStats("acme")).rejects.toMatchObject({
      code: "UPSTREAM_UNAVAILABLE",
      requestId: "hdr-id",
    });
  });

  it("wraps a network-level failure as a NETWORK_ERROR ApiError", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const client = new AdminApiClient(config, fetchMock as unknown as typeof fetch);

    await expect(client.listConnectorRuns()).rejects.toMatchObject({
      status: 0,
      code: "NETWORK_ERROR",
    });
  });

  it("sends confirm=true on tenant delete (mirrors the API guard)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(200, {
        bot_tag: "acme",
        deleted_chunks: 5,
        deleted_documents: 2,
        status: "deleted",
      }),
    );
    const client = new AdminApiClient(config, fetchMock as unknown as typeof fetch);

    await client.deleteTenant("acme", true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.test/admin/bots/acme/documents?confirm=true");
    expect(init.method).toBe("DELETE");
  });
});
