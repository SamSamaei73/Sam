import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { BridgeError } from "../bridge/bridge";
import type { ProvidersStatus } from "../bridge/types";
import { baseProviders, mockBridge, renderApp } from "./helpers";

async function openSettings(bridge: ReturnType<typeof mockBridge>) {
  renderApp(bridge);
  const user = userEvent.setup();
  await screen.findByLabelText("Connection: Connected");
  await user.click(screen.getByRole("button", { name: "Settings" }));
  await screen.findByText("AI Providers");
  return user;
}

describe("AI Providers settings", () => {
  it("shows trusted provider state, cost class, disclosures and paid fallback OFF", async () => {
    await openSettings(mockBridge());
    const claude = screen.getByTestId("provider-claude_subscription");
    expect(within(claude).getByText("Claude (subscription)")).toBeInTheDocument();
    expect(within(claude).getByText("Subscription — not API billing")).toBeInTheDocument();
    const gemini = screen.getByTestId("provider-gemini_free");
    expect(within(gemini).getByText("Free Tier")).toBeInTheDocument();
    expect(within(gemini).getAllByText(/data leaves this device/i).length).toBeGreaterThan(0);
    for (const id of ["openai_api", "grok_api"]) {
      const paid = screen.getByTestId(`provider-${id}`);
      expect(within(paid).getByText("Disabled")).toBeInTheDocument();
      expect(within(paid).getByText("Paid API — disabled")).toBeInTheDocument();
    }
    expect(screen.getByText("Paid fallback").nextElementSibling).toHaveTextContent("OFF");
    expect(screen.getByText("Maximum provider attempts").nextElementSibling).toHaveTextContent("2");
    expect(screen.getByText("Routing mode").nextElementSibling).toHaveTextContent("Auto");
  });

  it("never renders a key, token, prefix or credential anywhere in the panel", async () => {
    const view = renderApp(mockBridge());
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    await user.click(screen.getByRole("button", { name: "Settings" }));
    await screen.findByText("AI Providers");
    const text = view.container.textContent ?? "";
    for (const forbidden of ["AIza", "sk-ant", "Bearer", "api_key", "x-goog-api-key", "OAuth", "ANTHROPIC_API_KEY"]) {
      expect(text).not.toContain(forbidden);
    }
  });

  it("shows the content policy defaults: permissive, empty blocklist, no auto-save of private content", async () => {
    await openSettings(mockBridge());
    expect(screen.getByText("Content & Privacy")).toBeInTheDocument();
    expect(screen.getByText("Permissive")).toBeInTheDocument();
    for (const label of ["Profanity", "Explicit adult discussion", "Sensitive topics", "Controversial topics"]) {
      expect(screen.getByText(label).nextElementSibling).toHaveTextContent("Allowed");
    }
    expect(screen.getByText("Mirror my tone").nextElementSibling).toHaveTextContent("On");
    expect(screen.getByLabelText(/Sam's topic blocklist/)).toHaveValue("");
    expect(screen.getByText(/not saved to long-term Memory automatically/)).toBeInTheDocument();
    expect(screen.getByText(/does not change provider safety rules/)).toBeInTheDocument();
  });

  it("offers no control that could enable a paid provider or set a model, endpoint or key", async () => {
    await openSettings(mockBridge());
    const preferred = screen.getByLabelText("Preferred provider") as HTMLSelectElement;
    const options = Array.from(preferred.options).map((option) => option.value);
    expect(options).toEqual(["", "claude_subscription", "gemini_free"]); // disabled providers are not selectable
    expect(screen.queryByLabelText(/api key/i)).toBeNull();
    expect(screen.queryByLabelText(/endpoint/i)).toBeNull();
    expect(screen.queryByLabelText(/model id/i)).toBeNull();
    expect(screen.queryByRole("checkbox", { name: /paid/i })).toBeNull();
  });

  it("tightening saves without a step-up secret", async () => {
    const bridge = mockBridge();
    const user = await openSettings(bridge);
    await user.click(screen.getByRole("checkbox", { name: "Allow fallback to another free provider" }));
    await user.type(screen.getByLabelText(/Sam's topic blocklist/), "horse racing");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(bridge.setProviderPreferences).toHaveBeenCalledTimes(1));
    const sent = bridge.setProviderPreferences.mock.calls[0]?.[0];
    expect(sent).toMatchObject({ allow_free_fallback: false, topic_blocklist: ["horse racing"] });
    expect(sent.stepUp).toBeUndefined();
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("loosening privacy asks for the step-up secret and sends it only then", async () => {
    const bridge = mockBridge();
    const user = await openSettings(bridge);
    expect(screen.queryByLabelText(/Step-up secret/)).toBeNull();
    await user.click(screen.getByRole("checkbox", { name: "Send private content to the Gemini Free Tier" }));
    const secret = await screen.findByLabelText(/Step-up secret/);
    expect(secret).toHaveAttribute("type", "password");
    await user.type(secret, "my-step-up-secret-1");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(bridge.setProviderPreferences).toHaveBeenCalledTimes(1));
    expect(bridge.setProviderPreferences.mock.calls[0]?.[0]).toMatchObject({
      private_to_free_tier: true,
      stepUp: "my-step-up-secret-1",
    });
    await waitFor(() => expect(screen.queryByLabelText(/Step-up secret/)).toBeNull()); // cleared after saving
  });

  it("Gemini shows Free Tier intended but unattested by default, with the honest limit", async () => {
    await openSettings(mockBridge());
    const gemini = screen.getByTestId("provider-gemini_free");
    expect(within(gemini).getByTestId("gemini-attestation-state")).toHaveTextContent(/Free Tier intended/);
    expect(screen.getByText(/cannot independently verify Google Cloud billing state/)).toBeInTheDocument();
  });

  it("attesting the project is unbilled is a loosening: it needs the step-up secret", async () => {
    const bridge = mockBridge();
    const user = await openSettings(bridge);
    expect(screen.queryByLabelText(/Step-up secret/)).toBeNull();
    await user.click(screen.getByRole("checkbox", { name: /I attest the Google project/ }));
    const secret = await screen.findByLabelText(/Step-up secret/);
    await user.type(secret, "my-step-up-secret-1");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(bridge.setProviderPreferences).toHaveBeenCalledTimes(1));
    expect(bridge.setProviderPreferences.mock.calls[0]?.[0]).toMatchObject({
      gemini_attestation: "owner_attested_unbilled",
      stepUp: "my-step-up-secret-1",
    });
  });

  it("shows the safe reason code text for an unavailable Claude provider", async () => {
    const providers = baseProviders.providers.map((p) =>
      p.provider_id === "claude_subscription" ? { ...p, state: "unavailable" as const, detail: "managed_policy_present" } : p,
    );
    await openSettings(mockBridge({ providersStatus: async () => ({ ...baseProviders, providers }) }));
    expect(screen.getByTestId("detail-claude_subscription")).toHaveTextContent(/managed Claude Code policy/);
  });

  it("a refused change shows a safe message and keeps the secret out of the page", async () => {
    const bridge = mockBridge({
      setProviderPreferences: async () => {
        throw new BridgeError("step_up_failed", "That step-up secret wasn't accepted.");
      },
    });
    const user = await openSettings(bridge);
    await user.click(screen.getByRole("checkbox", { name: "Send personal content to the Gemini Free Tier" }));
    await user.type(await screen.findByLabelText(/Step-up secret/), "wrong-secret-value");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("That step-up secret wasn't accepted.")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("wrong-secret-value");
  });

  it("says so when provider settings are unavailable", async () => {
    const unavailable: ProvidersStatus = { ...baseProviders, available: false, providers: [], preferences: null, content: null };
    await openSettings(mockBridge({ providersStatus: async () => unavailable }));
    expect(screen.getByText("AI provider settings aren't available.")).toBeInTheDocument();
  });

  it("reflects a limit or missing sign-in honestly", async () => {
    const limited: ProvidersStatus = {
      ...baseProviders,
      providers: baseProviders.providers.map((p) =>
        p.provider_id === "claude_subscription" ? { ...p, state: "usage_limit" as const } : p,
      ),
    };
    await openSettings(mockBridge({ providersStatus: async () => limited }));
    expect(within(screen.getByTestId("provider-claude_subscription")).getByText("Usage limit reached")).toBeInTheDocument();
  });
});

describe("Private chat marking", () => {
  it("marks a message private, and only that message path sends the label", async () => {
    const bridge = mockBridge();
    renderApp(bridge);
    const user = userEvent.setup();
    await screen.findByLabelText("Connection: Connected");
    const chip = await screen.findByRole("button", { name: "Private" });
    expect(chip).toHaveAttribute("aria-pressed", "false");
    await user.type(screen.getByLabelText("Message Sam"), "normal question{Enter}");
    await waitFor(() => expect(bridge.chat).toHaveBeenCalledTimes(1));
    expect(bridge.chat.mock.calls[0]?.[2]).toBe("normal");
    await user.click(chip);
    expect(chip).toHaveAttribute("aria-pressed", "true");
    await user.type(screen.getByLabelText("Message Sam"), "a private thought{Enter}");
    await waitFor(() => expect(bridge.chat).toHaveBeenCalledTimes(2));
    expect(bridge.chat.mock.calls[1]?.[2]).toBe("private");
  });
});

describe("Persian AI Providers UI", () => {
  it("renders the providers and privacy panels in Persian, RTL, with exact canonical text", async () => {
    window.localStorage.setItem("sam.ui.prefs.v1", JSON.stringify({ uiLanguage: "fa" }));
    renderApp(mockBridge());
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "تنظیمات" }));
    expect(await screen.findByRole("heading", { name: "ارائه‌دهندگان هوش مصنوعی" })).toBeInTheDocument();
    expect(document.documentElement.dir).toBe("rtl");
    expect(screen.getByText("جایگزین پولی").nextElementSibling).toHaveTextContent("خاموش");
    expect(screen.getByRole("heading", { name: "حریم خصوصی و مسیریابی" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "محتوا و حریم خصوصی" })).toBeInTheDocument();
    const paid = screen.getByTestId("provider-openai_api");
    expect(within(paid).getByText("غیرفعال")).toBeInTheDocument();
    expect(within(paid).getByText("API پولی — غیرفعال")).toBeInTheDocument();
    expect(screen.getByLabelText(/ارسال محتوای خصوصی به سطح رایگان Gemini/)).toBeInTheDocument();
    // Technical names stay in Latin inside Persian text; nothing is transliterated.
    expect(screen.getByRole("option", { name: "Gemini (Free Tier)" })).toBeInTheDocument();
  });
});
