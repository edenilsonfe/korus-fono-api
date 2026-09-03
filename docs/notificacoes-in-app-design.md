# Korus Fono — Notificações in-app (anúncios broadcast)

**Data:** 8 de julho de 2026
**Repositórios:** `korus-one-api` (FastAPI / Python) · `korus-one-web` (TanStack Start + Vite)
**Status:** Design aprovado via grill — pronto para implementação
**Referência:** `afeto-clinic-manager` + `myclinic-back` (spec `2026-07-01-notificacoes-in-app-design.md`), adaptada ao stack e ao modelo de domínio do Korus Fono.

---

## Visão geral

### Extensão de aniversários (setembro de 2026)

A caixa agora inclui o tipo pessoal `birthday`: um resumo diário por profissional,
com link para `/dashboard`, gerado na consulta do sino/lista quando a preferência
`birthdayInAppEnabled` está ativa. `GET/PATCH /notifications/settings` persiste
essa escolha (desativada por padrão); ela é independente do evento WhatsApp
`patientBirthday`. A chave UUID determinística impede duplicação concorrente,
e visto/lido continua em `app_notification_reads`. A notificação expira à meia-noite
de `CLINIC_TIMEZONE`, e desativar a preferência a oculta imediatamente.
`filter=broadcast` continua exclusivo de anúncios; `all` inclui lembretes pessoais
apenas do destinatário. O resumo não inclui nomes ou informações clínicas.
As seções v1 abaixo registram o desenho original de anúncios.

### Problema

O Korus Fono não possui um canal in-app para comunicar aos profissionais **novidades de produto** (funcionalidades novas, tutoriais) nem **avisos operacionais da plataforma** (manutenção programada, mudanças de política). Hoje o único feedback in-app são os **toasts** (Sonner + shadcn), efêmeros e por dispositivo — não servem como histórico consultável e cross-device.

### Solução

Uma **caixa de entrada de notificações in-app** de natureza **broadcast** (anúncios de produto da Korus para os profissionais), persistida no backend, com:

1. **Sino no Header** (`NotificationBell`) com badge de não-vistos → abre um **Popover** compacto com os últimos anúncios.
2. **Página dedicada** `/notificacoes` — histórico completo, agrupado por data, com filtro por `type` e "Marcar todas como lidas".
3. **Área admin** `/admin/anuncios` — autoria/gestão de anúncios, protegida por uma flag `is_staff` no `Professional`, com lista, editor e métricas.

Notificações **não** substituem toasts: toast continua sendo feedback de ação imediata; notificação é registro persistente e consultável.

### Decisão de escopo (v1)

**v1 = broadcast apenas.** O schema nasce com a coluna `kind` (`'broadcast'` | `'personal'`) e `recipient_professional_id` (nullable) **pronta para `personal` futuro sem migração**, mas nesta entrega só emitimos broadcasts. Justificativa: em Korus Fono o `Professional` é o **único** usuário da conta — notificar a si mesmo de ações que ele mesmo desencadeou (agenda, equipe) seria ruído. Os poucos gatilhos pessoais que valeriam a pena (WhatsApp desconectou, cobrança/trial) ficam para v1.1.

### Objetivos

| # | Objetivo | Critério de sucesso |
|---|----------|---------------------|
| G1 | Comunicar novidades de produto in-app | Staff Korus publica um anúncio que aparece para o público-alvo |
| G2 | Persistência por usuário e cross-device | Estado visto/lido consistente entre dispositivos (backend) |
| G3 | Entrega simples e evolutiva | Inline (painel admin) + polling na v1; contrato permite SSE/WebSocket depois sem mudar API nem frontend |
| G4 | Medir engajamento | Eventos GA4 + métricas por anúncio sem custo de fan-out |
| G5 | Conformidade LGPD | GA4 só carrega após consentimento opt-in (banner replicado do `afeto-clinic-manager`) |

### Non-goals (fora da v1)

- Notificações **pessoais/operacionais** (schema pronto, gatilhos em v1.1).
- Push em tempo real (WebSocket/SSE).
- Digest por e-mail.
- Anúncios por especialidade-específica-além-do-audience (audience já cobre `specialty_key`).
- Preferências/mute por tipo.
- Rich text / markdown no corpo (texto puro com quebras de linha).
- Registro de consentimento no backend (fica só em `localStorage` nesta entrega — limitação documentada).
- E2E com Playwright (testes backend + Vitest mínimo cobrem o crítico).

---

## Modelo de dados

Namespace **distinto** do `notification_*` existente (que é comunicação outbound clínica→paciente via WhatsApp). Prefixo novo: **`app_notification*`**.

Padrão do codebase: UUID nativo do Postgres (`UUID(as_uuid=True)` + `new_uuid()`), `TimestampMixin` de `app/db/base.py` (`created_at`/`updated_at`), SQLAlchemy 2.0 (`Mapped`/`mapped_column`).

### Tabela `app_notifications`

Uma linha por notificação. **Broadcasts não fazem fan-out**: uma única linha serve todo o público-alvo.

| Coluna | Tipo | Notas |
|--------|------|-------|
| `id` | `UUID` PK | `new_uuid()` |
| `kind` | `String(20)` | `'broadcast'` \| `'personal'` (v1 só broadcast) |
| `type` | `String(20)` | `'feature'` \| `'tutorial'` \| `'notice'` (broadcast) |
| `title` | `String(200)` | |
| `body` | `Text` | **Texto puro** com quebras de linha (sem markdown) |
| `deep_link` | `String(500)` nullable | Rota interna (ex.: `/relatorios`, `/whatsapp`) |
| `severity` | `String(20)` | `'info'` \| `'success'` \| `'warning'` \| `'critical'`, default `'info'` |
| `recipient_professional_id` | `UUID` FK `professionals.id` nullable | **Preparado para `personal`.** `NULL` em broadcast |
| `audience` | `String(500)` nullable | `'all'` ou lista de `specialty_key` separada por vírgula (`'fono,to'`). `NULL` em pessoal |
| `status` | `String(20)` nullable | `'draft'` \| `'scheduled'` \| `'published'` \| `'archived'`. `NULL` em pessoal |
| `publish_at` | `DateTime(tz)` nullable | Quando `scheduled`, momento futuro de publicação |
| `expires_at` | `DateTime(tz)` nullable | Após esse instante não é mais entregue |
| `created_by` | `UUID` FK `professionals.id` nullable | Autor (staff Korus) |
| `created_at` / `updated_at` | `DateTime(tz)` | via `TimestampMixin` |

**Índices:** `ix_app_notifications_kind`, `ix_app_notifications_status`, e composto `ix_app_notifications_delivery (kind, status, publish_at, expires_at)`.

### Tabela `app_notification_reads`

Materializa **apenas interações** (visto/lido) — nunca a entrega. Ausência de linha = não-visto e não-lido.

| Coluna | Tipo | Notas |
|--------|------|-------|
| `id` | `UUID` PK | |
| `notification_id` | `UUID` FK `app_notifications.id` (`ondelete=CASCADE`) | |
| `professional_id` | `UUID` FK `professionals.id` (`ondelete=CASCADE`) | |
| `seen_at` | `DateTime(tz)` nullable | Marcado ao abrir o popover |
| `read_at` | `DateTime(tz)` nullable | Marcado ao clicar no item |
| `created_at` / `updated_at` | `DateTime(tz)` | via `TimestampMixin` |

**Constraint:** `UNIQUE(notification_id, professional_id)` (`uq_app_notification_reads_notif_prof`). Índices em `professional_id` e `notification_id`.

### Consulta do usuário (union sem fan-out)

A caixa de entrada de um profissional `P` (com `specialty_key S`, momento `now`) é:

- **Broadcasts vigentes:** `kind = 'broadcast'` AND `status IN ('published', 'scheduled')` AND `(publish_at IS NULL OR publish_at <= now)` AND `(expires_at IS NULL OR expires_at > now)` AND `(audience = 'all' OR S = ANY(string_to_array(audience, ',')))`.

> Nota: tratamos `scheduled` com `publish_at <= now` como vigente (a vigência é derivada na leitura — ver "Sem arq" abaixo). A "promoção formal `scheduled → published`" é cosmética e fica fora da v1.

Para cada linha, `LEFT JOIN app_notification_reads ar ON ar.notification_id = n.id AND ar.professional_id = P.id` deriva `seen = ar.seen_at IS NOT NULL` e `read = ar.read_at IS NOT NULL`.

Ordenação: `COALESCE(publish_at, created_at) DESC, id DESC`, paginação por **cursor opaco `(sort_ts, id)`**.

### Contagem sem fan-out

```sql
SELECT
  COUNT(*) FILTER (WHERE ar.seen_at IS NULL) AS badge,
  COUNT(*) FILTER (WHERE ar.read_at IS NULL) AS unread
FROM app_notifications n
LEFT JOIN app_notification_reads ar
  ON ar.notification_id = n.id AND ar.professional_id = :pid
WHERE <mesmo filtro da consulta unificada>;
```

O `LEFT JOIN` retorna `NULL` em `seen_at`/`read_at` para broadcasts nunca tocados — contam corretamente como não-vistos/não-lidos **sem** linha por usuário.

### Permissão de plataforma — flag `is_staff`

Korus Fono **não tem** tabela `roles` nem `require_roles`. Em vez de introduzir um sistema de papéis só para isto, adicionamos uma flag booleana direto no `Professional`:

- Coluna `is_staff: bool = False` em `professionals` (+ migration).
- `GET /api/v1/me` passa a retornar `isStaff`.
- Dependency `require_staff(professional = Depends(get_current_professional))` que levanta 403 se `not professional.is_staff`.
- Atribuição **operacional** (setar `is_staff = true` direto no banco para o profissional-staff da Korus). Sem tela de gestão de papéis na v1.

---

## API

Prefixo `/api/v1/` (router registrado em `app/api/v1/router.py`). Auth via `Depends(get_current_professional)`. Endpoints admin exigem `Depends(require_staff)`.

### Endpoints do usuário (`app/api/v1/notifications.py`)

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/notifications?filter=all\|broadcast&cursor=` | Lista paginada (cursor) com estado `seen`/`read` derivado |
| GET | `/notifications/unread-count` | `{ badge, unread }` |
| POST | `/notifications/seen` | Marca vigentes não-vistos como vistos (ao abrir o popover) |
| POST | `/notifications/{id}/read` | Marca um item como lido (404 se não visível) |
| POST | `/notifications/read-all` | Marca todos os vigentes como lidos |

> `filter` na v1 aceita `all` e `broadcast` (ambos retornam o mesmo conjunto, pois só há broadcast); o parâmetro existe para estabilidade de contrato quando `personal` chegar.

**GET `/notifications`** — resposta (CamelModel):

```jsonc
{
  "items": [
    {
      "id": "uuid",
      "kind": "broadcast",
      "type": "feature",
      "title": "Novo módulo de relatórios",
      "body": "Agora você pode...\n\nAcesse em Relatórios.",
      "deepLink": "/relatorios",
      "severity": "info",
      "seen": false,
      "read": false,
      "createdAt": "2026-07-08T12:00:00Z",
      "sortTs": "2026-07-08T12:00:00Z"
    }
  ],
  "nextCursor": "opaque-or-null"
}
```

**GET `/notifications/unread-count`** → `{ "badge": <não-vistos>, "unread": <não-lidos> }`.

**POST `/notifications/seen`** — sem body (marca todos os vigentes não-vistos). Retorna `{ "badge": 0, "unread": <n> }`.

**POST `/notifications/{id}/read`** — seta `read_at = now` (e `seen_at` se ainda nulo). Valida que o anúncio é visível ao profissional (bate no audience e na vigência); 404 se não. Retorna o item atualizado.

**POST `/notifications/read-all`** — marca todos os vigentes como lidos. Retorna `{ "badge": 0, "unread": 0 }`.

### Endpoints admin — Anúncios (`app/api/v1/admin_announcements.py`)

Todos exigem `Depends(require_staff)`. Operam apenas sobre `kind = 'broadcast'`.

| Método | Rota | Descrição |
|--------|------|-----------|
| POST | `/announcements` | Cria anúncio em `draft` |
| GET | `/announcements` | Lista (filtros por `status`) |
| GET | `/announcements/{id}` | Detalhe |
| PATCH | `/announcements/{id}` | Editar campos e/ou transicionar `status` |
| DELETE | `/announcements/{id}` | Excluir (hard delete; `CASCADE` remove reads) |
| GET | `/announcements/{id}/stats` | Métricas do anúncio |

**POST `/announcements`** — body:

```jsonc
{
  "type": "feature",            // 'feature' | 'tutorial' | 'notice'
  "title": "Novo módulo de relatórios",
  "body": "Agora você pode...",
  "severity": "info",
  "deepLink": "/relatorios",
  "audience": "all",            // 'all' | 'fono,to' | ...
  "publishAt": null,
  "expiresAt": null
}
```

Cria com `status = 'draft'`, `created_by = current_professional.id`, `kind = 'broadcast'`, `recipient_professional_id = NULL`.

**PATCH `/announcements/{id}`** — edita os campos acima e/ou transiciona `status` (ver máquina de estados).

**GET `/announcements/{id}/stats`** — métricas derivadas:

```jsonc
{
  "audienceSize": 128,
  "seenCount": 84,
  "readCount": 51,
  "clickCount": 51,
  "seenRate": 0.656,
  "readRate": 0.398,
  "clickRate": 0.398
}
```

`audienceSize` = count de `professionals` que batem no audience **no momento da consulta** (limitação documentada: reflete o público atual, não o da publicação). `clickCount` = `readCount` (clique = `read_at`), complementado por evento GA4.

### Máquina de estados do anúncio

```
draft ──publicar──────────────► published
  │                                 │
  └──agendar(publish_at futuro)──► scheduled
published ──despublicar / expires_at passou──► archived
arched  ──editar/republicar────────────────► draft/scheduled/published
```

| De | Para | Gatilho |
|----|------|---------|
| `draft` | `published` | Publicar agora (sem `publish_at`) |
| `draft` | `scheduled` | Agendar (`publish_at` futuro) |
| `scheduled` | `published` | Publicar agora (remover/agendar passado) — promoção formal por task fica fora da v1; a query trata `scheduled` com `publish_at <= now` como vigente |
| `published` | `archived` | Despublicar manualmente |
| qualquer | `draft` | Voltar a editar |

### Geração/entrega: inline + polling, **sem arq**

- **Criação:** síncrona via painel admin (nada de worker).
- **Vigência:** derivada na leitura pela query de entrega. **Não** usamos o worker arq (Redis) nesta entrega — nenhuma task de "promoção `scheduled → published`". Documentado que, no futuro, digest por e-mail ou promoção formal podem entrar como função arq em `worker.py` sem mudar API/schema.

---

## Service — `NotificationService` (`app/services/notification_service.py`)

```python
class NotificationService:
    def __init__(self, db: AsyncSession): ...

    # admin
    async def create_announcement(self, *, author, payload) -> AppNotification: ...
    async def list_announcements(self, *, status_filter) -> list[AppNotification]: ...
    async def get_announcement(self, id) -> AppNotification: ...
    async def update_announcement(self, *, id, payload) -> AppNotification: ...
    async def delete_announcement(self, id) -> None: ...
    async def announcement_stats(self, id) -> AnnouncementStats: ...

    # usuário
    async def list_for_professional(self, *, professional, filter, cursor, limit) -> NotificationPage: ...
    async def counts_for_professional(self, professional) -> UnreadCount: ...
    async def mark_seen(self, professional) -> UnreadCount: ...
    async def mark_read(self, professional, id) -> NotificationItem: ...
    async def mark_all_read(self, professional) -> UnreadCount: ...
```

- `mark_seen`/`mark_read` fazem **upsert** em `app_notification_reads` (`INSERT ... ON CONFLICT (notification_id, professional_id) DO UPDATE`), setando `seen_at`/`read_at` quando ainda nulos. `read` seta `seen` se nulo.
- Cursor opaco: `encode_cursor(sort_ts, id)` / `decode_cursor(cursor)` (base64 de `"iso_ts|uuid"`).

---

## Schemas (`app/schemas/app_notification.py`)

`CamelModel` (alias camelCase, `from_attributes=True`). Literais:

```python
NotificationKind = Literal["broadcast", "personal"]
NotificationType = Literal["feature", "tutorial", "notice"]
NotificationSeverity = Literal["info", "success", "warning", "critical"]
AnnouncementStatus = Literal["draft", "scheduled", "published", "archived"]
NotificationFilter = Literal["all", "broadcast"]
```

Modelos: `NotificationItem`, `NotificationPage`, `UnreadCount`, `AnnouncementCreate`, `AnnouncementUpdate`, `Announcement`, `AnnouncementStats`.

---

## Migration Alembic

Head atual: `h8i9j0k1l2m3`. Próxima: `i9j0k1l2m3n4_app_notifications`.

- Cria `app_notifications`, `app_notification_reads`, índices e unique constraint.
- Adiciona `is_staff BOOLEAN NOT NULL DEFAULT FALSE` em `professionals`.

Seed: 3 anúncios-demo em `app/seeds/demo.py` (dev apenas), todos `published`/`audience='all'`:

1. `feature`/`info` — "Novo módulo de relatórios com IA" (deep_link `/relatorios`).
2. `notice`/`warning` — "Manutenção programada sábado 03/08 das 02h às 04h".
3. `tutorial`/`info` — "Conecte seu WhatsApp para enviar lembretes" (deep_link `/whatsapp`).

---

## Frontend (`korus-one-web`)

Stack: TanStack Router (file-based) + TanStack Query + Zustand + shadcn/ui + Sonner. Auth por tokens em `localStorage` (`auth-storage.ts`).

### Componentes e páginas

| Artefato | Local |
|----------|-------|
| `NotificationBell` | `src/components/notifications/NotificationBell.tsx` (no `Header.tsx`, substitui o `Bell` decorativo atual) |
| `NotificationPopover` | `src/components/notifications/NotificationPopover.tsx` |
| `NotificationItem` | `src/components/notifications/NotificationItem.tsx` |
| Página `/notificacoes` | `src/routes/notificacoes.tsx` |
| Área `/admin/anuncios` | `src/routes/admin.anuncios.tsx` |
| Hooks | `src/lib/api/hooks/use-notifications.ts` |
| Serviços | `src/lib/api/services/notifications.ts` |
| Tipos | `src/lib/api/types.ts` (acrescentar) |
| Query keys | `src/lib/api/query-keys.ts` (acrescentar) |

### `NotificationBell`

- Ícone `Bell` (lucide). **Badge** = `unreadCount.badge` (não-vistos). Some quando 0. Cap `9+`.
- Envolve o `PopoverTrigger`.
- **Ao abrir:** dispara `POST /notifications/seen` (badge zera) + `analytics.trackNotificationBellOpened(badge)`.

### `NotificationPopover`

- `Popover` compacto, `align="end"`. **Sem abas** (só broadcast).
- Lista os últimos ~8 anúncios. Item: ícone por `severity`/`type`, título, trecho do corpo, timestamp relativo (`date-fns`), **dot/negrito** se não-lido.
- Rodapé: **"Ver todas"** → navega para `/notificacoes` + `analytics.trackNotificationViewAllClicked()`.
- **Ao clicar num item:** `POST /notifications/{id}/read` + navega pelo `deepLink` (se houver) + `analytics.trackNotificationItemClicked(...)`.

### Página `/notificacoes`

- Lista completa **agrupada por data** ("Hoje", "Ontem", "Esta semana", data absoluta) via `date-fns`.
- **Filtro** por `type` (Funcionalidade/Tutorial/Aviso) num `Select`.
- Ação **"Marcar todas como lidas"** no header → `POST /notifications/read-all`.
- Paginação por cursor (infinite scroll ou "carregar mais").

### Guard `/admin/anuncios`

- Rota `admin.anuncios.tsx` com `beforeLoad` checando `isStaff` do store de auth (via `useProfessional().data.isStaff`); não-staff → `throw redirect({ to: "/dashboard" })`.
- Entrada na **sidebar condicional** (grupo "Admin" visível só quando `isStaff`).

### Área `/admin/anuncios`

- **Lista** (`DataPage`-like com shadcn `Table`): colunas `status`, público-alvo, `publishAt`/`expiresAt`, resumo de métricas. Ações por linha: publicar, agendar, despublicar, editar, excluir.
- **Editor** (formulário): título, corpo (`Textarea`, texto puro), tipo (`Select`), deep link (`Input`), público-alvo (radio "Todos" + chips por especialidade), `publishAt`/`expiresAt` (date-time picker), ações Salvar rascunho/Publicar/Agendar/Despublicar/Editar/Excluir.
- **Métricas** por anúncio (`GET /announcements/{id}/stats`).
- **Sem pré-visualização** na v1.

### Hooks (React Query)

- `useNotifications(filter)` → infinite query por cursor; `refetchOnWindowFocus: true`.
- `useUnreadCount()` → `refetchInterval: 60_000`, `refetchOnWindowFocus: true`.
- **Refetch na troca de rota:** efeito com `useRouterState().location.pathname` invalida `notifications` + `unread-count`.
- Mutations: `useMarkSeen`, `useMarkRead`, `useMarkAllRead` (invalidam `unread-count` e a lista).
- `useAnnouncements()` (admin): lista; `useAnnouncement(id)`; `useCreateAnnouncement`, `useUpdateAnnouncement`, `useDeleteAnnouncement`.

### `ProfessionalDto` + store

- `ProfessionalDto` ganha `isStaff: boolean`.
- `useProfessional()` já alimenta o store/query; `isStaff` consumido pelo guard e pela sidebar.

### Estilo

- Severidade reutiliza classes Tailwind: `info→slate/blue`, `success→green`, `warning→amber`, `critical→red` (padrão `bg-*-500/15 text-*-700`).
- Primitivos shadcn (`Popover`, `Badge`, `Tabs` se necessário, `Select`, `Dialog`, `Table`) de `@/components/ui`.

---

## Analytics + LGPD (GA4)

Replica o padrão do `afeto-clinic-manager`, **adaptado para Vite/TanStack Start** (sem `@next/third-parties`).

### Camada de consentimento (`src/lib/cookie-consent/`)

- `constants.ts` — `CONSENT_STORAGE_KEY = "korus_cookie_consent"`, `CONSENT_MAX_AGE_MS = 183 dias`, `CURRENT_POLICY_VERSION`, `CookieConsentRecord`.
- `storage.ts` — `parseConsentRecord`, `readStoredConsent`, `writeStoredConsent`, `isConsentExpired`, `isConsentOutdated`, `shouldPromptForConsent`, `createConsentRecord`, `hasAnalyticsConsent`.
- `gpc.ts` — `isGlobalPrivacyControlEnabled`.
- `index.ts` — reexporta.

### Contexto + UI (`src/components/legal/`)

- `CookieConsentContext.tsx` — provider (`acceptAll`/`rejectAll`/`openPreferences`/`savePreferences`).
- `CookieConsentUi.tsx` — banner fixo no rodapé ("Aceitar todos"/"Rejeitar todos"/"Gerenciar preferências") + `Dialog` de preferências granulares (necessários sempre on; analytics toggle).
- `ConsentAwareAnalytics.tsx` — injeta `gtag.js` (script) somente quando `analyticsAllowed` e `VITE_GA4_ID` setado.
- Montar `CookieConsentProvider` + `CookieConsentUi` + `ConsentAwareAnalytics` no `__root.tsx`.

### Analytics (`src/lib/analytics.ts`)

- `isAnalyticsEnabled()` — checa `VITE_GA4_ID` + `hasAnalyticsConsent()`.
- `trackEvent(name, params)` — no-op silencioso se desabilitado; senão `gtag("event", name, params)`.
- Helpers tipados para os 4 eventos:
  - `trackNotificationBellOpened(badgeCount)`
  - `trackNotificationItemClicked({ notificationId, type, hasDeepLink })`
  - `trackNotificationViewAllClicked()`
  - `trackAnnouncementPublished({ announcementId, type, audience })`

**Limitação documentada:** consentimento só em `localStorage` (sem `POST /privacy/consent` no backend nesta entrega). Atende opt-in/opt-out/revogação/renovação/GPC; trilha de auditoria server-side fica para task dedicada de conformidade.

---

## Testes

### Backend (pytest, `tests/`)

| Alvo | Casos |
|------|-------|
| Contagem sem fan-out | Broadcast publicado sem reads conta no badge de todos os profissionais-alvo; após `seen`, badge cai; após `read`, unread cai; visto ≠ lido |
| Audience matching | `'all'` atinge todas as especialidades; `'fono,to'` atinge fono e to, **não** psicologia |
| Vigência | `scheduled` com `publish_at` futuro **não** aparece; com `publish_at` passado aparece; `expires_at` passado **não** aparece; `published` na janela aparece |
| Transições de status | draft→published, draft→scheduled, scheduled→published, published→archived; transições inválidas rejeitadas |
| Gate `is_staff` | Profissional sem flag → 403 em `/announcements/*`; com flag → 200 |
| `seen`/`read` upsert | Chamadas repetidas idempotentes; `read` seta `seen` se nulo |
| `read` de item não visível | 404 quando não bate no audience/vigência |
| Métricas | `audienceSize`, `seenCount`, `readCount`, `clickCount` e taxas corretas |
| `/me` retorna `isStaff` | `GET /me` inclui a flag |

### Frontend (Vitest)

| Alvo | Casos |
|------|-------|
| `NotificationBell` | Badge mostra count; some quando 0; `9+` no cap |
| `NotificationPopover` | Abrir dispara `seen` e zera badge; item não-lido em negrito; "Ver todas" navega |
| Guard `/admin/anuncios` | `isStaff` true acessa; false redireciona para `/dashboard` |

---

## Ordem de implementação

1. **Backend — migration** (`i9j0k1l2m3n4_app_notifications`): `app_notifications`, `app_notification_reads`, `is_staff` em `professionals`.
2. **Backend — modelos** (`app/models/app_notification.py`) + registro em `app/models/__init__.py`.
3. **Backend — schemas** (`app/schemas/app_notification.py`) + `is_staff` em `ProfessionalResponse`.
4. **Backend — `NotificationService`** + `require_staff` em `app/core/deps.py`.
5. **Backend — endpoints usuário** (`app/api/v1/notifications.py`) + endpoints admin (`app/api/v1/admin_announcements.py`) + registro no `router.py`.
6. **Backend — seed** dos 3 anúncios-demo em `app/seeds/demo.py`.
7. **Backend — testes** pytest.
8. **Frontend — tipos/services/hooks** (notifications.ts, query-keys, types).
9. **Frontend — cookie-consent + analytics** (replica do afeto, adaptado a Vite).
10. **Frontend — `NotificationBell` + `NotificationPopover`** no `Header`.
11. **Frontend — página `/notificacoes`**.
12. **Frontend — guard + área `/admin/anuncios`** + sidebar condicional.
13. **Frontend — testes** Vitest.

## Referências no codebase

### Backend (`korus-one-api`)
- `app/db/base.py` (`Base`, `TimestampMixin`, `new_uuid`)
- `app/core/deps.py` (`get_current_professional`)
- `app/core/specialty_catalog.py` (`SPECIALTY_KEYS`)
- `app/schemas/common.py` (`CamelModel`)
- `app/api/v1/router.py` (registro)
- `app/seeds/demo.py` (padrão de seed)

### Frontend (`korus-one-web`)
- `src/components/layout/Header.tsx` (sino decorativo atual)
- `src/components/layout/Sidebar.tsx` (grupos de nav)
- `src/routes/__root.tsx` (`beforeLoad`, providers)
- `src/lib/api/{client,types,query-keys}.ts`
- `src/lib/api/hooks/use-auth.ts` (`useProfessional`)
- `src/lib/auth-routes.ts` (`isPublicPath`)

### Referência externa
- `afeto-clinic-manager/src/lib/cookie-consent/*`
- `afeto-clinic-manager/src/contexts/CookieConsentContext.tsx`
- `afeto-clinic-manager/src/components/legal/CookieConsentUi.tsx`
- `afeto-clinic-manager/src/components/legal/ConsentAwareAnalytics.tsx`
- `afeto-clinic-manager/src/lib/analytics.ts`
