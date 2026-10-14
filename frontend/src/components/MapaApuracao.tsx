import { useEffect, useState, type KeyboardEvent } from "react";

import { ApiError, apiFetch } from "../lib/api.ts";
import {
  BAND_LABELS,
  colorOf,
  fillFor,
  isMapaResponse,
  leaderOf,
  NEUTRAL_OTHER,
  NO_DATA,
  rankedCandidates,
  toneOf,
  UF_NAMES,
  type MapaResponse,
  type MapaResultado,
} from "../lib/mapa.ts";
import { MAPA_UF, MAPA_VIEWBOX } from "../lib/mapaUf.ts";
import { formatPct, POLL_MS, POLL_SLACK_MS } from "../lib/tse.ts";

// Carregado sob demanda (React.lazy na página Apuração): o bundle principal não cresce.
// Polling igual ao da página: 60 s, só com a aba visível, uma requisição por vez; 404 desliga.

type State = { kind: "loading" } | { kind: "off" } | { kind: "ready"; data: MapaResponse; stale: boolean };

export default function MapaApuracao() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    let off = false;
    let lastFetch = 0;

    function stop() {
      off = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    }
    function refresh() {
      if (off || inFlight) return;
      inFlight = true;
      lastFetch = Date.now();
      apiFetch<unknown>("/tse/mapa", { anonymous: true, signal: controller.signal })
        .then((data) => {
          if (controller.signal.aborted) return;
          if (!isMapaResponse(data)) throw new Error("resposta do mapa fora do formato");
          setState({ kind: "ready", data, stale: false });
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          // TSE_MAPA_ENABLED=false: o mapa some e não consulta mais.
          if (error instanceof ApiError && error.status === 404) {
            stop();
            setState({ kind: "off" });
            return;
          }
          // Outro erro: mantém o último mapa e avisa; sem mapa ainda, tenta no próximo tick.
          setState((prev) => (prev.kind === "ready" ? { ...prev, stale: true } : prev));
        })
        .finally(() => {
          inFlight = false;
        });
    }
    function due(): boolean {
      return Date.now() - lastFetch >= POLL_MS - POLL_SLACK_MS;
    }
    function onVisibility() {
      if (document.visibilityState === "visible" && due()) refresh();
    }
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && due()) refresh();
    }, POLL_MS);
    document.addEventListener("visibilitychange", onVisibility);
    refresh();
    return () => {
      controller.abort();
      stop();
    };
  }, []);

  if (state.kind !== "ready") return null;
  const { data } = state;
  const byUf = new Map(data.estados.map((e) => [e.uf, e.resultado]));
  const names = candidateNames(data);
  const hasOther = data.estados.some((e) => {
    const leader = leaderOf(e.resultado);
    return leader !== null && colorOf(leader.numero, data.cores) === undefined;
  });

  function onKey(event: KeyboardEvent<SVGPathElement>, uf: string) {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    setSelected(uf);
  }

  return (
    <section aria-labelledby="mapa-titulo" className="space-y-4">
      <h2 id="mapa-titulo" className="text-xl font-bold">
        Mapa por estado
      </h2>
      {state.stale && (
        <p className="rounded-lg bg-amber-50 p-3 text-sm text-amber-950">
          Não foi possível atualizar agora; mostrando a última atualização do mapa.
        </p>
      )}
      <svg
        viewBox={MAPA_VIEWBOX}
        role="group"
        aria-label="Mapa do Brasil com o candidato à frente em cada estado. Cada estado é um botão; todos os números estão também em “Ver em tabela”."
        className="mx-auto block h-auto w-full max-w-xl"
      >
        {Object.entries(MAPA_UF).map(([uf, d]) => (
          <path
            key={uf}
            data-uf={uf}
            d={d}
            role="button"
            tabIndex={0}
            aria-label={UF_NAMES[uf] ?? uf.toUpperCase()}
            aria-pressed={selected === uf}
            fill={fillFor(byUf.get(uf) ?? null, data.cores)}
            stroke="#ffffff"
            strokeWidth={0.8}
            className="cursor-pointer outline-none"
            onClick={() => setSelected(uf)}
            onFocus={() => setSelected(uf)}
            onKeyDown={(event) => onKey(event, uf)}
          />
        ))}
        {/* Contorno do selecionado por cima de todos (senão os vizinhos o cobrem). */}
        {selected && MAPA_UF[selected] && (
          <path
            d={MAPA_UF[selected]}
            fill="none"
            stroke="#0f172a"
            strokeWidth={2.5}
            pointerEvents="none"
            aria-hidden="true"
          />
        )}
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
        {rankedCandidates(result).map((c) => (
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
    <div className="overflow-x-auto">
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
            const ranked = rankedCandidates(resultado);
            return (
              <tr key={uf} className="border-t border-slate-100">
                <th scope="row" className="font-normal">
                  {UF_NAMES[uf] ?? uf.toUpperCase()}
                </th>
                <td>{ranked[0]?.nome ?? "Sem dados"}</td>
                <td>
                  {ranked.length
                    ? ranked
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
    </div>
  );
}
