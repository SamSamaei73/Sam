import { useRef, useState, type ReactNode } from "react";
import { IconArrowUp } from "./Icons";
import { useSam } from "../state";
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
  value,
  onChange,
}: {
  /** Disables SENDING only; typing is never blocked. */
  disabled: boolean;
  onSend: (text: string) => void;
  leading?: ReactNode;
  trailing?: ReactNode;
  /** Optional controlled draft (kept by the caller, e.g. across the overlay closing). */
  value?: string;
  onChange?: (text: string) => void;
}) {
  const { t } = useSam();
  const [own, setOwn] = useState("");
  const text = value ?? own;
  const setText = onChange ?? setOwn;
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
        placeholder={t("chat.placeholder")}
        dir="auto"
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
          <IconButton label={t("chat.send")} type="submit" primary disabled={!canSend}>
            <IconArrowUp />
          </IconButton>
        </div>
      </div>
    </form>
  );
}
