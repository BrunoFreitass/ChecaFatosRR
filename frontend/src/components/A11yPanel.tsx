import { useEffect, useRef, useState } from "react";

import {
  applyPrefs,
  DEFAULT_PREFS,
  FONT_MAX,
  FONT_MIN,
  loadPrefs,
  savePrefs,
  type A11yPrefs,
} from "../lib/a11y.ts";
import { findPtBrVoice, mainText, splitForSpeech } from "../lib/ouvir.ts";

// Painel do menu de acessibilidade (chunk próprio, carregado pelo A11yButton). Diálogo modal:
// foco preso aqui dentro enquanto aberto, Esc fecha e o A11yButton devolve o foco ao botão.

type Toggle = Exclude<keyof A11yPrefs, "fonte">;

const TOGGLES: { key: Toggle; label: string }[] = [
  { key: "contraste", label: "Alto contraste" },
  { key: "inverter", label: "Inverter cores" },
  { key: "cinza", label: "Tons de cinza (monocromático)" },
  { key: "links", label: "Sublinhar links" },
  { key: "espaco", label: "Mais espaço entre linhas e letras" },
  { key: "pausar", label: "Pausar animações" },
  { key: "cursor", label: "Cursor grande" },
];

const FONT_PCT: Record<number, string> = { [-1]: "87,5%", 0: "100%", 1: "112,5%", 2: "125%", 3: "150%" };

// Sem voz pt-BR depois deste tempo, o "Ouvir" fica desabilitado (algumas carregam com atraso).
const VOICES_WAIT_MS = 2000;

const FOCUSABLE = "button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex='-1'])";

// Botões que chegam ao limite (A+, A−, Pausar, Parar) usam aria-disabled, não disabled: com
// disabled, o botão focado perde o foco e ele cai no <body>.
const BUTTON =
  "inline-flex min-h-11 items-center justify-center rounded-md border border-slate-300 px-3 text-sm font-semibold hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand-700 disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:cursor-not-allowed aria-disabled:opacity-50";

/** Tab e Shift+Tab circulam só entre os controles do painel (foco preso enquanto aberto). */
function trapFocus(dialog: HTMLElement, event: KeyboardEvent) {
  const items = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE)];
  const first = items.at(0);
  const last = items.at(-1);
  if (!first || !last) return;
  const active = document.activeElement;
  const inside = active instanceof Node && dialog.contains(active);
  if (event.shiftKey && (!inside || active === first || active === dialog)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (!inside || active === last)) {
    event.preventDefault();
    first.focus();
  }
}

/** Liga ou desliga uma opção. Alto contraste e inverter cores se excluem: juntos, a inversão
 * do fundo preto do contraste deixaria a página branca. */
function toggle(prefs: A11yPrefs, key: Toggle): A11yPrefs {
  const next = { ...prefs, [key]: !prefs[key] };
  if (key === "contraste" && next.contraste) next.inverter = false;
  if (key === "inverter" && next.inverter) next.contraste = false;
  return next;
}

type Speech = "idle" | "speaking" | "paused";

/** Texto selecionado na página agora (seleção dentro do próprio painel não conta). */
function pageSelection(dialog: HTMLElement | null): string {
  const selection = window.getSelection();
  if (!selection) return "";
  const text = selection.toString().trim();
  if (!text) return "";
  const anchor = selection.anchorNode;
  if (dialog && anchor && dialog.contains(anchor)) return "";
  return text;
}

export default function A11yPanel({
  id,
  onClose,
  capturedSelection = "",
}: {
  id: string;
  onClose: () => void;
  /** Seleção feita antes de abrir (o botão a descarta ao fechar, ao trocar de página e ao tocar fora). */
  capturedSelection?: string;
}) {
  const [prefs, setPrefs] = useState<A11yPrefs>(loadPrefs);
  const dialogRef = useRef<HTMLDivElement>(null);

  const synth = "speechSynthesis" in window ? window.speechSynthesis : null;
  // undefined = ainda procurando; null = sem voz pt-BR (ou navegador sem Web Speech).
  const [voice, setVoice] = useState<SpeechSynthesisVoice | null | undefined>(synth ? undefined : null);
  const [speech, setSpeech] = useState<Speech>("idle");
  const [status, setStatus] = useState("");
  const runRef = useRef(0);
  // Falas em fila guardadas aqui: o Chrome pode descartar uma fala sem referência antes do onend.
  const queueRef = useRef<SpeechSynthesisUtterance[]>([]);

  /** Encerra qualquer leitura: callbacks antigos passam a ser ignorados (runRef). */
  function stopSpeech(engine: SpeechSynthesis) {
    runRef.current += 1;
    queueRef.current = [];
    engine.cancel();
    // No Chrome, cancel() não tira o estado "pausado": sem resume(), a próxima fala fica muda.
    engine.resume();
  }

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  // Esc fecha; Tab fica preso no painel.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      } else if (event.key === "Tab" && dialogRef.current) {
        trapFocus(dialogRef.current, event);
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    if (!synth) return;
    const pick = () => {
      const found = findPtBrVoice(synth.getVoices());
      if (found) setVoice(found);
    };
    pick();
    synth.addEventListener?.("voiceschanged", pick);
    const timer = window.setTimeout(() => setVoice((v) => v ?? null), VOICES_WAIT_MS);
    return () => {
      synth.removeEventListener?.("voiceschanged", pick);
      window.clearTimeout(timer);
      // Fechar o menu encerra a leitura (e nenhuma fala fica órfã sem botão de parar).
      stopSpeech(synth);
    };
  }, [synth]);

  function update(next: A11yPrefs) {
    setPrefs(next);
    applyPrefs(next);
    savePrefs(next);
  }

  function ouvir() {
    if (!synth || !voice) return;
    stopSpeech(synth);
    const run = runRef.current;
    // Só a seleção atual: a feita agora na página ou a capturada nesta abertura (ainda válida).
    const selected = pageSelection(dialogRef.current) || capturedSelection;
    const parts = splitForSpeech(selected || mainText(document));
    if (parts.length === 0) {
      setSpeech("idle");
      setStatus("Não há texto para ler nesta página.");
      return;
    }
    const finish = (message: string) => {
      if (run !== runRef.current) return;
      queueRef.current = [];
      setSpeech("idle");
      setStatus(message);
    };
    queueRef.current = parts.map((part, index) => {
      const utterance = new SpeechSynthesisUtterance(part);
      utterance.voice = voice;
      utterance.lang = "pt-BR";
      if (index === parts.length - 1) utterance.onend = () => finish("Leitura concluída.");
      utterance.onerror = (event) => {
        if (event.error === "interrupted" || event.error === "canceled" || run !== runRef.current) return;
        stopSpeech(synth); // erro de verdade: o resto da fila não continua sozinho
        setSpeech("idle");
        setStatus("A leitura foi interrompida.");
      };
      return utterance;
    });
    for (const utterance of queueRef.current) synth.speak(utterance);
    setSpeech("speaking");
    setStatus(selected ? "Lendo o trecho selecionado." : "Lendo o conteúdo da página.");
  }

  function pausar() {
    if (!synth) return;
    if (speech === "speaking") {
      synth.pause();
      setSpeech("paused");
      setStatus("Leitura pausada.");
    } else if (speech === "paused") {
      synth.resume();
      setSpeech("speaking");
      setStatus("Leitura retomada.");
    }
  }

  function parar() {
    if (!synth || speech === "idle") return;
    stopSpeech(synth);
    setSpeech("idle");
    setStatus("Leitura parada.");
  }

  const titleId = `${id}-titulo`;
  return (
    <div
      ref={dialogRef}
      id={id}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      tabIndex={-1}
      className="fixed bottom-18 left-3 z-50 max-h-[calc(100dvh-6rem)] w-[min(22rem,calc(100vw-1.5rem))] overflow-y-auto rounded-xl border border-slate-300 bg-white p-4 text-slate-900 shadow-2xl outline-none"
    >
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 id={titleId} className="text-lg font-bold">
          Acessibilidade
        </h2>
        <button type="button" onClick={onClose} className={BUTTON} aria-label="Fechar o menu de acessibilidade">
          Fechar
        </button>
      </div>

      <div role="group" aria-labelledby={`${id}-fonte`} className="mb-4">
        <p id={`${id}-fonte`} className="mb-1 text-sm font-semibold">
          Tamanho do texto
        </p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className={BUTTON}
            aria-label="Diminuir o texto"
            aria-disabled={prefs.fonte <= FONT_MIN}
            onClick={() => prefs.fonte > FONT_MIN && update({ ...prefs, fonte: prefs.fonte - 1 })}
          >
            A−
          </button>
          <output aria-live="polite" className="min-w-16 text-center text-sm">
            {FONT_PCT[prefs.fonte]}
          </output>
          <button
            type="button"
            className={BUTTON}
            aria-label="Aumentar o texto"
            aria-disabled={prefs.fonte >= FONT_MAX}
            onClick={() => prefs.fonte < FONT_MAX && update({ ...prefs, fonte: prefs.fonte + 1 })}
          >
            A+
          </button>
        </div>
      </div>

      <ul className="mb-4 grid gap-2" aria-label="Opções de cores, texto e movimento">
        {TOGGLES.map(({ key, label }) => (
          <li key={key}>
            <button
              type="button"
              aria-pressed={prefs[key]}
              onClick={() => update(toggle(prefs, key))}
              className={`${BUTTON} w-full justify-between gap-2 text-left aria-pressed:border-brand-700 aria-pressed:bg-brand-50`}
            >
              <span>{label}</span>
              <span aria-hidden="true">{prefs[key] ? "Ligado" : "Desligado"}</span>
            </button>
          </li>
        ))}
      </ul>
      {prefs.cinza && (
        <p className="-mt-2 mb-4 text-sm text-slate-700">
          Nos tons de cinza o mapa da Apuração perde as cores: use “Ver em tabela” logo abaixo dele.
        </p>
      )}

      <div role="group" aria-labelledby={`${id}-ouvir`} className="mb-4">
        <p id={`${id}-ouvir`} className="mb-1 text-sm font-semibold">
          Ouvir a página
        </p>
        <div className="flex flex-wrap gap-2">
          <button type="button" className={BUTTON} disabled={!voice} onClick={ouvir}>
            Ouvir
          </button>
          <button type="button" className={BUTTON} aria-disabled={speech === "idle"} onClick={pausar}>
            {speech === "paused" ? "Continuar" : "Pausar"}
          </button>
          <button type="button" className={BUTTON} aria-disabled={speech === "idle"} onClick={parar}>
            Parar
          </button>
        </div>
        <p className="mt-1 text-xs text-slate-600">
          {voice === null
            ? "Este aparelho não tem voz em português do Brasil; a leitura em voz alta não está disponível."
            : "Lê o trecho selecionado ou, sem seleção, o conteúdo principal. Fechar o menu encerra a leitura."}
        </p>
        <p role="status" aria-live="polite" className="mt-1 text-sm">
          {status}
        </p>
      </div>

      <button type="button" className={`${BUTTON} w-full`} onClick={() => update({ ...DEFAULT_PREFS })}>
        Restaurar padrão
      </button>
    </div>
  );
}
