import { useEffect, useRef, useState } from "react";

const BUTTON =
  "inline-flex min-h-11 items-center justify-center rounded-lg px-4 font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-700";

/** Compartilhar o resultado (o público principal recebe e repassa pelo WhatsApp). */
export function ShareButtons({ text, url }: { text: string; url: string }) {
  const [copied, setCopied] = useState(false);
  const [showLink, setShowLink] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const message = `${text}\n${url}`;
  const canShare = typeof navigator !== "undefined" && typeof navigator.share === "function";

  useEffect(() => () => window.clearTimeout(timer.current), []);

  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 3000);
    } catch {
      // Sem acesso à área de transferência (navegador dentro de app): mostra o link para copiar.
      setShowLink(true);
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        <a
          href={`https://wa.me/?text=${encodeURIComponent(message)}`}
          target="_blank"
          rel="noopener noreferrer"
          className={`${BUTTON} bg-green-700 text-white hover:bg-green-800`}
        >
          Enviar no WhatsApp
        </a>
        {canShare && (
          <button
            type="button"
            onClick={() => void navigator.share({ text, url }).catch(() => undefined)}
            className={`${BUTTON} border border-slate-300 bg-white text-slate-900 hover:bg-slate-50`}
          >
            Compartilhar
          </button>
        )}
        <button
          type="button"
          onClick={() => void copy()}
          className={`${BUTTON} border border-slate-300 bg-white text-slate-900 hover:bg-slate-50`}
        >
          Copiar link
        </button>
      </div>
      <p role="status" aria-live="polite" className="min-h-5 text-sm text-green-800">
        {copied ? "Link copiado!" : ""}
      </p>
      {showLink && (
        <label className="block text-sm">
          Copie o link:
          <input
            readOnly
            value={url}
            onFocus={(event) => event.currentTarget.select()}
            className="mt-1 block min-h-11 w-full rounded-lg border border-slate-300 bg-white px-3 text-base"
          />
        </label>
      )}
    </div>
  );
}
