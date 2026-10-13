import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link, useLocation } from "react-router";

import { useAuth } from "../auth/AuthContext.ts";
import { ApiError } from "../lib/api.ts";
import {
  COMMENT_MAX_LENGTH,
  deleteComment,
  listComments,
  postComment,
  reportComment,
  violationOf,
  type Comment,
  type Violation,
} from "../lib/comments.ts";
import { formatDateTime, relativeTime } from "../lib/format.ts";
import { Loading } from "./Loading.tsx";
import { ViolationNotice } from "./ViolationNotice.tsx";

type Page = { items: Comment[]; cursor: string | null };
type Update = (id: string, change: (comment: Comment) => Comment) => void;

const ACTION =
  "inline-flex min-h-11 items-center rounded-md px-2 text-sm font-semibold text-brand-700 underline hover:bg-brand-50 disabled:opacity-60";
const LINK_CLASS = "font-semibold text-brand-700 underline";

function message(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

/** Aplica `change` ao comentário `id`, seja raiz ou resposta (1 nível). */
function updateIn(items: Comment[], id: string, change: (comment: Comment) => Comment): Comment[] {
  return items.map((c) => {
    if (c.id === id) return change(c);
    if (!c.respostas.some((r) => r.id === id)) return c;
    return { ...c, respostas: c.respostas.map((r) => (r.id === id ? change(r) : r)) };
  });
}

function AccountState({ violation }: { violation: Violation }) {
  if (violation.status === "banned") {
    return <p className="text-sm font-semibold">Sua conta foi banida e não pode mais comentar.</p>;
  }
  if (violation.status === "suspended" && violation.suspendedUntil) {
    return (
      <p className="text-sm font-semibold">
        Sua conta está suspensa até {formatDateTime(violation.suspendedUntil)} (horário de Roraima).
      </p>
    );
  }
  if (violation.strikes > 0) {
    return (
      <p className="text-sm">
        Advertências ativas: {violation.strikes}. Com 3, a conta é suspensa por 7 dias; com 5, é banida.
      </p>
    );
  }
  return null;
}

function ViolationBlock({ violation }: { violation: Violation }) {
  return (
    <div className="space-y-2">
      <ViolationNotice rule={violation.rule} explanation={violation.explanation} />
      <AccountState violation={violation} />
    </div>
  );
}

/** Formulário de comentário ou de resposta. O texto nunca vira HTML: o React escapa tudo. */
function CommentForm({
  checagem,
  replyTo,
  onPosted,
  onAccountChange,
  onCancel,
}: {
  checagem: string | null;
  replyTo: Comment | null;
  onPosted: (comment: Comment) => void;
  /** A violação suspendeu ou baniu a conta: o aviso precisa sobreviver ao formulário. */
  onAccountChange: (violation: Violation) => void;
  onCancel?: () => void;
}) {
  const { reloadProfile } = useAuth();
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [violation, setViolation] = useState<Violation | null>(null);
  const fieldId = useId();
  const field = useRef<HTMLTextAreaElement>(null);
  const isReply = replyTo !== null;

  // Ao abrir uma resposta, o foco vai para o campo (só ao montar).
  useEffect(() => {
    if (isReply) field.current?.focus();
  }, [isReply]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const texto = text.trim();
    if (!texto || sending) return;
    setSending(true);
    setError(null);
    setViolation(null);
    try {
      const created = await postComment(texto, checagem, replyTo?.id ?? null);
      setText("");
      onPosted(created);
    } catch (err) {
      const found = violationOf(err);
      if (found) {
        setViolation(found);
        if (found.status !== "active") {
          onAccountChange(found);
          reloadProfile(); // o perfil (e o menu) passam a saber da suspensão
        }
      } else {
        setError(message(err, "Não foi possível publicar. Tente de novo."));
      }
    } finally {
      setSending(false);
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="space-y-2">
      <label htmlFor={fieldId} className="block font-semibold">
        {replyTo ? `Responder a ${replyTo.apelido}` : "Escreva um comentário"}
      </label>
      <textarea
        id={fieldId}
        ref={field}
        value={text}
        onChange={(event) => setText(event.target.value)}
        maxLength={COMMENT_MAX_LENGTH}
        rows={3}
        required
        className="block w-full rounded-lg border border-slate-300 bg-white p-3 text-base"
      />
      <p className="text-sm text-slate-600">
        {text.length}/{COMMENT_MAX_LENGTH} · Seja respeitoso: os comentários passam por moderação automática.
      </p>
      {violation && <ViolationBlock violation={violation} />}
      {error && (
        <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-red-900">
          {error}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          disabled={sending || !text.trim()}
          className="min-h-11 rounded-md bg-brand-700 px-4 font-semibold text-white disabled:opacity-60"
        >
          {sending ? "Publicando…" : replyTo ? "Responder" : "Comentar"}
        </button>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="min-h-11 rounded-md border border-slate-300 bg-white px-4 font-semibold"
          >
            Cancelar
          </button>
        )}
      </div>
    </form>
  );
}

interface ItemProps {
  comment: Comment;
  checagem: string | null;
  canAct: boolean;
  ownApelido: string | null;
  isReply: boolean;
  onUpdate: Update;
  onAccountChange: (violation: Violation) => void;
}

function CommentItem({ comment, checagem, canAct, ownApelido, isReply, onUpdate, onAccountChange }: ItemProps) {
  const [replying, setReplying] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const articleRef = useRef<HTMLElement>(null);
  const replyButton = useRef<HTMLButtonElement>(null);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  // Para onde o foco vai quando o elemento que o tinha some (aplicado após o render).
  const pendingFocus = useRef<"article" | "reply" | "delete" | "confirm" | null>(null);
  // Apelido é único e não muda; o servidor confere o dono ao apagar.
  const own = ownApelido !== null && comment.apelido === ownApelido;
  const removed = comment.removido_pelo_autor || comment.texto === null;
  const who = comment.apelido;

  useEffect(() => {
    if (pendingFocus.current === null) return;
    const target = {
      article: articleRef,
      reply: replyButton,
      delete: deleteButton,
      confirm: confirmButton,
    }[pendingFocus.current];
    pendingFocus.current = null;
    target.current?.focus();
  });

  async function report() {
    setBusy(true);
    setNote(null);
    try {
      const result = await reportComment(comment.id);
      setNote(result.status === "recebida" ? "Denúncia recebida. Obrigado!" : "Você já denunciou este comentário.");
    } catch (err) {
      setNote(message(err, "Não foi possível denunciar agora."));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setNote(null);
    try {
      await deleteComment(comment.id);
      setConfirmingDelete(false);
      onUpdate(comment.id, (c) => ({ ...c, texto: null, removido_pelo_autor: true }));
      pendingFocus.current = "article";
    } catch (err) {
      setNote(message(err, "Não foi possível apagar agora."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article
      ref={articleRef}
      tabIndex={-1}
      aria-label={removed ? "Comentário removido" : `Comentário de ${who}`}
      className="space-y-2 rounded-lg border border-slate-200 bg-white p-3 outline-none focus-visible:outline-2 focus-visible:outline-brand-700"
    >
      {removed ? (
        <p className="italic text-slate-600">Comentário removido pelo autor</p>
      ) : (
        <>
          <p className="text-sm text-slate-600">
            <strong className="text-slate-900">{who}</strong> ·{" "}
            <time dateTime={comment.criado_em} title={formatDateTime(comment.criado_em)}>
              {relativeTime(comment.criado_em)}
            </time>
          </p>
          <p className="whitespace-pre-wrap break-words text-slate-900">{comment.texto}</p>
        </>
      )}

      {canAct && !removed && (
        <div className="flex flex-wrap gap-1">
          {!isReply && !replying && (
            <button
              ref={replyButton}
              type="button"
              className={ACTION}
              aria-label={`Responder a ${who}`}
              onClick={() => setReplying(true)}
            >
              Responder
            </button>
          )}
          {!own && (
            <button
              type="button"
              className={ACTION}
              aria-label={`Denunciar comentário de ${who}`}
              disabled={busy}
              onClick={() => void report()}
            >
              Denunciar
            </button>
          )}
          {own && !confirmingDelete && (
            <button
              ref={deleteButton}
              type="button"
              className={ACTION}
              aria-label="Apagar meu comentário"
              onClick={() => {
                setConfirmingDelete(true);
                pendingFocus.current = "confirm";
              }}
            >
              Apagar
            </button>
          )}
        </div>
      )}

      {confirmingDelete && (
        <div role="group" aria-label="Confirmar exclusão" className="space-y-2 rounded-lg bg-red-50 p-3">
          <p className="font-semibold text-red-900">Apagar este comentário? Não dá para desfazer.</p>
          <div className="flex flex-wrap gap-2">
            <button
              ref={confirmButton}
              type="button"
              disabled={busy}
              onClick={() => void remove()}
              className="min-h-11 rounded-md bg-red-800 px-4 font-semibold text-white disabled:opacity-60"
            >
              Sim, apagar
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirmingDelete(false);
                pendingFocus.current = "delete";
              }}
              className="min-h-11 rounded-md border border-slate-300 bg-white px-4 font-semibold"
            >
              Cancelar
            </button>
          </div>
        </div>
      )}

      {note && (
        <p role="status" className="text-sm text-slate-700">
          {note}
        </p>
      )}

      {comment.respostas.length > 0 && (
        <ul className="space-y-2 border-l-2 border-slate-200 pl-3">
          {comment.respostas.map((reply) => (
            <li key={reply.id}>
              <CommentItem
                comment={reply}
                checagem={checagem}
                canAct={canAct}
                ownApelido={ownApelido}
                isReply
                onUpdate={onUpdate}
                onAccountChange={onAccountChange}
              />
            </li>
          ))}
        </ul>
      )}

      {/* Suspensa no meio da resposta: o formulário fecha (o aviso fica no topo). */}
      {replying && canAct && (
        <div className="border-l-2 border-slate-200 pl-3">
          <CommentForm
            checagem={checagem}
            replyTo={comment}
            onAccountChange={onAccountChange}
            onCancel={() => {
              setReplying(false);
              pendingFocus.current = "reply";
            }}
            onPosted={(created) => {
              setReplying(false);
              onUpdate(comment.id, (c) => ({ ...c, respostas: [...c.respostas, created] }));
              pendingFocus.current = "article";
            }}
          />
        </div>
      )}
    </article>
  );
}

/** Quem pode comentar: login + apelido + conta ativa. Os outros veem o motivo. */
function WhoCanComment({
  checagem,
  onPosted,
  accountViolation,
  onAccountChange,
}: {
  checagem: string | null;
  onPosted: (c: Comment) => void;
  accountViolation: Violation | null;
  onAccountChange: (violation: Violation) => void;
}) {
  const { status, profile } = useAuth();
  const location = useLocation();

  if (status === "loading") return null;
  if (status === "anonymous" || status === "error") {
    const volta = encodeURIComponent(location.pathname + location.search);
    return (
      <p className="text-slate-700">
        <Link to={`/entrar?volta=${volta}`} className={LINK_CLASS}>
          Entre com sua conta Google
        </Link>{" "}
        para comentar.
      </p>
    );
  }
  if (status === "needs_profile") {
    return (
      <p className="text-slate-700">
        <Link to="/bem-vindo" className={LINK_CLASS}>
          Escolha um apelido
        </Link>{" "}
        para comentar.
      </p>
    );
  }
  // Suspensa/banida por este comentário: o aviso (regra e motivo) continua na tela.
  if (accountViolation && profile?.status !== "active") return <ViolationBlock violation={accountViolation} />;
  if (profile?.status === "banned") {
    return <p className="text-slate-700">Sua conta foi banida e não pode comentar.</p>;
  }
  if (profile?.status === "suspended" && profile.suspended_until) {
    return (
      <p className="text-slate-700">
        Sua conta está suspensa até {formatDateTime(profile.suspended_until)} (horário de Roraima).
      </p>
    );
  }
  return <CommentForm checagem={checagem} replyTo={null} onPosted={onPosted} onAccountChange={onAccountChange} />;
}

/** Comentários de uma checagem de link (`checagem`) ou do feed geral da Comunidade (null). */
export function Comments({ checagem, title }: { checagem: string | null; title: string }) {
  const { status, profile } = useAuth();
  const [page, setPage] = useState<Page | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [posted, setPosted] = useState(false);
  const [accountViolation, setAccountViolation] = useState<Violation | null>(null);
  const moreController = useRef<AbortController | null>(null);
  const headingId = useId();

  useEffect(() => {
    // Recarregar descarta um "carregar mais" ainda em andamento.
    moreController.current?.abort();
    const controller = new AbortController();
    listComments(checagem, null, controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        setLoaded(true);
        // Mantém no topo o que foi publicado aqui antes de a lista chegar (sem repetir).
        setPage((prev) => {
          const fromServer = new Set(data.items.map((c) => c.id));
          const local = (prev?.items ?? []).filter((c) => !fromServer.has(c.id));
          return { items: [...local, ...data.items], cursor: data.next_cursor };
        });
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(message(err, "Não foi possível carregar os comentários."));
      });
    return () => controller.abort();
  }, [checagem, attempt]);

  useEffect(() => () => moreController.current?.abort(), []);

  async function loadMore() {
    if (!page?.cursor || loadingMore) return;
    const controller = new AbortController();
    moreController.current = controller;
    setLoadingMore(true);
    setError(null);
    try {
      const data = await listComments(checagem, page.cursor, controller.signal);
      if (controller.signal.aborted) return;
      setPage((prev) => {
        // Um comentário publicado aqui pode reaparecer na página seguinte: sem repetir.
        const seen = new Set(prev?.items.map((c) => c.id));
        return {
          items: [...(prev?.items ?? []), ...data.items.filter((c) => !seen.has(c.id))],
          cursor: data.next_cursor,
        };
      });
    } catch (err) {
      if (!controller.signal.aborted) setError(message(err, "Não foi possível carregar mais."));
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
    }
  }

  const canAct = status === "ready" && profile?.status === "active";
  const ownApelido = status === "ready" && profile ? profile.apelido : null;

  const update: Update = (id, change) =>
    setPage((prev) => (prev ? { ...prev, items: updateIn(prev.items, id, change) } : prev));

  function prepend(created: Comment) {
    setPosted(true);
    setPage((prev) => ({ items: [created, ...(prev?.items ?? [])], cursor: prev?.cursor ?? null }));
  }

  return (
    <section aria-labelledby={headingId} className="space-y-4" aria-busy={!loaded || loadingMore}>
      <h2 id={headingId} className="text-xl font-bold">
        {title}
      </h2>

      <WhoCanComment
        checagem={checagem}
        onPosted={prepend}
        accountViolation={accountViolation}
        onAccountChange={setAccountViolation}
      />
      <p role="status" className="sr-only">
        {posted ? "Comentário publicado." : ""}
      </p>

      {!loaded && !error && <Loading />}
      {loaded && page && page.items.length === 0 && <p className="text-slate-700">Ainda não há comentários.</p>}
      {page && page.items.length > 0 && (
        <ul className="space-y-3">
          {page.items.map((comment) => (
            <li key={comment.id}>
              <CommentItem
                comment={comment}
                checagem={checagem}
                canAct={canAct}
                ownApelido={ownApelido}
                isReply={false}
                onUpdate={update}
                onAccountChange={setAccountViolation}
              />
            </li>
          ))}
        </ul>
      )}

      {error && (
        <div role="alert" className="space-y-3 rounded-lg border border-red-200 bg-red-50 p-4 text-red-900">
          <p>{error}</p>
          {!loaded && (
            <button
              type="button"
              onClick={() => {
                setError(null);
                setAttempt((value) => value + 1);
              }}
              className="min-h-11 rounded-md bg-red-800 px-4 font-semibold text-white"
            >
              Tentar de novo
            </button>
          )}
        </div>
      )}

      {page?.cursor && (
        <button
          type="button"
          onClick={() => void loadMore()}
          disabled={loadingMore}
          className="min-h-11 w-full rounded-lg border border-slate-300 bg-white px-4 font-semibold hover:bg-slate-50 disabled:opacity-60"
        >
          {loadingMore ? "Carregando…" : "Carregar mais comentários"}
        </button>
      )}
    </section>
  );
}
