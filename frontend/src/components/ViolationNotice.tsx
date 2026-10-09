/** Aviso de violação: diz qual regra foi quebrada e por quê (usado no apelido e nos comentários). */
export function ViolationNotice({ rule, explanation }: { rule: string; explanation: string }) {
  return (
    <div role="alert" className="rounded-lg border-l-4 border-amber-500 bg-amber-50 p-4 text-amber-950">
      <p className="font-semibold">Aviso de violação: {rule}</p>
      <p className="mt-1 text-sm">{explanation}</p>
      <p className="mt-2 text-sm">
        Veja as regras completas nos{" "}
        <a href="/termos" className="font-semibold underline">
          Termos de Uso
        </a>
        .
      </p>
    </div>
  );
}
