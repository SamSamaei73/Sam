import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BridgeError } from "../bridge/bridge";
import type { IdentityStatus } from "../bridge/types";
import { SamProvider, useSam } from "../state";
import { baseIdentity, mockBridge } from "./helpers";

function deferredIdentity() {
  let resolve!: (value: IdentityStatus) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<IdentityStatus>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const verified: IdentityStatus = { ...baseIdentity, last_verification: "verified" };
const rejected: IdentityStatus = { ...baseIdentity, last_verification: "not_verified" };

function IdentityState() {
  const { identity, refreshIdentity } = useSam();
  return <>
    <output aria-label="Identity result">{identity?.last_verification ?? "unavailable"}</output>
    <button onClick={() => void refreshIdentity()}>Refresh identity</button>
  </>;
}

describe("identity refresh ordering", () => {
  it.each(["mismatch", "failure"] as const)("ignores an older %s after the latest request verifies the owner", async (oldResult) => {
    const older = deferredIdentity();
    const newer = deferredIdentity();
    const bridge = mockBridge({ identityStatus: vi.fn()
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise) });
    render(<SamProvider bridge={bridge}><IdentityState /></SamProvider>);
    fireEvent.click(screen.getByRole("button", { name: "Refresh identity" }));
    expect(bridge.identityStatus).toHaveBeenCalledTimes(2);
    await act(async () => newer.resolve(verified));
    expect(screen.getByLabelText("Identity result")).toHaveTextContent(/^verified$/);
    await act(async () => {
      if (oldResult === "mismatch") older.resolve(rejected);
      else older.reject(new BridgeError("unauthorized", "Not authorized"));
    });
    expect(screen.getByLabelText("Identity result")).toHaveTextContent(/^verified$/);
  });

  it("clears identity on the latest unauthorized response and ignores an older success", async () => {
    const older = deferredIdentity();
    const newer = deferredIdentity();
    const bridge = mockBridge({ identityStatus: vi.fn()
      .mockResolvedValueOnce(verified)
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise) });
    render(<SamProvider bridge={bridge}><IdentityState /></SamProvider>);
    await screen.findByText("verified");
    fireEvent.click(screen.getByRole("button", { name: "Refresh identity" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh identity" }));
    await act(async () => newer.reject(new BridgeError("unauthorized", "Not authorized")));
    expect(screen.getByLabelText("Identity result")).toHaveTextContent(/^unavailable$/);
    await act(async () => older.resolve(verified));
    expect(screen.getByLabelText("Identity result")).toHaveTextContent(/^unavailable$/);
  });

  it("ignores a pending identity response from the previous bridge lifecycle", async () => {
    const pending = deferredIdentity();
    const oldBridge = mockBridge({ identityStatus: vi.fn(() => pending.promise) });
    const newBridge = mockBridge({ identityStatus: vi.fn(async () => verified) });
    const view = render(<SamProvider bridge={oldBridge}><IdentityState /></SamProvider>);
    view.rerender(<SamProvider bridge={newBridge}><IdentityState /></SamProvider>);
    await screen.findByText("verified");
    await act(async () => pending.resolve(rejected));
    expect(screen.getByLabelText("Identity result")).toHaveTextContent(/^verified$/);
    view.unmount();
    expect(screen.queryByLabelText("Identity result")).toBeNull();
  });
});
