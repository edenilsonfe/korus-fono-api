# Afiliados: correções e liberação

Complementa a auditoria de 04/09. Alterações locais na API e no web; não houve deploy, habilitação de caixa, mudança de saldo real ou transferência.

## Resultado da implementação

| Caminho | Correção |
| --- | --- |
| Portal/proxy | Preserva cabeçalho de confirmação e User-Agent; sessão independente do entitlement clínico. |
| Cadastro | Prova de atribuição assinada e expirada pelo servidor; primeiro código preservado; cookie inválido não impede criar conta. |
| Identidade/termos | Vincula parceiro à conta cliente; verifica trial vencido/equipe; renovação de termos disponível; valores exibidos vêm da política. |
| Webhooks | Recibo durável e recuperável; reenvio de evento não processado executa os efeitos; conclusão financeira transacional e recuperação pelo worker. |
| Reembolso | Parcial reconhecido, soma somente itens DONE, distingue reembolsos sucessivos e suporta eventos fora de ordem. Pagamento tardio não reativa cobrança estornada. |
| Revisão | Pagamentos sob revisão são registrados e retidos; aprovação posterior permite liberação após carência. |
| Crédito | Tentativas e transições persistidas; liquidação/devolução não debitam reservado duas vezes; correlação com pagamento após troca do checkout; lock compartilhado com saque. |
| Preço | Abatimento inicial separado do preço futuro; atualização no Asaas usa `updatePendingPayments=false`. |
| Saques | Pedido idempotente, saldo serializado, mínimo vigente, perfil atual, carência de 48h e dupla aprovação. |
| Transferência | GET confere ID, valor, chave Pix e referência; unicidade impede reutilização; concilia callback anterior ao vínculo e callback ausente. Fato terminal verificado é contabilizado mesmo após suspensão posterior. |
| Operação | Cancelamento administrativo auditado de reserva não enviada; correção contábil repetida não duplica lançamento; telas permitem alteração fiscal e cancelamento. |

## Migration

`766aed0ea62c` sucede `z7a8b9c0d1e2`. Cria `affiliate_credit_checkouts`, chave idempotente do saque e unicidade da transferência; adiciona `subscriptions.checkout_recurring_price_cents` e `billing_events.last_attempt_at`.

Adota reservas antigas somente quando os lançamentos e a assinatura permitem correlação. Não reescreve o ledger. Interrompe diante de transferência duplicada, reserva ambígua, liquidação e liberação simultâneas antigas ou reserva sem assinatura rastreável. Esses casos precisam de conciliação específica; não apagar registros para fazer a migration passar. Downgrade recusa remover histórico de reservas.

Foram exercitados upgrade e downgrade vazio no PostgreSQL descartável, além de backfill que preserva lançamentos e rejeita histórico inconsistente. Publicar API, worker e web de forma coordenada: durante rollout parcial, cadastro sem prova funciona, mas não recebe nova atribuição. Indicações persistidas conservam snapshots.

## Validação reproduzível

Execução final em 05/09/2026: **110 testes backend passaram**, incluindo 4 verificações com PostgreSQL real; **85% de cobertura** agregada nos seis serviços medidos. No web: **16 testes unitários**, **4 cenários Playwright** (desktop/celular), typecheck, ESLint dos arquivos alterados e build de produção passaram. Ruff (imports/erros estáticos) e `git diff --check` também passaram. O build emitiu avisos de depreciação do runtime e de configuração Vite; não houve falha de compilação.

No backend, injetar `TEST_AFFILIATE_PG_URL` apontando para PostgreSQL descartável em `127.0.0.1:55439` para incluir concorrência; sem essa variável os testes PostgreSQL são pulados.

```powershell
.\.venv-pytest\Scripts\python.exe -m pytest tests/test_affiliates.py tests/test_affiliate_credit.py tests/test_affiliate_payouts.py tests/test_affiliate_portal.py tests/test_affiliate_audit_regressions.py tests/test_affiliate_billing_integration.py tests/test_affiliate_concurrency.py tests/test_billing_webhooks_http.py tests/test_billing_reconciliation.py tests/test_billing_unpaid_recurring_guard.py tests/test_billing.py -q
```

No web:

```powershell
bun run test src/lib/api/cloudflare-api-proxy.test.ts src/lib/referral-attribution.test.ts
bun run typecheck
bun run test:e2e affiliates.spec.ts
bun run build
```

O navegador cobre convite, código/prova, navegação até cadastro, mínimo e cancelamento, em desktop e celular, com APIs simuladas. O teste HTTP usa FastAPI e banco de teste para link, sessão, cabeçalho, termos, perfil, pedido repetido, isolamento, cancelamento e logout. A integração financeira passa pelo HTTP, normalizador, recibo, assinatura, recompensa e ledger. Chamadas externas são simuladas: não comprovam e-mail ou transferência real. Cobertura refere-se aos serviços de afiliados/recuperação, não à aplicação inteira.

## Verificação antes da liberação

Injetar `DATABASE_URL` explicitamente no ambiente correto. O script não carrega `.env`, usa transação PostgreSQL somente leitura e imprime agregados, sem documentos, chaves Pix ou URL de conexão.

```powershell
python scripts/check_affiliate_readiness.py
python scripts/check_affiliate_readiness.py --expect-cash-enabled
```

Exit code 1 indica atenção necessária. Saída limpa não certifica o provedor ou entrega de e-mail. Conferir:

1. Migration aplicada, revisão correta de API/worker/web e `run_affiliate_maintenance` executando.
2. Políticas vigentes e flags no banco `affiliate_customer_program`, `affiliate_partner_program` e, quando liberado, `affiliate_cash_payouts`. As antigas variáveis fictícias de programa foram removidas do exemplo.
3. Caixa: `AFFILIATE_CASH_PAYOUTS_ENABLED=true` e chave Fernet válida/permanente em `AFFILIATE_PAYOUT_ENCRYPTION_KEY`, igual na API e worker. Guardar cópia segura; trocar a chave sem migrar os perfis impede validá-los. Manter `AFFILIATE_PAYOUT_SINGLE_OPERATOR_PILOT=false` para dupla aprovação.
4. Asaas e webhook autenticado configurados; eventos de recebimento, reembolso parcial/integral e transferência habilitados. Alterar valor da recorrência com cartão exige tokenização habilitada na conta Asaas. Sandbox não comprova habilitação em produção.
5. E-mail de acesso entregue e link/cookie testados no domínio publicado.
6. Homologação externa autorizada: convite → cadastro → pagamento → carência → saldo → saque → transferência → callback e leitura independente. Duplicar evento não altera saldo; reembolso parcial devolve só a parcela correspondente.

Na auditoria anterior, os nomes das variáveis de caixa não apareceram na listagem da API Railway. Esta correção não alterou segredos nem flags de produção.

## Tratamento operacional

- **Falha após reservar crédito:** manter a reserva enquanto o efeito no provedor for incerto, inclusive erro HTTP em etapa posterior à criação. Nova criação com reserva sem vínculo fica bloqueada. Correlacionar profissional, sessão, valor e tentativas. Se houver cobrança, vincular a reserva ao pagamento correto e reprocessar o recibo. Se comprovadamente não houver cobrança que possa ser paga, usar `AffiliateCreditService.release_checkout_reservation` em transação auditada para o ID exato. Não liberar por timeout nem ajustar somente o saldo. O preflight aponta reservas sem vínculo.
- **Transferência manual:** usar UUID do saque como `externalReference`, chave do perfil aprovado e valor líquido exato. Informar o ID no painel e conciliar. Nenhuma transferência é criada ou repetida automaticamente.
- **Perfil mudou após reservar saque:** conferir no Asaas que nada foi enviado, registrar motivo e “Cancelar reserva”; novo pedido usará o perfil atual. Transferência vinculada só termina por conciliação.
- **Transferência antiga sem referência correta:** o endpoint normal rejeita; exige conciliação específica de evidências, sem inventar vínculo.
- **Pagamento antigo descartado sob revisão pela versão anterior:** não recriar comissões históricas indiscriminadamente. Identificar pagamento externo e snapshot daquele caso, reprocessar explicitamente e conferir antes/depois. Eventos novos ficam registrados e retidos.
- **Eventos pendentes:** worker percorre os menos recentemente tentados para evitar bloqueio pelos mesmos eventos sem correspondência. Não marcar manualmente como processado sem conciliar os efeitos.

## Referências verificadas

- [Atualizar assinatura](https://docs.asaas.com/reference/atualizar-assinatura-existente): próximas cobranças e preservação das emitidas.
- [Assinatura com cartão](https://docs.asaas.com/docs/criando-assinatura-com-cartao-de-credito): tokenização para alterar valor.
- [Recuperar transferência](https://docs.asaas.com/reference/recuperar-uma-unica-transferencia) e [formato da resposta](https://docs.asaas.com/reference/list-transfers): GET por ID e `bankAccount.pixAddressKey`.
