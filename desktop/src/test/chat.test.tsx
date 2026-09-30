import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { BridgeError } from "../bridge/bridge";
import { baseStatus, mockBridge, ok, renderApp } from "./helpers";

async function send(text: string) {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Type to Sam" }));
  await user.type(screen.getByLabelText("Message Sam"), text);
  await user.click(screen.getByRole("button", { name: "Send message" }));
  return user;
}

async function openHistory(user = userEvent.setup()) {
  const nav = screen.getByRole("navigation", { name: "Main" });
  await user.click(within(nav).getByRole("button", { name: "History" }));
  return user;
}

describe("shell and connection", () => {
  it("renders the navigation and Sam's voice-first home", async () => {
    renderApp(mockBridge());
    const nav = await screen.findByRole("navigation", { name: "Main" });
    for (const name of ["Home", "History", "Knowledge", "Memory", "Tools", "Permissions", "Activity", "Settings"]) {
      expect(nav).toHaveTextContent(name);
    }
    expect(screen.getByRole("heading", { name: "SAM" })).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Sam is ready" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Talk to Sam/ })).toBeInTheDocument();
    // Text is secondary: no permanent composer and no chat log on Home.
    expect(screen.queryByLabelText("Message Sam")).toBeNull();
    expect(screen.queryByRole("log")).toBeNull();
  });

  it("shows only Sam starting up (no navigation, buttons or error), then Home", async () => {
    let release: (v: typeof baseStatus) => void = () => undefined;
    const bridge = mockBridge({ status: vi.fn(() => new Promise<typeof baseStatus>((r) => (release = r))) });
    renderApp(bridge);
    expect(screen.getByRole("img", { name: "Sam is starting" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(document.body.textContent).not.toMatch(/reachable|unavailable/i);
    release(baseStatus);
    expect(await screen.findByLabelText("Connection: Connected")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Sam is starting" })).toBeNull();
  });

  it("keeps waiting while the app's backend reports it is starting, then connects", async () => {
    let calls = 0;
    const status = vi.fn(async () => {
      calls += 1;
      if (calls < 3) throw "starting";
      return baseStatus;
    });
    renderApp(mockBridge({ status }));
    expect(screen.getByRole("img", { name: "Sam is starting" })).toBeInTheDocument();
    expect(await screen.findByLabelText("Connection: Connected", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(status.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByText(/backend isn't reachable/i)).toBeNull();
  });

  it("stops waiting after the bounded startup window and reports unavailable", async () => {
    const now = vi.spyOn(Date, "now");
    const t0 = 1_000_000;
    now.mockReturnValue(t0);
    const status = vi.fn(async () => {
      throw "starting";
    });
    renderApp(mockBridge({ status }));
    await waitFor(() => expect(status).toHaveBeenCalled());
    now.mockReturnValue(t0 + 61_000);
    expect(await screen.findByLabelText("Connection: Unavailable", {}, { timeout: 4000 })).toBeInTheDocument();
    const settled = status.mock.calls.length;
    await new Promise((r) => setTimeout(r, 1000));
    expect(status.mock.calls.length).toBe(settled); // no endless retries
    now.mockRestore();
  });

  it("shows Unavailable when the backend can't be reached", async () => {
    renderApp(mockBridge({ status: vi.fn(async () => Promise.reject(new BridgeError("unavailable", "x"))) }));
    expect(await screen.findByLabelText("Connection: Unavailable")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Sam is offline" })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Action required: 1" }));
    expect(screen.getByText(/backend isn't reachable/i)).toBeInTheDocument();
  });

  it("shows Degraded and disables chat when the model isn't configured", async () => {
    renderApp(mockBridge({ status: vi.fn(async () => ({ ...baseStatus, agent: "not_configured" as const })) }));
    expect(await screen.findByLabelText("Connection: Degraded")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Action required/ }));
    expect(screen.getByText("No language model is available")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Type to Sam" }));
    expect(screen.getByText("Sam can't answer text right now.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  });

  it("collapses and re-opens the sidebar via labelled controls", async () => {
    const user = userEvent.setup();
    renderApp(mockBridge());
    await user.click(await screen.findByRole("button", { name: "Collapse sidebar" }));
    await user.click(screen.getByRole("button", { name: "Expand sidebar" }));
    expect(screen.getByRole("button", { name: "Collapse sidebar" })).toBeInTheDocument();
  });

  it("keeps session-local conversations in History, switchable", async () => {
    renderApp(mockBridge());
    await screen.findByLabelText("Connection: Connected");
    await send("first topic");
    await screen.findByText("Hello from Sam");
    const user = await openHistory();
    const log = screen.getByRole("log");
    expect(within(log).getByText("first topic")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(screen.queryByText("Hello from Sam")).toBeNull();
    await user.selectOptions(screen.getByLabelText("Conversation"), "first topic");
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
  it("sends a message from the text overlay and shows the reply transiently", async () => {
    const bridge = mockBridge();
    renderApp(bridge);
    await screen.findByLabelText("Connection: Connected");
    await send("hello sam");
    // The overlay closes after sending; the latest exchange shows under Sam.
    expect(screen.queryByRole("dialog", { name: "Type to Sam" })).toBeNull();
    const latest = screen.getByRole("region", { name: "Latest exchange" });
    expect(await within(latest).findByText("Hello from Sam")).toBeInTheDocument();
    expect(within(latest).getByText("hello sam")).toBeInTheDocument();
    expect(bridge.chat).toHaveBeenCalledWith("hello sam", "auto", "normal");
    await userEvent.setup().click(within(latest).getByRole("button", { name: "Open in History" }));
    expect(within(screen.getByRole("log")).getByText("Hello from Sam")).toBeInTheDocument();
  });

  it("the text overlay closes on Escape without sending", async () => {
    const bridge = mockBridge();
    renderApp(bridge);
    await screen.findByLabelText("Connection: Connected");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Type to Sam" }));
    expect(screen.getByRole("dialog", { name: "Type to Sam" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Message Sam")).toHaveFocus());
    await user.type(screen.getByLabelText("Message Sam"), "draft");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Type to Sam" })).toBeNull();
    expect(bridge.chat).not.toHaveBeenCalled();
  });

  it("truncates a long reply on Home but keeps it whole in History", async () => {
    const long = "word ".repeat(120).trim();
    renderApp(mockBridge({ chat: vi.fn(async () => ({ ...ok, reply: long, language: "en" as const, direction: "ltr" as const })) }));
    await screen.findByLabelText("Connection: Connected");
    await send("tell me a lot");
    const latest = screen.getByRole("region", { name: "Latest exchange" });
    await within(latest).findByText(/…$/);
    expect(within(latest).queryByText(long)).toBeNull();
    await openHistory();
    expect(within(screen.getByRole("log")).getByText(long)).toBeInTheDocument();
  });

  it("dismisses the transient exchange", async () => {
    renderApp(mockBridge());
    await screen.findByLabelText("Connection: Connected");
    const user = await send("hi");
    await screen.findByText("Hello from Sam");
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("region", { name: "Latest exchange" })).toBeNull();
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
    await send("hi");
    await screen.findByText("Hello from Sam");
    const user = await openHistory();
    await user.click(screen.getByRole("button", { name: "Clear chat" }));
    expect(screen.queryByText("Hello from Sam")).toBeNull();
  });

  it("sends nothing for empty or whitespace-only input", async () => {
    const bridge = mockBridge();
    renderApp(bridge);
    await screen.findByLabelText("Connection: Connected");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Type to Sam" }));
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
