# Deploy Railway (API + worker + Postgres + Redis + S3 AWS) — Design

**Data:** 2026-07-15  
**Status:** aprovado (brainstorm)  
**Repos:** `korus-one-api`  
**Relacionado:** web Cloudflare — `korus-one-web/docs/superpowers/specs/2026-07-15-cloudflare-deploy-design.md` (`API_ORIGIN`)

## Problema

A API roda local via Docker Compose (Postgres, Redis, MinIO, uvicorn `--reload`). Não há Dockerfile de produção, config Railway, nem documentação das env vars para:

- Postgres/Redis gerenciados
- Storage em **AWS S3** (não MinIO)
- Worker ARQ separado
- Ligação com o frontend no Cloudflare (CORS / `FRONTEND_URL` / `API_ORIGIN`)

## Objetivo

Em um ciclo, deixar o backend publicável no Railway com:

1. Dockerfile + `.dockerignore` + `railway.toml`
2. Serviços: API, worker, Postgres, Redis
3. Release: `alembic upgrade head`
4. S3 AWS (`S3_ENDPOINT` vazio)
5. Normalização de `DATABASE_URL` para asyncpg
6. `.env.example` + README/AGENTS com checklist completo de variáveis

## Decisões

| Tema | Decisão |
|------|--------|
| Escopo | Stack completa: API + worker + Postgres + Redis; storage AWS S3 |
| Build | Dockerfile (não Nixpacks) |
| Migrations | Release command Railway: `alembic upgrade head` |
| CI GitHub → Railway | Fora deste ciclo |
| Storage | AWS S3; não subir MinIO no Railway |
| Domínio customizado | Fora; `*.up.railway.app` basta no início |

## Arquitetura

```
Cloudflare Worker (web) ──/api/*──► Railway API (uvicorn)
                                       │
                                       ├─ Postgres (plugin)
                                       ├─ Redis (plugin)
                                       ├─ Worker ARQ (mesmo Dockerfile)
                                       └─ AWS S3
```

| Serviço | Start | Release |
|---------|-------|---------|
| `api` | `alembic upgrade head && uvicorn … --port $PORT` (CMD Docker; releaseCommand também) | `alembic upgrade head` |
| `worker` | `arq worker.WorkerSettings` | — |
| Postgres / Redis | plugins Railway | — |

## Peças no repo

| Peça | Papel |
|------|--------|
| `Dockerfile` | Python 3.11 + uv; imagem sem `--reload` |
| `.dockerignore` | excluir `.venv`, `.env`, caches, etc. |
| `railway.toml` | dockerfile builder; start API; release migrations |
| `app/core/config.py` | `postgresql://` → `postgresql+asyncpg://`; default `s3_endpoint` vazio ok |
| `app/services/storage.py` | omitir `endpoint_url` quando `S3_ENDPOINT` vazio |
| `.env.example` | vars de prod documentadas |
| `README.md` / `AGENTS.md` | checklist Railway + ponte Cloudflare |

## Variáveis de ambiente

Aplicam a `api` e `worker`, exceto onde indicado.

### Core / HTTP

| Var | Obrigatória | Nota |
|-----|-------------|------|
| `DEBUG` | sim | `false` em produção |
| `PORT` | Railway | só API; uvicorn `--port $PORT` |
| `API_V1_PREFIX` | não | default `/api/v1` |
| `CORS_ORIGINS` | sim | origem do Worker Cloudflare (CSV) |
| `FRONTEND_URL` | sim | URL do web (reset senha, billing) |
| `APP_PUBLIC_URL` | se Evolution | URL pública da API Railway |
| `JWT_SECRET` | sim | ≥32 chars; boot falha se fraco com `DEBUG=false` |
| `JWT_ALGORITHM` | não | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | não | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | não | |

### Dados

| Var | Obrigatória | Nota |
|-----|-------------|------|
| `DATABASE_URL` | sim | ref. Postgres; normalizar para `+asyncpg` |
| `REDIS_URL` | sim | ref. Redis |

### AWS S3

| Var | Obrigatória | Nota |
|-----|-------------|------|
| `S3_ENDPOINT` | não | **vazio** na AWS (não MinIO) |
| `S3_ACCESS_KEY` | sim | AWS access key id |
| `S3_SECRET_KEY` | sim | AWS secret access key |
| `S3_BUCKET` | sim | bucket pré-criado ou com permissão CreateBucket |
| `S3_REGION` | sim | ex. `us-east-1` |
| `MAX_UPLOAD_BYTES` | não | default 26214400 |

### E-mail / IA / Billing / Observabilidade

| Var | Obrigatória | Nota |
|-----|-------------|------|
| `RESEND_API_KEY` | p/ e-mail | |
| `EMAIL_FROM` | p/ e-mail | |
| `EMAIL_SENDING_ENABLED` | não | `true` para envio real |
| `PASSWORD_TOKEN_EXPIRE_MINUTES` | não | |
| `PASSWORD_RESET_COOLDOWN_SECONDS` | não | |
| `OPENCODE_API_KEY` | produção | API + worker; boot fail-fast quando ausente em produção |
| `OPENCODE_BASE_URL` | não | |
| `OPENCODE_MODEL` | não | |
| `AUDIO_TRANSCRIPTION_API_KEY` | p/ áudio | sem ela as telas de áudio ficam desabilitadas |
| `AUDIO_TRANSCRIPTION_BASE_URL` / `AUDIO_TRANSCRIPTION_MODEL` | não | provider `/v1/audio/transcriptions` |
| `BILLING_PROVIDER` | não | `stub` até Asaas |
| `ASAAS_API_KEY` / `ASAAS_API_BASE_URL` / `ASAAS_WEBHOOK_TOKEN` | se Asaas | |
| `TRIAL_DAYS` | não | |
| `SENTRY_DSN` / `SENTRY_ENVIRONMENT` / `SENTRY_RELEASE` | opcional | |

### WhatsApp (Evolution)

Com `DEBUG=false` e `WHATSAPP_PROVIDER=evolution`, o boot exige:

| Var | Nota |
|-----|------|
| `WHATSAPP_PROVIDER` | default `evolution` |
| `EVOLUTION_API_BASE_URL` | |
| `EVOLUTION_GLOBAL_API_KEY` | obrigatória |
| `EVOLUTION_WEBHOOK_SECRET` | obrigatória |
| `WHATSAPP_CREDENTIAL_ENCRYPTION_KEY` | obrigatória |
| `APP_PUBLIC_URL` | obrigatória |
| `WHATSAPP_USE_ARQ_DISPATCH` | `true` se worker up |
| `CLINIC_TIMEZONE` | default `America/Sao_Paulo` |

### Ponte Cloudflare (web)

Após a API ter URL pública:

- Cloudflare Worker var `API_ORIGIN` = origin Railway **sem** path (ex. `https://korus-one-api.up.railway.app`)
- `CORS_ORIGINS` / `FRONTEND_URL` = origem do web

## Fluxos

1. Build imagem → release migrations → start API
2. Worker sobe com a mesma imagem e `REDIS_URL` / `DATABASE_URL`
3. Storage: client S3 sem `endpoint_url` se endpoint vazio; `ensure_bucket` permanece (IAM CreateBucket ou bucket pré-criado)

## Verificação

1. Testes: normalização `DATABASE_URL`; storage omite endpoint quando vazio
2. Smoke: `docker build -t korus-one-api .`

## Fora de escopo

- GitHub Actions deploy Railway
- Domínio customizado / DNS
- Provisionar bucket/IAM na AWS via código
- Seed demo em produção
- Mudanças no `korus-one-web` além da documentação de `API_ORIGIN`

## Critérios de sucesso

- `/health` 200 na API Railway
- Migrations na release sem erro
- Worker conecta ao Redis
- Upload/presign com S3 AWS e `S3_ENDPOINT` vazio
- `.env.example` cobre as vars de produção listadas acima
