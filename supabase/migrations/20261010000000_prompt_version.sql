-- Etapa 2: versão do prompt na chave do cache de checagens.
-- Resultado de outra versão (ou de antes desta coluna: null) não é servido como cache;
-- continua acessível pelo link público, com a análise da época.
alter table public.checks add column prompt_version text;

comment on column public.checks.prompt_version is
  'gemini.PROMPT_VERSION que produziu o resultado; o cache só reaproveita a mesma versão';
