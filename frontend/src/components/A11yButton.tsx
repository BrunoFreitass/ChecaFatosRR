import { lazy, Suspense, useRef, useState } from "react";
import { createPortal } from "react-dom";

// Menu de acessibilidade (exceção ao congelamento, 16/10/2026): só o botão fica no bundle
// principal; o painel é carregado sob demanda. Portal no <body>, fora do #root: os filtros de
// "Inverter cores" e "Tons de cinza" (aplicados no #root) não atingem o botão nem o painel.
const A11yPanel = lazy(() => import("./A11yPanel.tsx"));

export const A11Y_PANEL_ID = "a11y-painel";

export function A11yButton() {
  const [open, setOpen] = useState(false);
  const [selection, setSelection] = useState("");
  const buttonRef = useRef<HTMLButtonElement>(null);

  function handleToggle() {
    if (open) {
      close();
    } else {
      const sel = window.getSelection()?.toString().trim() ?? "";
      setSelection((prev) => prev || sel);
      setOpen(true);
    }
  }

  function close() {
    setOpen(false);
    buttonRef.current?.focus();
  }

  return createPortal(
    <>
      <button
        ref={buttonRef}
        type="button"
        aria-label="Acessibilidade: fonte, contraste e leitura em voz alta"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? A11Y_PANEL_ID : undefined}
        onPointerDown={() => {
          if (!open) {
            const sel = window.getSelection()?.toString().trim() ?? "";
            if (sel) setSelection(sel);
          }
        }}
        onClick={handleToggle}
        className="fixed bottom-3 left-3 z-50 inline-flex size-12 items-center justify-center rounded-full border-2 border-white bg-brand-700 text-white shadow-lg hover:bg-brand-800 focus-visible:outline-4 focus-visible:outline-offset-2 focus-visible:outline-slate-900"
      >
        {/* Ícone nosso: pessoa de braços abertos dentro de um círculo. */}
        <svg aria-hidden="true" viewBox="0 0 24 24" className="size-7" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <circle cx="12" cy="12" r="10.5" strokeWidth="1.5" />
          <circle cx="12" cy="6.8" r="1.6" fill="currentColor" stroke="none" />
          <path d="M6.5 9.5 12 10.6l5.5-1.1M12 10.6v3.6M12 14.2 9.3 18.6M12 14.2l2.7 4.4" />
        </svg>
      </button>
      {open && (
        <Suspense fallback={null}>
          <A11yPanel id={A11Y_PANEL_ID} onClose={close} initialSelection={selection} />
        </Suspense>
      )}
    </>,
    document.body,
  );
}
