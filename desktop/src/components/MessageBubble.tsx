import type { ReactNode } from "react";

export interface ChatMessage {
  id: string;
  role: "user" | "sam" | "notice";
  text: string;
  status?: "ok" | "failed";
  referenceId?: string | null;
  source?: "text" | "voice";
}

/**
 * Message text is model/user/provider content: it is rendered strictly as
 * text (React escapes it). There is no HTML or Markdown interpretation, so a
 * reply containing `<img onerror=…>` or `<script>` is displayed literally.
 */
export function MessageBubble({ message, tools }: { message: ChatMessage; tools?: ReactNode }) {
  const label = message.role === "user" ? "You" : message.role === "sam" ? "Sam" : "Notice";
  return (
    <article className="message" data-role={message.role} data-status={message.status ?? "ok"} aria-label={`${label} message`}>
      <span className="message-label">
        {label}
        {message.source === "voice" ? " · voice" : ""}
      </span>
      <div className="bubble">{message.text}</div>
      {message.status === "failed" && message.referenceId ? (
        <span className="message-meta">Reference: {message.referenceId}</span>
      ) : null}
      {tools ? <div className="message-tools">{tools}</div> : null}
    </article>
  );
}
