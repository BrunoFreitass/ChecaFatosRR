import {
  Component,
  lazy,
  Suspense,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { Loading } from "../components/Loading.tsx";
import { ApiError, apiFetch } from "../lib/api.ts";
import { formatDateTime } from "../lib/format.ts";
import {
  formatPct,
  formatVotes,
  isTseResponse,
  OFFICIAL_SITE,
  POLL_MS,
  POLL_SLACK_MS,
  SCOPE_LABELS,
  type TseCandidate,
  type TseResponse,
  type TseResult,
  type TseScope,
} from "../lib/tse.ts";

// Chunk separado: o contorno dos estados (~20 KB) não entra no bundle principal.
const MapaApuracao = lazy(() => import("../components/MapaApuracao.tsx"));

const SCOPES: TseScope[] = ["br", "rr"];
const LINK_CLASS = "font-semibold text-brand-700 underline break-words";
const EXTERNAL = { target: "_blank", rel: "noopener noreferrer" } as const;

/** Se o mapa quebrar (chunk antigo depois de um deploy, rede), some só ele: a apuração fica. */
class MapBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? null : this.props.children;
  }
}

type State =
  | { kind: "loading" }
  | { kind: "ready"; data: TseResponse; stale: boolean }
  | { kind: "unavailable"; message: string }
  | { kind: "error"; message: string };

/**
 * Resultado OFICIAL do TSE, sem projeção nem destaque de vencedor: ordem e números do TSE,
 * "Eleito" só quando o TSE marca. Consulta a cada 60 s, só com a aba visível, uma por vez.
 */
export function Apuracao() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [scope, setScope] = useState<TseScope>("br");
  const [attempt, setAttempt] = useState(0);
  const lastFetch = useRef(0);
  const tabs = useRef<Record<TseScope, HTMLButtonElement | null>>({ br: null, rr: null });

  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;

    function refresh() {
      if (inFlight) return; // uma consulta por vez: nada de resposta velha sobrescrever nova
      inFlight = true;
      lastFetch.current = Date.now();
      apiFetch<unknown>("/tse/resultado", { anonymous: true, signal: controller.signal })
        .then((data) => {
          if (controller.signal.aborted) return;
          if (!isTseResponse(data)) throw new Error("resposta fora do formato");
          setState({ kind: "ready", data, stale: false });
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setState((prev) => {
            // Já tem números: mantém e avisa (melhor o último resultado que nenhum).
            if (prev.kind === "ready") return { ...prev, stale: true };
            if (error instanceof ApiError && error.code === "apuracao_indisponivel") {
              return { kind: "unavailable", message: error.message };
            }
            return { kind: "error", message: "Não foi possível carregar a apuração." };
          });
        })
        .finally(() => {
          inFlight = false;
        });
    }
    function due(): boolean {
      return Date.now() - lastFetch.current >= POLL_MS - POLL_SLACK_MS;
    }
    function onVisibility() {
      if (document.visibilityState === "visible" && due()) refresh();
    }
    refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && due()) refresh();
    }, POLL_MS);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [attempt]);

  function select(next: TseScope) {
    setScope(next);
    tabs.current[next]?.focus();
  }

  function onTabKey(event: KeyboardEvent<HTMLButtonElement>) {
    const index = SCOPES.indexOf(scope);
    const moves: Record<string, number> = {
      ArrowRight: (index + 1) % SCOPES.length,
      ArrowLeft: (index - 1 + SCOPES.length) % SCOPES.length,
      Home: 0,
      End: SCOPES.length - 1,
    };
    const target = moves[event.key];
    if (target === undefined) return;
    event.preventDefault();
    const next = SCOPES[target];
    if (next) select(next);
  }

  const results = state.kind === "ready" ? state.data.escopos : [];
  const resultOf = (item: TseScope) => results.find((entry) => entry.escopo === item)?.resultado ?? null;
  const headline = resultOf("br") ?? resultOf("rr");
  const active = resultOf(scope);

  return (
    <section className="space-y-5">
      <title>Apuração oficial — ChecaFatosRR</title>
      <h1 className="text-2xl font-bold leading-tight sm:text-3xl">Apuração oficial</h1>
      <p className="text-slate-700">
        {headline?.cargo || "Presidente"} · {headline?.turno ?? 2}º turno
      </p>

      {state.kind === "loading" && <Loading />}
      {state.kind === "unavailable" && <p className="rounded-lg bg-slate-100 p-4">{state.message}</p>}
      {state.kind === "error" && (
        <div className="space-y-3">
          <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-4 text-red-900">
            {state.message}
          </p>
          <button
            type="button"
            onClick={() => {
              setState({ kind: "loading" });
              setAttempt((value) => value + 1);
            }}
            className="min-h-11 rounded-md bg-brand-700 px-4 font-semibold text-white"
          >
            Tentar de novo
          </button>
        </div>
      )}
      {state.kind === "ready" && (
        <>
          {state.stale && (
            <p role="alert" className="rounded-lg bg-amber-50 p-3 text-sm text-amber-950">
              Não foi possível atualizar agora; mostrando a última atualização.
            </p>
          )}
          {/* Região viva fixa (fora dos painéis): anuncia a mudança do percentual com o escopo. */}
          <p aria-live="polite" className="sr-only">
            {active?.secoes_pct != null &&
              `${SCOPE_LABELS[scope]}: ${formatPct(active.secoes_pct)} das seções totalizadas`}
          </p>
          <div role="tablist" aria-label="Abrangência" className="flex gap-2">
            {SCOPES.map((item) => (
              <button
                key={item}
                ref={(node) => {
                  tabs.current[item] = node;
                }}
                type="button"
                role="tab"
                id={`aba-${item}`}
                aria-selected={scope === item}
                aria-controls={`painel-${item}`}
                tabIndex={scope === item ? 0 : -1}
                onClick={() => setScope(item)}
                onKeyDown={onTabKey}
                className={`min-h-11 rounded-md px-4 font-semibold ${
                  scope === item ? "bg-slate-900 text-white" : "bg-white text-slate-800 ring-1 ring-slate-300"
                }`}
              >
                {SCOPE_LABELS[item]}
              </button>
            ))}
          </div>
          {SCOPES.map((item) => (
            <div
              key={item}
              role="tabpanel"
              id={`painel-${item}`}
              aria-labelledby={`aba-${item}`}
              tabIndex={0}
              hidden={scope !== item}
            >
              {scope === item && <ScopePanel result={resultOf(item)} />}
            </div>
          ))}
          <MapBoundary>
            <Suspense fallback={null}>
              <MapaApuracao />
            </Suspense>
          </MapBoundary>
        </>
      )}

      <p className="text-sm text-slate-700">
        Fonte: TSE. Resultados oficiais em{" "}
        <a href={OFFICIAL_SITE} {...EXTERNAL} className={LINK_CLASS}>
          resultados.tse.jus.br<span className="sr-only"> (abre em nova aba)</span>
        </a>
      </p>
    </section>
  );
}

function ScopePanel({ result }: { result: TseResult | null }) {
  if (result === null) {
    return <p className="rounded-lg bg-slate-100 p-4">Aguardando os primeiros resultados do TSE.</p>;
  }
  const updated = result.totalizado_em ? formatDateTime(result.totalizado_em) : "";
  return (
    <div className="space-y-4">
      <div className="space-y-1">
        {result.secoes_pct !== null && (
          <p className="text-lg font-semibold">{formatPct(result.secoes_pct)} das seções totalizadas</p>
        )}
        {result.final && <p className="font-semibold">Totalização concluída pelo TSE</p>}
        {updated && (
          <p className="text-sm text-slate-600">Atualizado em {updated} (horário de Boa Vista)</p>
        )}
      </div>
      <ol className="space-y-3">
        {result.candidatos.map((candidate) => (
          <CandidateRow key={candidate.numero} candidate={candidate} />
        ))}
      </ol>
      <dl className="grid grid-cols-1 gap-2 text-sm text-slate-700 sm:grid-cols-3">
        <Total label="Brancos" votes={result.brancos} pct={result.brancos_pct} />
        <Total label="Nulos" votes={result.nulos} pct={result.nulos_pct} />
        <Total label="Abstenções" votes={result.abstencao} pct={result.abstencao_pct} />
      </dl>
    </div>
  );
}

function CandidateRow({ candidate }: { candidate: TseCandidate }) {
  const details = [`nº ${candidate.numero}`, candidate.partido, candidate.vice ? `vice: ${candidate.vice}` : null]
    .filter((part): part is string => Boolean(part))
    .join(" · ");
  const width = Math.max(0, Math.min(candidate.pct ?? 0, 100));
  return (
    <li className="space-y-2 rounded-lg border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="font-bold">
          <span>{candidate.nome}</span>
          {candidate.eleito && (
            <span className="ml-2 rounded bg-slate-900 px-2 py-0.5 text-xs font-semibold text-white">
              {candidate.situacao || "Eleito"}
            </span>
          )}
        </p>
        <p className="text-lg font-semibold">
          <span>{formatPct(candidate.pct)}</span>
        </p>
      </div>
      <p className="text-sm text-slate-600">{details}</p>
      {/* Barra neutra (sem cor de partido); largura via CSSOM, permitido pela CSP. */}
      <div className="h-2 w-full rounded bg-slate-200" aria-hidden="true">
        <div className="h-2 rounded bg-slate-600" style={{ width: `${width}%` }} />
      </div>
      <p className="text-sm text-slate-700">
        <span>{formatVotes(candidate.votos)} votos</span>
      </p>
    </li>
  );
}

function Total({ label, votes, pct }: { label: string; votes: number | null; pct: number | null }) {
  return (
    <div className="rounded-lg bg-slate-100 p-2">
      <dt className="font-semibold">{label}</dt>
      <dd>
        {formatVotes(votes)} ({formatPct(pct)})
      </dd>
    </div>
  );
}
