# Método de pagamento na lista administrativa

O campo `paymentMethod` de `GET /api/v1/admin/professionals` representa o método
conhecido da cobrança da assinatura mais recente, não uma confirmação de quitação.
O contrato continua aceitando `pix`, `credit_card` ou `null`; o frontend existente
já apresenta esses valores como PIX, Cartão de crédito e Não informado.

- A criação e a reutilização de checkout preservam o `billingType` retornado pelo
  Asaas na cobrança, inclusive antes de gerar novamente o QR Code.
- Checkout hospedado com `billingTypes: [PIX, CREDIT_CARD]` só informa opções
  permitidas; essa lista não é tratada como método escolhido.
- A consulta da sessão pode recuperar um método ausente a partir da cobrança
  exata referenciada pela assinatura. A reconciliação também pode recuperá-lo de
  um pagamento confirmado já processado, sem reaplicar eventos financeiros.
- A recuperação só preenche campos vazios e verifica o vínculo da cobrança no
  banco antes de escrever. Não sobrescreve escolhas concorrentes, não muda o
  status/acesso e não altera a ordenação das assinaturas por `updated_at`.
- Substituir a cobrança por um checkout sem método conhecido limpa a escolha da
  cobrança anterior; reutilizar a mesma cobrança não perde um método já conhecido.

Não há backfill automático ao abrir o admin nem consultas ao Asaas por linha.
Registros existentes podem ser recuperados pelos fluxos de consulta/reconciliação
acima. Trial sem cobrança ou checkout sem escolha continua sem método informado.
Esta correção usa a coluna existente e não exige nova migration.
