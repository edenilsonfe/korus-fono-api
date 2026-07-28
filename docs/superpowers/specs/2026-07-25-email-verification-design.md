# Design: Verificação de e-mail no signup

**Data:** 2026-07-25  
**Status:** draft escrito — aguardando review do arquivo  
**Repos:** `korus-one-api` (contrato) + `korus-one-web` (UI / guard)  
**Relacionado:** reset de senha (`password_reset.py`, Resend, `PasswordResetToken.purpose`)

## Problema

Contas podem ser criadas com e-mails inventados ou tipados errados. Queremos garantir que o endereço usado no signup é real **antes** do uso da plataforma, enviando um link de confirmação.

## Decisões de produto

| Decisão | Escolha |
| -------- | ------- |
| Gate | Soft: login/sessão ok; app bloqueado até verificar (tela de confirmação + reenvio) |
| Contas existentes | Também precisam verificar (`email_verified_at` null após migration) |
| Envio no login | Automático se não verificado (com cooldown) + botão reenviar |
| Abordagem técnica | Reutilizar `password_reset_tokens` com novo `purpose` + flag no professional |
| Troca de e-mail no perfil | Fora de escopo nesta entrega |

## Abordagem escolhida

Reutilizar a infraestrutura de e-mail/token do forgot-password:

1. Coluna `professionals.email_verified_at` (`timestamptz`, nullable; null = não verificado)
2. `PURPOSE_EMAIL_VERIFICATION = "email_verification"` em `PasswordResetToken`
3. Template Resend + serviço espelhando `password_reset.py`
4. Front: `/verificar-email` (aguardando / processando token)

Não criar tabela nova de tokens. Não usar OTP nesta entrega.

## Fluxos

### Signup

1. `POST /auth/register` cria o professional com `email_verified_at = null` (demo patient e trial como hoje)
2. Emite cookies de sessão
3. Gera token (`purpose=email_verification`), invalida tokens anteriores do mesmo purpose
4. Envia e-mail em background com link `{frontend_url}/verificar-email?token=...`
5. Web redireciona para `/verificar-email`

### Login (não verificado)

1. Autentica e emite sessão como hoje
2. Se `email_verified_at` is null: tenta enviar verificação (respeitando cooldown Redis; fail-open se Redis cair)
3. Web, ao ver `emailVerified: false` em `/me`, redireciona para `/verificar-email`

### Verificar

1. Usuário abre o link → web chama `POST /auth/verify-email` com `{ token }`
2. API valida token (hash, purpose, não usado, não expirado), seta `email_verified_at = now()`, marca token usado
3. Idempotência: resolver o token pelo hash **sem** filtrar `used_at`/`expires_at` primeiro; se o professional ligado já tem `email_verified_at`, responder `200` (link antigo não assusta quem já confirmou). Token desconhecido / purpose errado → `400`
4. Conta ainda não verificada + token expirado ou já usado → `400` “Token inválido ou expirado”; UI oferece reenviar

### Reenviar

1. `POST /auth/resend-verification` (autenticado, sem exigir e-mail verificado)
2. Regenera token + envia e-mail, com cooldown
3. Resposta estável de sucesso para a UI da sessão atual

## Contrato API

### Modelo / schema

- `Professional.email_verified_at: datetime | None`
- `ProfessionalResponse.email_verified: bool` (derivado: `email_verified_at is not None`) — wire camelCase `emailVerified`
- Migration: adicionar coluna nullable; **não** backfill — contas existentes ficam não verificadas de propósito

### Endpoints novos

| Método | Path | Auth | Notas |
| ------ | ---- | ---- | ----- |
| `POST` | `/api/v1/auth/verify-email` | público (token no body) | Body: `{ token }`. Sucesso: `MessageResponse` |
| `POST` | `/api/v1/auth/resend-verification` | sessão (sem gate de verificação) | Cooldown; `MessageResponse` |

### Endpoints existentes (comportamento)

- `POST /auth/register` — após criar conta, agenda envio do e-mail de verificação
- `POST /auth/login` — se não verificado, agenda envio (com cooldown)
- `GET /me` — passa a incluir `emailVerified`; permanece acessível sem verificação
- Demais rotas autenticadas — bloqueadas até verificar (ver Enforcement)

### Enforcement

- Nova dependência `require_verified_professional` = `get_current_professional` + exige `email_verified_at is not None`
- Sem verificação: `403` com `detail` exatamente `"E-mail não verificado"` (string fixa para o web)
- `get_current_professional` **não** checa verificação (continua usado em auth, logout-all, resend, GET `/me`)
- Routers de produto passam a usar `require_verified_professional` (incluindo `PATCH /me`)
- Liberados sem verificação:
  - `/api/v1/auth/*` (incluindo verify/resend/logout/refresh/logout-all)
  - `GET /api/v1/me` apenas
- Staff (`is_staff`) segue a mesma regra
- Sem middleware novo nesta entrega

### Config

- Settings dedicados:
  - `email_verification_expire_minutes: int = 1440` (24h)
  - `email_verification_cooldown_seconds: int = 60`
- Envio: `email_sending_enabled` + `resend_api_key` + `frontend_url` (já existentes)

## Web (`korus-one-web`)

- Rota pública `/verificar-email` (com e sem `?token=`)
- Incluir em `auth-routes` / paths públicos conforme padrão atual
- Guard (AuthBootstrap ou equivalente): sessão + `!emailVerified` → redirecionar para `/verificar-email`, exceto essa rota
- Após verify ok → `/dashboard` (ou `redirect` query se existir)
- UI: mensagem “enviamos um link…”, botão reenviar, logout
- Espelhar `emailVerified` em `types` + `fetchMe`
- Atualizar `PAGES.md` / `CONTEXT.md` no fluxo de auth

## E-mail

- Template novo em `app/services/email/templates.py` (assunto/corpo pt-BR, link CTA, validade)
- Envio via `resend_client.send_email`, background task no register/login/resend (mesmo padrão do forgot-password)
- Se envio desabilitado: criar token + log (sem vazar e-mail em excesso), sem falhar o register/login

## Fora de escopo

- Re-verificação ao alterar e-mail no perfil
- OTP / código numérico
- Provedor de e-mail além do Resend
- Soft-delete de contas não verificadas após N dias
- Exceção de grandfathering para contas antigas

## Testes

### API

- Register → `email_verified_at` null; token criado com purpose correto
- Endpoint clínico (ou qualquer rota gated) → `403` com detail fixo antes do verify
- `POST /auth/verify-email` → marca verified; rota gated passa a funcionar
- Verify com token inválido/expirado → `400`
- Resend respeita cooldown
- Login de não-verificado agenda envio (Resend mockado / envio desabilitado ok)
- `GET /me` retorna `emailVerified: false|true` sem 403

### Web

- Guard redireciona não-verificado para `/verificar-email`
- Página trata sucesso e erro do token
- (Opcional) teste de `auth-routes` incluindo o novo path

## Critérios de aceite

1. Novo signup recebe e-mail (quando envio habilitado) e não acessa o app até clicar no link
2. Login de conta não verificada (incluindo legadas) recebe e-mail automático (com cooldown) e cai na tela de confirmação
3. Link válido libera a plataforma; link inválido mostra erro + reenvio
4. API rejeita uso autenticado não verificado com `403` `"E-mail não verificado"`
5. Contas existentes não são auto-verificadas na migration
