import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { Loading } from "../components/Loading.tsx";
import { ApiError } from "../lib/api.ts";
import { formatDateTime } from "../lib/format.ts";
import { fetchPainel, isPainelResponse, WEB_ANALYTICS_URL, type PainelResponse } from "../lib/painel.ts";

// Painel mínimo do admin (plano mapa-e-painel-admin §B), só leitura. Carregado sob demanda: o
// bundle de quem não é admin não cresce. Só números agregados por dia; nada por pessoa.

const NUMBER = new Intl.NumberFormat("pt-BR");
const BUTTON = "min-h-11 rounded-md border border-slate-300 bg-white px-3 font-semibold";
const CARD = "rounded-lg border border-slate-200 bg-white p-4";
const LINK = "inline-flex min-h-11 items-center font-semibold text-brand-700 underline";

const USO: Record<string, string> = { checagem: "Checagem", moderacao: "Moderação" };

type State = { kind: "loading" } | { kind: "error"; message: string } | { kind: "ready"; data: PainelResponse };

function n(value: number): string {
  return NUMBER.format(value);
}

/** "2026-10-14" → "14/10". */
function shortDay(iso: string): string {
  return `${iso.slice(8, 10)}/${iso.slice(5, 7)}`;
}

function plural(value: number, one: string, many: string): string {
  return `${n(value)} ${value === 1 ? one : many}`;
}

export default function PainelAdmin() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    fetchPainel(controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        if (!isPainelResponse(data)) throw new Error("resposta fora do formato");
        setState({ kind: "ready", data });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const message =
          error instanceof ApiError ? error.message : "Não foi possível carregar o painel. Tente de novo.";
        setState({ kind: "error", message });
      });
    return () => controller.abort();
  }, [attempt]);

  return (
    <section className="space-y-5" aria-busy={state.kind === "loading"}>
      <title>Painel — ChecaFatosRR</title>
      <meta name="robots" content="noindex" />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-bold">Painel do administrador</h1>
        <button
          type="button"
          className={BUTTON}
          onClick={() => {
            setState({ kind: "loading" });
            setAttempt((value) => value + 1);
          }}
        >
          Recarregar
        </button>
      </div>
      <p className="text-sm text-slate-600">
        Só números agregados por dia (horário de Boa Vista); nada por pessoa.
      </p>

      {state.kind === "loading" && <Loading />}
      {state.kind === "error" && (
        <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-4 text-red-900">
          {state.message}
        </p>
      )}
      {state.kind === "ready" && <Painel data={state.data} />}
    </section>
  );
}

function Painel({ data }: { data: PainelResponse }) {
  // Hoje primeiro: é o que o admin olha no dia.
  const days = [...data.checagens].reverse();
  const byDay = <T extends { dia: string }>(rows: T[]) => new Map(rows.map((r) => [r.dia, r]));
  const pedidos = byDay(data.pedidos);
  const comentarios = byDay(data.comentarios);
  const denuncias = byDay(data.denuncias);
  const novos = byDay(data.usuarios_novos);
  const tokens = [...data.tokens].reverse();

  return (
    <div className="space-y-5">
      <p className="text-sm text-slate-600">
        Atualizado em {formatDateTime(data.gerado_em)} (horário de Boa Vista)
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className={CARD}>
          <Link to="/admin/moderacao" className={LINK}>
            Fila de moderação ({plural(data.fila.pendentes, "pendente", "pendentes")})
          </Link>
          <p className="text-sm text-slate-600">
            {plural(data.fila.revisao, "comentário para revisar", "comentários para revisar")} ·{" "}
            {plural(data.fila.confirmacao_strike, "bloqueio para confirmar", "bloqueios para confirmar")}
          </p>
        </div>
        <div className={CARD}>
          <a href={WEB_ANALYTICS_URL} target="_blank" rel="noopener noreferrer" className={LINK}>
            Visitas no Cloudflare Web Analytics<span className="sr-only"> (abre em nova aba)</span>
          </a>
          <p className="text-sm text-slate-600">Sem cookies; os dados ficam no Cloudflare.</p>
        </div>
      </div>

      <div className={CARD}>
        <p className="font-semibold">{plural(data.usuarios.total, "usuário", "usuários")}</p>
        <p className="text-sm text-slate-600">
          {plural(data.usuarios.suspensos, "suspenso", "suspensos")} ·{" "}
          {plural(data.usuarios.banidos, "banido", "banidos")}
        </p>
      </div>

      <Table caption={`Cota do Gemini hoje (dia do Pacífico, ${shortDay(data.gemini.dia_pacifico)})`}>
        <thead>
          <tr>
            <Th>Uso</Th>
            <Th num>Usadas</Th>
            <Th num>Limite</Th>
            <Th num>Restante</Th>
          </tr>
        </thead>
        <tbody>
          {data.gemini.cota.map((q) => (
            <tr key={q.uso} className="border-t border-slate-100">
              <Th row>{USO[q.uso] ?? q.uso}</Th>
              <Td>{n(q.usadas)}</Td>
              <Td>{n(q.limite)}</Td>
              <Td>{n(q.restante)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>

      <Table caption="Modelos do Gemini hoje (limite diário do nível gratuito)">
        <thead>
          <tr>
            <Th>Modelo</Th>
            <Th num>Usadas</Th>
            <Th num>Limite</Th>
            <Th num>Restante</Th>
          </tr>
        </thead>
        <tbody>
          {data.gemini.modelos.map((m) => (
            <tr key={m.modelo} className="border-t border-slate-100">
              <Th row>
                <span className="block break-all">{m.modelo}</span>
                <span className="block text-xs font-normal text-slate-600">
                  {chainLabel(m.checagem, m.moderacao)}
                </span>
              </Th>
              <Td>{n(m.usadas)}</Td>
              <Td>{n(m.limite)}</Td>
              <Td>{n(m.restante)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>

      <Table caption="Checagens por dia (resultado)">
        <thead>
          <tr>
            <Th>Dia</Th>
            <Th num>Total</Th>
            <Th num>Falso</Th>
            <Th num>Enganoso</Th>
            <Th num>Fora de contexto</Th>
            <Th num>Verdadeiro</Th>
            <Th num>Sem checagem</Th>
            <Th num>Só agências</Th>
          </tr>
        </thead>
        <tbody>
          {days.map((d) => (
            <tr key={d.dia} className="border-t border-slate-100">
              <Th row>{shortDay(d.dia)}</Th>
              <Td>{n(d.total)}</Td>
              <Td>{n(d.falso)}</Td>
              <Td>{n(d.enganoso)}</Td>
              <Td>{n(d.fora_de_contexto)}</Td>
              <Td>{n(d.verdadeiro)}</Td>
              <Td>{n(d.nao_verificavel)}</Td>
              <Td>{n(d.so_agencias)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>

      <Table caption="Atividade por dia">
        <thead>
          <tr>
            <Th>Dia</Th>
            <Th num>Pedidos de checagem</Th>
            <Th num>Do cache</Th>
            <Th num>Comentários</Th>
            <Th num>Ocultos</Th>
            <Th num>Denúncias</Th>
            <Th num>Usuários novos</Th>
          </tr>
        </thead>
        <tbody>
          {days.map(({ dia }) => (
            <tr key={dia} className="border-t border-slate-100">
              <Th row>{shortDay(dia)}</Th>
              <Td>{n(pedidos.get(dia)?.total ?? 0)}</Td>
              <Td>{n(pedidos.get(dia)?.do_cache ?? 0)}</Td>
              <Td>{n(comentarios.get(dia)?.total ?? 0)}</Td>
              <Td>{n(comentarios.get(dia)?.ocultos ?? 0)}</Td>
              <Td>{n(denuncias.get(dia)?.total ?? 0)}</Td>
              <Td>{n(novos.get(dia)?.total ?? 0)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>

      <Table caption="Tokens por dia (checagens; antes de 14/10 não eram gravados)">
        <thead>
          <tr>
            <Th>Dia</Th>
            <Th num>Checagens</Th>
            <Th num>Entrada</Th>
            <Th num>Saída</Th>
            <Th num>Raciocínio</Th>
          </tr>
        </thead>
        <tbody>
          {tokens.map((t) => (
            <tr key={t.dia} className="border-t border-slate-100">
              <Th row>{shortDay(t.dia)}</Th>
              <Td>{n(t.checagens)}</Td>
              <Td>{n(t.entrada)}</Td>
              <Td>{n(t.saida)}</Td>
              <Td>{n(t.raciocinio)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

function chainLabel(checagem: boolean, moderacao: boolean): string {
  if (checagem && moderacao) return "checagem e moderação";
  if (checagem) return "checagem";
  if (moderacao) return "moderação";
  return "fora das cadeias";
}

/**
 * Tabela larga: rola dentro da caixa no celular (a página não rola para o lado). A caixa recebe
 * foco pelo teclado (região com nome) para quem não usa o toque conseguir rolar.
 */
function Table({ caption, children }: { caption: string; children: ReactNode }) {
  return (
    <div
      role="region"
      aria-label={caption}
      /* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- caixa com rolagem precisa de foco (axe: scrollable-region-focusable) */
      tabIndex={0}
      className="overflow-x-auto rounded-lg border border-slate-200 bg-white focus-visible:outline-2 focus-visible:outline-brand-700"
    >
      <table className="w-full text-left text-sm">
        <caption className="px-3 pt-3 text-left font-semibold">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

function Th({ children, num, row }: { children: ReactNode; num?: boolean; row?: boolean }) {
  return (
    <th
      scope={row ? "row" : "col"}
      className={`px-3 py-2 ${row ? "font-semibold" : "font-medium text-slate-600"} ${num ? "text-right" : ""}`}
    >
      {children}
    </th>
  );
}

function Td({ children }: { children: ReactNode }) {
  return <td className="px-3 py-2 text-right tabular-nums">{children}</td>;
}
