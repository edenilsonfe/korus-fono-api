# Revisão de afiliados — 07/09/2026

## Atualização após correções locais

Os três defeitos reproduzidos foram corrigidos. As reproduções foram promovidas a `tests/test_affiliate_checkout_regressions.py`, coletado automaticamente, com cenários adicionais de repetição, conta divergente, checkout já pago e preservação de saldo. O quarto ponto foi resolvido explicitando na interface e no contrato que o crédito é aplicado no checkout manual, sem abatimento automático em renovações.

O avanço de ciclo consulta cobranças do Asaas e exige ID distinto, mesma assinatura e estado pendente/vencido; o lock da assinatura garante uma única nova sessão entre solicitações concorrentes. Checkout anual compara o total dos itens/valor antes de reutilizar. Autoindicação consulta a identidade comercial mesmo sem vínculo explícito com a conta clínica. Crédito integral fica bloqueado quando já existe cobrança externa, sem debitar saldo, até conciliação dessa cobrança.

Os detalhes abaixo registram a auditoria anterior às correções. Não houve commit, deploy ou alteração de dados de produção nesta etapa.

Validação final das correções: **128 testes backend passaram**, incluindo cinco casos com PostgreSQL 18 descartável em `127.0.0.1:55439` (migração de reservas legadas, concorrência de resgates, magic link e avanço único de ciclo). Typecheck e ESLint da tela alterada passaram no web; Ruff dos arquivos alterados e `git diff --check` passaram. Não foram executados build, Playwright, migration no ambiente da aplicação ou transações reais do Asaas nesta etapa.

Referências do provedor conferidas para a correção: [estrutura e valores dos itens do checkout](https://docs.asaas.com/docs/introduction-1) e [cancelamento do checkout](https://docs.asaas.com/reference/cancelar-um-checkout). As chamadas externas foram simuladas nos testes; isso não comprova a configuração da conta Asaas em produção.

## Parecer

Não recomendo liberar o fluxo completo antes de corrigir os três defeitos reproduzidos abaixo e alinhar a aplicação de crédito às renovações. Esta é uma revisão local: não houve alteração de regras de negócio, saldo, flags, segredos, commit ou deploy.

API: `afiliados-update`, HEAD `63b6946`; implementação financeira principal no commit `94994dd`. Web: `afiliados-update`, HEAD `9335f9c`. Comparação com a `main` local e leitura dos fluxos compartilhados que essas alterações utilizam. Não foi feita atualização dos refs remotos nem verificação de CI/produção. Mudanças locais preexistentes em dependências, WhatsApp e documentos foram preservadas.

## Achados

### 1. [P1] Crédito liquidado impede pagar um ciclo posterior vencido

Referências: `app/services/affiliate_credit_service.py:164`, `app/api/v1/billing.py:391` e `app/api/v1/billing.py:583`.

A reserva usa `Subscription.checkout_session_id`. Quando a assinatura fica `past_due`, o endpoint reutiliza a assinatura e o mesmo identificador. Se houve crédito liquidado no checkout anterior, `reserve_for_checkout` devolve erro antes de criar a cobrança seguinte, mesmo sem saldo de crédito restante.

Reprodução HTTP local: criar checkout de R$100 com R$50 de crédito, liquidar a reserva, passar assinatura/profissional para `past_due` e solicitar checkout novamente. Resultado: HTTP 409, `O crédito deste checkout já foi liquidado`. A guarda introduzida na branch confunde repetição da cobrança anterior com uma nova competência.

Correção necessária: identificar a cobrança/competência da reserva e distinguir retry de um novo ciclo. Preservar a idempotência da cobrança anterior; não basta retirar a guarda ou gerar um UUID em todo retry.

### 2. [P1] Checkout anual reutilizado pode cobrar o valor cheio e consumir crédito

Referências: `app/api/v1/billing.py:587`, `app/api/v1/billing.py:712`, `app/billing/asaas_gateway.py:577` e `app/services/affiliate_credit_service.py:288`.

O endpoint reserva crédito e calcula o valor externo menor, mas o gateway reutiliza checkout anual pendente sem conferir se o valor existente corresponde ao novo `charge_cents`. Trocar saldo/crédito não ativa `replace_existing_checkout`, que considera plano e documento. Depois, a reserva é vinculada ao checkout antigo; o recebimento pode liquidá-la sem validar o abatimento efetivo.

Reprodução isolada com o gateway real e somente consultas/mutações externas simuladas: checkout anual pendente de R$100; nova solicitação com `charge_cents=5000` e R$50 de crédito. O gateway retorna o mesmo checkout e não cria a cobrança com desconto. Isso demonstra o caminho local de reutilização; nenhuma cobrança real foi efetuada.

Correção necessária: conferir o valor da cobrança reutilizada antes de vinculá-la à reserva e tratar alteração de valor conforme o estado autoritativo do provedor. Checkout já pago não pode ser reinterpretado como pagamento com crédito.

### 3. [P1] Parceiro externo consegue atribuir a própria conta ao seu código

Referências: `app/services/affiliate_service.py:315`, `app/services/affiliate_service.py:468` e `app/services/affiliate_service.py:120`.

O parceiro pode ser ativado com `professional_id=None`. Ao criar posteriormente uma conta clínica com o mesmo e-mail, o cadastro não estabelece esse vínculo, e `register_referral` não compara os e-mails. A verificação por UUID/documento fica sem identidade de origem. A vinculação existente ocorre no opt-in de cliente, que é opcional e posterior.

Reprodução local: convidar parceiro, aceitar termos, criar `Professional` com o mesmo e-mail e registrar o código do parceiro. A indicação é aceita, quando deveria ser rejeitada. A verificação financeira por documento também retorna antecipadamente quando o parceiro não possui `professional_id`.

Correção necessária: resolver a identidade coincidente no cadastro/atribuição e no processamento financeiro, preservando a separação entre os papéis. Não depender de o parceiro aderir voluntariamente à modalidade cliente. O bloqueio fiscal posterior de saque não evita a atribuição nem o benefício inicial indevidos.

### 4. [P2] Aplicação automática de crédito na renovação não está implementada

Referências: web `src/routes/indicacoes.tsx:327`; API `app/services/affiliate_credit_service.py:68` e `app/api/v1/billing.py:587`.

A tela afirma que o crédito será aplicado automaticamente na próxima cobrança elegível. A conversão apenas transfere lançamentos de `available` para `credit`; o único chamador de `reserve_for_checkout` no produto é o POST `/billing/checkout`. Não há etapa de aplicação do saldo às cobranças recorrentes geradas pelo provedor. Assim, converter saldo durante uma assinatura recorrente não garante abatimento na próxima renovação.

Constatação por rastreamento de todos os chamadores e do worker, não por observação de uma renovação em produção. É necessário implementar a aplicação antes da cobrança recorrente, ou restringir explicitamente a funcionalidade e a promessa da interface ao checkout manual.

## Validação executada nesta revisão

- API: 106 testes passaram e quatro foram pulados na suíte existente de afiliados, crédito, saques, portal, regressões, integração de billing, webhooks e reconciliação.
- Os quatro pulados exigem `TEST_AFFILIATE_PG_URL`: concorrência/PostgreSQL não foram exercitados nesta rodada.
- Web: 16 testes passaram (proxy e atribuição); `bun run typecheck` passou.
- Três reproduções adicionais falharam exatamente nas expectativas dos achados 1–3. Foram preservadas em `tests/review_affiliates_2026_09_07.py`, fora da descoberta automática padrão, para reprodução explícita. Os provedores são simulados e o banco é SQLite em memória.
- Não executei build, Playwright, migrations nem homologação externa nesta rodada. Resultados registrados nas auditorias anteriores não foram tratados como execuções atuais.

```powershell
.\.venv-pytest\Scripts\python.exe -m pytest tests/test_affiliate_checkout_regressions.py -q
```

Na revisão inicial, os três cenários falhavam. Após as correções, o comando acima executa as regressões promovidas à suíte e deve passar.

## Estruturas verificadas e limites operacionais

O código contém políticas independentes e snapshots por indicação, prova assinada de atribuição, ledger por lançamentos, lock compartilhado de participante, recibos recuperáveis de webhook, reversões proporcionais, revisão de risco, carência, reserva de saque, dupla aprovação e conciliação de transferência por ID/valor/chave/referência. Os testes existentes cobrem diversos caminhos de repetição e reversão. Isso não elimina os achados acima nem comprova concorrência real nesta execução.

Para saques manuais, o painel/API só expõe documento e Pix mascarados. Não encontrei caminho de descriptografia/exportação dos perfis de afiliados para o operador executar a transferência, apenas armazenamento criptografado e comparação por fingerprint. Se esses dados chegam ao financeiro por um processo externo, esse processo precisa integrar a homologação; não foi observado nesta revisão. Também não há chamada pública passando retenção diferente de zero para `request_cash_payout`, embora o modelo armazene bruto/retenção/líquido. A adequação fiscal depende do processo de operação definido, que não foi auditado aqui.

Produção permanece não verificada: versão aplicada de API/worker/web, migration `766aed0ea62c`, flags de programa/caixa, chave Fernet, execução do cron, entrega de magic link e comportamento real de pagamento/estorno/transferência.
