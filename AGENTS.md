---
description: 
alwaysApply: false
---

# AGENTS.md — Korus Fono API

Instruções para agentes de IA que trabalham neste repositório. Leia este arquivo e o `README.md` antes de implementar mudanças. O contrato com o frontend vive aqui — o web espelha schemas e rotas **manualmente**.

## Visão geral

| Item | Valor |
| ---- | ----- |
| Produto | **Korus Fono** — Sistema Operacional para Terapias Infantis |
| Especialidade inicial | Fonoaudiologia (linguagem, TEA, desenvolvimento infantil) |
| Repositório backend | `korus-one-api` (este repo) |
| Repositório frontend | `korus-one-web` (TanStack Start / React) |
| Papel deste repo | **Autoridade do contrato** — schemas Pydantic + rotas FastAPI |
| Prefixo HTTP | `/api/v1` (ver `settings.api_v1_prefix`) |

Credenciais demo (seed): `admin@admin.com` / `admin123`.

---

## Stack

| Camada | Tecnologia |
| ------ | ---------- |
| Runtime | Python 3.11+, [uv](https://docs.astral.sh/uv/) |
| API | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2 (async) + asyncpg |
| Migrations | Alembic |
| Validação / DTOs | Pydantic v2 (`CamelModel`) |
| Auth | JWT (access/refresh) + Argon2 |
| Filas / jobs IA | Redis + ARQ (`worker.py`) |
| Storage | MinIO / S3 (`aioboto3`) |
| Infra local | Docker Compose (Postgres `5433`, Redis `6380`, MinIO `9000`/`9001`, API `8000`) |
| Testes | pytest + pytest-asyncio (`tests/`) |

---

## Estrutura de pastas

```
app/
├── main.py                 # FastAPI app, CORS, EntitlementMiddleware, /health
├── api/v1/                 # Routers HTTP (um domínio por arquivo)
│   └── router.py           # Agrega todos os routers
├── schemas/                # DTOs Pydantic — fonte da verdade do JSON (camelCase)
├── models/                 # SQLAlchemy ORM
├── services/               # Regras de negócio (scoring, billing, WhatsApp, IA…)
├── core/                   # config, deps (auth), security, catálogos
├── db/                     # engine / session async
├── middleware/             # entitlement (trial/plano)
├── data/                   # Pacotes de instrumentos {slug}/ (+ instrument_samples/)
├── seeds/                  # demo.py, protocols.py
└── utils/
alembic/versions/           # Migrations — revisar autogenerate antes de commit
tests/                      # pytest
scripts/                    # Build de pacotes (ABLLS-R, ADL2, AMIOFE…)
worker.py                   # ARQ worker (jobs IA)
```

**Não criar:** routers fora de `app/api/v1/`, schemas snake_case no JSON de resposta, codegen OpenAPI → TypeScript no web.

---

## Contrato com o frontend (manual)

Não há codegen. O web copia tipos/chamadas em `korus-one-web/src/lib/api/`.

### Fonte da verdade (ordem)

1. **`app/schemas/`** — DTOs (`CamelModel`); JSON **camelCase**
2. **`app/api/v1/`** — rotas, métodos, query params, status codes
3. Documentação humana no web: `CONTEXT.md`, `PAGES.md` (repo `korus-one-web`)
4. Espelho TS: `korus-one-web/src/lib/api/types.ts` + `services/`

### Workflow para agentes

1. Alterar **modelo** (se precisar) → migration Alembic → revisar arquivo gerado
2. Alterar / criar **schema** em `app/schemas/`
3. Implementar lógica em **`app/services/`** (router fino)
4. Expor em **`app/api/v1/<domínio>.py`** e registrar em `router.py` se for router novo
5. Teste em `tests/test_*.py`
6. Avisar / espelhar no web: tipos + service + `PAGES.md` / `CONTEXT.md` quando o contrato mudar

`/docs` e `/redoc` são só debug local — **não** são a fonte do contrato.

---

## Convenções de código

### Python / FastAPI

- Routers: `APIRouter`, deps em `app/core/deps.py` (`get_current_professional`, `get_patient_for_professional`, `require_staff`)
- Schemas herdam `CamelModel` (`app/schemas/common.py`) — `alias_generator` camelCase, `populate_by_name=True`
- Modelos ORM em **snake_case**; serialização para API via schema
- Services async; sessão `AsyncSession` via `Depends(get_db)`
- Erros: `HTTPException` com `detail` em português quando voltado ao usuário
- IDs: UUID

### Idioma

- Mensagens de API / erros voltados ao usuário: **pt-BR**
- Identificadores de código: inglês (`Patient`, `Assessment`, `scoring_mode`)

### Auth e multi-tenant

- Quase tudo exige Bearer access token → `Professional`
- Pacientes são escopados por `professional_id` (`get_patient_for_professional`)
- Admin plataforma: `require_staff` (`is_staff`)
- Público (sem auth): ex. webhooks, trechos de auth (login/register/forgot)

### Entitlement

- `EntitlementMiddleware` bloqueia mutações quando trial/plano não permite escrita
- Serviços de billing/planos em `app/services/entitlement_service.py`, `saas_billing_service.py`, etc.

---

## Mapa de módulos (API)

| Domínio | Router | Notas |
| ------- | ------ | ----- |
| Auth | `auth.py` | login, register, refresh, forgot/reset password |
| Me | `me.py` | profissional logado |
| Pacientes | `patients.py` | CRUD, caregivers, filtros `q` |
| Agenda | `appointments.py` | `from`/`to`; séries recorrentes |
| Sessões | `sessions.py` | global + por paciente |
| Prontuário | `prontuario.py` | evoluções, anamnese, anexos |
| Timeline | `timeline.py` | global + por paciente |
| Clínico | `clinical.py` | protocols, assessments, goals, analytics |
| Instrumentos | `instruments.py` | manifesto + score (`/instruments/:id/score`) |
| Baterias | `batteries.py` | ABFW, PROC, PARD, ADL Linguagem, etc. |
| Dashboard | `dashboard.py` | KPIs operacionais |
| Catálogo | `catalog.py` | diagnósticos / especialidades |
| IA | `ai.py` | jobs assíncronos + assistente |
| WhatsApp | `whatsapp.py` | Evolution / Meta |
| Webhooks | `webhooks.py` | Asaas, WhatsApp, etc. |
| Billing | `billing.py` | planos, checkout, assinatura |
| Notificações | `notifications.py` | in-app |
| Admin | `admin_*.py` | contas, protocolos, flags, billing, anúncios |

---

## Protocolos e scoring

Pacotes de conteúdo em `app/data/{slug}/` (prioridade sobre `instrument_samples/`). Seed do catálogo: `app/seeds/protocols.py`.

| Modo (conceito) | Onde pontua | Serviços / rotas típicas |
| --------------- | ----------- | ------------------------ |
| Manifest | API | `instrument_scoring_service`, `instruments.py` — ABLLS-R (`domain_mastery`), VB-MAPP, FOIS, ADL… |
| Battery | API | `battery_scoring_service`, `battery_service`, `batteries.py` — ABFW, PROC, Denver, ADL Linguagem (`adl2`)… |
| Client-side | Frontend | API só persiste `answers`/`scores` via assessments |

Removidos do produto (hard purge): SPM, TVIP, SDQ, TLI, EAT-10, MASA — IDs discontinued para histórico; migration `u0v1w2x3y4z5` dropa tabelas SPM.

Rebuild de pacotes (exemplos):

```bash
py -3.13 scripts/build_ablls_r_package.py
py -3.13 scripts/build_adl2_package.py
```

Normas / fidelidade: ver docs no web (`docs/normas-br-checklist.md`, `docs/protocolos-fidelidade-testes.md`) e `app/services/norms_status.py`.

---

## Diretrizes para agentes

1. **Router fino, service gordo** — validação HTTP no router; regra de negócio no service
2. **Novo endpoint** → schema + service + router + teste; depois espelho no `korus-one-web`
3. **Novo campo no JSON** → schema Pydantic primeiro; nome Python snake_case, wire camelCase
4. **Migration** → `alembic revision --autogenerate` e **revisar** o diff (renomeações falham fácil)
5. **Não** inventar campos “só para o front” sem schema; não quebrar camelCase
6. Preferir reutilizar deps (`get_patient_for_professional`) a filtrar `professional_id` na mão
7. Testes: um arquivo focado por domínio (`tests/test_<domínio>.py`); async ok (`asyncio_mode = auto`)

### Pendências / cuidado

- Object storage / recursos (biblioteca de materiais) ainda incompleto no produto
- Billing Asaas + webhooks: não alterar reconcilição sem ler serviços existentes
- WhatsApp: providers Evolution/Meta — respeitar abstração em `whatsapp_provider.py`

---

## Comandos

```bash
# Infra + API (profile app é obrigatório; --profile ANTES do subcomando)
docker compose --profile app up -d
docker compose --profile app logs -f api
docker compose --profile tools run --rm seed

# Host local (Postgres em localhost:5433)
uv sync
uv run uvicorn app.main:app --reload --port 8000
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "descricao"
uv run python -m app.seeds.demo
uv run pytest
uv run arq worker.WorkerSettings   # jobs IA (opcional)
```

Health: `GET http://localhost:8000/health`  
API: `http://localhost:8000/api/v1/...`

### Deploy Railway

- Spec: `docs/superpowers/specs/2026-07-15-railway-deploy-design.md`
- `Dockerfile` + `railway.toml` (release: `alembic upgrade head`)
- Plugins: Postgres + Redis; storage AWS S3 (`S3_ENDPOINT` vazio)
- Worker: mesmo image, start `arq worker.WorkerSettings`
- Vars: bloco Railway em `.env.example`; ponte web `API_ORIGIN` no Cloudflare

---

## O que evitar

- Commitar migration autogenerate sem revisão
- Respostas JSON em snake_case (quebrar o web)
- Lógica de scoring pesada dentro do router
- Endpoints admin sem `require_staff`
- Acesso a paciente de outro profissional
- Adicionar codegen OpenAPI → TS no web (contrato manual de propósito)
- Force-push em branches compartilhadas

---

## Documentos relacionados

| Arquivo | Conteúdo |
| ------- | -------- |
| `README.md` | Setup Docker, env, migrations, credenciais |
| `docs/assistente-ia-design.md` | Design do assistente clínico |
| `docs/notificacoes-in-app-design.md` | Notificações in-app |
| `korus-one-web/CONTEXT.md` | Domínio e contratos REST (visão produto) |
| `korus-one-web/PAGES.md` | Tela → endpoints |
| `korus-one-web/AGENTS.md` | Convenções do frontend |

### Ordem de leitura sugerida

1. Este arquivo — mapa e convenções da API  
2. `README.md` — subir o ambiente  
3. `app/schemas/common.py` + um schema do domínio que for tocar  
4. Router + service correspondentes  
5. No web: `CONTEXT.md` / `PAGES.md` se a mudança afetar UI  

Ao alterar rota, schema ou comportamento observável, **atualize o espelho no `korus-one-web`** (types/services) e a doc de telas quando couber.
