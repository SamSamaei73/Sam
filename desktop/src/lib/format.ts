export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function humanize(value: string): string {
  const text = value.replace(/[_:.-]+/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Extension -> Knowledge resource kind, or null when unsupported. */
export function resourceKindFor(filename: string): "pdf" | "txt" | "markdown" | "json" | "csv" | null {
  const ext = filename.toLowerCase().split(".").pop() ?? "";
  switch (ext) {
    case "pdf":
      return "pdf";
    case "txt":
      return "txt";
    case "md":
    case "markdown":
      return "markdown";
    case "json":
      return "json";
    case "csv":
      return "csv";
    default:
      return null;
  }
}

export const MAX_UPLOAD_BYTES = 20_000_000;
