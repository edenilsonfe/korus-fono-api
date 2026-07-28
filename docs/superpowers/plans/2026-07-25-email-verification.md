# Email Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exigir verificação de e-mail (link) antes do uso da plataforma, com gate soft (sessão ok, app bloqueado) para signup e contas existentes.

**Architecture:** Flag `email_verified_at` no `Professional`; tokens com `purpose=email_verification` na tabela `password_reset_tokens`; envio via Resend (mesmo padrão do forgot-password); dependência `require_verified_professional` nas rotas de produto; web com `/verificar-email` + guard em `emailVerified`.

**Tech Stack:** FastAPI, SQLAlchemy/Alembic, Resend, pytest; TanStack Start/Router, Vitest.

**Spec:** `docs/superpowers/specs/2026-07-25-email-verification-design.md`

## Global Constraints

- Detail de bloqueio API: exatamente `"E-mail não verificado"` (HTTP 403)
- Wire JSON camelCase (`emailVerified`)
- Contas existentes: migration **sem** backfill (`email_verified_at` null)
- Settings: `email_verification_expire_minutes=1440`, `email_verification_cooldown_seconds=60`
- `GET /me` e `/auth/*` acessíveis sem verificação; `PATCH /me` exige verificação
- `get_patient_for_professional` e `require_staff` devem usar `require_verified_professional`
- Fixture de teste `professional` em `tests/conftest.py` deve setar `email_verified_at` para não quebrar a suíte
- Mensagens de e-mail/UI em pt-BR; commits em inglês conventional (`feat:`, `test:`, `docs:`)
- Repos: API em `korus-one-api`, web em `korus-one-web` — trabalhar na branch `feat/email-verification` em ambos
- Commits permitidos nesta execução (pedido explícito do usuário para implementar via SDD)

## File map

### API (`korus-one-api`)

| File | Responsibility |
| ---- | -------------- |
| `alembic/versions/*_email_verified_at.py` | Coluna `email_verified_at` |
| `app/models/professional.py` | Campo ORM |
| `app/models/password_reset_token.py` | `PURPOSE_EMAIL_VERIFICATION` |
| `app/core/config.py` | Settings de expiry/cooldown |
| `app/core/deps.py` | `require_verified_professional`; atualizar `get_patient_for_professional` + `require_staff` |
| `app/schemas/professional.py` | `email_verified: bool` |
| `app/schemas/auth.py` | `VerifyEmailRequest` |
| `app/services/email/templates.py` | Template de verificação |
| `app/services/email_verification.py` | Create/send/verify/resend (novo) |
| `app/api/v1/auth.py` | Endpoints + hooks register/login |
| `app/api/v1/me.py` | GET com flag; PATCH com dep verificada |
| `app/api/v1/*.py` (produto) | Trocar dep quando usam `get_current_professional` direto |
| `tests/conftest.py` | Fixture verified |
| `tests/test_email_verification.py` | Testes novos |

### Web (`korus-one-web`)

| File | Responsibility |
| ---- | -------------- |
| `src/lib/api/types.ts` | `emailVerified` |
| `src/lib/api/services/auth.ts` | verify/resend |
| `src/lib/api/hooks/use-auth.ts` | hooks |
| `src/lib/auth-routes.ts` | path público |
| `src/routes/verificar-email.tsx` | rota |
| `src/components/auth/VerifyEmailPage.tsx` | UI |
| guard em `__root.tsx` e/ou `AuthBootstrap` | redirect |
| `src/lib/auth-routes.test.ts` | testes path |
| `PAGES.md` / `CONTEXT.md` | doc auth |

---

### Task 1: Model, config, migration, purpose constant

**Repo:** `korus-one-api`  
**Files:**
- Modify: `app/models/professional.py`
- Modify: `app/models/password_reset_token.py`
- Modify: `app/core/config.py`
- Create: Alembic revision `email_verified_at` (nullable DateTime timezone)

**Interfaces:**
- Produces: `Professional.email_verified_at: datetime | None`
- Produces: `PURPOSE_EMAIL_VERIFICATION = "email_verification"`
- Produces: `Settings.email_verification_expire_minutes: int = 1440`
- Produces: `Settings.email_verification_cooldown_seconds: int = 60`

- [ ] **Step 1:** Add ORM field + purpose constant + settings
- [ ] **Step 2:** Create migration adding nullable `email_verified_at` (no server default that verifies users; no backfill)
- [ ] **Step 3:** Commit `feat(auth): add email_verified_at and verification settings`

---

### Task 2: Email template + verification service

**Repo:** `korus-one-api`  
**Files:**
- Modify: `app/services/email/templates.py`
- Create: `app/services/email_verification.py`

**Interfaces:**
- Consumes: `create_password_token`-style pattern from `password_reset.py` (reuse `PasswordResetToken` + `hash_token` + `PURPOSE_EMAIL_VERIFICATION`)
- Produces:
  - `email_verification_email(user_name, verify_url, expires_minutes) -> RenderedEmail`
  - `async def request_email_verification(db, professional, *, redis_client=None, force=False) -> str | None` — returns raw token if sent/created; respects cooldown unless force on register
  - `def send_email_verification_email_sync(to_email, user_name, raw_token) -> None`
  - `async def verify_email_with_token(db, raw_token) -> Professional` — idempotent per spec
- Link: `{frontend_url}/verificar-email?token=...`
- Cooldown Redis key: `email_verify_cooldown:{professional_id}`
- On register: create token even if cooldown would block (first email must go out) — use `force=True` or skip cooldown when no prior unused token flow

- [ ] **Step 1:** Add template mirroring `password_reset_email` copy for “Confirme seu e-mail”
- [ ] **Step 2:** Implement service (TDD: write `tests/test_email_verification.py` covering verify success, invalid token, idempotent already-verified, cooldown) then implement
- [ ] **Step 3:** Run `uv run pytest tests/test_email_verification.py -v`
- [ ] **Step 4:** Commit `feat(auth): email verification service and template`

---

### Task 3: Auth endpoints + ProfessionalResponse + deps gate

**Repo:** `korus-one-api`  
**Files:**
- Modify: `app/schemas/auth.py` — `VerifyEmailRequest(token: str)`
- Modify: `app/schemas/professional.py` — `email_verified: bool`
- Modify: `app/api/v1/me.py` — include flag; PATCH uses verified dep
- Modify: `app/core/deps.py` — add `require_verified_professional`; wire into `get_patient_for_professional` and `require_staff`
- Modify: `app/api/v1/auth.py` — register/login send email; `POST /verify-email`; `POST /resend-verification`
- Modify: all product routers that `Depends(get_current_professional)` directly → `require_verified_professional` (appointments, prontuario, billing, whatsapp, timeline, sessions, spm, resources, patients, instruments, notifications, clinical, dashboard, batteries, ai — keep auth using unverified; GET `/me` unverified)
- Modify: `tests/conftest.py` — set `email_verified_at=datetime.now(UTC)` on fixture `professional`

**Detail strings:**
- 403: `"E-mail não verificado"`
- 400 verify: `"Token inválido ou expirado"`

- [ ] **Step 1:** Implement schemas + deps + me + auth hooks/endpoints
- [ ] **Step 2:** Swap product router deps (+ patient/staff helpers)
- [ ] **Step 3:** Extend `tests/test_email_verification.py` (or auth tests): register → me `emailVerified false` → clinical 403 → verify → 200; resend; login triggers send path
- [ ] **Step 4:** Run `uv run pytest tests/test_email_verification.py tests/test_auth.py tests/test_auth_hardening.py -v` and a quick smoke `uv run pytest tests/test_dashboard.py -v` if exists
- [ ] **Step 5:** Commit `feat(auth): enforce email verification on API`

---

### Task 4: Web types, services, routes, guard, UI

**Repo:** `korus-one-web`  
**Files:** as in file map

**Behavior:**
- `ProfessionalDto.emailVerified: boolean`
- `verifyEmail(token)`, `resendVerification()`
- Public path `/verificar-email`
- After login/register/bootstrap: if authenticated && `!emailVerified` && path !== verify page → redirect `/verificar-email`
- Page: waiting state + token handling + resend + logout
- Follow `AuthSplitLayout` / reset-password patterns
- `routeTree.gen.ts` updates via Vite/TanStack codegen when route file added (do not hand-edit unless project requires running dev/build once)

- [ ] **Step 1:** Types + auth service/hooks
- [ ] **Step 2:** Route + VerifyEmailPage
- [ ] **Step 3:** auth-routes + guard redirect
- [ ] **Step 4:** Update `auth-routes.test.ts`; run `bun run test src/lib/auth-routes.test.ts`
- [ ] **Step 5:** Brief note in `PAGES.md` auth section
- [ ] **Step 6:** Commit `feat(auth): email verification UI and guard`

---

### Task 5: Cross-repo smoke + docs commit for API spec

**Repos:** both

- [ ] Ensure API spec file is committed if not yet: `docs/superpowers/specs/2026-07-25-email-verification-design.md` + this plan
- [ ] Run API: `uv run pytest tests/test_email_verification.py -v`
- [ ] Run web: `bun run test src/lib/auth-routes.test.ts`
- [ ] Commit any remaining docs on API: `docs: email verification design and plan`

---

## Acceptance checklist (from spec)

1. Signup envia e-mail (quando enabled) e não usa app até verificar  
2. Login não-verificado envia e-mail (cooldown) + tela de confirmação  
3. Link válido libera; inválido mostra erro + reenvio  
4. API 403 `"E-mail não verificado"`  
5. Migration sem auto-verify de contas existentes  
