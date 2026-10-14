-- ChecaFatosRR — mapa da apuração: resumo mais recente do TSE por UF (PR 2, plano
-- mapa-e-painel-admin.plan.md). Aplicar ANTES do merge do PR do mapa (SQL Editor).
-- Só cria uma tabela nova e as 27 linhas dela: compatível com o código atual da main.
-- Etapa 9: tabela nova — se o papel app_backend existir, rode de novo o bloco "do $$ ... $$"
-- de supabase/manual/app_backend_role.sql (hoje ele não existe: pule).

-- Uma linha por UF, criada já aqui (vencida desde sempre): o claim em lote é um update só,
-- com "skip locked" (duas requisições ao mesmo tempo nunca buscam a mesma UF no TSE).
create table if not exists public.tse_uf_snapshots (
  uf             text        primary key check (uf in (
                   'ac','al','am','ap','ba','ce','df','es','go','ma','mg','ms','mt','pa',
                   'pb','pe','pi','pr','rj','rn','ro','rr','rs','sc','se','sp','to')),
  -- Eleição do resumo (o ensaio usa 6257; outra eleição não aparece no mapa).
  eleicao        text        check (eleicao ~ '^[0-9]{1,6}$'),
  -- Só o resumo (candidatos com número, nome e %; seções apuradas; final). Nunca o JSON bruto.
  -- octet_length do texto (e não pg_column_size, que mede o valor comprimido): teto determinístico.
  resumo         jsonb       not null default '{}'::jsonb
                             check (jsonb_typeof(resumo) = 'object'
                                    and octet_length(resumo::text) <= 16384),
  tse_updated_at timestamptz,
  fetched_at     timestamptz not null default '-infinity'
);

insert into public.tse_uf_snapshots (uf)
select unnest(array[
  'ac','al','am','ap','ba','ce','df','es','go','ma','mg','ms','mt','pa',
  'pb','pe','pi','pr','rj','rn','ro','rr','rs','sc','se','sp','to'])
on conflict (uf) do nothing;

alter table public.tse_uf_snapshots enable row level security;
revoke all on public.tse_uf_snapshots from public, anon, authenticated;
