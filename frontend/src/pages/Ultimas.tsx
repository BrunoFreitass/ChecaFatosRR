import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { Loading } from "../components/Loading.tsx";
import { VerdictBadge } from "../components/VerdictBadge.tsx";
import { ApiError, apiFetch } from "../lib/api.ts";
import { isAwaitingCheck, relativeTime } from "../lib/format.ts";
import type { CheckSummary, LatestChecks } from "../lib/types.ts";

type Page = { items: CheckSummary[]; cursor: string | null };

function fetchPage(cursor: string | null, signal?: AbortSignal): Promise<LatestChecks> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return apiFetch<LatestChecks>(`/checks/latest${query}`, { signal });
}

function message(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

export function Ultimas() {
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const moreController = useRef<AbortController | null>(null);
  const firstNewItem = useRef<HTMLAnchorElement | null>(null);
  const [focusIndex, setFocusIndex] = useState<number | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchPage(null, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setPage({ items: data.items, cursor: data.next_cursor });
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(message(err, "Não foi possível carregar."));
      });
    return () => controller.abort();
  }, [attempt]);

  useEffect(() => () => moreController.current?.abort(), []);

  // Depois de "Carregar mais", o foco vai para o primeiro item novo (o botão pode sumir).
  useEffect(() => {
    if (focusIndex !== null) firstNewItem.current?.focus();
  }, [focusIndex]);

  async function loadMore() {
    if (!page?.cursor || loadingMore) return;
    const controller = new AbortController();
    moreController.current = controller;
    setLoadingMore(true);
    setError(null);
    try {
      const data = await fetchPage(page.cursor, controller.signal);
      setFocusIndex(page.items.length);
      setPage((prev) => ({
        items: [...(prev?.items ?? []), ...data.items],
        cursor: data.next_cursor,
      }));
    } catch (err) {
      if (!controller.signal.aborted) setError(message(err, "Não foi possível carregar mais."));
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
    }
  }

  return (
    <section className="space-y-4" aria-busy={page === null || loadingMore}>
      <title>Últimas checagens — ChecaFatosRR</title>
      <h1 className="text-2xl font-bold sm:text-3xl">Últimas checagens</h1>
      <p className="text-slate-700">
        Links de notícias checados recentemente. Mensagens coladas não aparecem aqui.
      </p>

      {page === null && !error && <Loading />}
      {page && page.items.length === 0 && (
        <p className="text-slate-700">
          Ainda não há checagens.{" "}
          <Link to="/" className="font-semibold text-brand-700 underline">
            Seja o primeiro a checar um link
          </Link>
          .
        </p>
      )}

      {page && page.items.length > 0 && (
        <ul className="space-y-3">
          {page.items.map((item, index) => (
            <li key={item.id}>
              <Link
                ref={index === focusIndex ? firstNewItem : undefined}
                to={`/checagem/${item.id}`}
                className="block space-y-2 rounded-lg border border-slate-200 bg-white p-4 hover:border-brand-700 focus-visible:outline-2 focus-visible:outline-brand-700"
              >
                {item.modo === "checagem_de_agencia" && item.agencia ? (
                  // O link é a checagem de uma agência: o selo é dela, sem veredito nosso.
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="inline-flex items-center rounded-full bg-slate-200 px-2.5 py-1 text-sm font-bold text-slate-900">
                      Checagem de {item.agencia}
                    </span>
                    {item.classificacao && (
                      <span className="text-sm font-semibold text-slate-800">
                        Classificação da agência: {item.classificacao}
                      </span>
                    )}
                  </span>
                ) : (
                  <VerdictBadge
                    verdict={item.veredito}
                    awaitingCheck={isAwaitingCheck(item.veredito, item.total_agencias)}
                  />
                )}
                <span className="block break-words font-semibold text-slate-900">
                  {item.title || item.domain}
                </span>
                <span className="block break-words text-sm text-slate-600">
                  {item.domain} · {relativeTime(item.created_at)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}

      {error && (
        <div role="alert" className="space-y-3 rounded-lg border border-red-200 bg-red-50 p-4 text-red-900">
          <p>{error}</p>
          {page === null && (
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
          {loadingMore ? "Carregando…" : "Carregar mais"}
        </button>
      )}
    </section>
  );
}
