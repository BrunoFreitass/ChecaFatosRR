export type ProfileStatus = "active" | "suspended" | "banned";

export interface Profile {
  apelido: string;
  status: ProfileStatus;
  suspended_until: string | null;
  is_admin: boolean;
  terms_version: string;
  created_at: string;
}

export type NicknameRejection = "formato" | "reservado" | "conteudo_proibido" | "em_uso";

export interface NicknameAvailability {
  disponivel: boolean;
  motivo: NicknameRejection | null;
}

export type Verdict = "verdadeiro" | "enganoso" | "falso" | "fora_de_contexto" | "nao_verificavel";

export interface AgencyCheck {
  agencia: string;
  titulo: string;
  veredito: string;
  url: string;
  data: string | null;
}

/** O link enviado é a própria checagem de uma agência (sem veredito nem confiança nossos). */
export interface AgencyPage {
  agencia: string;
  url: string;
  classificacao: string;
  /** Alegação verificada (claimReviewed). null: só o título da checagem. */
  alegacao: string | null;
  titulo: string;
}

/** Resultado de uma checagem. `id` é o identificador público aleatório. */
export interface Check {
  id: string;
  tipo: "link" | "texto";
  url: string | null;
  domain: string | null;
  title: string | null;
  published_at: string | null;
  texto: string | null;
  veredito: Verdict;
  rebaixado_por_confianca: boolean;
  confianca: number;
  resumo: string;
  pontos_suspeitos: string[];
  contexto_faltante: string | null;
  checagens_agencias: AgencyCheck[];
  fontes_sugeridas: string[];
  created_at: string;
  cache: boolean;
  /** Feita com uma versão anterior do verificador (prompt). API antiga: ausente. */
  analise_anterior?: boolean;
  /** "Verdadeiro" sem agência que confirme: mostrado como "Ainda sem checagem". */
  rebaixado_sem_agencia?: boolean;
  /** Checagens da busca que a análise não usou (outro assunto). API antiga: ausente. */
  checagens_relacionadas?: AgencyCheck[];
  /** "agencias": Gemini indisponível, só o que as agências publicaram (sem IA). API antiga: ausente. */
  modo?: "ia" | "agencias" | "checagem_de_agencia";
  /** Só no modo "checagem_de_agencia". */
  checagem_de_agencia?: AgencyPage | null;
  /** Busca nas agências desta análise, gravada na hora da checagem. Ausente/null: checagem antiga. */
  busca_agencias?: "ok" | "falhou" | null;
}

export interface CheckSummary {
  id: string;
  domain: string;
  title: string | null;
  veredito: Verdict;
  created_at: string;
  /** Checagens de agência encontradas (0 + nao_verificavel = "Ainda sem checagem"). */
  total_agencias?: number;
}

export interface LatestChecks {
  items: CheckSummary[];
  next_cursor: string | null;
}
