export function Loading({ label = "Carregando…" }: { label?: string }) {
  return (
    <p role="status" aria-live="polite" className="py-8 text-center text-slate-600">
      {label}
    </p>
  );
}
