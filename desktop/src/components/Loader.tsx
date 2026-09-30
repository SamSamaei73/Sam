/**
 * The one full-view loading indicator: a small luminous ring, centred both
 * horizontally and vertically in its container, with no buttons or prose.
 * Loading is never an error: errors use ``Notice`` instead.
 */
export function Loader({ label = "Loading" }: { label?: string }) {
  return (
    <div className="loader-wrap" role="status" aria-live="polite">
      <span className="loader-ring" aria-hidden="true" />
      <span className="sr-only">{label}…</span>
    </div>
  );
}
