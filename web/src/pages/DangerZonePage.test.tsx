import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiProvider } from "../api/ApiContext";
import type { AdminApiClient } from "../api/client";
import DangerZonePage from "./DangerZonePage";

function renderPage(client: Partial<AdminApiClient>, botTag = "acme") {
  return render(
    <ApiProvider value={{ client: client as AdminApiClient, botTag }}>
      <DangerZonePage />
    </ApiProvider>,
  );
}

describe("DangerZonePage tenant delete", () => {
  beforeEach(() => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps the tenant-delete button disabled until confirm box + matching bot_tag", async () => {
    const user = userEvent.setup();
    const deleteTenant = vi.fn();
    renderPage({ deleteTenant });

    const tenantCard = screen
      .getByRole("heading", { name: /Delete all documents for a bot_tag/i })
      .closest(".danger-card") as HTMLElement;
    const button = within(tenantCard).getByRole("button", {
      name: /Delete entire bot_tag/i,
    });

    expect(button).toBeDisabled();

    // Check the box only — still disabled without the typed tag.
    await user.click(within(tenantCard).getByRole("checkbox"));
    expect(button).toBeDisabled();

    // Type a WRONG tag — still disabled.
    const typeInput = within(tenantCard).getByLabelText(/Re-type bot_tag/i);
    await user.type(typeInput, "wrong");
    expect(button).toBeDisabled();

    // Fix to the correct tag — now armed.
    await user.clear(typeInput);
    await user.type(typeInput, "acme");
    expect(button).toBeEnabled();
  });

  it("calls deleteTenant with confirm=true and shows the result", async () => {
    const user = userEvent.setup();
    const deleteTenant = vi.fn().mockResolvedValue({
      bot_tag: "acme",
      deleted_chunks: 7,
      deleted_documents: 3,
      status: "deleted",
    });
    renderPage({ deleteTenant });

    const tenantCard = screen
      .getByRole("heading", { name: /Delete all documents for a bot_tag/i })
      .closest(".danger-card") as HTMLElement;

    await user.click(within(tenantCard).getByRole("checkbox"));
    await user.type(within(tenantCard).getByLabelText(/Re-type bot_tag/i), "acme");
    await user.click(
      within(tenantCard).getByRole("button", { name: /Delete entire bot_tag/i }),
    );

    expect(deleteTenant).toHaveBeenCalledWith("acme", true);
    expect(await within(tenantCard).findByText(/3 document\(s\)/)).toBeInTheDocument();
  });

  it("treats an idempotent document delete (0 chunks) as success", async () => {
    const user = userEvent.setup();
    const deleteDocument = vi.fn().mockResolvedValue({
      bot_tag: "acme",
      document_id: "ghost",
      deleted_chunks: 0,
      status: "deleted",
    });
    renderPage({ deleteDocument });

    const docCard = screen
      .getByRole("heading", { name: /Delete a document/i })
      .closest(".danger-card") as HTMLElement;

    await user.type(within(docCard).getByLabelText(/Document ID to delete/i), "ghost");
    await user.click(within(docCard).getByRole("button", { name: /Delete document/i }));

    expect(deleteDocument).toHaveBeenCalledWith("acme", "ghost");
    expect(await within(docCard).findByText(/0 chunk\(s\) removed/)).toBeInTheDocument();
  });
});
