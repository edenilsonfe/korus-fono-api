# Korus Fono API

Backend FastAPI para o Korus Fono — sistema operacional para terapias infantis.

## Requisitos

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Docker (PostgreSQL, Redis, MinIO)

## Setup

```bash
cp .env.example .env

# Subir infra + migrations + API (profile "app" é obrigatório para a API)
docker compose --profile app up -d

# Seed demo (opcional, primeira vez)
docker compose --profile tools run --rm seed
```

A API fica em **http://localhost:8000** — confira com `curl http://localhost:8000/health`.

> **Importante:** o serviço `api` usa o profile `app`. O flag `--profile` vem **antes** do subcomando:
> `docker compose --profile app up -d` (correto) — **não** `docker compose up -d --profile app`.

### Comandos Docker do dia a dia

```bash
docker compose --profile app up -d          # subir tudo (postgres, redis, minio, migrate, api)
docker compose --profile app down           # parar e remover containers
docker compose --profile app down --remove-orphans   # se der erro de rede órfã
docker compose --profile app ps             # status dos containers
docker compose --profile app logs -f api    # logs da API em tempo real
docker compose --profile tools run --rm seed   # recarregar dados demo
```

Se aparecer `network ... not found`, recrie a stack:

```bash
docker rm -f korus-one-api-api-1 2>/dev/null
docker compose --profile app down --remove-orphans
docker compose --profile app up -d
```

Portas expostas: **API** `8000`, **Postgres** `5433`, **Redis** `6380`, **MinIO** `9000`/`9001`.

Para rodar a API direto no host (requer Postgres acessível em localhost:5433):

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

> **Nota Windows:** conexões do host ao Postgres Docker podem exigir `scram-sha-256`. Use o serviço `api` via `docker compose --profile app up -d` ou `docker compose run --rm migrate` para bootstrap.

## Env de e-mail (Resend)

Para o fluxo de recuperação de senha (`/auth/forgot-password` → `/auth/reset-password`), configure no `.env`:

- `RESEND_API_KEY`: chave da conta Resend.
- `EMAIL_FROM`: remetente aprovado no Resend.
- `EMAIL_SENDING_ENABLED=true`: habilita envio real (em dev pode ficar `false`).
- `TRIAL_EMAIL_RESEND_COOLDOWN_HOURS`: intervalo mínimo entre campanhas da mesma régua para o mesmo usuário (padrão: 24h).
- `FRONTEND_URL`: base usada no link `.../reset-password?token=...`.
- `PASSWORD_TOKEN_EXPIRE_MINUTES`: validade do token de reset.
- `PASSWORD_RESET_COOLDOWN_SECONDS`: cooldown entre solicitações por usuário.

## Endpoints

- Health: `GET /health`
- API: `GET /api/v1/...`
- Docs: `GET /docs` (Swagger — debug local; contrato = `app/schemas/` + `app/api/v1/`)

### Checkout da assinatura

O cadastro pago usa `POST /api/v1/auth/register-checkout`. Essas contas mantêm
`signup_payment_required=true`: `/me`, auth e billing continuam acessíveis para recuperação, mas
dashboard e rotas do produto respondem `403` até um evento `PAYMENT_SUCCEEDED` confirmar a
cobrança, salvo uma liberação temporária explícita do administrador. `SUBSCRIPTION_CREATED`
e checkout apenas criado não liberam acesso. A sessão local
persistida é devolvida por `GET /api/v1/billing/me` para o frontend retomar o mesmo pagamento.
Nessa variante, o link de verificação de e-mail só é criado e enviado após o pagamento ser
confirmado, ou pelo login/reenvio durante uma liberação temporária vigente.

### Liberação temporária pelo administrador

Em `/admin/contas/:id`, administradores com `billing:write` podem conceder de 1 a 7 dias
(padrão: 2 dias de 24 horas) com motivo de 5 a 500 caracteres. A API expõe
`POST /api/v1/admin/professionals/:id/temporary-access` (`days`, `reason`) e
`POST .../temporary-access/revoke` (`reason`). Concessão e revogação registram ator, conta,
motivo e prazo na auditoria. Uma concessão já vigente retorna `409`, sem prorrogar o prazo;
revogar novamente é inócuo. Contas desativadas, staff ou com acesso regular vigente não recebem
uma nova concessão.

`Professional.temporary_access_ends_at` é independente do trial e dos registros financeiros.
A liberação permite superar o bloqueio de acesso por pagamento inicial, mas mantém
`signup_payment_required=true`, e-mail obrigatório e a cobrança original. `/me` e `/billing/me`
expõem `temporaryAccessEndsAt`; `signupPaymentRequired` continua sendo a pendência real.
O cliente liberado entra novamente para solicitar o e-mail de verificação. Cada requisição
revalida o vencimento, sem cron: ao expirar ou revogar, voltam as condições normais da conta.
Um `PAYMENT_SUCCEEDED` encerra a exceção e ativa o acesso pago pelo fluxo existente.
Aplicar a migration `y6z7a8b9c0d1` antes de publicar API e frontend. Nenhuma conta existente
é convertida automaticamente para uma liberação temporária.

### Pagamento no checkout

O cartão é processado no formulário do KorusFono: o frontend envia PAN/CVV por HTTPS ao endpoint
autenticado de billing, que os repassa imediatamente ao Asaas sem persistir ou registrar esses
campos. Essa arquitetura exige operação em conformidade com PCI DSS SAQ-D; não habilite o Asaas em
produção antes de concluir os controles e a validação de compliance aplicáveis.

O PIX também permanece no formulário do KorusFono. No plano anual, a API cria uma cobrança avulsa
à vista no Asaas e devolve QR Code e copia e cola pela sessão local; no mensal, usa a primeira
cobrança da assinatura recorrente. Em ambos os casos, somente a confirmação do provedor libera o
acesso. Enquanto essa primeira cobrança mensal não for paga, a recorrência fica `INACTIVE` no
Asaas: a confirmação do primeiro pagamento a reativa com o próximo vencimento para um mês depois,
e a exclusão da primeira cobrança de uma assinatura nunca paga cancela a recorrência. Toda nova
cobrança sai explicitamente como `PIX` ou `CREDIT_CARD`; o tipo genérico `UNDEFINED` não é usado.

### Financeiro interno da clínica

O domínio `/api/v1/finance/*` controla contas a receber/pagar, baixas parciais,
pacotes, fluxo de caixa e comprovantes internos por profissional. Ele é separado
de `/api/v1/billing/*`, que continua exclusivo para trial e assinatura do SaaS.

- Não há PIX, cartão ou boleto do paciente pela plataforma.
- Valores monetários são inteiros em centavos.
- Toda conta começa com Cartão de crédito, Cartão de débito, Pix e Dinheiro como formas de pagamento.
- Categorias iniciais de receita e despesa são criadas automaticamente e continuam editáveis pelo profissional.
- Cancelamentos e estornos preservam a trilha; registros pagos não são apagados.
- `POST /appointments/{id}/complete` cria a sessão uma única vez e exige a decisão
  explícita entre cobrança individual, consumo de pacote ou cortesia.
- Ao selecionar um serviço financeiro no agendamento, a agenda guarda nome e preço
  em centavos como snapshot; reajustes posteriores não alteram a cobrança daquele atendimento.
- O PDF é um comprovante de controle interno e não substitui Receita Saúde ou NFS-e.

## Migrations (Alembic)

```bash
# Aplicar migrations (também roda automaticamente no docker compose --profile app up)
docker compose run --rm migrate

# Seed demo (primeira vez ou reset manual)
docker compose --profile tools run --rm seed

# Gerar nova migration após alterar modelos em app/models/
docker compose run --rm migrate sh -c "uv sync && uv run alembic revision --autogenerate -m 'descricao da mudanca'"

# Comandos locais (Postgres em localhost:5433)
uv sync
uv run alembic upgrade head          # aplicar
uv run alembic revision --autogenerate -m "descricao"  # gerar
uv run alembic current               # versão atual
uv run alembic history               # histórico
```

> **Importante:** revise sempre o arquivo gerado em `alembic/versions/` antes de commitar — o autogenerate pode omitir renomeações ou detectar falsos positivos.

### Importação de clínica anterior

Backups SQL do formato legado suportado devem passar pelo importador em
`dry-run`; nunca execute o SQL diretamente no PostgreSQL. O procedimento,
mapeamento de estados, trava de WhatsApp e aplicação idempotente com manifesto
local — sem migration no banco de destino — estão em
[`docs/legacy-clinic-import.md`](docs/legacy-clinic-import.md).

### Boas-vindas por WhatsApp

O cadastro grava a mensagem de boas-vindas em uma fila durável. A instância
central envia no máximo 5 tentativas em uma janela de 15 minutos
(`WHATSAPP_WELCOME_MAX_MESSAGES_PER_WINDOW=5` e
`WHATSAPP_WELCOME_RATE_LIMIT_WINDOW_SECONDS=900`) e o scheduler recupera no
máximo 5 itens por ciclo, evitando rajadas depois de reconectar/sincronizar a Evolution.
Se uma tentativa chegou ao provedor ou ficou com resultado ambíguo, ela não é
reenviada automaticamente; registros que aguardavam uma conexão ativa continuam
elegíveis sem consumir tentativa.

## Aniversariantes do dia

O dashboard exibe os aniversários pela data corrente em `CLINIC_TIMEZONE`, com nome,
idade e acesso ao paciente. Cadastros de demonstração e pacientes inativos são excluídos.
O card permanece visível quando não há aniversariantes.

- **Lembrete no KorusFono:** ativado em Configurações → Notificações, via
  `GET/PATCH /api/v1/notifications/settings` (`birthdayInAppEnabled`). A consulta
  do sino gera um único resumo por profissional e dia, válido até a meia-noite
  da clínica, com estado visto/lido persistido e link para o dashboard.
- **Parabéns por WhatsApp:** ativado separadamente em WhatsApp → Mensagens,
  evento `patientBirthday` (`patient_birthday` nos templates e logs). Requer
  canal habilitado, conexão ativa e consentimento do responsável principal.
  O template é editável. O worker ARQ verifica a cada 15 minutos e envia entre
  9h e 18h no fuso da clínica, no máximo uma entrega por paciente/dia.
- As duas opções começam desativadas. O dashboard independe delas. Nascidos em
  29/02 são considerados somente em 29/02. Não há envio de aniversários atrasados.
- A fila persiste antes do envio e revalida cadastro, preferência e consentimento.
  Uma entrega incerta não é repetida. O novo evento não grava texto personalizado
  ou resposta bruta do provedor no histórico.

Aplicar a migration `v3w4x5y6z7a8` antes de subir a nova API/worker. O envio
automático por WhatsApp exige o worker em execução; o lembrete no sino é gerado
ao consultar a caixa de notificações e não exige o worker.

## Worker IA (opcional)

```bash
uv run arq worker.WorkerSettings
```

Configure `OPENCODE_API_KEY` no `.env` (chave em [opencode.ai/auth](https://opencode.ai/auth)). Modelos disponíveis: [OpenCode Zen](https://opencode.ai/docs/zen/).

O mesmo worker recupera a fila persistida do Google Agenda. Configuração completa
do OAuth, callback e variáveis: [`docs/google-calendar-setup.md`](docs/google-calendar-setup.md).

Transcrição de áudio usa um provedor separado compatível com `/v1/audio/transcriptions`:

- `AUDIO_TRANSCRIPTION_API_KEY`
- `AUDIO_TRANSCRIPTION_BASE_URL` (default `https://api.openai.com/v1`)
- `AUDIO_TRANSCRIPTION_MODEL` (default `gpt-4o-mini-transcribe`)

Sem essa configuração, `GET /api/v1/ai/capabilities` informa que áudio está indisponível e as telas não geram conteúdo simulado.

## Testes

Importação de exportações clínicas CSV: veja o fluxo de prévia, backup e aplicação
em [`docs/legacy-csv-import.md`](docs/legacy-csv-import.md).

```bash
uv run pytest
```

## Deploy Railway

Spec: `docs/superpowers/specs/2026-07-15-railway-deploy-design.md`.

Checklist no painel (projeto com Postgres + Redis + serviços `api` e `worker`):

1. Conectar o repo / deploy via `railway up` (Dockerfile + `railway.toml`).
2. Serviço **api**: start e release já no `railway.toml` (`alembic upgrade head` + uvicorn).
3. Serviço **worker**: mesma imagem; start command `arq worker.WorkerSettings`.
4. Variáveis: ver bloco Railway em `.env.example` (obrigatórias: `DEBUG=false`, `JWT_SECRET`, `DATABASE_URL`, `REDIS_URL`, `CORS_ORIGINS`, `FRONTEND_URL`, S3 AWS com `S3_ENDPOINT` vazio/omitido, Evolution se `WHATSAPP_PROVIDER=evolution`; `POSTHOG_PROJECT_TOKEN` é opcional e habilita `purchase` autoritativo no webhook).
5. Após a URL pública da API: setar `APP_PUBLIC_URL` e no Cloudflare Worker `API_ORIGIN` (origin sem path).

Migrations: o `CMD` do Dockerfile roda `alembic upgrade head` antes do uvicorn (o `releaseCommand` do `railway.toml` pode não executar em todos os deploys CLI — o start garante o schema).

Worker: `railway.worker.toml` (start `arq worker.WorkerSettings`); para redeploy do worker, troque temporariamente por `railway.toml` ou configure o start no painel.
```bash
docker build -t korus-one-api .
# CLI: npm i -g @railway/cli && railway login && railway link && railway up
```

## Credenciais demo

- Email: `admin@admin.com`
- Senha: `admin123`
