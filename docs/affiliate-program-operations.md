# Programa de afiliados — operação e rollout

## Escopo

O programa usa um núcleo único para indicações de clientes e parceiros externos, mas congela uma política independente em cada atribuição. O ledger é append-only: saldos são derivados dos lançamentos e correções são sempre compensatórias.

## Rollout seguro

As três flags são criadas desligadas pela migration:

- `affiliate_customer_program`
- `affiliate_partner_program`
- `affiliate_cash_payouts`

Ative clientes e parceiros separadamente. Dinheiro exige também `AFFILIATE_CASH_PAYOUTS_ENABLED=true`, `AFFILIATE_PAYOUT_ENCRYPTION_KEY`, sandbox/reconciliação Asaas validados e aprovação contábil/jurídica. A flag de caixa não substitui esse master switch do ambiente.

## Fluxo financeiro

1. O cadastro novo registra o primeiro código válido capturado dentro de 30 dias. A atribuição não pode ser substituída ou criada retroativamente.
2. `PAYMENT_CONFIRMED` cria recompensa `pending`; `PAYMENT_RECEIVED` inicia os 14 dias de segurança.
3. O worker libera recompensas vencidas para `available` no minuto 10 de cada hora.
4. Cliente pode converter `available` em crédito. O checkout reserva o crédito e só o liquida no evento financeiro autoritativo; uma cobrança integralmente paga por crédito gera um `BillingEvent` interno.
5. Pix reserva saldo disponível. O lote fecha segunda-feira às 12h em `America/Sao_Paulo`, exige aprovação e, fora do piloto explícito, operador distinto.
6. Inserir o identificador da transferência apenas marca o pedido como `processing`. Somente `TRANSFER_DONE` remove o saldo reservado; falha/cancelamento devolve o saldo para `available`, sem repetição automática.
7. Reembolso ou chargeback gera reversão proporcional. Se o valor já saiu, o saldo disponível fica negativo e compensa recompensas futuras; não há débito automático do participante.

## Dados fiscais e privacidade

CPF/CNPJ e chave Pix são criptografados com Fernet e possuem fingerprint HMAC para igualdade/antiabuso. A API só devolve máscaras. Troca de perfil exige nova aprovação e impõe bloqueio de 48 horas. O v1 registra metadados e aprovação manual; não transmite documento fiscal.

O indicado nunca vê a identidade de um cliente indicador. Parceiro pode ter apenas o nome público aprovado. IP e user-agent usados em risco são armazenados como hashes. Não enviar CPF/CNPJ, chave Pix, e-mail ou dados identificáveis ao PostHog.

## Administração

O painel `/admin/afiliados` é protegido por:

- `affiliates:read`: visão e consultas;
- `affiliates:write`: participantes, risco, políticas e correções;
- `affiliates:payout`: fiscal, lotes e liquidação.

Antes de ativar caixa, teste no ambiente Asaas: transferência concluída, falha, cancelamento, webhook duplicado, ausência temporária de webhook e reconciliação. Preserve evidência da validação e da aprovação fiscal/jurídica no processo interno.
