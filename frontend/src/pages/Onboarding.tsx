import { useEffect, useId, useState, type FormEvent } from "react";
import { useNavigate } from "react-router";

import { useAuth } from "../auth/AuthContext.ts";
import { ViolationNotice } from "../components/ViolationNotice.tsx";
import { ApiError, apiFetch } from "../lib/api.ts";
import { readReturnPath } from "../lib/returnPath.ts";
import type { NicknameAvailability, NicknameRejection, Profile } from "../lib/types.ts";

const FORMAT = /^[A-Za-z0-9_]{3,20}$/;
const FORMAT_HINT = "De 3 a 20 caracteres: letras sem acento, números ou _ (sublinhado).";
const REJECTIONS: Record<NicknameRejection, string> = {
  formato: FORMAT_HINT,
  reservado: "Esse apelido é reservado. Escolha outro.",
  conteudo_proibido: "Esse apelido não é permitido pelas regras do site.",
  em_uso: "Esse apelido já está em uso. Escolha outro.",
};

type Check =
  | { state: "idle" }
  | { state: "checking" }
  | { state: "ok" }
  // A consulta falhou (rede/servidor): deixa enviar — o servidor valida de novo.
  | { state: "unknown" }
  | { state: "bad"; message: string };

type Remote = { value: string; result: NicknameAvailability | null };

export function Onboarding() {
  const { termsVersion, setProfile, reloadProfile } = useAuth();
  const navigate = useNavigate();
  const inputId = useId();
  const hintId = useId();
  const statusId = useId();

  const [apelido, setApelido] = useState("");
  const [aceito, setAceito] = useState(false);
  const [remote, setRemote] = useState<Remote | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [violation, setViolation] = useState<{ rule: string; explanation: string } | null>(null);

  const value = apelido.trim();
  const formatOk = FORMAT.test(value);

  // Consulta a disponibilidade 400 ms depois que a pessoa para de digitar.
  useEffect(() => {
    if (!formatOk) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      apiFetch<NicknameAvailability>(`/apelido/disponivel?q=${encodeURIComponent(value)}`, {
        signal: controller.signal,
      })
        .then((result) => setRemote({ value, result }))
        .catch(() => {
          if (!controller.signal.aborted) setRemote({ value, result: null });
        });
    }, 400);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [value, formatOk]);

  let check: Check = { state: "idle" };
  if (value && !formatOk) check = { state: "bad", message: FORMAT_HINT };
  else if (formatOk && remote?.value !== value) check = { state: "checking" };
  else if (formatOk && remote && !remote.result) check = { state: "unknown" };
  else if (formatOk && remote?.result) {
    check = remote.result.disponivel
      ? { state: "ok" }
      : { state: "bad", message: REJECTIONS[remote.result.motivo ?? "formato"] };
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setViolation(null);
    if (!termsVersion) {
      setError("Estamos atualizando os dados da página. Tente de novo em alguns segundos.");
      reloadProfile();
      return;
    }
    setSubmitting(true);
    try {
      const profile = await apiFetch<Profile>("/me", {
        method: "POST",
        body: { apelido: value, aceito_termos: true, terms_version: termsVersion },
      });
      setProfile(profile);
      navigate(readReturnPath(), { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.code === "apelido_proibido") {
        setViolation({
          rule: String(err.details.regra ?? "Regra de conteúdo"),
          explanation: String(err.details.explicacao ?? err.message),
        });
      } else if (err instanceof ApiError && err.code === "apelido_em_uso") {
        // Alguém pegou o apelido entre a verificação e o envio: marca o campo.
        setRemote({ value, result: { disponivel: false, motivo: "em_uso" } });
      } else if (err instanceof ApiError && err.code === "termos_desatualizados") {
        setError(err.message);
        reloadProfile();
      } else {
        setError(err instanceof ApiError ? err.message : "Não foi possível concluir. Tente de novo.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  const canSubmit = (check.state === "ok" || check.state === "unknown") && aceito && !submitting;

  return (
    <section className="mx-auto max-w-md space-y-5">
      <title>Escolha seu apelido — ChecaFatosRR</title>
      <h1 className="text-2xl font-bold">Escolha seu apelido</h1>
      <p className="text-slate-700">
        É assim que você vai aparecer nos comentários. Seu nome real e seu e-mail nunca são
        mostrados.
      </p>

      <form onSubmit={handleSubmit} className="space-y-5" noValidate>
        <div className="space-y-2">
          <label htmlFor={inputId} className="block font-semibold">
            Apelido
          </label>
          <input
            id={inputId}
            value={apelido}
            onChange={(event) => setApelido(event.target.value)}
            maxLength={20}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            aria-describedby={`${hintId} ${statusId}`}
            aria-invalid={check.state === "bad"}
            className="block min-h-12 w-full rounded-lg border border-slate-300 px-3 text-base focus:border-brand-700 focus:outline-2 focus:outline-brand-700"
          />
          <p id={hintId} className="text-sm text-slate-600">
            {FORMAT_HINT}
          </p>
          <p id={statusId} aria-live="polite" className="min-h-5 text-sm">
            {check.state === "checking" && <span className="text-slate-600">Verificando…</span>}
            {check.state === "ok" && <span className="text-green-800">Disponível!</span>}
            {check.state === "unknown" && (
              <span className="text-slate-600">
                Não deu para verificar agora; vamos conferir quando você continuar.
              </span>
            )}
            {check.state === "bad" && <span className="text-red-800">{check.message}</span>}
          </p>
        </div>

        <label className="flex items-start gap-3">
          <input
            type="checkbox"
            checked={aceito}
            onChange={(event) => setAceito(event.target.checked)}
            className="mt-1 size-5 shrink-0 accent-brand-700"
          />
          <span>
            Li e aceito os{" "}
            <a href="/termos" target="_blank" rel="noreferrer" className="font-semibold underline">
              Termos de Uso
            </a>{" "}
            e a{" "}
            <a href="/privacidade" target="_blank" rel="noreferrer" className="font-semibold underline">
              Política de Privacidade
            </a>
            .
          </span>
        </label>

        {violation && <ViolationNotice rule={violation.rule} explanation={violation.explanation} />}
        {error && (
          <p role="alert" className="rounded-lg bg-red-50 p-3 text-red-900">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={!canSubmit}
          className="inline-flex min-h-12 w-full items-center justify-center rounded-lg bg-brand-700 px-4 font-semibold text-white hover:bg-brand-800 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? "Salvando…" : "Continuar"}
        </button>
      </form>
    </section>
  );
}
