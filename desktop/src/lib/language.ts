import type { Direction, ResponseLanguage } from "../bridge/types";

/**
 * Persian/English text helpers. They only decide presentation (which way a
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

function wordLanguage(word: string): ResponseLanguage | null {
  let persian = 0;
  let latin = 0;
  for (const ch of word) {
    if (isPersianLetter(ch)) persian++;
    else if (isLatinLetter(ch)) latin++;
  }
  if (persian === 0 && latin === 0) return null;
  return persian >= latin ? "fa" : "en";
}

/** Same word-count rule as the backend: the language carrying the sentence. */
export function detectLanguage(text: string): ResponseLanguage | null {
  const words = text
    .split(/\s+/)
    .map(wordLanguage)
    .filter((w): w is ResponseLanguage => w !== null);
  if (words.length === 0) return null;
  const fa = words.filter((w) => w === "fa").length;
  const en = words.length - fa;
  if (fa === en) return words[0] ?? null;
  return fa > en ? "fa" : "en";
}

export function directionFor(language: ResponseLanguage | null): Direction {
  return language === "fa" ? "rtl" : "ltr";
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
