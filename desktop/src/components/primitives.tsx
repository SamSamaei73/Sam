import { useId, type ButtonHTMLAttributes, type HTMLAttributes, type ReactNode } from "react";

export function GlassPanel({
  className = "",
  children,
  ...rest
}: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={`glass panel ${className}`.trim()} {...rest}>
      {children}
    </div>
  );
}

type NeonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "quiet" | "danger";
};

export function NeonButton({ variant = "primary", className = "", type = "button", ...rest }: NeonProps) {
  return <button type={type} data-variant={variant} className={`neon-button ${className}`.trim()} {...rest} />;
}

type IconButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label"> & {
  label: string;
  active?: boolean;
  primary?: boolean;
  selected?: boolean;
  children: ReactNode;
};

/** Icon-only buttons must always carry an accessible name. */
export function IconButton({
  label,
  active,
  primary,
  selected,
  children,
  type = "button",
  ...rest
}: IconButtonProps) {
  return (
    <button
      type={type}
      className="icon-button"
      aria-label={label}
      title={label}
      data-active={active ? "true" : undefined}
      data-tone={primary ? "primary" : undefined}
      data-selected={selected ? "true" : undefined}
      {...rest}
    >
      {children}
    </button>
  );
}

export type Tone = "ok" | "warn" | "danger" | "info" | "muted" | "low" | "medium" | "high" | "critical";

export function StatusPill({ tone, label, detail }: { tone: Tone; label: string; detail?: string }) {
  return (
    <span className="pill" data-tone={tone}>
      <span className="pill-text">{label}</span>
      {detail ? <span className="faint">{detail}</span> : null}
    </span>
  );
}

/** Dropdown pill ("New Chat ⌄" in the reference): a real, labelled <select>. */
export function SelectPill({
  label,
  value,
  onChange,
  options,
  size,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  size?: "sm";
}) {
  return (
    <span className="select-pill" data-size={size}>
      <select aria-label={label} value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </span>
  );
}

export function SidebarItem({
  icon,
  label,
  current,
  onSelect,
}: {
  icon: ReactNode;
  label: string;
  current: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className="sidebar-item"
      aria-current={current ? "page" : undefined}
      onClick={onSelect}
      title={label}
    >
      {icon}
      <span className="sidebar-label">{label}</span>
    </button>
  );
}

export function EmptyState({ title, body, action }: { title: string; body: string; action?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      <p>{body}</p>
      {action}
    </div>
  );
}

export function SectionHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="section-header">
      <div>
        <h2>{title}</h2>
        {description ? <p>{description}</p> : null}
      </div>
      {actions ? <div className="row">{actions}</div> : null}
    </div>
  );
}

export function Tooltip({ text, children }: { text: string; children: ReactNode }) {
  const id = useId();
  return (
    <span className="tooltip-wrap" aria-describedby={id}>
      {children}
      <span role="tooltip" id={id} className="tooltip">
        {text}
      </span>
    </span>
  );
}

export function Notice({
  tone,
  children,
  live = true,
}: {
  tone?: "danger" | "warn";
  children: ReactNode;
  live?: boolean;
}) {
  return (
    <div className="notice" data-tone={tone} role={live ? (tone === "danger" ? "alert" : "status") : undefined}>
      {children}
    </div>
  );
}
