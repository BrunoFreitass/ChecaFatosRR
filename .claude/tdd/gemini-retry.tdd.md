# TDD — chamadas ao Gemini (09/10/2026)

**Origem:** teste manual da etapa 2. O Google respondeu 504 (~33 s) e depois 503 (~22 s), e o `POST /checks` voltou 503 "checagem indisponível". Os testes foram escritos antes da implementação. Não houve commits intermediários de checkpoint: a regra do projeto é commitar só no fim da etapa aprovada.

## Diagnóstico (`/build-fix` + `cost-aware-llm-pipeline`)
- O SDK google-genai não faz novas tentativas por padrão (`retry_options=None`).
- O timeout do `HttpOptions` vale por operação de rede, não para a chamada inteira.
- Com o thinking padrão, `gemini-flash-latest` (Flash 3.x) passou de 60 s; com `thinking_budget=0`, levou de 7 a 13 s.
- A chave vai no cabeçalho `x-goog-api-key`.
- Modelo escolhido pela lista da chave (`models.list`): **`gemini-3.8-flash`**, o Flash estável mais recente com nome fixo.

## RED
`pytest tests/unit/test_gemini_retry.py` → `ImportError: cannot import name 'MAX_PROMPT_CONTENT_CHARS'` (RED de compilação: a implementação não existia).

## GREEN
`pytest tests/unit/test_gemini_retry.py` → 21 passed. Suíte final (backend com integração em PostgreSQL 17 local) → **466 passed**.

| # | O que é garantido | Teste |
|---|---|---|
| 1 | 503 e 504 seguidos de sucesso concluem a checagem | `test_503_and_504_then_success_completes_the_check`, `test_transient_google_errors_then_success` |
| 2 | Novas tentativas não reservam outra unidade da cota diária | `test_retry_does_not_reserve_new_daily_quota` |
| 3 | 400/401/403/404 não são repetidos | `test_client_errors_are_not_retried`, `test_bad_request_is_not_retried` |
| 4 | Texto enviado ao Gemini cortado em 8.000 caracteres | `test_text_sent_to_gemini_is_truncated_to_about_8k` |
| 5 | Só 429/500/503/504 e falha de rede são repetidos, no máximo 2 novas tentativas | `test_retryable_statuses`, `test_network_error_is_retried`, `test_gives_up_after_two_new_attempts` |
| 6 | Backoff exponencial + jitter; teto por tentativa; orçamento total | `test_exponential_backoff_with_jitter`, `test_attempt_that_hangs_is_cut_and_retried`, `test_total_budget_stops_retries`, `test_retries_respect_the_analyzer_deadline` |
| 7 | Pior caso cabe no Cloud Run (180 s) e no prazo do claim | `test_worst_case_fits_cloud_run_timeout` |
| 8 | Modelo fixo (sem latest/preview/exp) e thinking desligado | `test_default_model_is_fixed_stable_flash_with_thinking_off`, `test_generate_config_disables_thinking_and_uses_json_schema` |
| 9 | Log com status, mensagem do Google, tentativa e tempo, sem a chave | `test_error_log_has_google_message_and_timing`, `test_google_message_never_carries_the_key`, `test_key_never_reaches_the_log` |
| 10 | 429 de cota diária: sem nova tentativa e mensagem "esgotada hoje" | `test_daily_quota_429_is_not_retried`, `test_google_daily_quota_shows_come_back_tomorrow` |
| 11 | Resposta sem texto (SAFETY/MAX_TOKENS) não é repetida | `test_empty_response_is_not_retried` |
| 12 | Segredos fora do `repr`/`str` de `Settings` | `test_settings_repr_hides_secrets` |

## Verificação com a chave real (`gemini-3.8-flash`, `thinking_budget=0`)
- **Chamadas diretas:** 6,8 s, 12,3 s e 12,6 s em sucesso; também houve 503 "high demand", 504 aos 58 s e erros de rede.
- **Caminho completo:** 13,7 s; 18,3 s (dois 503 e depois sucesso); 4,8 s; 30,0 s (504 e depois sucesso). Uma checagem esgotou os 60 s (504 e 503 seguidos).
- **Cota:** depois disso, 429 de cota diária gratuita do modelo esgotada.

## Lacunas conhecidas
- Sem medição de cobertura: `pytest-cov` não está nas dependências.
- Sem checagem estática de tipos no Python: pyright/mypy não estão instalados.
- **Decisão do usuário:** novas tentativas não contam na cota diária (`GEMINI_DAILY_CHECKS`). Em sequências de erro, o Google pode receber até 3 chamadas por unidade.
