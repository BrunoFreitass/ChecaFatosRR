import { useEffect, useId, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";

import { useAuth } from "../auth/AuthContext.ts";
import { ApiError, apiFetch } from "../lib/api.ts";
import type { Profile } from "../lib/types.ts";

// Datas sempre no horário de Roraima.
const DATE_TIME = new Intl.DateTimeFormat("pt-BR", {
  dateStyle: "short",
  timeStyle: "short",
  timeZone: "America/Boa_Vista",
});

function StatusLine({ profile }: { profile: Profile }) {
  if (profile.status === "banned") {
    return (
      <p className="rounded-lg bg-red-50 p-3 text-red-900">
        Sua conta foi banida por violar as regras. Você ainda pode ler o site.
      </p>
    );
  }
  if (profile.status === "suspended" && profile.suspended_until) {
    return (
      <p className="rounded-lg bg-amber-50 p-3 text-amber-950">
        Sua conta está suspensa até {DATE_TIME.format(new Date(profile.suspended_until))}{" "}
        (horário de Roraima). Até lá você não pode comentar.
      </p>
    );
  }
  return <p className="text-green-800">Conta ativa.</p>;
}

export function MinhaConta() {
  const { profile, signOut } = useAuth();
  const navigate = useNavigate();
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const confirmId = useId();
  const confirmRef = useRef<HTMLParagraphElement>(null);

  // Ao abrir a confirmação, leva o foco (e o leitor de tela) até a pergunta.
  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
  }, [confirming]);

  if (!profile) return null;

  async function handleSignOut() {
    navigate("/", { replace: true });
    await signOut(); // signOut() já trata falhas de rede (limpa a sessão local).
  }

  async function handleDelete() {
    setError(null);
    setDeleting(true);
    try {
      await apiFetch<void>("/me", { method: "DELETE" });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Não foi possível excluir sua conta agora.");
      setDeleting(false);
      return;
    }
    // A conta já foi excluída: daqui em diante, qualquer falha não pode dizer o contrário.
    navigate("/", { replace: true, state: { contaExcluida: true } });
    await signOut();
  }

  return (
    <section className="mx-auto max-w-md space-y-6">
      <title>Minha conta — ChecaFatosRR</title>
      <h1 className="text-2xl font-bold">Minha conta</h1>

      <div className="space-y-2 rounded-lg border border-slate-200 bg-white p-4">
        <p>
          Apelido: <strong>{profile.apelido}</strong>
        </p>
        <StatusLine profile={profile} />
      </div>

      {profile.is_admin && (
        <Link
          to="/admin/moderacao"
          className="inline-flex min-h-12 w-full items-center justify-center rounded-lg bg-brand-700 px-4 font-semibold text-white"
        >
          Fila de moderação
        </Link>
      )}

      <button
        type="button"
        onClick={handleSignOut}
        className="inline-flex min-h-12 w-full items-center justify-center rounded-lg border border-slate-300 bg-white px-4 font-semibold"
      >
        Sair
      </button>

      <div className="space-y-3 border-t border-slate-200 pt-6">
        <h2 className="text-lg font-semibold">Excluir minha conta</h2>
        <p className="text-sm text-slate-700">
          Apaga seu apelido, seus comentários e seu histórico. Não dá para desfazer. Se sua conta
          tiver advertências ou estiver suspensa, isso fica guardado por 30 dias (sem dados
          pessoais) e volta se você criar uma conta nova com o mesmo Google.
        </p>
        {!confirming ? (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="inline-flex min-h-11 items-center rounded-lg px-1 font-semibold text-red-800 underline"
          >
            Quero excluir minha conta
          </button>
        ) : (
          <div role="group" aria-labelledby={confirmId} className="space-y-3 rounded-lg bg-red-50 p-4">
            <p id={confirmId} ref={confirmRef} tabIndex={-1} className="font-semibold text-red-900 outline-none">
              Tem certeza? Essa ação é definitiva.
            </p>
            <div className="flex flex-col gap-2 sm:flex-row">
              <button
                type="button"
                onClick={handleDelete}
                disabled={deleting}
                className="inline-flex min-h-11 items-center justify-center rounded-lg bg-red-800 px-4 font-semibold text-white disabled:opacity-60"
              >
                {deleting ? "Excluindo…" : "Sim, excluir"}
              </button>
              <button
                type="button"
                onClick={() => setConfirming(false)}
                disabled={deleting}
                className="inline-flex min-h-11 items-center justify-center rounded-lg border border-slate-300 bg-white px-4 font-semibold"
              >
                Cancelar
              </button>
            </div>
          </div>
        )}
        {error && (
          <p role="alert" className="text-sm text-red-800">
            {error}
          </p>
        )}
      </div>
    </section>
  );
}
