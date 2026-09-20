import { describe, expect, it } from "vitest";
import { STRINGS, translate, type StringKey } from "../i18n/strings";
import { detectLanguage, directionFor, splitSegments } from "../lib/language";

const FA_HELLO = "سلام سام، امروز چه خبر؟";
const FA_MIXED = "این API با FastAPI کار می‌کند؟";
const FA_FILE = "لطفاً این فایل را بررسی کن.";

describe("language detection", () => {
  it.each([
    [FA_HELLO, "fa"],
    [FA_MIXED, "fa"],
    [FA_FILE, "fa"],
    ["Please review this file", "en"],
    ["Docker و GitHub را نصب کن", "fa"],
    ["Please explain این خطا", "en"],
    ["12345 😀", null],
    ["", null],
  ])("%s -> %s", (text, expected) => {
    expect(detectLanguage(text)).toBe(expected);
  });

  it("maps language to direction", () => {
    expect(directionFor("fa")).toBe("rtl");
    expect(directionFor("en")).toBe("ltr");
    expect(directionFor(null)).toBe("ltr");
  });
});

describe("segments keep code and URLs intact", () => {
  it("splits fenced code, inline code and URLs out of Persian text", () => {
    const text = "برای نصب اجرا کن:\n```bash\npip install fastapi\n```\nو `uvicorn main:app` را بزن. مستندات: https://fastapi.tiangolo.com/ ممنون";
    const segments = splitSegments(text);
    expect(segments.filter((s) => s.kind === "code").map((s) => s.text)).toEqual(["bash\npip install fastapi\n"]);
    expect(segments.filter((s) => s.kind === "inline").map((s) => s.text)).toEqual(["uvicorn main:app"]);
    expect(segments.filter((s) => s.kind === "url").map((s) => s.text)).toEqual(["https://fastapi.tiangolo.com/"]);
    expect(segments.map((s) => s.text).join("")).toContain("ممنون");
  });

  it("returns plain text untouched, including ZWNJ and Persian digits", () => {
    const text = "می‌کند ۱۲۳۴ و 5678";
    expect(splitSegments(text)).toEqual([{ kind: "text", text }]);
  });

  it("never produces HTML: markup is just text", () => {
    const segments = splitSegments('<img src=x onerror="alert(1)"> `<b>x</b>`');
    expect(segments.some((s) => s.kind === "text" && s.text.includes("<img"))).toBe(true);
  });
});

describe("static UI strings", () => {
  const keys = Object.keys(STRINGS.en) as StringKey[];

  it("Persian covers exactly the English keys, with no empty strings", () => {
    expect(Object.keys(STRINGS.fa).sort()).toEqual(keys.slice().sort());
    for (const key of keys) {
      expect(STRINGS.en[key].trim().length).toBeGreaterThan(0);
      expect(STRINGS.fa[key].trim().length).toBeGreaterThan(0);
    }
  });

  it("security-critical text is genuinely translated, not left in English", () => {
    const persian = /[؀-ۿ]/;
    for (const key of keys.filter((k) => /^(confirm|guest|enroll|error|voice|settings\.(privacy|voiceIdentityOff))/.test(k))) {
      expect(STRINGS.fa[key]).toMatch(persian);
    }
    expect(translate("fa", "confirm.approve")).toBe("تأیید");
    expect(translate("fa", "confirm.deny")).toBe("رد");
    expect(translate("fa", "voice.ownerRequired")).not.toMatch(/[A-Za-z]{4,}/);
  });

  it("critical Persian strings are exactly the canonical logical-order text", () => {
    const fa = (key: StringKey) => STRINGS.fa[key];
    expect(fa("settings.language")).toBe("زبان");
    expect(fa("settings.ownerVoice")).toBe("صدای مالک");
    expect(fa("settings.guestMode")).toBe("حالت مهمان");
    expect(fa("mic.recording")).toBe("در حال ضبط");
    expect(fa("voice.ownerRequired")).toBe("تأیید هویت صوتی مالک لازم است.");
    expect(fa("guest.banner")).toBe("حالت مهمان فعال است");
    expect(fa("enroll.title")).toBe("ثبت صدای مالک");
    expect(fa("confirm.title")).toBe("سام به تأیید شما نیاز دارد");
    expect(fa("error.step_up_failed")).toBe("رمز واردشده پذیرفته نشد.");
  });

  it("stored order is logical, checked by code point (independent of any viewer)", () => {
    // "زبان" must START with ZAIN (U+0632) and END with NOON (U+0646); the
    // reversed "نابز" would start with NOON. Same for the other key words.
    const cp = (s: string) => Array.from(s).map((c) => c.codePointAt(0));
    expect(cp(STRINGS.fa["settings.language"])).toEqual([0x632, 0x628, 0x627, 0x646]);
    expect(cp(STRINGS.fa["settings.ownerVoice"]).slice(0, 4)).toEqual([0x635, 0x62f, 0x627, 0x6cc]); // صدای
    expect(cp(STRINGS.fa["settings.guestMode"]).slice(0, 4)).toEqual([0x62d, 0x627, 0x644, 0x62a]); // حالت
    expect(cp(STRINGS.fa["mic.recording"]).slice(0, 2)).toEqual([0x62f, 0x631]); // در
    expect(STRINGS.fa["settings.language"]).not.toBe("نابز");
    expect(STRINGS.fa["settings.guestMode"]).not.toBe("نامهم تلاح");
  });

  it("no Persian string is pre-shaped or reversed: no presentation forms, no bidi controls", () => {
    for (const key of keys) {
      const value = STRINGS.fa[key];
      // Arabic Presentation Forms-A/B are what shaped or visually reversed text uses.
      expect(value).not.toMatch(/[ﭐ-﷿ﹰ-ﻼ]/);
      expect(value).not.toMatch(/[‪-‮⁦-⁩]/); // no embedded/override bidi controls
    }
  });

  it("Persian words appear in natural order: common final letters end words, not start them", () => {
    // In logical Persian, words far more often END in ی/ه/ا/ن than START with
    // them being final-only. A reversed corpus flips this ratio.
    const words = keys
      .map((k) => STRINGS.fa[k])
      .join(" ")
      .split(/[\s.,:;«»()…—؛،؟!"“”]+/)
      .filter((w) => /^[؀-ۿ‌]+$/.test(w) && w.length >= 3);
    const ending = words.filter((w) => /[یهن]$/.test(w)).length;
    const startingYeh = words.filter((w) => /^ی/.test(w)).length;
    expect(ending).toBeGreaterThan(startingYeh * 3);
    // "مالک" (owner) is used consistently for the owner identity terms.
    expect(Object.values(STRINGS.fa).join(" ")).not.toContain("صاحب");
  });

  it("technical terms stay intact, in Latin, inside Persian text", () => {
    expect(STRINGS.fa["settings.privacy"]).toContain("Keychain");
    expect(STRINGS.fa["enroll.removeBody"]).toContain("Keychain");
    expect(STRINGS.fa["settings.persianTtsNote"]).toContain("Google Gemini");
  });

  it("discloses that Gemini Persian TTS is external, free-tier, with paid fallback OFF", () => {
    expect(STRINGS.en["settings.persianTtsNote"]).toMatch(/external service/);
    expect(STRINGS.en["settings.persianTtsNote"]).toMatch(/sent to Google/);
    expect(STRINGS.en["settings.persianTtsNote"]).toMatch(/Paid fallback is OFF/);
    expect(STRINGS.fa["settings.persianTtsNote"]).toContain("جایگزین پولی خاموش");
    expect(STRINGS.fa["settings.persianTtsNote"]).toContain("برای Google ارسال می‌شود");
  });

  it("has no mojibake or replacement characters", () => {
    for (const language of ["en", "fa"] as const) {
      for (const key of keys) expect(STRINGS[language][key]).not.toMatch(/�|Ø|Ù|Ã/);
    }
  });

  it("strings are static: none contains a template placeholder", () => {
    for (const key of keys) expect(STRINGS.en[key]).not.toMatch(/\$\{|\{\{/);
  });
});
