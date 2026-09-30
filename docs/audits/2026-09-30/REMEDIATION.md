# Correções da auditoria — 30/09/2026

Os sete achados de [AUDIT.md](AUDIT.md) foram corrigidos localmente na API e no web.
Não houve commit, deploy, alteração no Google/Asaas ou acesso a dados de produção.

| Achado | Correção | Verificação executada |
| --- | --- | --- |
| AUD-001 | Claim atômico no PostgreSQL, versão do snapshot e prazo de dez minutos; conclusão exige a posse vigente. IDs de criação remota são persistidos antes do POST e reutilizados após timeout/conflito. | Dois dispatchers concorrentes; edição/cancelamento em voo; primeira inserção concorrente; recuperação de posse expirada; conclusão de worker antigo; timeout e conflito no provedor simulado. |
| SCR-001 | Configuração aceita somente `stub`/`asaas`; registry rejeita chave desconhecida sem fallback. | Configuração inválida e checkout HTTP 503 sem persistir customer/subscription. |
| SCR-002 | Cookie aleatório HttpOnly vincula o state ao navegador; transação de uso único expira em dez minutos. A versão e o estado da conta são validados antes e depois da troca do código. Callback pelo domínio do web. | Cookie ausente/de outro navegador, expiração, replay, conta desativada/não verificada, sessão revogada e invalidação durante troca; consumo concorrente no PostgreSQL. |
| SCR-003 | Persistência manifest recalcula scores, resultado, percentual e fields a partir de answers. Formulário envia respostas e bloqueia alterações durante preview/gravação. | Criação/conclusão de rascunho com scores forjados; leitura do resultado persistido; respostas vazias recusadas; teste React do formulário. |
| AUD-002 | Contexto compartilhado de IA trata título de evolução ausente com texto neutro. | Evolução criada por HTTP sem título seguida de análise de fala HTTP com LLM simulado. |
| SCR-004 | Branding lê no máximo 2 MiB + 1 byte; proxy limita o corpo durante streaming (1 MiB geral, 26 MiB multipart, 2 MiB + 64 KiB branding). | Arquivo acima do teto não chega ao storage; comprimento declarado e comprimento real; cancelamento do stream; runtime nativo workerd com upstream simulado. |
| AUD-003 | Campos PATCH obrigatórios aceitam omissão e recusam null explícito antes do banco. | Perfil, paciente e sessão: 422 para null, valor preservado por omissão e persistência de atualizações/limpeza de campos nullable. |

Os contratos e fluxos observáveis estão espelhados nos services do web,
`CONTEXT.md` e `PAGES.md`. Sem nova dependência de runtime.

## Validação local

- **92 testes da API aprovados**, cobrindo os sete achados, com HTTP/SQLite e
  concorrência em schemas isolados de PostgreSQL 18.6.
- **24 testes de proxy/cookies aprovados**, incluindo dois no runtime nativo do
  Worker, e **um teste React do formulário aprovado**.
- Typecheck, ESLint dos arquivos alterados e build do web aprovados.
- Histórico Alembic completo aplicado em banco vazio até `gc20260930a`, seguido
  de downgrade a `pt20260925a` e upgrade novamente. Teste com registros existentes
  confirmou preservação de ID remoto, status, tentativas e credencial Google.
- A suíte completa, antes do ajuste do fixture CARS, terminou com **1.433 aprovados
  e 48 falhas**. A revisão original `38a328a`, exportada em uma cópia temporária e
  executada no mesmo ambiente, terminou com **1.390 aprovados e 47 falhas**.
  As 47 falhas já existiam; a falha adicional foi o fixture CARS que enviava
  respostas sintéticas e scores prontos, agora substituídos por respostas válidas.
  Após o ajuste, os **14 testes de composição de laudos passaram**.
  A suíte completa não está verde neste ambiente. Evidência: [validation.json](validation.json).

As falhas da revisão original concentram-se em dashboard sem `actionItems` (1),
portal/família retornando 503 (29), contratos de fotos (2), ensaio de migration F14
(1), tarefas pessoais PostgreSQL (1) e fixtures SQLite de cleanup sem tabela
`intake_files` (13). A lista exata está no arquivo de evidências. Não foram
incluídas correções desses módulos no escopo dos sete achados.

O provedor Google, LLM e object storage foram simulados nos testes; não houve
ensaio de consentimento com uma conta Google real nem validação em produção.

## Antes da publicação

1. Cadastrar no cliente OAuth Google o redirect exato
   `<FRONTEND_URL>/api/v1/google-calendar/oauth/callback`, conforme
   [google-calendar-setup.md](../../google-calendar-setup.md).
2. Drenar/interromper dispatchers antigos, aplicar a migration
   `gc20260930a_google_calendar_oauth_and_sync_claims.py` e iniciar API/worker na
   mesma revisão. O código antigo não conhece as novas regras de posse da fila.
3. Publicar o web com o proxy atualizado e conferir o fluxo de conexão,
   reagendamento e cancelamento no ambiente autorizado.

A migration só foi aplicada ao banco descartável local. Conexões Google existentes
são preservadas; autorizações pendentes anteriores devem ser iniciadas novamente.
