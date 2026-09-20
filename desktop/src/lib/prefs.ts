/**
 * Non-sensitive UI preferences ONLY. This is a strict allowlist: any key or
 * value outside it is ignored, so a transcript, message, token, document
 * text or confirmation id can never be persisted here.
 */

const STORAGE_KEY = "sam.ui.prefs.v1";

export interface Prefs {
  sidebarCollapsed: boolean;
  reducedMotion: boolean;
  readAloudVoice: string | null;
  /** Interface language (static reviewed strings). */
  uiLanguage: "en" | "fa";
  /** Which language Sam should answer in. Instruction only, never authority. */
  responseLanguage: "auto" | "fa" | "en";
}

export const DEFAULT_PREFS: Prefs = {
  sidebarCollapsed: false,
  reducedMotion: false,
  readAloudVoice: null,
  uiLanguage: "en",
  responseLanguage: "auto",
};

const PROFILE_ID = /^[a-z0-9_-]{1,64}$/;

export function sanitizePrefs(raw: unknown): Prefs {
  const out: Prefs = { ...DEFAULT_PREFS };
  if (typeof raw !== "object" || raw === null) return out;
  const value = raw as Record<string, unknown>;
  if (typeof value.sidebarCollapsed === "boolean") out.sidebarCollapsed = value.sidebarCollapsed;
  if (typeof value.reducedMotion === "boolean") out.reducedMotion = value.reducedMotion;
  if (typeof value.readAloudVoice === "string" && PROFILE_ID.test(value.readAloudVoice)) {
    out.readAloudVoice = value.readAloudVoice;
  }
  if (value.uiLanguage === "en" || value.uiLanguage === "fa") out.uiLanguage = value.uiLanguage;
  if (
    value.responseLanguage === "auto" ||
    value.responseLanguage === "fa" ||
    value.responseLanguage === "en"
  ) {
    out.responseLanguage = value.responseLanguage;
  }
  return out;
}

export function loadPrefs(): Prefs {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? sanitizePrefs(JSON.parse(raw)) : { ...DEFAULT_PREFS };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

export function savePrefs(prefs: Prefs): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(sanitizePrefs(prefs)));
  } catch {
    /* storage unavailable: preferences simply don't persist */
  }
}
