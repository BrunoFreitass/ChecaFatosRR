import { useEffect, useRef, useState } from "react";

import { useAuth } from "../auth/AuthContext.ts";
import { Loading } from "../components/Loading.tsx";
import { ApiError } from "../lib/api.ts";
import { fetchQueue, queueAction, queueReasonLabel, type AdminAction, type QueueItem } from "../lib/comments.ts";
import { formatDateTime } from "../lib/format.ts";

const BUTTON = "min-h-11 rounded-md border border-slate-300 bg-white px-3 font-semibold disabled:opacity-60";
const DANGER = "min-h-11 rounded-md bg-red-800 px-3 font-semibold text-white disabled:opacity-60";

/** Ações de cada tipo: comentário publicado (revisão) ou bloqueado pela IA (confirmação). */
function actionsFor(item: QueueItem): { acao: AdminAction; label: string }[] {
  if (item.tipo === "strike_confirmation") {
    return [
      { acao: "aprovar", label: "Publicar" },
      { acao: "confirmar_strike", label: "Confirmar strike" },
      { acao: "descartar", label: "Descartar" },
    ];
  }
  return [
    { acao: "aprovar", label: "Aprovar" },
    { acao: "remover", label: "Remover" },
  ];
}

function QueueCard({ item, onDone }: { item: QueueItem; onDone: (id: number) => void }) {
  const [busy, setBusy] = useState(false);
  const [confirmingBan, setConfirmingBan] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function act(acao: AdminAction) {
    setBusy(true);
    setError(null);
    try {
      await queueAction(item.id, acao);
      onDone(item.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Não foi possível concluir.");
      setBusy(false);
    }
  }

  const who = item.apelido ?? "conta excluída";
  return (
    <li className="space-y-3 rounded-lg border border-slate-200 bg-white p-4">
      <p className="text-sm font-semibold text-slate-700">{queueReasonLabel(item.motivo)}</p>
      <p className="text-sm text-slate-600">
        {who} · {formatDateTime(item.criado_em)}
        {item.categoria && (
          <>
            {" "}
            · {item.categoria}
            {item.confianca !== null && <> · confiança {Math.round(item.confianca * 100)}%</>}
          </>
        )}
      </p>
      {/* Texto do usuário: só texto, nunca HTML (o React escapa). */}
      <p className="whitespace-pre-wrap break-words rounded bg-slate-50 p-3 text-slate-900">
        {item.texto ?? "(sem texto)"}
      </p>
      <div className="flex flex-wrap gap-2">
        {actionsFor(item).map(({ acao, label }) => (
          <button
            key={acao}
            type="button"
            className={BUTTON}
            aria-label={`${label}: item ${item.id} de ${who}`}
            disabled={busy}
            onClick={() => void act(acao)}
          >
            {label}
          </button>
        ))}
        {item.apelido && !confirmingBan && (
          <button
            type="button"
            className={DANGER}
            aria-label={`Banir ${who} (item ${item.id})`}
            disabled={busy}
            onClick={() => setConfirmingBan(true)}
          >
            Banir
          </button>
        )}
      </div>
      {confirmingBan && (
        <div role="group" aria-label="Confirmar banimento" className="space-y-2 rounded-lg bg-red-50 p-3">
          <p className="font-semibold text-red-900">
            Banir {who}? A conta não poderá mais comentar, nem com uma conta nova do mesmo Google.
          </p>
          <div className="flex flex-wrap gap-2">
            <button type="button" className={DANGER} disabled={busy} onClick={() => void act("banir")}>
              Sim, banir {who}
            </button>
            <button type="button" className={BUTTON} disabled={busy} onClick={() => setConfirmingBan(false)}>
              Cancelar
            </button>
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-red-800">
          {error}
        </p>
      )}
    </li>
  );
}

/** Fila de moderação mínima (o painel completo fica para a etapa 7). Só ADMIN_EMAIL. */
export function AdminModeracao() {
  const { profile } = useAuth();
  const isAdmin = profile?.is_admin === true;
  const [items, setItems] = useState<QueueItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  // Depois de resolver um item, o cartão some: o foco volta ao título da fila.
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (!isAdmin) return;
    const controller = new AbortController();
    fetchQueue(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setItems(data.items);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          setError(err instanceof ApiError ? err.message : "Não foi possível carregar a fila.");
        }
      });
    return () => controller.abort();
  }, [isAdmin, attempt]);

  if (!isAdmin) {
    return (
      <section className="space-y-2">
        <title>Moderação — ChecaFatosRR</title>
        <h1 className="text-2xl font-bold">Moderação</h1>
        <p className="text-slate-700">Só a administração do site pode ver esta página.</p>
      </section>
    );
  }

  return (
    <section className="space-y-4" aria-busy={items === null}>
      <title>Moderação — ChecaFatosRR</title>
      <meta name="robots" content="noindex" />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 ref={heading} tabIndex={-1} className="text-2xl font-bold outline-none">
          Fila de moderação
        </h1>
        <button
          type="button"
          className={BUTTON}
          onClick={() => {
            setError(null);
            setItems(null);
            setAttempt((value) => value + 1);
          }}
        >
          Recarregar
        </button>
      </div>

      {items === null && !error && <Loading />}
      {error && (
        <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-4 text-red-900">
          {error}
        </p>
      )}
      {items && items.length === 0 && <p className="text-slate-700">A fila está vazia.</p>}
      {items && items.length > 0 && (
        <ul className="space-y-3">
          {items.map((item) => (
            <QueueCard
              key={item.id}
              item={item}
              onDone={(id) => {
                setItems((prev) => prev?.filter((i) => i.id !== id) ?? prev);
                heading.current?.focus();
              }}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
