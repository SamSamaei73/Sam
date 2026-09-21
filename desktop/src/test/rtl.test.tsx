import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { baseStatus, mockBridge, ok, renderApp } from "./helpers";

const FA_QUESTION = "سلام سام، امروز چه خبر؟";
const FA_REPLY =
  "برای اجرا بنویس `uvicorn main:app` و بعد به https://example.com/docs برو.\n```bash\npip install fastapi\n```\nاین API با FastAPI کار می‌کند ۱۲۳.";

async function chat(bridge: ReturnType<typeof mockBridge>, text: string) {
  renderApp(bridge);
  const user = userEvent.setup();
  await screen.findByLabelText("Connection: Connected");
  await user.type(screen.getByLabelText("Message Sam"), text);
  await user.click(screen.getByRole("button", { name: "Send message" }));
  return user;
}

describe("Persian / RTL rendering", () => {
  it("renders a Persian answer right-to-left without flipping the app", async () => {
    const bridge = mockBridge({
      chat: vi.fn(async () => ({ ...ok, reply: FA_REPLY, language: "fa" as const, direction: "rtl" as const })),
    });
    await chat(bridge, "hello");
    const log = screen.getByRole("log");
    const bubble = await waitFor(() => {
      const found = Array.from(log.querySelectorAll<HTMLElement>(".bubble")).find((b) => b.getAttribute("dir") === "rtl");
      if (!found) throw new Error("no rtl bubble yet");
      return found;
    });
    expect(bubble).toHaveAttribute("lang", "fa");
    expect(document.documentElement.dir).toBe("ltr"); // only the message flipped
    expect(bubble.textContent).toContain("۱۲۳");
    expect(bubble.textContent).toContain("این API با FastAPI کار می‌کند");
  });

  it("keeps code blocks, inline code and URLs left-to-right inside Persian text", async () => {
    const bridge = mockBridge({
      chat: vi.fn(async () => ({ ...ok, reply: FA_REPLY, language: "fa" as const, direction: "rtl" as const })),
    });
    await chat(bridge, "x");
    const log = screen.getByRole("log");
    await waitFor(() => expect(log.querySelector("pre.code-block")).not.toBeNull());
    const code = log.querySelector("pre.code-block") as HTMLElement;
    expect(code).toHaveAttribute("dir", "ltr");
    expect(code.textContent).toContain("pip install fastapi");
    const inline = log.querySelector("bdi.inline-code") as HTMLElement;
    expect(inline).toHaveAttribute("dir", "ltr");
    expect(inline.textContent).toBe("uvicorn main:app");
    const url = log.querySelector("bdi.url") as HTMLElement;
    expect(url).toHaveAttribute("dir", "ltr");
    expect(url.textContent).toBe("https://example.com/docs");
  });

  it("renders English left-to-right and detects direction when the backend didn't say", async () => {
    const bridge = mockBridge({
      chat: vi.fn(async () => ({ ...ok, reply: "This is an English answer.", language: null, direction: null })),
    });
    await chat(bridge, "hi");
    await screen.findByText("This is an English answer.");
    const bubble = screen.getByText("This is an English answer.").closest(".bubble") as HTMLElement;
    expect(bubble).toHaveAttribute("dir", "ltr");
    expect(bubble).toHaveAttribute("lang", "en");
  });

  it("detects a Persian message without backend hints (user's own text)", async () => {
    await chat(mockBridge(), FA_QUESTION);
    const own = (await within(screen.getByRole("log")).findByText(FA_QUESTION)).closest(".bubble") as HTMLElement;
    expect(own).toHaveAttribute("dir", "rtl");
    expect(own).toHaveAttribute("lang", "fa");
  });

  it("mixed Persian-English keeps technical terms un-transliterated and uncorrupted", async () => {
    const reply = "برای Docker و GitHub از Python استفاده کن";
    await chat(
      mockBridge({ chat: vi.fn(async () => ({ ...ok, reply, language: "fa" as const, direction: "rtl" as const })) }),
      "x",
    );
    const text = await screen.findByText(reply);
    expect(text.textContent).toBe(reply);
    expect(text.closest(".bubble")).toHaveAttribute("dir", "rtl");
  });

  it("markup in a Persian reply stays inert text", async () => {
    const reply = 'سلام <img src=x onerror="window.__fa=1"> <script>window.__fa=1</script>';
    await chat(
      mockBridge({ chat: vi.fn(async () => ({ ...ok, reply, language: "fa" as const, direction: "rtl" as const })) }),
      "x",
    );
    await screen.findByText(reply);
    expect(document.querySelector(".bubble img")).toBeNull();
    expect((window as unknown as { __fa?: number }).__fa).toBeUndefined();
  });

  it("the composer flows naturally for Persian typing (dir=auto)", async () => {
    renderApp(mockBridge());
    const box = await screen.findByLabelText("Message Sam");
    expect(box).toHaveAttribute("dir", "auto");
  });

  it("passes the response-language preference to the backend", async () => {
    const bridge = mockBridge();
    renderApp(bridge);
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await user.click(screen.getByRole("button", { name: "Settings" }));
    await user.selectOptions(await screen.findByRole("combobox", { name: /response language/i }), "fa");
    await user.click(screen.getByRole("button", { name: "Chat" }));
    await user.type(screen.getByLabelText("Message Sam"), "hello");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(bridge.chat).toHaveBeenCalledWith("hello", "fa", "normal"));
    expect(JSON.parse(window.localStorage.getItem("sam.ui.prefs.v1") ?? "{}").responseLanguage).toBe("fa");
  });
});

describe("Persian interface", () => {
  it("switching the interface language flips direction and shows reviewed Persian strings", async () => {
    renderApp(mockBridge());
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await user.click(screen.getByRole("button", { name: "Settings" }));
    await user.selectOptions(await screen.findByRole("combobox", { name: /interface language/i }), "fa");
    expect(document.documentElement.dir).toBe("rtl");
    expect(document.documentElement.lang).toBe("fa");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByText("گفتگو")).toBeInTheDocument();
    expect(within(nav).getByText("تنظیمات")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "تنظیمات" })).toBeInTheDocument();
    expect(screen.getByText("زبان رابط")).toBeInTheDocument();
  });

  it("the confirmation dialog speaks Persian in the Persian interface", async () => {
    window.localStorage.setItem("sam.ui.prefs.v1", JSON.stringify({ uiLanguage: "fa" }));
    const bridge = mockBridge({
      knowledgeList: vi.fn(async () => ({
        ...ok,
        resources: [{ resource_id: "r1", name: "n.txt", resource_type: "txt", size_bytes: 1, chunk_count: 1, created_at: "2026-01-01T00:00:00Z" }],
      })),
      knowledgeRemove: vi.fn(async () => ({
        ...ok,
        status: "confirmation_required" as const,
        challenge: {
          confirmation_id: "c1",
          action: "delete",
          resource: "knowledge",
          scope: "default",
          risk: "high" as const,
          target: "n.txt",
          reason: null,
          expires_at: "2026-01-01T00:05:00Z",
        },
      })),
    });
    renderApp(bridge);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "دانش" }));
    await user.click(await screen.findByRole("button", { name: "Remove n.txt" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("سام به تأیید شما نیاز دارد")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "تأیید" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "رد" })).toBeInTheDocument();
    expect(within(dialog).getByText("ریسک بالا")).toBeInTheDocument();
  });

  it("read-aloud is hidden when no trusted voice speaks the response language", async () => {
    const status = (langs: ("fa" | "en")[]) =>
      vi.fn(async () => ({
        ...baseStatus,
        speech_output: "configured" as const,
        speech_profiles: [{ profile_id: "sam_default", languages: langs }],
      }));
    const reply = { ...ok, reply: FA_REPLY, language: "fa" as const, direction: "rtl" as const };
    const englishOnly = mockBridge({ status: status(["en"]), chat: vi.fn(async () => reply) });
    await chat(englishOnly, "x");
    await waitFor(() => expect(document.querySelector('.bubble[dir="rtl"]')).not.toBeNull());
    expect(screen.queryByRole("button", { name: "Read aloud" })).toBeNull();
  });

  it("read-aloud appears when a trusted Persian voice exists", async () => {
    const bridge = mockBridge({
      status: vi.fn(async () => ({
        ...baseStatus,
        speech_output: "configured" as const,
        speech_profiles: [{ profile_id: "sam_fa", languages: ["fa" as const] }],
      })),
      chat: vi.fn(async () => ({ ...ok, reply: FA_REPLY, language: "fa" as const, direction: "rtl" as const })),
    });
    await chat(bridge, "x");
    expect(await screen.findByRole("button", { name: "Read aloud" })).toBeInTheDocument();
  });
});
