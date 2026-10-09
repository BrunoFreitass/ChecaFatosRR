import {
  useEffect,
  useId,
  useRef,
  useState,
  type ClipboardEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { useNavigate } from "react-router";

import { useAuth } from "../auth/AuthContext.ts";
import { ApiError, apiFetch } from "../lib/api.ts";
import {
  clearPendingCheck,
  readPendingCheck,
  savePendingCheck,
  type CheckMode,
} from "../lib/pendingCheck.ts";
import type { Check } from "../lib/types.ts";

const MAX_TEXT = 5000;
const MODES: CheckMode[] = ["link", "texto"];
const STEPS: Record<CheckMode, string[]> = {
  link: ["Lendo a notícia…", "Buscando checagens das agências…", "Analisando o conteúdo…"],
  texto: ["Lendo a mensagem…", "Buscando checagens das agências…", "Analisando o conteúdo…"],
};
const STEP_STARTS = [0, 4, 10]; // segundos: uma requisição só, mensagens por tempo
const RETRY_BUSY_MS = 5000;

const URL_ONLY = /^https?:\/\/\S+$/i;
const BARE_DOMAIN = /^[\w-]+(\.[\w-]+)+(\/\S*)?$/;

/** "g1.globo.com/x" → "https://g1.globo.com/x" (quem cola do celular costuma perder o https). */
function withScheme(value: string): string {
  const trimmed = value.trim();
  return BARE_DOMAIN.test(trimmed) && !URL_ONLY.test(trimmed) ? `https://${trimmed}` : trimmed;
}

function looksLikeLink(value: string): boolean {
  const trimmed = value.trim();
  return URL_ONLY.test(trimmed) || BARE_DOMAIN.test(trimmed);
}

/** Espera que termina antes se o envio for cancelado (a pessoa saiu da tela). */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(new DOMException("Cancelado", "AbortError"));
      },
      { once: true },
    );
  });
}

type FormError = { message: string; suggestText: boolean };

export function CheckForm() {
  const { status } = useAuth();
  const navigate = useNavigate();
  const baseId = useId();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const linkRef = useRef<HTMLInputElement>(null);
  const alertRef = useRef<HTMLDivElement>(null);
  const tabRefs = useRef<Record<CheckMode, HTMLButtonElement | null>>({ link: null, texto: null });
  const inFlight = useRef<AbortController | null>(null);

  const [pending] = useState(readPendingCheck);
  const [mode, setMode] = useState<CheckMode>(pending?.modo ?? "link");
  const [link, setLink] = useState(pending?.modo === "link" ? pending.valor : "");
  const [text, setText] = useState(pending?.modo === "texto" ? pending.valor : "");
  const [submitting, setSubmitting] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<FormError | null>(null);
  // Pedido de foco no campo da aba atual (depois que o React renderizou o painel novo).
  const [focusField, setFocusField] = useState(0);

  // O rascunho guardado antes do login já foi para o formulário: apaga.
  useEffect(() => {
    if (pending) clearPendingCheck();
  }, [pending]);

  // Saiu da tela no meio da checagem: cancela (sem navegar depois nem tentar de novo).
  useEffect(() => () => inFlight.current?.abort(), []);

  useEffect(() => {
    if (!submitting) return;
    const started = Date.now();
    const timer = window.setInterval(() => setElapsed((Date.now() - started) / 1000), 500);
    return () => window.clearInterval(timer);
  }, [submitting]);

  useEffect(() => {
    if (focusField === 0) return;
    (mode === "texto" ? textareaRef : linkRef).current?.focus();
  }, [focusField, mode]);

  useEffect(() => {
    if (error) alertRef.current?.focus();
  }, [error]);

  const tooLong = text.length > MAX_TEXT;
  const value = mode === "link" ? link.trim() : text.trim();
  const step = STEP_STARTS.filter((start) => elapsed >= start).length - 1;

  function switchTo(next: CheckMode, { focusInput = false } = {}) {
    setMode(next);
    setError(null);
    if (focusInput) setFocusField((count) => count + 1);
  }

  // Padrão ARIA de abas: setas, Home e End movem o foco entre as abas (e ativam a aba).
  function onTabKey(event: KeyboardEvent<HTMLButtonElement>) {
    const index = MODES.indexOf(mode);
    const targets: Record<string, number> = {
      ArrowRight: (index + 1) % MODES.length,
      ArrowLeft: (index - 1 + MODES.length) % MODES.length,
      Home: 0,
      End: MODES.length - 1,
    };
    const target = targets[event.key];
    if (target === undefined) return;
    event.preventDefault();
    const next = MODES[target] ?? "link";
    switchTo(next);
    tabRefs.current[next]?.focus();
  }

  // Colou uma mensagem inteira no campo de link: leva para a aba de texto.
  function onLinkPaste(event: ClipboardEvent<HTMLInputElement>) {
    const pasted = event.clipboardData.getData("text");
    if (pasted.trim().length > 60 && !looksLikeLink(pasted)) {
      event.preventDefault();
      setText((current) => (current.trim() ? `${current.trimEnd()}\n${pasted}` : pasted));
      switchTo("texto", { focusInput: true });
    }
  }

  async function post(body: { url: string } | { texto: string }, signal: AbortSignal): Promise<Check> {
    try {
      return await apiFetch<Check>("/checks", { method: "POST", body, signal });
    } catch (err) {
      // Alguém está checando o mesmo conteúdo agora: espera e tenta uma vez mais.
      if (err instanceof ApiError && err.code === "checagem_em_andamento") {
        await sleep(RETRY_BUSY_MS, signal);
        return apiFetch<Check>("/checks", { method: "POST", body, signal });
      }
      throw err;
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // A trava é o ref (dois cliques no mesmo instante passariam pelo estado).
    if (!value || inFlight.current || tooLong) return;

    if (status === "anonymous" || status === "needs_profile") {
      savePendingCheck({ modo: mode, valor: mode === "link" ? link : text });
      navigate(status === "anonymous" ? `/entrar?volta=${encodeURIComponent("/")}` : "/bem-vindo");
      return;
    }

    const controller = new AbortController();
    inFlight.current = controller;
    setSubmitting(true);
    setElapsed(0);
    setError(null);
    try {
      const body = mode === "link" ? { url: withScheme(link) } : { texto: text };
      const check = await post(body, controller.signal);
      navigate(`/checagem/${check.id}`, { state: { check } });
    } catch (err) {
      if (controller.signal.aborted) return;
      if (err instanceof ApiError && err.status === 401) {
        savePendingCheck({ modo: mode, valor: mode === "link" ? link : text });
        navigate(`/entrar?volta=${encodeURIComponent("/")}`);
        return;
      }
      setError({
        message: err instanceof ApiError ? err.message : "Não foi possível checar agora. Tente de novo.",
        suggestText: err instanceof ApiError && err.details.sugestao === "colar_texto",
      });
    } finally {
      if (inFlight.current === controller) inFlight.current = null;
      if (!controller.signal.aborted) setSubmitting(false);
    }
  }

  const tabClass = (active: boolean) =>
    `min-h-11 flex-1 rounded-md px-3 font-semibold focus-visible:outline-2 focus-visible:outline-brand-700 ${
      active ? "bg-white text-brand-800 shadow-sm" : "text-slate-600 hover:text-slate-900"
    }`;
  const fieldClass = `block w-full rounded-lg border border-slate-300 bg-white text-base focus:border-brand-700 focus:outline-2 focus:outline-brand-700 ${
    submitting ? "opacity-70" : ""
  }`;

  return (
    <form onSubmit={(event) => void handleSubmit(event)} className="space-y-4" noValidate>
      <div role="tablist" aria-label="O que você quer checar?" className="flex gap-1 rounded-lg bg-slate-200 p-1">
        {MODES.map((tab) => (
          <button
            key={tab}
            ref={(element) => {
              tabRefs.current[tab] = element;
            }}
            type="button"
            role="tab"
            id={`${baseId}-tab-${tab}`}
            aria-selected={mode === tab}
            aria-controls={`${baseId}-panel`}
            tabIndex={mode === tab ? 0 : -1}
            onClick={() => switchTo(tab)}
            onKeyDown={onTabKey}
            className={tabClass(mode === tab)}
          >
            {tab === "link" ? "Link" : "Texto da mensagem"}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id={`${baseId}-panel`}
        aria-labelledby={`${baseId}-tab-${mode}`}
        className="space-y-2"
      >
        {mode === "link" ? (
          <>
            <label htmlFor={`${baseId}-link`} className="block font-semibold">
              Cole o link da notícia
            </label>
            <input
              ref={linkRef}
              id={`${baseId}-link`}
              type="url"
              inputMode="url"
              autoComplete="off"
              placeholder="https://"
              value={link}
              onChange={(event) => setLink(event.target.value)}
              onPaste={onLinkPaste}
              readOnly={submitting}
              className={`${fieldClass} min-h-12 px-3`}
            />
          </>
        ) : (
          <>
            <label htmlFor={`${baseId}-texto`} className="block font-semibold">
              Cole aqui a mensagem que você recebeu no WhatsApp
            </label>
            <textarea
              ref={textareaRef}
              id={`${baseId}-texto`}
              rows={7}
              value={text}
              onChange={(event) => setText(event.target.value)}
              readOnly={submitting}
              aria-describedby={`${baseId}-contador ${baseId}-aviso`}
              aria-invalid={tooLong}
              className={`${fieldClass} p-3`}
            />
            <p
              id={`${baseId}-contador`}
              className={`text-right text-sm ${tooLong ? "font-semibold text-red-800" : "text-slate-600"}`}
            >
              {text.length.toLocaleString("pt-BR")} / {MAX_TEXT.toLocaleString("pt-BR")}
              {tooLong && " — cole só a parte principal"}
            </p>
            <p id={`${baseId}-aviso`} className="text-sm text-slate-600">
              Não cole conversas pessoais. Quem receber o link do resultado verá o texto, sem o seu
              nome. Telefones, e-mails e CPF são escondidos automaticamente.
            </p>
          </>
        )}
      </div>

      <button
        type="submit"
        disabled={!value || submitting || tooLong || status === "loading"}
        className="inline-flex min-h-12 w-full items-center justify-center rounded-lg bg-brand-700 px-4 text-lg font-semibold text-white hover:bg-brand-800 disabled:opacity-60 sm:w-auto"
      >
        {submitting ? "Checando…" : "Checar"}
      </button>

      <p role="status" aria-live="polite" className="min-h-6 text-slate-700">
        {submitting ? STEPS[mode][Math.max(0, step)] : ""}
      </p>

      {error && (
        <div
          ref={alertRef}
          role="alert"
          tabIndex={-1}
          className="space-y-3 rounded-lg border border-red-200 bg-red-50 p-4 text-red-900 outline-none"
        >
          <p>{error.message}</p>
          {error.suggestText && mode === "link" && (
            <button
              type="button"
              onClick={() => switchTo("texto", { focusInput: true })}
              className="min-h-11 rounded-md bg-red-800 px-4 font-semibold text-white"
            >
              Colar o texto da mensagem
            </button>
          )}
        </div>
      )}
    </form>
  );
}
