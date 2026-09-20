import type { ReactNode } from "react";
import type { Direction, ResponseLanguage } from "../bridge/types";
import { detectLanguage, directionFor, splitSegments } from "../lib/language";
import { useSam } from "../state";

export interface ChatMessage {
  id: string;
  role: "user" | "sam" | "notice" | "guest";
  text: string;
  status?: "ok" | "failed";
  referenceId?: string | null;
  source?: "text" | "voice";
  /** Backend-chosen presentation language; detected from the text if absent. */
  language?: ResponseLanguage | null;
  direction?: Direction | null;
}

/**
 * Message text is model/user/provider content: it is rendered strictly as
 * React text nodes (never HTML or Markdown). Each message carries its own
 * lang/dir, so a Persian answer reads right-to-left inside an English UI (and
 * vice-versa) without flipping the rest of the app. Fenced/inline code and
 * URLs stay left-to-right inside a Persian sentence.
 */
export function MessageText({ message }: { message: ChatMessage }) {
  const language = message.language ?? detectLanguage(message.text) ?? "en";
  const direction = message.direction ?? directionFor(language);
  return (
    <div className="bubble" lang={language} dir={direction} data-direction={direction}>
      {splitSegments(message.text).map((segment, index) => {
        switch (segment.kind) {
          case "code":
            return (
              <pre key={index} className="code-block" dir="ltr" lang="en">
                {segment.text}
              </pre>
            );
          case "inline":
            return (
              <bdi key={index} dir="ltr" lang="en" className="inline-code">
                {segment.text}
              </bdi>
            );
          case "url":
            return (
              <bdi key={index} dir="ltr" lang="en" className="url">
                {segment.text}
              </bdi>
            );
          default:
            return <span key={index}>{segment.text}</span>;
        }
      })}
    </div>
  );
}

export function MessageBubble({ message, tools }: { message: ChatMessage; tools?: ReactNode }) {
  const { t } = useSam();
  const label =
    message.role === "user"
      ? t("chat.you")
      : message.role === "sam"
        ? t("chat.sam")
        : message.role === "guest"
          ? t("chat.guest")
          : "Notice";
  return (
    <article
      className="message"
      data-role={message.role}
      data-status={message.status ?? "ok"}
      aria-label={`${label}`}
    >
      <span className="message-label">
        {label}
        {message.source === "voice" ? " · voice" : ""}
      </span>
      <MessageText message={message} />
      {message.status === "failed" && message.referenceId ? (
        <span className="message-meta">Reference: {message.referenceId}</span>
      ) : null}
      {tools ? <div className="message-tools">{tools}</div> : null}
    </article>
  );
}
