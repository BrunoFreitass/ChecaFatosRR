# TDD — fallback de modelos do Gemini (09/10/2026)

**Pedido:** `GEMINI_MODEL_FALLBACKS` (lista separada por vírgula, padrão `gemini-3.8-flash,gemini-3.7-flash`). Em 429 de cota diária do modelo atual, tenta o próximo; o log indica qual modelo respondeu. Testes só com mocks (regra do projeto).

## RED
`pytest tests/unit/test_gemini_fallback.py` → `ImportError: cannot import name 'Generated'` (RED de compilação). Nos testes de fumaça, `CheckResult(model=...)` → `TypeError`: a checagem ainda não registrava o modelo que respondeu.

## GREEN
Suíte completa com integração em PostgreSQL 17 local, banco novo → **496 passed**, 0 pulados. ruff limpo, quality-gate 0 problemas.

| # | O que é garantido | Teste |
|---|---|---|
| 1 | Cadeia padrão 3.8 → 3.7; parsing com strip/dedup; nomes inválidos recusados; leitura do ambiente | `test_default_chain_is_38_then_37`, `test_chain_parsing`, `test_invalid_chain_is_rejected`, `test_chain_comes_from_the_environment` |
| 2 | 429 diário no 1º modelo → 2º responde, um cliente só; logs `gemini_api_error`/`gemini_model_quota_exhausted`/`gemini_model_responded` com o modelo | `test_daily_quota_on_primary_falls_back_to_next_model` |
| 3 | Sucesso no 1º não toca o 2º | `test_primary_success_does_not_touch_backup` |
| 4 | Todos esgotados → "esgotada hoje"; sem gastar nossa cota nas checagens seguintes | `test_all_models_out_of_daily_quota_is_quota_exhausted`, `test_all_models_exhausted_does_not_spend_our_daily_quota` |
| 5 | 503, 400 e rede NÃO trocam de modelo; 429 por minuto repete o mesmo modelo | `test_other_errors_do_not_fall_back`, `test_per_minute_429_retries_the_same_model` |
| 6 | Modelo esgotado é pulado até a meia-noite do Pacífico, no máximo 1 h | `test_exhausted_model_is_skipped_until_the_quota_resets`, `test_exhausted_model_is_rechecked_after_at_most_an_hour`, `test_every_model_known_exhausted_fails_without_calling`, `test_quota_resets_at_pacific_midnight` |
| 7 | A checagem grava o modelo que respondeu; o fallback reserva uma unidade só; o modelo fica fora do JSON público | `test_check_result_records_the_model_that_answered`, `test_saves_the_model_that_answered` |
| 8 | Router monta a cadeia a partir do Settings | `test_router_builds_the_chain_from_settings` |
| 9 | "PerDay" só na mensagem também conta como cota diária | `test_daily_quota_named_only_in_the_message_also_falls_back` |

## Lacunas conhecidas
- Não houve chamada real ao Google (regra: só com autorização, no máximo 3 por rodada).
- Não houve medição de cobertura nem checagem de tipos (pytest-cov e pyright não estão instalados).
