// Tipos da resposta do GET /admin/painel, sem dependências: o e2e (Node) importa o fixture.

export interface ChecagensDia {
  dia: string;
  total: number;
  verdadeiro: number;
  enganoso: number;
  falso: number;
  fora_de_contexto: number;
  nao_verificavel: number;
  so_agencias: number;
}

export interface TokensDia {
  dia: string;
  checagens: number;
  entrada: number;
  saida: number;
  raciocinio: number;
}

export interface PainelResponse {
  gerado_em: string;
  checagens: ChecagensDia[];
  tokens: TokensDia[];
  pedidos: { dia: string; total: number; do_cache: number }[];
  comentarios: { dia: string; total: number; ocultos: number }[];
  denuncias: { dia: string; total: number }[];
  usuarios_novos: { dia: string; total: number }[];
  fila: { pendentes: number; revisao: number; confirmacao_strike: number };
  usuarios: { total: number; suspensos: number; banidos: number };
  gemini: {
    dia_pacifico: string;
    cota: { uso: string; usadas: number; limite: number; restante: number }[];
    modelos: {
      modelo: string;
      usadas: number;
      limite: number;
      restante: number;
      checagem: boolean;
      moderacao: boolean;
    }[];
  };
}
