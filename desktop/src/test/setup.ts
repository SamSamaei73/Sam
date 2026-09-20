import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
  try {
    window.localStorage.clear();
    window.sessionStorage.clear();
  } catch {
    /* storage unavailable in some environments */
  }
});
