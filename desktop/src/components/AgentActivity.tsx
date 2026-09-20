export function AgentActivity({ label }: { label: string }) {
  return (
    <div className="glass activity-strip" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}
