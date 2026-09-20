import { useRef, useState, type ReactNode } from "react";
import { IconArrowUp } from "./Icons";
import { IconButton } from "./primitives";

export const MAX_COMPOSER_CHARS = 100_000;

/**
 * The reference-style composer: a luminous glass box with the text area on
 * top and a divider + control bar underneath (leading chip, trailing
 * squircle buttons). Enter sends, Shift+Enter inserts a newline.
 */
export function Composer({
  disabled,
  onSend,
  leading,
  trailing,
}: {
  disabled: boolean;
  onSend: (text: string) => void;
  leading?: ReactNode;
  trailing?: ReactNode;
}) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  const trimmed = text.trim();
  const tooLong = text.length > MAX_COMPOSER_CHARS;
  const canSend = !disabled && trimmed.length > 0 && !tooLong;

  const submit = () => {
    if (!canSend) return;
    onSend(trimmed);
    setText("");
    ref.current?.focus();
  };

  return (
    <form
      className="rim composer"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <label className="sr-only" htmlFor="composer-input">
        Message Sam
      </label>
      <textarea
        id="composer-input"
        ref={ref}
        rows={2}
        value={text}
        placeholder="Ask Sam anything…"
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            submit();
          }
        }}
        aria-invalid={tooLong}
      />
      {tooLong ? (
        <span role="alert" className="faint">
          Message is too long.
        </span>
      ) : null}
      <div className="composer-bar">
        <div className="lead">{leading}</div>
        <div className="trail">
          {trailing}
          <IconButton label="Send message" type="submit" primary disabled={!canSend}>
            <IconArrowUp />
          </IconButton>
        </div>
      </div>
    </form>
  );
}
