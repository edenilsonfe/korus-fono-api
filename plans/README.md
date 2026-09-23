# Implementation Plans (API mirror)

Canonical plan bodies live in the sibling repo:

`C:/Users/ed/Documents/projetos/korus-one-web/plans/`

## Planos de produto recentes

| Plano canônico no web | Escopo API + web | Status |
| --- | --- | --- |
| [Jornada clínica — 22/09/2026](../../korus-one-web/plans/2026-09-22-jornada-clinica-revisao-entrada-alta.md) | Itens 1, 2, 4 e 5: revisão, pré-atendimento, alta e retorno funcional da família | DONE — validado localmente, sem deploy |

## Wave 2 (security) — status

| Plan | Title | Status |
|------|-------|--------|
| 018 | Asaas webhook `compare_digest` | DONE |
| 019 | Escapar HTML no e-mail de reset | DONE |
| 020 | Desligar OpenAPI docs fora de debug | DONE |
| 021 | IP confiável para rate-limit de auth (XFF) | DONE |
| 022 | Evidência de bateria: upload chunked + cap | DONE |
| 025 | DOCX exige estrutura OOXML | DONE |
| 026 | Omitir JWT do JSON de auth (API half) | DONE |
| 027 | Bloquear `DEBUG`/billing stub em produção | DONE |
| 028 | Magic sniff em evidências e recursos | DONE |
| 029 | Presigned URL TTL + Content-Disposition (API half) | DONE |
| 031 | Spike+impl fatura Asaas cartão (API half) | DONE |

Web-only: **023 DONE**, **024 DONE**, **030 TODO**.

Update both indexes when a plan finishes.
