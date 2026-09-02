# Spike: cartão tokenizado / fora do escopo PCI (plano 031 — Fase A)

> **Decisão posterior — 2026-08-25:** a recomendação histórica abaixo foi substituída por decisão
> explícita de adotar o cartão transparente e assumir o escopo PCI DSS SAQ-D. A implementação atual
> usa `POST /billing/checkout/{session_id}/credit-card`, exige HTTPS fora de debug, limita tentativas
> e mantém PAN/CVV somente em memória durante a chamada ao Asaas, sem persistência ou logs. O texto
> restante permanece como registro do trade-off que motivou a decisão.

**Status:** APPROVED (opção A) — Fase B implemented (hosted invoice)  
**Branch alvo:** `advisor/031-pci-tokenize-spike`  
**Data:** 2026-07-21  
**Repos lidos:** `app/billing/asaas_gateway.py`, `app/services/billing_checkout_service.py`, `app/schemas/billing.py`, `app/api/v1/billing.py`, `app/billing/stub_gateway.py`, web `PaymentCheckout.tsx`, `src/lib/security-headers.ts`  
**Docs Asaas:** [PCI-DSS](https://docs.asaas.com/docs/pci-dss-1), [Tokenização](https://docs.asaas.com/reference/tokenizacao-de-cartao-de-credito), [Cobranças cartão](https://docs.asaas.com/docs/cobrancas-via-cartao-de-credito), [Redirecionamento pós-pagamento](https://docs.asaas.com/docs/redirecionamento-apos-o-pagamento)

---

## Veredito (STOP condition do plano)

**O Asaas não oferece tokenização client-side (browser / hosted fields / JS SDK com chave pública).**  
A própria doc PCI do Asaas afirma isso e recomenda SAQ-D se a captura for via API com PAN no back-end.

Consequência para a Fase B original (“API recebe `creditCardToken` em vez de PAN”):

- `POST /v3/creditCard/tokenizeCreditCard` e o campo `creditCardToken` em `payWithCreditCard` / `POST /payments` **existem**, mas a tokenização é **server-side** (exige `access_token` / API key).
- Na **primeira** captura, PAN/CVV ainda precisam chegar a um servidor nosso (ou Worker) que chame o Asaas → **continua SAQ-D**.
- Trocar o schema da API para só aceitar token **não** tira o Worker/API do escopo PCI se o token for gerado no nosso backend a partir do PAN.

**Recomendação do spike:** não implementar a Fase B como “aceitar token no checkout transparente”. Preferir **Fatura Asaas (`invoiceUrl`)** (ou Checkout/Link de Pagamento) para cartão, mantendo PIX in-app. Isso é o produto Asaas que mapeia para SAQ-A / “dados de cartão não aplicáveis” na tabela PCI deles.

---

## 1. Qual produto Asaas para tokenização no browser?

| Produto | Browser tokeniza? | Escopo PCI (doc Asaas) | Serve ao objetivo 031? |
| ------- | ----------------- | ---------------------- | ---------------------- |
| Tokenização API (`/v3/creditCard/tokenizeCreditCard`) | Não — server-side | SAQ-D | Não (PAN ainda passa por nós) |
| Checkout transparente via API (fluxo atual: form → nossa API → `payWithCreditCard`) | Não | SAQ-D | Não (status quo) |
| **Fatura Asaas (`invoiceUrl`)** | N/A — captura na UI Asaas | Não aplicável / SAQ-A | **Sim** |
| Checkout Asaas / Link de Pagamento | N/A — UI Asaas | SAQ-A | Sim (alternativa) |
| Tokenização client-side | **Não oferecido** | SAQ-A (hipotético) | Indisponível |

**Decisão proposta:** cartão via **Fatura Asaas**. O gateway já conhece `invoiceUrl` em `_payment_checkout_url`, mas `create_checkout_session` hoje devolve `build_in_app_payment_url` (formulário próprio em `/planos/pagamento`).

Fluxo alvo (conceitual):

1. Criar assinatura + 1ª cobrança somente com `billingType` `PIX` ou `CREDIT_CARD`;
   `UNDEFINED` não é permitido porque o Asaas o exibe como “Pergunte ao cliente”.
2. Expor `invoiceUrl` (+ `callback.successUrl` / `autoRedirect` para `/planos/retorno`).
3. UI: botão “Pagar com cartão” → redirect (top-level) para a fatura; não coletar PAN/CVV.
4. Remover (ou desligar) `POST .../credit-card` com `number`/`ccv`.
5. PIX permanece no checkout in-app.

Parcelamento anual: validar na fatura/sandbox se o Asaas cobre o caso atual (`pay_with_credit_card_installments` + defer renewal). Se não, tratar como open question antes da Fase B.

---

## 2. O que a API passa a receber (em vez de PAN/CVV)?

### Fluxo recomendado (fatura)

Para cartão, **nada de PAN/CVV/token no body**. A sessão de checkout já existente basta; o cliente paga na Asaas. A API continua:

- Criar/reconciliar sessão (`external_checkout_id`, webhooks, `reconcile`).
- Opcional: endpoint que devolve `{ invoiceUrl }` se ainda não estiver no `checkout_url`.

Campos sensíveis a **remover** do contrato atual (`CreditCardPaymentRequest`):

- `number`, `ccv` (e, no path de cartão in-app, também `expiryMonth` / `expiryYear` se o form sumir).

Campos de titular (`holderName`, CEP, telefone, etc.) deixam de ser necessários na nossa API no path de fatura (a Asaas coleta na página dela).

### Se alguém insistir em `creditCardToken` (não recomendado para 031)

Contrato Asaas em `POST /payments/{id}/payWithCreditCard` (e criação de cobrança):

- Com token: body com `creditCardToken` (substitui `creditCard` **e** `creditCardHolderInfo`).
- Sem token: `creditCard` + `creditCardHolderInfo` (fluxo atual em `AsaasPaymentGateway.pay_with_credit_card`).

Isso só faz sentido para **reuso** após token já existente — não resolve a primeira captura nem o escopo PCI do Worker.

---

## 3. Stub provider em debug

Hoje:

- `effective_billing_provider`: se `billing_provider=asaas` sem chave `$aact_*` em `debug`, cai para **`stub`**.
- `BillingCheckoutService.pay_credit_card`: se `provider != "asaas"`, chama `StubPaymentGateway.pay_with_credit_card(**_)` — **ignora** número/CVV e retorna `{ status: "CONFIRMED" }`, depois simula reconcile / upgrade.
- Stub **não** valida formato de cartão; qualquer payload “passa”.

Com fatura hospedada:

- Em stub/debug: manter simulação sem redirect externo (ex.: botão “Simular pagamento” / reconcile simulate já existente) — sem exigir token fake.
- Se Fase B for token (improvável): aceitar token sentinela **somente** com `settings.debug` (e rejeitar em produção), alinhado ao plano; stub continua ignorando o valor.

Não logar body de cartão em nenhum provider.

---

## 4. Impacto CSP (`script-src`)

Arquivo: `korus-one-web/src/lib/security-headers.ts`

CSP atual (trecho relevante):

- `script-src 'self' 'unsafe-inline' https://www.googletagmanager.com https://www.google-analytics.com`
- `connect-src` — self + Sentry + GA (sem Asaas)
- `frame-src 'self' blob:`
- `form-action 'self'`

| Abordagem | Mudança CSP |
| --------- | ----------- |
| Redirect top-level para `invoiceUrl` (recomendado) | Em geral **nenhum** host novo em `script-src`. Navegação full-page sai do documento. Confirmar se `form-action` / allowlist de checkout (plano 024) precisa incluir origem Asaas se houver POST intermediário. |
| iframe da fatura Asaas | Provável `frame-src` (+ talvez `child-src`) para `https://*.asaas.com` / sandbox; **ainda sem** script Asaas se a página for só frame. |
| SDK JS Asaas no nosso origin | **N/A** — produto inexistente; **não** afrouxar CSP com `'unsafe-eval'` “por precaução”. |

STOP do plano: se no futuro surgir script Asaas que exija `'unsafe-eval'` amplo — **não** afrouxar sem aprovação explícita.

---

## 5. Plano de migração sem downtime

1. **Feature flag** (config ou flag admin): `card_checkout=hosted_invoice | in_app_pan` (default atual = in_app).
2. **API:** em `create_checkout_session` / GET sessão, passar a popular `checkout_url` (ou campo dedicado `invoiceUrl`) com a fatura Asaas quando flag = hosted; manter `sessionId` para PIX.
3. **WEB:** com flag hosted, `PaymentCheckout` esconde formulário de cartão e oferece “Pagar com cartão” → `window.location` / link para fatura; PIX inalterado.
4. **Deploy API + WEB** na mesma janela (contrato aditivo primeiro: novos campos, endpoint credit-card ainda vivo).
5. **Sandbox:** pagamento de teste na fatura → webhook/reconcile → `/planos/retorno`.
6. **Desligar** flag in_app; depois remover schema/rota `credit-card` com PAN (breaking só para clientes que ainda postavam PAN — só o nosso web).
7. **Parcelamento:** só cortar o path in_app depois de validar equivalente na fatura; senão manter flag só para anual parcelado até haver desenho.

Rollback: reverter flag para in_app (aceitar regressão PCI temporária).

---

## 6. Open questions

1. Parcelamento 2–10× no plano anual é suportado na fatura Asaas da 1ª cobrança da assinatura, ou só via `POST /payments` com `installmentCount` (path atual server-side)?
2. Domínio de callback/`successUrl` já está cadastrado nas informações comerciais Asaas (prod + sandbox)?
3. Preferimos redirect full-page ou iframe embutido (CSP + UX)?
4. Assinatura recorrente: após pagar a 1ª fatura com cartão na UI Asaas, o Asaas tokeniza sozinho para ciclos seguintes, ou precisamos de passo extra?
5. Coordenação com allowlist de URLs (plano 024): quais hosts Asaas (prod `asaas.com` / sandbox) entram na allowlist?
6. Trocar de PSP por um com hosted fields (Stripe-like) está **fora de escopo** do 031 — confirmar se produto aceita UX de fatura hospedada.
7. Há obrigação contratual/legal de SAQ-D enquanto o form in_app existir em produção?

---

## Estado atual (âncora)

- Schema: `CreditCardPaymentRequest` com `number` + `ccv` (+ expiry, holder address).
- Router: `POST /checkout/{session_id}/credit-card` repassa PAN ao service.
- Gateway Asaas: `pay_with_credit_card` → `POST .../payWithCreditCard` com objeto `creditCard` completo; installments → `POST /payments` com o mesmo.
- WEB: `PaymentCheckout.tsx` guarda `cardNumber`/`ccv` em state React e posta via `payCheckoutCreditCard`.
- Worker proxy: qualquer POST de cartão passa pelo edge Cloudflare antes da API → superfície PCI ampla.

---

## Decisão e implementação

**Aprovado:** opção **A** (2026-07-21).

Implementado:

- `GET /billing/checkout/{session_id}` → `invoiceUrl` (fatura Asaas)
- `POST .../credit-card` com PAN/CVV **removido**
- WEB: “Pagar com cartão” → redirect top-level para `invoiceUrl` (allowlist plano 024)
- PIX in-app inalterado; stub usa “Simular cartão (dev)”
- Callback Asaas `successUrl` → `/planos/retorno` (best-effort em `set_payment_callback`)

Parcelamento anual: escolhido na UI da fatura Asaas (não mais no form Korus).
