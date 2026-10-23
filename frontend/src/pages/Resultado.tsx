import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router";

import { Comments } from "../components/Comments.tsx";
import { Loading } from "../components/Loading.tsx";
import { ShareButtons } from "../components/ShareButtons.tsx";
import { VerdictBadge } from "../components/VerdictBadge.tsx";
import { ApiError, apiFetch } from "../lib/api.ts";
import {
  AGENCIES_ONLY_LABEL,
  AWAITING_CHECK_LABEL,
  formatDate,
  formatDateTime,
  isAwaitingCheck,
  isTrustedUrl,
  safeHref,
  splitLinks,
  trustedHosts,
  verdictLabel,
  visibleSuspiciousPoints,
} from "../lib/format.ts";
import type { AgencyCheck, AgencyPage, Check } from "../lib/types.ts";

// Busca nas agências falhada: nunca "não encontramos" (a página é pública).
const SEARCH_FAILED = "A busca nas agências de checagem falhou nesta análise. Tente novamente mais tarde.";

const DISCLAIMER =
  "Análise automatizada. Confira também as agências de checagem e o site do TSE.";
const EXTERNAL = { target: "_blank", rel: "noopener noreferrer nofollow ugc" } as const;
const LINK_CLASS = "font-semibold text-brand-700 underline break-words";

type State =
  | { kind: "loading" }
  | { kind: "ready"; check: Check }
  | { kind: "not_found" }
  | { kind: "error"; message: string };

/**
 * Texto gerado pela IA. Só endereços de agências e de sites institucionais viram link; os
 * demais aparecem como texto (o React escapa todo o conteúdo).
 */
function TextWithLinks({ text, hosts }: { text: string; hosts: Set<string> }) {
  return (
    <>
      {splitLinks(text).map((part, index) => {
        const href = part.url && isTrustedUrl(part.url, hosts) ? safeHref(part.url) : null;
        return href ? (
          <a key={index} href={href} {...EXTERNAL} className={LINK_CLASS}>
            {part.text}
          </a>
        ) : (
          <span key={index} className="break-words">
            {part.text}
          </span>
        );
      })}
    </>
  );
}

function AgencyList({ items }: { items: AgencyCheck[] }) {
  return (
    <ul className="space-y-3">
      {items.map((item, index) => {
        const href = safeHref(item.url);
        return (
          <li key={`${item.url}-${index}`} className="rounded-lg border border-slate-200 bg-white p-3">
            <p className="text-sm font-semibold text-slate-600">
              {item.agencia}
              {item.data && <> · {formatDate(item.data)}</>}
            </p>
            {href ? (
              <a href={href} {...EXTERNAL} className={LINK_CLASS}>
                {item.titulo}
              </a>
            ) : (
              <p className="font-semibold">{item.titulo}</p>
            )}
            <p className="text-sm text-slate-800">Classificação da agência: {item.veredito}</p>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * O link é a própria checagem de uma agência (23/10/2026): mostra o que ela publicou, sem
 * veredito nem confiança nossos. O título da matéria nunca aparece como alegação.
 */
function AgencyPageResult({ check, page }: { check: Check; page: AgencyPage }) {
  const href = safeHref(page.url);
  const shareText = page.alegacao
    ? `ChecaFatosRR: checagem de ${page.agencia} — alegação "${page.alegacao}" classificada como ${page.classificacao}`
    : `ChecaFatosRR: checagem de ${page.agencia} — "${page.titulo}" classificada como ${page.classificacao}`;
  return (
    <article className="space-y-6">
      <title>{`Checagem de ${page.agencia}: ${page.titulo} — ChecaFatosRR`}</title>
      <header className="space-y-3">
        <p className="inline-flex rounded-full bg-slate-200 px-4 py-2 text-lg font-semibold text-slate-900">
          Checagem de agência
        </p>
        <h1 className="break-words text-2xl font-bold leading-tight sm:text-3xl">{page.titulo}</h1>
      </header>
      <section aria-labelledby="checagem-agencia" className="space-y-3">
        <h2 id="checagem-agencia" className="sr-only">
          Checagem da agência
        </h2>
        <p className="text-lg text-slate-800">Este link é uma checagem publicada por {page.agencia}.</p>
        {page.alegacao ? (
          <p className="break-words text-slate-800">Alegação verificada: {page.alegacao}</p>
        ) : (
          <p className="break-words text-slate-800">Título da checagem: {page.titulo}</p>
        )}
        <p className="font-semibold text-slate-900">Classificação da agência: {page.classificacao}</p>
        {href && (
          <a
            href={href}
            {...EXTERNAL}
            className="inline-flex min-h-11 items-center justify-center rounded-lg bg-brand-700 px-4 font-semibold text-white hover:bg-brand-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-700"
          >
            Ler a checagem completa
          </a>
        )}
      </section>
      <section aria-labelledby="compartilhar" className="space-y-2">
        <h2 id="compartilhar" className="text-xl font-bold">
          Compartilhe a checagem
        </h2>
        <ShareButtons text={shareText} url={window.location.href} />
      </section>
      <Comments checagem={check.id} title="Comentários" />
      <p className="text-sm text-slate-600">
        Consultado em {formatDateTime(check.created_at)} (horário de Boa Vista).{" "}
        <Link to="/" className={LINK_CLASS}>
          Checar outro conteúdo
        </Link>
      </p>
    </article>
  );
}

/** Remonta a página a cada checagem: o estado nunca mostra a anterior enquanto carrega. */
export function Resultado() {
  const { slug = "" } = useParams();
  return <ResultadoDaChecagem key={slug} slug={slug} />;
}

function ResultadoDaChecagem({ slug }: { slug: string }) {
  const location = useLocation();
  // Vindo do formulário, o resultado já está em memória: não busca de novo.
  const fromForm = (location.state as { check?: Check } | null)?.check;
  const [state, setState] = useState<State>(() =>
    fromForm?.id === slug ? { kind: "ready", check: fromForm } : { kind: "loading" },
  );
  const [attempt, setAttempt] = useState(0);
  const needsFetch = state.kind === "loading";

  useEffect(() => {
    if (!needsFetch) return;
    const controller = new AbortController();
    apiFetch<Check>(`/checks/${encodeURIComponent(slug)}`, { signal: controller.signal })
      .then((check) => {
        if (!controller.signal.aborted) setState({ kind: "ready", check });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) setState({ kind: "not_found" });
        else
          setState({
            kind: "error",
            message: error instanceof ApiError ? error.message : "Não foi possível carregar.",
          });
      });
    return () => controller.abort();
  }, [slug, needsFetch, attempt]);

  if (state.kind === "loading") return <Loading />;
  if (state.kind === "not_found") {
    return (
      <section className="space-y-4">
        <title>Checagem não encontrada — ChecaFatosRR</title>
        <meta name="robots" content="noindex" />
        <h1 className="text-2xl font-bold">Checagem não encontrada</h1>
        <p className="text-slate-700">Confira se o link está completo.</p>
        <Link to="/" className={LINK_CLASS}>
          Checar um link ou mensagem
        </Link>
      </section>
    );
  }
  if (state.kind === "error") {
    return (
      <section className="space-y-4">
        <title>Não foi possível carregar — ChecaFatosRR</title>
        <h1 className="text-2xl font-bold">Não foi possível carregar a checagem</h1>
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
      </section>
    );
  }

  const { check } = state;
  if (check.modo === "checagem_de_agencia" && check.checagem_de_agencia) {
    return <AgencyPageResult check={check} page={check.checagem_de_agencia} />;
  }
  const related = check.checagens_relacionadas ?? [];
  const hosts = trustedHosts([...check.checagens_agencias, ...related].map((item) => item.url));
  // Gemini indisponível (sem cota ou no limite): só o que as agências publicaram, sem veredito.
  const agenciesOnly = check.modo === "agencias";
  // Status da busca nas agências gravado na hora da checagem (antigas: sem registro).
  const searchFailed = check.busca_agencias === "falhou";
  // Não verificável sem checagem de agência: neutro ("Ainda sem checagem"), nunca "parece falso".
  const awaiting =
    !agenciesOnly && isAwaitingCheck(check.veredito, check.checagens_agencias.length);
  const label = agenciesOnly
    ? AGENCIES_ONLY_LABEL
    : awaiting
      ? AWAITING_CHECK_LABEL
      : verdictLabel(check.veredito);
  // Sem IA, não há pontos suspeitos nem contexto (e nada de texto de IA sob o selo neutro).
  const suspicious = agenciesOnly
    ? []
    : visibleSuspiciousPoints(check.pontos_suspeitos, awaiting);
  const subject =
    check.tipo === "link" ? check.title || check.domain || "Notícia" : "Mensagem encaminhada";
  // Não verificável (com ou sem checagens encontradas): nunca soa como desmentido (23/10).
  // No modo "só agências" o selo é "Análise automática indisponível agora" (não houve análise).
  const notVerifiable = check.veredito === "nao_verificavel";
  const shareText = `ChecaFatosRR: "${subject}" — ${notVerifiable && !agenciesOnly ? "ainda sem checagem conclusiva" : label}`;
  const originalHref = safeHref(check.url);

  return (
    <article className="space-y-6">
      <title>{`${label}: ${subject} — ChecaFatosRR`}</title>
      {/* Texto colado não vai para buscadores (só quem recebeu o link vê). */}
      {check.tipo === "texto" && <meta name="robots" content="noindex" />}

      <header className="space-y-3">
        {agenciesOnly ? (
          <p className="inline-flex rounded-full bg-slate-200 px-4 py-2 text-lg font-semibold text-slate-900">
            {AGENCIES_ONLY_LABEL}
          </p>
        ) : (
          <VerdictBadge verdict={check.veredito} size="lg" awaitingCheck={awaiting} />
        )}
        {awaiting && (
          <p className="rounded-lg bg-slate-100 p-3 text-slate-800">
            {searchFailed
              ? `${SEARCH_FAILED} Isso não significa que o conteúdo seja falso.`
              : "Não encontramos checagens sobre este conteúdo. Isso não significa que seja falso."}
          </p>
        )}
        {!agenciesOnly && check.rebaixado_por_confianca && (
          <p className="rounded-lg bg-slate-100 p-3 text-sm text-slate-800">
            {awaiting
              ? "A análise automática também teve pouca confiança neste conteúdo."
              : "A análise automática teve pouca confiança, por isso classificamos como Não verificável."}
          </p>
        )}
        <h1 className="break-words text-2xl font-bold leading-tight sm:text-3xl">{subject}</h1>
        {check.analise_anterior && (
          <p className="text-sm text-slate-600">
            Análise anterior: feita com uma versão anterior do nosso verificador.
          </p>
        )}
        {check.tipo === "link" && originalHref && (
          <p className="break-words text-slate-700">
            {check.domain}
            {check.published_at && <> · publicado em {formatDate(check.published_at)}</>} ·{" "}
            <a href={originalHref} {...EXTERNAL} className={LINK_CLASS}>
              abrir notícia original
            </a>
          </p>
        )}
        {check.tipo === "texto" && check.texto && (
          <details className="rounded-lg border border-slate-200 bg-white p-3">
            <summary className="min-h-11 cursor-pointer content-center font-semibold">
              Mensagem analisada
            </summary>
            <p className="mt-2 whitespace-pre-wrap break-words text-slate-800">{check.texto}</p>
          </details>
        )}
      </header>

      <section aria-labelledby="explicacao" className="space-y-3">
        <h2 id="explicacao" className="text-xl font-bold">
          O que encontramos
        </h2>
        <p className="text-lg text-slate-800">
          <TextWithLinks text={check.resumo} hosts={hosts} />
        </p>
        {/* A confiança é a do modelo no veredito dele: sem veredito, o número confunde. */}
        {!notVerifiable && !agenciesOnly && (
          <p className="text-sm text-slate-600">Confiança da análise: {check.confianca}%</p>
        )}
      </section>

      {suspicious.length > 0 && (
        <section aria-labelledby="suspeitos" className="space-y-2">
          <h2 id="suspeitos" className="text-xl font-bold">
            Pontos suspeitos
          </h2>
          <ul className="list-disc space-y-1 pl-6 text-slate-800">
            {suspicious.map((item, index) => (
              <li key={index}>
                <TextWithLinks text={item} hosts={hosts} />
              </li>
            ))}
          </ul>
        </section>
      )}

      {!agenciesOnly && check.contexto_faltante && (
        <section aria-labelledby="contexto" className="space-y-2">
          <h2 id="contexto" className="text-xl font-bold">
            Contexto que falta
          </h2>
          <p className="text-slate-800">
            <TextWithLinks text={check.contexto_faltante} hosts={hosts} />
          </p>
        </section>
      )}

      <section aria-labelledby="agencias" className="space-y-2">
        <h2 id="agencias" className="text-xl font-bold">
          {agenciesOnly ? "Checagens de agências encontradas" : "O que dizem as agências de checagem"}
        </h2>
        {check.checagens_agencias.length === 0 ? (
          <p className="text-slate-700">
            {/* Falha da busca não é "não encontramos" (o link é público): o status é o gravado
                na hora da checagem. */}
            {searchFailed ? SEARCH_FAILED : "Não encontramos checagens de agências sobre este conteúdo."}
          </p>
        ) : (
          <AgencyList items={check.checagens_agencias} />
        )}
      </section>

      {related.length > 0 && (
        <section aria-labelledby="relacionadas" className="space-y-2">
          <h2 id="relacionadas" className="text-xl font-bold">
            Checagens sobre assuntos relacionados
          </h2>
          <p className="text-slate-700">
            Estas checagens tratam de assuntos parecidos, não deste conteúdo.
          </p>
          <AgencyList items={related} />
        </section>
      )}

      {check.fontes_sugeridas.length > 0 && (
        <section aria-labelledby="fontes" className="space-y-2">
          <h2 id="fontes" className="text-xl font-bold">
            Onde conferir
          </h2>
          <ul className="list-disc space-y-1 pl-6 text-slate-800">
            {check.fontes_sugeridas.map((item, index) => (
              <li key={index}>
                <TextWithLinks text={item} hosts={hosts} />
              </li>
            ))}
          </ul>
        </section>
      )}

      <p className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-amber-950">{DISCLAIMER}</p>

      <section aria-labelledby="compartilhar" className="space-y-2">
        <h2 id="compartilhar" className="text-xl font-bold">
          Compartilhe a checagem
        </h2>
        <ShareButtons text={shareText} url={window.location.href} />
      </section>

      {/* Comentários só em checagens de links (texto colado é privado de quem recebeu). */}
      {check.tipo === "link" && <Comments checagem={check.id} title="Comentários" />}

      <p className="text-sm text-slate-600">
        Checado em {formatDateTime(check.created_at)} (horário de Boa Vista).{" "}
        <Link to="/" className={LINK_CLASS}>
          Checar outro conteúdo
        </Link>
      </p>
    </article>
  );
}
