# E-mail de resumo semanal — design

## Objetivo

Enviar ao profissional titular um resumo operacional opcional da semana encerrada. O e-mail contém somente agregados de agenda e financeiro, nunca nomes de pacientes, diagnósticos, notas clínicas ou promoções.

## Semana operacional

- Período: segunda-feira 00:00 até sábado 18:59:59 em `CLINIC_TIMEZONE` (`America/Sao_Paulo` por padrão).
- Envio normal: sábado às 19:00.
- Atendimentos iniciados às 18:00 entram. Atividades após o fechamento e aos domingos permanecem no sistema, mas não entram no resumo.
- O primeiro envio ocorre somente no próximo fechamento após a adesão; não há resumo parcial nem retroativo.
- Correções posteriores não alteram nem reenviam o snapshot já emitido.

## Elegibilidade e preferência

O destinatário precisa ser o titular da conta, ter aderido explicitamente, possuir e-mail verificado, conta habilitada e acesso vigente por assinatura ativa ou trial válido. Staff e cadastros aguardando pagamento ficam excluídos.

A preferência fica em Configurações > Notificações e registra ativação, retirada e origem. O e-mail também oferece descadastro público idempotente, sem login, limitado ao resumo semanal. O link comum confirma a ação e os headers `List-Unsubscribe`/`List-Unsubscribe-Post` permitem descadastro em um clique.

Bounce permanente ou reclamação de spam suprime somente o resumo semanal. Recuperação de senha, verificação de conta e entregas solicitadas continuam independentes.

## Conteúdo

Assunto: `Seu resumo semanal | {dia inicial} a {dia final} de {mês}`.

### Agenda

- Total de horários.
- Concluídos.
- Faltas.
- Cancelados.
- Não finalizados: `pendente` + `confirmado` no fechamento.
- Taxa de comparecimento: `concluídos / (concluídos + faltas)`; quando o denominador for zero, exibir ausência de base em vez de `0%`.

O total é a soma das quatro classificações. Pacientes de demonstração e seus horários ficam excluídos.

### Financeiro

- Recebimentos confirmados na semana.
- Despesas pagas na semana.
- Saldo real: recebimentos menos despesas.
- Quantidade e saldo restante de todas as contas vencidas ainda abertas no momento do envio, independentemente da semana de origem.

Os registros financeiros possuem data civil, não horário. O resumo considera os registros já existentes no envio cuja data esteja entre segunda e sábado. Atendimento concluído não é tratado como receita, pois pode consumir pacote ou ser cortesia.

### Apresentação

O HTML é simples, responsivo e acessível, com alternativa em texto puro, duas seções numéricas e links descritivos **Ver agenda** e **Ver financeiro**. Não há gráficos, comparação com semana anterior, dados por paciente ou nova tela interna.

O rodapé identifica KorusFono, informa o contato de suporte e oferece descadastro. Por decisão de produto, não inclui endereço comercial.

## Envio e confiabilidade

- Reutilizar o cliente Resend, o layout de e-mail e o worker ARQ existentes.
- Persistir um registro por profissional e semana operacional, com restrição única, snapshot dos agregados, tentativas, status, erro, ID do provedor e horário de aceitação.
- Usar a mesma chave determinística no banco e na idempotência do provedor.
- Se o worker perder o horário normal, tentar novamente por até 24 horas. Depois de domingo às 19:00, registrar falha e não enviar o resumo envelhecido.
- `sent` significa aceitação pela Resend, não entrega na caixa postal.
- Validar a assinatura dos webhooks Resend antes de processar bounce ou reclamação.
- Não enviar quando não houver horários, movimentação financeira nem pendências vencidas.

## Contrato previsto

- Estender `GET/PATCH /api/v1/notifications/settings` com `weeklySummaryEmailEnabled`.
- Adicionar endpoint público idempotente de descadastro com token limitado a esse propósito.
- Adicionar webhook autenticado para eventos relevantes da Resend.
- Espelhar o campo e as chamadas no frontend e documentar a tela em `PAGES.md`.

## Fora do MVP

- Comparações históricas e tendências.
- Pacientes, avaliações, relatórios, metas ou conteúdo clínico.
- Tela de histórico dos resumos.
- Envio manual de teste ou reenvio manual.
- Personalização de horário, frequência ou métricas.
- Promoções, anúncios e novidades de produto.

## Critérios de aceite

1. Uma conta inelegível ou sem adesão nunca recebe o resumo.
2. Uma semana produz no máximo um envio aceito por profissional, mesmo com retries ou reinício do worker.
3. Os indicadores respeitam a semana operacional, o tenant e a exclusão de pacientes demo.
4. Sem atividade e sem pendência vencida, nenhum e-mail é enviado.
5. O descadastro direto é imediato, idempotente e não afeta e-mails essenciais.
6. O e-mail não contém dados identificáveis de pacientes e continua legível em HTML, texto puro, mobile e leitores de tela.
7. Falha permanente, bounce e reclamação ficam auditáveis sem expor conteúdo clínico.
