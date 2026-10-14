import { useEffect, useRef, useState } from "react";

import { ApiError, apiFetch } from "../lib/api.ts";
import {
  BAND_LABELS,
  fillFor,
  isMapaResponse,
  leaderOf,
  NEUTRAL_OTHER,
  NO_DATA,
  toneOf,
  UF_NAMES,
  type MapaResponse,
  type MapaResultado,
} from "../lib/mapa.ts";
import { MAPA_UF, MAPA_VIEWBOX } from "../lib/mapaUf.ts";
import { formatPct, POLL_MS } from "../lib/tse.ts";

// Carregado sob demanda (React.lazy na página Apuração): o bundle principal não cresce.
// Polling igual ao da página: 60 s, só com a aba visível, uma requisição por vez.

type State = { kind: "loading" } | { kind: "off" } | { kind: "ready"; data: MapaResponse };

export default function MapaApuracao() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [selected, setSelected] = useState<string | null>(null);
  const off = useRef(false);

  useEffect(() => {
    let controller: AbortController | null = null;
    let lastFetch = 0;

    function refresh() {
      if (off.current || controller) return;
      const current = new AbortController();
      controller = current;
      lastFetch = Date.now();
      apiFetch<unknown>("/tse/mapa", { anonymous: true, signal: current.signal })
        .then((data) => {
          if (!current.signal.aborted && isMapaResponse(data)) setState({ kind: "ready", data });
        })
        .catch((error: unknown) => {
          // TSE_MAPA_ENABLED=false: o mapa some e não consulta mais.
          if (error instanceof ApiError && error.status === 404) {
            off.current = true;
            setState({ kind: "off" });
          }
        })
        .finally(() => {
          if (controller === current) controller = null;
        });
    }

    refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && Date.now() - lastFetch >= POLL_MS - 1_000) {
        refresh();
      }
    }, POLL_MS);
    return () => {
      window.clearInterval(timer);
      controller?.abort();
    };
  }, []);

  if (state.kind !== "ready") return null;
  const { data } = state;
  const byUf = new Map(data.estados.map((e) => [e.uf, e.resultado]));
  const names = candidateNames(data);
  const hasOther = data.estados.some((e) => {
    const leader = leaderOf(e.resultado);
    return leader !== null && !(leader.numero in data.cores);
  });

  return (
    <section aria-labelledby="mapa-titulo" className="space-y-4">
      <h2 id="mapa-titulo" className="text-xl font-bold">
        Mapa por estado
      </h2>
      <svg
        viewBox={MAPA_VIEWBOX}
        role="img"
        aria-label="Mapa do Brasil com o candidato à frente em cada estado. Os números de cada estado estão na tabela abaixo."
        className="mx-auto block h-auto w-full max-w-xl"
      >
        {Object.entries(MAPA_UF).map(([uf, d]) => (
          <path
            key={uf}
            data-uf={uf}
            d={d}
            fill={fillFor(byUf.get(uf) ?? null, data.cores)}
            stroke={selected === uf ? "#0f172a" : "#ffffff"}
            strokeWidth={selected === uf ? 2 : 0.8}
            className="cursor-pointer"
            onClick={() => setSelected(uf)}
            onMouseEnter={() => setSelected(uf)}
          />
        ))}
      </svg>

      <div role="status" aria-live="polite" className="min-h-16 rounded-lg bg-slate-50 p-3 text-sm">
        {selected ? (
          <Detail uf={selected} result={byUf.get(selected) ?? null} />
        ) : (
          "Toque num estado para ver os números."
        )}
      </div>

      <ul aria-label="Legenda do mapa" className="flex flex-wrap gap-x-4 gap-y-2 text-sm">
        {Object.entries(data.cores).map(([numero, color]) => (
          <li key={numero} className="flex items-center gap-2">
            <Swatch color={toneOf(color)} />
            {`${names.get(numero) ?? `Candidato ${numero}`} à frente`}
          </li>
        ))}
        {hasOther && (
          <li className="flex items-center gap-2">
            <Swatch color={NEUTRAL_OTHER} />
            Outro candidato à frente
          </li>
        )}
        <li className="flex items-center gap-2">
          <Swatch color={NO_DATA} />
          Sem dados
        </li>
        <li className="w-full text-slate-600">
          Cor mais forte = vantagem maior ({BAND_LABELS.join("; ")}).
        </li>
      </ul>

      <details className="rounded-lg border border-slate-200 p-3">
        <summary className="cursor-pointer font-semibold">Ver em tabela</summary>
        <StatesTable data={data} />
      </details>

      <p className="text-xs text-slate-600">
        Fonte: TSE (resultados oficiais). Contorno dos estados: Natural Earth (domínio público).
      </p>
    </section>
  );
}

function candidateNames(data: MapaResponse): Map<string, string> {
  const names = new Map<string, string>();
  for (const { resultado } of data.estados) {
    for (const c of resultado?.candidatos ?? []) names.set(c.numero, c.nome);
  }
  return names;
}

function Swatch({ color }: { color: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4 shrink-0">
      <rect width="16" height="16" rx="2" fill={color} />
    </svg>
  );
}

function Detail({ uf, result }: { uf: string; result: MapaResultado | null }) {
  if (!result) return <p>{UF_NAMES[uf]}: sem dados do TSE ainda.</p>;
  return (
    <div>
      <p className="font-semibold">{UF_NAMES[uf]}</p>
      <ul>
        {result.candidatos.map((c) => (
          <li key={c.numero}>
            {c.nome}: {formatPct(c.pct)}
          </li>
        ))}
      </ul>
      <p>Urnas apuradas: {formatPct(result.secoes_pct)}</p>
    </div>
  );
}

function StatesTable({ data }: { data: MapaResponse }) {
  return (
    <table className="mt-3 w-full text-left text-sm">
      <caption className="sr-only">Resultado por estado</caption>
      <thead>
        <tr>
          <th scope="col">Estado</th>
          <th scope="col">À frente</th>
          <th scope="col">Percentuais</th>
          <th scope="col">Urnas apuradas</th>
        </tr>
      </thead>
      <tbody>
        {data.estados.map(({ uf, resultado }) => {
          const leader = leaderOf(resultado);
          return (
            <tr key={uf} className="border-t border-slate-100">
              <th scope="row" className="font-normal">
                {UF_NAMES[uf] ?? uf.toUpperCase()}
              </th>
              <td>{leader ? leader.nome : "Sem dados"}</td>
              <td>
                {resultado
                  ? resultado.candidatos
                      .slice(0, 2)
                      .map((c) => `${c.nome} ${formatPct(c.pct)}`)
                      .join(" · ")
                  : "—"}
              </td>
              <td>{resultado ? formatPct(resultado.secoes_pct) : "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
