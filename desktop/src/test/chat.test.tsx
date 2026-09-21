import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { BridgeError } from "../bridge/bridge";
import { baseStatus, mockBridge, ok, renderApp } from "./helpers";

async function send(text: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Message Sam"), text);
  await user.click(screen.getByRole("button", { name: "Send message" }));
  return user;
}

describe("shell and connection", () => {
  it("renders the navigation and the Sam view", async () => {
    renderApp(mockBridge());
    const nav = screen.getByRole("navigation", { name: "Main" });
    for (const name of ["Chat", "Knowledge", "Memory", "Tools", "Permissions", "Activity", "Settings"]) {
      expect(nav).toHaveTextContent(name);
    }
    expect(screen.getByRole("heading", { name: /good (morning|afternoon|evening)/i })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Search chats" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sam engine status/ })).toBeInTheDocument();
  });

  it("shows Starting, then Connected", async () => {
    let release: (v: typeof baseStatus) => void = () => undefined;
    const bridge = mockBridge({ status: vi.fn(() => new Promise<typeof baseStatus>((r) => (release = r))) });
    renderApp(bridge);
    expect(screen.getByLabelText("Connection: Starting")).toBeInTheDocument();
    release(baseStatus);
    expect(await screen.findByLabelText("Connection: Connected")).toBeInTheDocument();
  });

  it("shows Unavailable when the backend can't be reached", async () => {
    renderApp(mockBridge({ status: vi.fn(async () => Promise.reject(new BridgeError("unavailable", "x"))) }));
    expect(await screen.findByLabelText("Connection: Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/backend isn't reachable/i)).toBeInTheDocument();
  });

  it("shows Degraded and disables chat when the model isn't configured", async () => {
    renderApp(mockBridge({ status: vi.fn(async () => ({ ...baseStatus, agent: "not_configured" as const })) }));
    expect(await screen.findByLabelText("Connection: Degraded")).toBeInTheDocument();
    expect(screen.getByText(/language model isn't configured/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  });

  it("collapses and re-opens the sidebar via labelled controls", async () => {
    const user = userEvent.setup();
    renderApp(mockBridge());
    await user.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    await user.click(screen.getByRole("button", { name: "Expand sidebar" }));
    expect(screen.getByRole("button", { name: "Collapse sidebar" })).toBeInTheDocument();
  });

  it("keeps session-local conversations, searchable and switchable", async () => {
    const user = userEvent.setup();
    renderApp(mockBridge());
    await screen.findByLabelText("Connection: Connected");
    await user.type(screen.getByLabelText("Message Sam"), "first topic");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText("Hello from Sam");
    await user.click(screen.getAllByRole("button", { name: "New chat" })[0]!);
    expect(screen.queryByText("Hello from Sam")).toBeNull();
    const list = screen.getByRole("list", { name: "Conversations" });
    expect(within(list).getByText("first topic")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Search chats" }), "zzz");
    expect(within(list).queryByText("first topic")).toBeNull();
    await user.clear(screen.getByRole("textbox", { name: "Search chats" }));
    await user.click(within(list).getByText("first topic"));
    expect(await screen.findByText("Hello from Sam")).toBeInTheDocument();
    expect(window.localStorage.getItem("sam.ui.prefs.v1") ?? "").not.toContain("first topic");
  });

  it("toggles the system panel with capability status", async () => {
    const user = userEvent.setup();
    renderApp(mockBridge());
    await screen.findByLabelText("Connection: Connected");
    await user.click(screen.getByRole("button", { name: "Show system panel" }));
    const panel = screen.getByRole("complementary", { name: "System panel" });
    expect(within(panel).getByText("Language model")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Hide system panel" }));
    expect(screen.queryByRole("complementary", { name: "System panel" })).toBeNull();
  });
});

describe("chat", () => {
  it("sends a message and shows the reply", async () => {
    const bridge = mockBridge();
    renderApp(bridge);
    await screen.findByLabelText("Connection: Connected");
    await send("hello sam");
    expect(await screen.findByText("Hello from Sam")).toBeInTheDocument();
    expect(bridge.chat).toHaveBeenCalledWith("hello sam", "auto", "normal");
  });

  it("renders untrusted model output as inert text", async () => {
    const payload = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>**bold**';
    renderApp(mockBridge({ chat: vi.fn(async () => ({ ...ok, reply: payload, language: null, direction: null })) }));
    await screen.findByLabelText("Connection: Connected");
    await send("hi");
    expect(await screen.findByText(payload)).toBeInTheDocument();
    expect(document.querySelector("img[src='x']")).toBeNull();
    expect(document.querySelector(".bubble script")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("shows a safe error with a reference id when the request fails", async () => {
    renderApp(
      mockBridge({
        chat: vi.fn(async () => ({
          ...ok,
          status: "failed" as const,
          message: "The language model timed out.",
          reference_id: "ref-123",
          reply: null,
          language: null,
          direction: null,
        })),
      }),
    );
    await screen.findByLabelText("Connection: Connected");
    await send("hi");
    expect(await screen.findByText("The language model timed out.")).toBeInTheDocument();
    expect(screen.getByText("Reference: ref-123")).toBeInTheDocument();
  });

  it("does not leak raw bridge errors", async () => {
    renderApp(mockBridge({ chat: vi.fn(async () => Promise.reject(new Error("Bearer sk-ant-XYZ /etc/passwd"))) }));
    await screen.findByLabelText("Connection: Connected");
    await send("hi");
    expect(await screen.findByText("Something went wrong.")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/sk-ant|passwd|Bearer/);
  });

  it("keeps history in memory only and never in browser storage", async () => {
    renderApp(mockBridge());
    await screen.findByLabelText("Connection: Connected");
    await send("my private sentence");
    await screen.findByText("Hello from Sam");
    const dump = JSON.stringify({ ...window.localStorage }) + JSON.stringify({ ...window.sessionStorage });
    expect(dump).not.toContain("private");
    expect(dump).not.toContain("Hello from Sam");
  });

  it("clears the conversation on request", async () => {
    renderApp(mockBridge());
    await screen.findByLabelText("Connection: Connected");
    const user = await send("hi");
    await screen.findByText("Hello from Sam");
    await user.click(screen.getByRole("button", { name: "Clear chat" }));
    expect(screen.queryByText("Hello from Sam")).toBeNull();
  });

  it("sends nothing for empty or whitespace-only input", async () => {
    const bridge = mockBridge();
    renderApp(bridge);
    await screen.findByLabelText("Connection: Connected");
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Message Sam"), "   ");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    expect(bridge.chat).not.toHaveBeenCalled();
  });

  it("does not auto-write memory or knowledge after chatting", async () => {
    const bridge = mockBridge();
    renderApp(bridge);
    await screen.findByLabelText("Connection: Connected");
    await send("remember this");
    await waitFor(() => expect(bridge.chat).toHaveBeenCalled());
    expect(bridge.knowledgeIngest).not.toHaveBeenCalled();
    expect(bridge.memorySearch).not.toHaveBeenCalled();
  });
});
