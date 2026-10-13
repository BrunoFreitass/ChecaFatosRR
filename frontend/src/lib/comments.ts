import { ApiError, apiFetch } from "./api.ts";

/** Comentário público. `texto` null = removido pelo autor (as respostas continuam). */
export interface Comment {
  id: string;
  apelido: string;
  texto: string | null;
  criado_em: string;
  removido_pelo_autor: boolean;
  respostas: Comment[];
}

export interface CommentList {
  items: Comment[];
  next_cursor: string | null;
}

export type QueueKind = "comment_review" | "strike_confirmation";

export interface QueueItem {
  id: number;
  tipo: QueueKind;
  motivo: string;
  categoria: string | null;
  confianca: number | null;
  criado_em: string;
  apelido: string | null;
  comentario: string | null;
  status_comentario: string | null;
  texto: string | null;
}

export type AdminAction = "aprovar" | "remover" | "banir" | "confirmar_strike" | "descartar";

/** Limite de caracteres de um comentário (o mesmo da API). */
export const COMMENT_MAX_LENGTH = 1000;

export function listComments(
  checagem: string | null,
  cursor: string | null,
  signal?: AbortSignal,
): Promise<CommentList> {
  const query = new URLSearchParams();
  if (checagem) query.set("checagem", checagem);
  if (cursor) query.set("cursor", cursor);
  // Sem URLSearchParams.size: não existe em navegadores antigos (Safari < 17).
  const qs = query.toString();
  return apiFetch<CommentList>(`/comments${qs ? `?${qs}` : ""}`, { signal, anonymous: true });
}

export function postComment(texto: string, checagem: string | null, respostaA: string | null) {
  const body: Record<string, string> = { texto };
  if (checagem) body.checagem = checagem;
  if (respostaA) body.resposta_a = respostaA;
  return apiFetch<Comment>("/comments", { method: "POST", body });
}

export function reportComment(id: string) {
  return apiFetch<{ status: "recebida" | "ja_recebida" }>(
    `/comments/${encodeURIComponent(id)}/denuncia`,
    { method: "POST", body: {} },
  );
}

export function deleteComment(id: string) {
  return apiFetch<void>(`/comments/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function fetchQueue(signal?: AbortSignal) {
  return apiFetch<{ items: QueueItem[] }>("/admin/moderacao/fila", { signal });
}

export function queueAction(id: number, acao: AdminAction) {
  return apiFetch<{ status: string }>(`/admin/moderacao/fila/${id}`, {
    method: "POST",
    body: { acao },
  });
}

/** Comentário bloqueado pela moderação: qual regra, por quê e como ficou a conta. */
export interface Violation {
  rule: string;
  explanation: string;
  strikes: number;
  status: "active" | "suspended" | "banned";
  suspendedUntil: string | null;
}

export function violationOf(error: unknown): Violation | null {
  if (!(error instanceof ApiError) || error.code !== "violacao") return null;
  const d = error.details;
  const status = d.status === "suspended" || d.status === "banned" ? d.status : "active";
  return {
    rule: typeof d.regra === "string" ? d.regra : "regras da comunidade",
    explanation: typeof d.explicacao === "string" ? d.explicacao : error.message,
    strikes: typeof d.strikes === "number" ? d.strikes : 0,
    status,
    suspendedUntil: typeof d.suspenso_ate === "string" ? d.suspenso_ate : null,
  };
}

const REASON_LABEL: Record<string, string> = {
  desinformacao: "Possível desinformação (publicado)",
  nao_moderado_ia: "Publicado sem análise da IA",
  hidden_by_reports: "Oculto por denúncias",
  ai_low_confidence: "Bloqueado pela IA com pouca confiança",
};

export function queueReasonLabel(reason: string): string {
  return REASON_LABEL[reason] ?? reason;
}
