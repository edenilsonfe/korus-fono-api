# Auditoria de afiliados — 04/09/2026 (America/Sao_Paulo)

Atualização: as correções posteriores e seus limites de liberação estão em [Afiliados: correções e liberação](affiliate-production-readiness-2026-09-05.md). O texto abaixo preserva os achados originais.

**Resultado: a implementação ainda não pode ser considerada correta de ponta a ponta.** Existem fluxos básicos cobertos e funcionando nos testes, mas também bloqueios no portal e falhas financeiras de recuperação, concorrência e contabilização.

Escopo: backend, frontend, contratos manuais, cadastro, desconto, webhook Asaas, comissão, revisão de risco, crédito, perfil fiscal, saques, worker e configuração operacional. Revisão do backend local em `1f1afd9`; alterações preexistentes de importação CSV/pyproject foram preservadas. Não houve correção de lógica de produção, commit, deploy, transferência ou alteração de saldo real.

## Evidência executada

| Verificação | Resultado | Limite |
| --- | --- | --- |
| Quatro arquivos existentes de afiliados | 15 testes passaram | SQLite, execução sequencial; não valida concorrência PostgreSQL |
| Afiliados + billing + webhooks HTTP + reconciliação + proteção de recorrência | 76 passaram, 6 xfailed | Os 6 xfails são defeitos conhecidos adicionados nesta auditoria |
| Seis regressões com `--runxfail` | 6 falhas reproduzidas nas asserções esperadas | Não são erros de configuração do ambiente |
| Atribuição no frontend | 2 testes passaram | Cookie/first-touch, sem cadastro real pelo navegador |
| Typecheck frontend | Passou | Não demonstra passagem de cabeçalhos pelo proxy |
| Execução direta de `filterProxyRequestHeaders` | `portalHeader: null`, cookie encaminhado | Confirma remoção do cabeçalho necessário no proxy local |
| Railway produção | API e worker SUCCESS | Status de deploy não demonstra integridade de saldos |
| Logs do worker atual | Inicialização registra `run_affiliate_maintenance` e cron correspondente | Não observado término de uma execução de liberação no recorte consultado |

Backend executado com `.venv-pytest/Scripts/python.exe`. A `.venv` principal contém binários Linux; o Python fornecido pelo app não possui pytest. O launcher do ambiente de testes e o esbuild exigiram execução fora do sandbox restrito; as execuções autorizadas terminaram normalmente.

Comandos reproduzíveis no PowerShell, a partir de cada repositório:

```powershell
# API
.\.venv-pytest\Scripts\python.exe -m pytest tests/test_affiliates.py tests/test_affiliate_credit.py tests/test_affiliate_payouts.py tests/test_affiliate_portal.py tests/test_affiliate_audit_regressions.py tests/test_billing_webhooks_http.py tests/test_billing_reconciliation.py tests/test_billing_unpaid_recurring_guard.py tests/test_billing.py -q
.\.venv-pytest\Scripts\python.exe -m pytest tests/test_affiliate_audit_regressions.py --runxfail -q --tb=short
# Web
bun run test src/lib/referral-attribution.test.ts
bun run typecheck
```

Os novos testes usam `xfail(strict=True)`: não escondem uma aprovação; documentam falhas conhecidas e passam a exigir atualização quando uma correção fizer a asserção passar. Remover o marcador quando corrigir cada caso. O teste de revisão manual é uma reprodução pelo service; não simula reprocessamento externo.

## Achados prioritários

### 1. P1 — Proxy bloqueia as mutações do portal de parceiros

O frontend envia `X-Affiliate-Portal: 1`, e a API exige o cabeçalho para aceitar termos, salvar perfil fiscal, pedir saque e sair. O proxy de produção usa uma allowlist que não contém esse cabeçalho. O cookie passa, mas a confirmação não: o resultado é 403 nesses endpoints quando servidos por esse proxy. A reprodução direta retornou `portalHeader: null`.

Fontes: [allowlist do proxy](C:/Users/ed/Documents/projetos/korus-one-web/src/lib/api/cloudflare-api-proxy.ts:10), `korus-one-web/src/server.ts:77`, `korus-one-web/src/lib/api/services/affiliate-portal.ts:9`, `app/api/v1/affiliate_portal.py:88`.

Correção: encaminhar o cabeçalho específico e testar a requisição através do proxy até a validação da API. Não retirar a proteção na API. Também revisar a perda de `User-Agent` e de informações confiáveis de origem: a allowlist atual reduz a qualidade do fingerprint antifraude, cujo resultado em produção depende dos proxies intermediários.

### 2. P1 — Webhook gravado antes de uma falha não é recuperado no retry

`record_webhook_raw` faz commit do evento antes de executar os efeitos. Qualquer evento já existente retorna `None`, mesmo com status `received`. O endpoint só aplica os efeitos quando recebe uma linha nova. Se ocorrer erro após persistir e antes de concluir, o Asaas pode reenviar e receber 200 sem recuperar a comissão ou o crédito.

Reprodução: gravar evento em `received` e repetir a chamada devolveu `None`. A reconciliação também não garante recuperação de afiliados quando a assinatura já está ativa: seu reparo adicional depende do status da assinatura/profissional.

Fontes: `app/services/saas_billing_service.py:120`, `app/api/v1/billing.py:900`, `app/services/billing_reconciliation_service.py:255`.

Correção: separar deduplicação de recebimento de confirmação de processamento; reivindicar/reexecutar eventos incompletos com efeitos idempotentes e recuperação durável.

### 3. P1 — Reembolso parcial não chega ao ledger

O normalizador não inclui `PAYMENT_PARTIALLY_REFUNDED`: devolve lista vazia e a rota responde sucesso sem reversão. O teste existente chama `reverse_external_payment` diretamente, portanto não cobre a integração quebrada. Na reprodução, um evento com reembolso de R$25 em cobrança de R$100 produziu zero eventos internos.

Fontes: `app/billing/webhook_normalizer.py:85`, `app/services/saas_billing_service.py:473`, `tests/test_affiliates.py`.

O Asaas documenta esse evento e orienta reconciliar a soma dos itens `refunds` com status `DONE`. A implementação atual procura primeiro `payload.value`, que representa a cobrança, e não soma esses itens. Apenas adicionar o nome ao normalizador não basta: isso pode converter reembolso parcial em reversão integral. A identidade sintética por tipo+pagamento também precisará distinguir ou reconciliar múltiplos reembolsos do mesmo pagamento.

Fontes externas: [eventos de pagamento](https://docs.asaas.com/docs/payment-events), [reembolsos](https://docs.asaas.com/docs/refunds).

### 4. P1 — Reserva de crédito permite reaproveitamento e contabilização negativa

Dois cenários reproduzidos:

- Reservar R$50, devolver a reserva e tentar novamente com o mesmo identificador: o service concede novamente R$50 de abatimento, mas mantém os mesmos R$50 disponíveis em crédito. A consulta considera a reserva antiga sem verificar que foi liberada.
- Reservar R$50, liquidar e depois devolver: a conta `reserved` termina em **-R$50**, pois os dois caminhos debitam a reserva original.

A rota de checkout usa o identificador da sessão e devolve crédito em falha do gateway; o billing devolve em eventos de falha e liquida no recebimento. Portanto a exclusividade das transições é necessária também fora do teste isolado. Uma devolução após liquidação pode ser válida, mas precisa ter lançamentos próprios de reembolso, sem retirar novamente a reserva já consumida.

Fontes: `app/services/affiliate_credit_service.py:135`, `:186`, `:220`; `app/api/v1/billing.py:577`; `app/services/saas_billing_service.py:486`.

Correção: modelar transições exclusivas, revalidar reservas liberadas e correlacionar cada pagamento com sua reserva original.

### 5. P1 — Solicitações simultâneas de saque podem gastar o mesmo saldo

O saque lê a soma de `available`, valida e insere lançamentos com um novo UUID sem bloquear o participante. Duas transações podem ler R$100 e ambas reservar R$100. As chaves idempotentes são diferentes por pedido, logo a unicidade não impede o saldo negativo. A conversão para crédito usa lock, mas o saque não compartilha esse lock; também existe corrida entre sacar e converter.

Fonte: `app/services/affiliate_payout_service.py:271` comparada a `app/services/affiliate_credit_service.py:36`.

Evidência: inspeção da transação e das constraints; **não reproduzido com duas conexões PostgreSQL**. Correção: trava comum por participante antes de ler/modificar saldo, idempotência da solicitação e teste concorrente no banco real de homologação.

### 6. P1 — Aprovar indicação depois do pagamento não recupera recompensa

Pagamentos de uma indicação em `manual_review` são descartados pelo service de comissão. Aprovar a revisão apenas muda campos da indicação; o worker só busca recompensas já existentes em `coolingOff`. No cenário reproduzido, pagamento recebido durante análise + aprovação + manutenção resultou em **zero recompensas**. Retentativa do mesmo webhook também encontra a deduplicação descrita no achado 2.

Fontes: `app/services/affiliate_service.py:472`, `:806`, `:608`; `worker.py:86`.

Correção: guardar a obrigação financeira em espera ou reconciliar pagamentos elegíveis ao aprovar, preservando a política original e sem depender de novo pagamento.

### 7. P1 — Confirmação de transferência pode chegar antes da vinculação manual

O operador informa o ID de uma transferência após o envio. Se `TRANSFER_DONE` chegar antes de o ID ser salvo no pedido, `complete_transfer` retorna `None`, mas o endpoint marca o evento como processado. Informar o ID depois só coloca o saque em `processing`; não reprocessa o evento anterior e não consulta o provedor.

Além disso, `provider_transfer_id` não é único. Dois pedidos podem receber o mesmo ID e a consulta com `scalar_one_or_none` falhar no callback. Não há conferência de valor/destinatário no vínculo; a rota reduz o evento persistido ao ID. Aprovar um lote ou marcar transferência tampouco revalida a suspensão do participante ou mudanças posteriores de perfil/risco.

Fontes: `app/api/v1/billing.py:856`, `app/services/affiliate_payout_service.py:393`, `:422`, `:436`, `app/models/affiliate.py:349`.

Evidência: inspeção do fluxo; sem transferência real. Correção: vínculo inequívoco, reconciliação de eventos antecipados/ausentes, unicidade e verificação do valor e titular antes de liquidar. O envio do Pix atualmente depende de operação externa/manual; a tela não cria a transferência no Asaas.

### 8. P2 — Parceiro que também vira cliente não fica vinculado à conta

O opt-in localiza participante pelo e-mail ou `professional_id`. Quando reutiliza um parceiro criado por convite, não preenche `professional_id`. A tela pode exibir sua participação, mas crédito e rotas de perfil/saque de cliente procuram exclusivamente por `professional_id` e não o encontram. Também enfraquece a checagem de autoindicação baseada na conta.

Reprodução: opt-in do profissional com o mesmo e-mail do parceiro deixou `professional_id=None`.

Fontes: `app/services/affiliate_service.py:130`, `:160`; `app/api/v1/affiliates.py:50`; `app/services/affiliate_credit_service.py:36`.

Correção: vínculo transacional entre identidades verificadas, com tratamento de conflitos e preservação das duas modalidades.

### 9. P2 — Reconciliação pode iniciar segurança antes do recebimento efetivo

A reconciliação aceita estados de sucesso e os normaliza como `PAYMENT_RECEIVED`, inclusive pagamentos ainda `CONFIRMED`. Isso inicia `coolingOff` sem a distinção que o próprio programa exige. O cálculo de `available_at` usa `paymentDate`/`clientPaymentDate`; não necessariamente a data em que o saldo ficou disponível. Em cartão, confirmar e receber podem acontecer em momentos distintos.

Fontes: `app/services/billing_reconciliation_service.py:235`, `app/billing/webhook_normalizer.py:221`, `app/services/affiliate_service.py:540`. [Semântica dos eventos Asaas](https://docs.asaas.com/docs/payment-events).

Correção: manter a diferença entre confirmação e recebimento em webhook e reconciliação; fixar a data-base financeira correta. Não concluir que `pending` é defeito só porque a assinatura está ativa.

### 10. P2 — Convite inválido guardado no cookie pode impedir cadastro

O cookie dura 30 dias, ambos os formulários o enviam automaticamente e erros de indicação abortam `_register_account` com 400. Se o participante for suspenso ou os termos mudarem depois do clique, uma pessoa pode ficar impedida de criar conta por uma atribuição obsoleta. `clearReferralAttribution` existe, mas só é chamado nos testes. A janela/first-touch é controlada pelo cliente, sem comprovante de captura validado no servidor.

Fontes: `korus-one-web/src/lib/referral-attribution.ts`, `src/components/auth/RegisterForm.tsx:58`, `src/components/auth/RegisterCheckoutForm.tsx:287`, `app/api/v1/auth.py:230`.

Correção: recuperar a experiência com indicação expirada/inválida, informando a perda do benefício; se a janela for requisito antifraude, validar captura no servidor.

## Outros pontos encontrados por inspeção

- **Políticas:** `effective_at` é salvo, mas ativar aposenta imediatamente a política anterior e `_active_policy` não filtra a data. `payout_minimum_cents` é configurável e entra no snapshot, mas o saque fixa 10000 centavos. A UI do portal exibe 20%/15% fixos mesmo com política personalizada. Fontes: `affiliate_service.py:108,769`, `affiliate_payout_service.py:285`, `korus-one-web/src/routes/afiliados.tsx`.
- **Elegibilidade:** opt-in checa o rótulo `trialing/active`, sem validar prazo de trial; código já ativo continua aceitando indicações sem revalidar assinatura do indicador. A proteção contra staff cobre cliente no opt-in, mas convite externo não cruza e-mail com contas staff. Fontes: `affiliate_service.py:142,195,261,294`.
- **Perfil fiscal:** é possível aprovar versão antiga `superseded`; o service não exige que seja a versão vigente. Isso permite reaproveitar dados antigos após troca de chave. A UI manda `pixValidated: true` no clique, mas a validação de titularidade é uma operação manual externa, não uma consulta Asaas. Fontes: `affiliate_payout_service.py:216`, `korus-one-web/src/lib/api/services/admin-affiliates.ts:97`.
- **Magic link:** uso único foi testado sequencialmente, mas troca do token é SELECT seguido de UPDATE sem consumo condicional/lock. Duas requisições concorrentes podem trocar o mesmo token. Fonte: `affiliate_portal_service.py:85`. Requer teste concorrente PostgreSQL.
- **Portal e assinatura:** `EntitlementMiddleware` também pode bloquear rotas do portal externo quando o navegador carrega cookie de uma conta clínica sem entitlement. Essas rotas não estão isentas. Um parceiro sem esse cookie segue outro caminho. Fonte: `app/middleware/entitlement.py:17,80`.
- **Cancelamento de saque:** existe rota para cliente, mas não existe equivalente no portal do parceiro nem controle de cancelamento na tela, apesar da janela `cancellableUntil` no contrato.
- **Configuração:** `.env.example` contém `AFFILIATE_PARTNER_PROGRAM` e `AFFILIATE_CUSTOMER_PROGRAM`, mas Settings não consome essas variáveis. As flags verdadeiras vivem no banco e são criadas desligadas na migration. Apenas preencher essas duas variáveis não ativa o programa.

## O que está sustentado pelo código/testes

- Políticas por modalidade e snapshot por indicação; unicidade de atribuição por conta.
- Comissão proporcional ao valor externo informado e recompensa única de cliente no fluxo sequencial.
- Ciclo básico `pending → coolingOff → available` e reversão proporcional pelo service.
- Ledger por lançamentos, com chaves idempotentes, sem contador de saldo mutável.
- Separação de permissões administrativas e respostas que omitem documentos/chaves Pix em claro.
- Criptografia fiscal, bloqueio de 48h e dupla aprovação no fluxo básico de saque.
- Magic link com expiração e token de sessão específico do portal.
- Tipos principais do frontend espelham os schemas de dashboard/participação/fiscal/saque.

Esses pontos não neutralizam as falhas de integração acima.

## Produção: observado e pendente

Consulta somente leitura ao Railway, projeto `korus-one-api`, ambiente `production`:

- API: deployment `7fd3af0e-bbfb-4a5c-a29c-e772c0a66af4`, SUCCESS.
- Worker: deployment `3015a955-5ec7-483f-bbdf-7963b5e508e6`, SUCCESS.
- Ambos criados em 2026-09-05 02:43:54 UTC (04/09 23:43:54 em São Paulo).
- Log do worker de 02:44:23 UTC registra função e cron de afiliados. A ferramenta rotula stderr como `error`, mas o texto é de inicialização, não demonstra falha.
- A lista de nomes de variáveis da API retornada pela ferramenta não contém `AFFILIATE_CASH_PAYOUTS_ENABLED` nem `AFFILIATE_PAYOUT_ENCRYPTION_KEY`. O código local exige ambas para disponibilizar/configurar saques. Valores renderizados e eventuais outras fontes de configuração não foram consultados; não declarar caixa operacional com essa evidência.

Não foram consultados registros financeiros de clientes, flags do banco, saldos de afiliados, pagamentos/transferências individuais no Asaas, revisão exata do frontend publicado ou a revisão exata da imagem em execução. Não houve teste pelo navegador autenticado nem jornada de pagamento em sandbox. As falhas demonstradas são do código local; não se atribui retroativamente impacto a pessoas reais sem correlacionar seus registros.

## Sequência de correção e aceite

1. Corrigir passagem do cabeçalho do portal e executar jornada via proxy: magic link, termos, dashboard, perfil, pedido e logout.
2. Corrigir recuperação de webhooks, eventos de estorno e correlação das reservas; exigir as seis regressões passando sem xfail.
3. Serializar operações de saldo; testar duas sessões PostgreSQL para saque/saque, saque/crédito e liquidação/estorno.
4. Recuperar pagamentos retidos por revisão; testar aprovação depois de ambos os eventos financeiros já terem sido processados.
5. Completar reconciliação de transferências e testar evento antes do vínculo, duplicação, falha, cancelamento e ausência de callback em sandbox.
6. Corrigir identidade híbrida, política vigente, elegibilidade e cadastro com cookie obsoleto.
7. Validar migrations em PostgreSQL de homologação e comparar código publicado; então fazer leitura conciliada de indicação, política, cobrança, eventos, recompensa, lançamentos e resultado do worker para um caso autorizado.

Até concluir esses itens, testes verdes do fluxo básico ou status SUCCESS do Railway não sustentam afirmar que o programa funciona integralmente.
