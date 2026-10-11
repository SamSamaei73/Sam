import type { Direction, ResponseLanguage } from "../bridge/types";
import type { UiLanguage } from "../i18n/strings";

/**
 * Text helpers only decide presentation (which way a
 * message reads, which font/language attribute it carries). Language state is
 * never authorization.
 */

const isPersianLetter = (ch: string): boolean => {
  const cp = ch.codePointAt(0) ?? 0;
  const inRange =
    (cp >= 0x0600 && cp <= 0x06ff) ||
    (cp >= 0x0750 && cp <= 0x077f) ||
    (cp >= 0xfb50 && cp <= 0xfdff) ||
    (cp >= 0xfe70 && cp <= 0xfeff);
  return inRange && /\p{L}/u.test(ch);
};
const isLatinLetter = (ch: string): boolean => /^[A-Za-z]$/.test(ch);

function wordLanguage(word: string, arabicLanguage: ResponseLanguage): ResponseLanguage | null {
  if (/\p{Script=Hebrew}/u.test(word)) return "he";
  if (/[\p{Script=Hiragana}\p{Script=Katakana}]/u.test(word)) return "ja";
  if (/\p{Script=Hangul}/u.test(word)) return "ko";
  if (/\p{Script=Han}/u.test(word)) return "zh";
  if (/\p{Script=Greek}/u.test(word)) return "el";
  if (/\p{Script=Thai}/u.test(word)) return "th";
  let persian = 0;
  let latin = 0;
  for (const ch of word) {
    if (isPersianLetter(ch)) persian++;
    else if (isLatinLetter(ch)) latin++;
  }
  if (persian === 0 && latin === 0) return null;
  return persian >= latin ? arabicLanguage : "en";
}

/** Presentation fallback only. Backend language metadata takes precedence;
 * a script heuristic cannot distinguish languages sharing the Latin script. */
export function detectLanguage(text: string): ResponseLanguage | null {
  const arabicLanguage = /[ٹڈڑںھہے]/u.test(text) ? "ur" : /[پچژگکی]/u.test(text) ? "fa" : "ar";
  const words = text
    .split(/\s+/)
    .map((word) => wordLanguage(word, arabicLanguage))
    .filter((w): w is ResponseLanguage => w !== null);
  if (words.length === 0) return null;
  const counts = new Map<ResponseLanguage, number>();
  for (const language of words) counts.set(language, (counts.get(language) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
}

/** Normalize browser locale variants without accepting malformed attributes. */
export function canonicalLanguageTag(language: string | null | undefined): string | null {
  if (!language || language.length > 35 || language === "auto") return null;
  try { return Intl.getCanonicalLocales(language.replaceAll("_", "-"))[0] ?? null; }
  catch { return null; }
}

export function languageBase(language: string | null | undefined): string | null {
  return canonicalLanguageTag(language)?.split("-")[0]?.toLowerCase() ?? null;
}

/** Retry/system text has two translated UI catalogs, unlike model replies. */
export function uiLanguageFor(language: ResponseLanguage | null, fallback: UiLanguage): UiLanguage {
  const base = languageBase(language);
  return base === "fa" || base === "en" ? base : fallback;
}

const RTL_LANGUAGES = new Set(["ar", "fa", "he", "ur", "ps", "sd", "ug", "yi", "dv", "ckb", "ks", "syr", "nqo"]);
const RTL_SCRIPTS = new Set(["Arab", "Hebr", "Syrc", "Thaa", "Nkoo", "Adlm", "Rohg", "Mand", "Samr"]);

export function directionFor(language: ResponseLanguage | null): Direction {
  const tag = canonicalLanguageTag(language);
  const script = tag?.split("-").find((part) => /^[A-Z][a-z]{3}$/.test(part));
  if (script) return RTL_SCRIPTS.has(script) ? "rtl" : "ltr";
  return RTL_LANGUAGES.has(languageBase(tag) ?? "") ? "rtl" : "ltr";
}

export type Segment =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "inline"; text: string }
  | { kind: "url"; text: string };

const TOKEN = /```([\s\S]*?)```|`([^`\n]+)`|(https?:\/\/[^\s<>)\]]+)/g;

/**
 * Splits message text into plain text, fenced code, inline code and URLs so
 * that code and URLs can be pinned LTR inside a Persian (RTL) sentence. The
 * result is rendered as React text nodes only, never as HTML.
 */
export function splitSegments(text: string): Segment[] {
  const out: Segment[] = [];
  let last = 0;
  for (const match of text.matchAll(TOKEN)) {
    const index = match.index ?? 0;
    if (index > last) out.push({ kind: "text", text: text.slice(last, index) });
    if (match[1] !== undefined) out.push({ kind: "code", text: match[1].replace(/^\n/, "") });
    else if (match[2] !== undefined) out.push({ kind: "inline", text: match[2] });
    else if (match[3] !== undefined) out.push({ kind: "url", text: match[3] });
    last = index + match[0].length;
  }
  if (last < text.length) out.push({ kind: "text", text: text.slice(last) });
  return out;
}
