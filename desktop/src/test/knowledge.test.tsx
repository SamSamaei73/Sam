import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { mockBridge, ok, renderApp } from "./helpers";

async function goto(name: string) {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name }));
  return user;
}

describe("Knowledge", () => {
  it("shows an empty state and honest storage wording", async () => {
    renderApp(mockBridge());
    await goto("Knowledge");
    expect(await screen.findByText("No documents yet")).toBeInTheDocument();
    expect(screen.getByText(/for this session only/i)).toBeInTheDocument();
  });

  it("renders search results with only the provenance the backend supplied", async () => {
    renderApp(
      mockBridge({
        knowledgeQuery: vi.fn(async () => ({
          ...ok,
          hits: [
            {
              resource_id: "r1",
              resource_name: "Plan.pdf",
              resource_type: "pdf",
              chunk_id: "c1",
              score: 0.812,
              snippet: "<script>alert(1)</script> quarterly goals",
              location: {
                page_number: 4,
                section_title: null,
                paragraph_index: null,
                character_start: null,
                character_end: null,
              },
            },
          ],
        })),
      }),
    );
    const user = await goto("Knowledge");
    await user.type(await screen.findByLabelText("Search Knowledge"), "goals");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Page 4")).toBeInTheDocument();
    expect(screen.queryByText(/Section:/)).toBeNull();
    expect(screen.queryByText(/Characters/)).toBeNull();
    expect(screen.getByText(/<script>alert\(1\)<\/script> quarterly goals/)).toBeInTheDocument();
    expect(document.querySelector(".snippet script")).toBeNull();
  });

  it("says when location is unavailable rather than inventing one", async () => {
    renderApp(
      mockBridge({
        knowledgeQuery: vi.fn(async () => ({
          ...ok,
          hits: [
            {
              resource_id: "r1",
              resource_name: "a.txt",
              resource_type: "txt",
              chunk_id: "c1",
              score: 0.5,
              snippet: "text",
              location: {
                page_number: null,
                section_title: null,
                paragraph_index: null,
                character_start: null,
                character_end: null,
              },
            },
          ],
        })),
      }),
    );
    const user = await goto("Knowledge");
    await user.type(await screen.findByLabelText("Search Knowledge"), "x");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Location not available")).toBeInTheDocument();
  });

  it("rejects unsupported file types before anything is sent", async () => {
    const bridge = mockBridge();
    renderApp(bridge);
    await goto("Knowledge");
    const user = userEvent.setup({ applyAccept: false });
    const input = await screen.findByLabelText("Choose a document");
    await user.upload(input, new File(["MZ"], "malware.exe"));
    expect(await screen.findByText(/file type isn't supported/i)).toBeInTheDocument();
    expect(bridge.knowledgeIngest).not.toHaveBeenCalled();
  });

  it("ingests a supported file with only name, type and content", async () => {
    const bridge = mockBridge();
    renderApp(bridge);
    const user = await goto("Knowledge");
    await user.upload(await screen.findByLabelText("Choose a document"), new File(["hello"], "notes.txt", { type: "text/plain" }));
    await waitFor(() => expect(bridge.knowledgeIngest).toHaveBeenCalled());
    const arg = (bridge.knowledgeIngest as ReturnType<typeof vi.fn>).mock.calls[0]?.[0];
    expect(Object.keys(arg).sort()).toEqual(["confirmationId", "contentBase64", "name", "resourceType"]);
    expect(arg.resourceType).toBe("txt");
    expect(atob(arg.contentBase64)).toBe("hello");
  });

  it("surfaces a rejected document (e.g. a secret) without echoing content", async () => {
    renderApp(
      mockBridge({
        knowledgeIngest: vi.fn(async () => ({
          ...ok,
          status: "rejected" as const,
          reason_code: "secret_detected",
          message: "That looked like it contained a secret, so it was withheld.",
          resource: null,
          duplicate_of: null,
        })),
      }),
    );
    const user = await goto("Knowledge");
    await user.upload(await screen.findByLabelText("Choose a document"), new File(["k"], "n.txt"));
    expect(await screen.findByText(/contained a secret/)).toBeInTheDocument();
  });
});

describe("Memory", () => {
  it("is read-only and distinct from Knowledge", async () => {
    renderApp(mockBridge());
    await goto("Memory");
    expect(await screen.findByText("Nothing remembered yet")).toBeInTheDocument();
    expect(screen.getByText(/separate from Knowledge/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add|save|edit|delete|remove/i })).toBeNull();
  });

  it("renders memory content as inert text", async () => {
    renderApp(
      mockBridge({
        memorySearch: vi.fn(async () => ({
          ...ok,
          items: [
            {
              memory_id: "m1",
              memory_type: "preference",
              content: "<img src=x onerror=alert(1)>likes tea",
              source: "user_stated",
              confidence: "high",
              created_at: "2026-01-01T00:00:00Z",
              tags: [],
            },
          ],
          working: [],
        })),
      }),
    );
    await goto("Memory");
    expect(await screen.findByText(/<img src=x onerror=alert\(1\)>likes tea/)).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });
});
