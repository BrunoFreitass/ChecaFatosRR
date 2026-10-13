-- ChecaFatosRR — contador diário de chamadas POR MODELO do Gemini (nível gratuito).
-- Aplicar depois de 20261013000000_comentarios.sql (SQL Editor ou `supabase db push`).
-- Etapa 9: tabela nova — rode de novo o bloco "do $$ ... $$" de
-- supabase/manual/app_backend_role.sql para a policy do app_backend.
--
-- Cada modelo tem o seu RPD (AI Studio, 12/10/2026: 20 nos Flash, 500 nos Flash-Lite, 14.400
-- nos Gemma). O backend reserva uma unidade antes de chamar o modelo e pula o esgotado sem
-- chamá-lo. No banco (e não em memória) porque o Render dorme e zeraria o contador.
-- day = dia no fuso do Pacífico: é quando a cota gratuita do Google zera.

create table public.gemini_model_usage (
  day   date    not null,
  model text    not null check (char_length(model) between 1 and 64),
  calls integer not null default 0 check (calls >= 0),
  primary key (day, model)
);

alter table public.gemini_model_usage enable row level security;
revoke all on public.gemini_model_usage from anon, authenticated;
