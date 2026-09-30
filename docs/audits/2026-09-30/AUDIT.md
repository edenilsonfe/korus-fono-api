# Auditoria do código — KorusFono

Data: 30/09/2026. Revisão: `secure-code-review` v1.0.0 e `ponytail-audit`.
Snapshot local: API `38a328a`; web `d380472`. Ambos estavam sem alterações locais no início da revisão.

**Atualização:** os sete achados foram corrigidos localmente após autorização.
Os detalhes abaixo preservam a evidência do snapshot original; implementação,
testes e requisitos de publicação estão em [REMEDIATION.md](REMEDIATION.md).

## Escopo e método

API FastAPI/Python/SQLAlchemy como escopo principal; frontend React/TanStack Start/Cloudflare nos contratos e chamadores dos fluxos examinados. Inventário da árvore, busca de referências, leitura dos fluxos críticos e análise sintática por AST. Não foi uma revisão linha por linha de todos os módulos.

| Área | Arquivos examinados e responsabilidade |
| --- | --- |
| Inicialização e acesso | `app/main.py`, `app/core/{config,deps,security,auth_cookies,client_ip}.py`, `app/middleware/entitlement.py`, `app/db/session.py`: configuração, autenticação e transações |
| Sessão e administração | `app/api/v1/{auth,me,router}.py`, gates dos routers `admin_*`, `app/services/{auth_rate_limit,refresh_token_service}.py`, schemas/modelos correspondentes |
| Prontuário compartilhado | `app/api/v1/{patients,sessions,prontuario,clinical,batteries}.py`, `app/services/{patient_access,care_team_service,patient_record,anamnese_service}.py` |
| Avaliações e IA | `app/services/{assessment_service,assessment_scoring,scoring_session,ai_service}.py`, `app/api/v1/{ai,instruments}.py`, `app/services/assistant/{tools,conversation_patient}.py` e formulário manifest no web |
| Billing e financeiro | `app/billing/{__init__,stub_gateway,http_client}.py`, trechos de `billing.py`, serviços de checkout/reconciliação e de conclusão de atendimento/financeiro; chamador de conclusão no web |
| Integrações | Router, serviço, modelo e testes do Google Agenda; `worker.py`; webhook Evolution, verificação de assinaturas e rotas públicas de agenda/família/programa de casa |
| Arquivos e privacidade | Storage, branding, leitores limitados de upload, scrubbing Sentry, tracking e consentimento; proxy, cookies e cliente HTTP/SSR do web |
| Verificação estrutural | Todos os arquivos Python de `app/`, `tests/`, `alembic/versions/` e `worker.py`; workflow de testes e fixtures |

Fronteiras de confiança consideradas: corpos/IDs HTTP, cookies e headers, tokens públicos, callbacks OAuth, webhooks, arquivos multipart, configuração de ambiente e execução concorrente API/worker. Referência: [ASVS 4.0.3](https://github.com/OWASP/ASVS/tree/v4.0.3/4.0/en), com cobertura parcial por inspeção.

Nenhum código da aplicação ou teste foi executado. O script de AST apenas leu e analisou o texto dos arquivos, sem importá-los. Não houve chamada a provedor, envio de mensagem, cobrança, consulta a dados reais, commit ou deploy.

## Resultado

Sete achados abertos: dois P1 e cinco P2. Severidade: Critical 0, High 0, Medium 5, Low 2. Prioridade indica ordem de correção; severidade descreve impacto e condições necessárias. Os caminhos problemáticos estão confirmados no código; os efeitos em runtime abaixo são inferências de fluxo, não reproduções executadas ou incidentes confirmados em produção.

| ID | Prioridade | Severidade | Problema |
| --- | --- | --- | --- |
| AUD-001 | P1 | Medium | Sincronização Google sem claim exclusivo nem proteção contra conclusão de snapshot antigo |
| SCR-001 | P1 | Medium | Provedor de billing desconhecido passa pelo guard de produção e vira stub |
| SCR-002 | P2 | Medium | OAuth Google sem vínculo do `state` ao navegador/sessão iniciadora |
| SCR-003 | P2 | Medium | Scores enviados pelo cliente substituem o cálculo manifest na persistência |
| AUD-002 | P2 | Low | Evolução sem título quebra o contexto usado por ferramentas de IA |
| SCR-004 | P2 | Medium | Proxy e branding carregam corpos completos antes de limitar tamanho |
| AUD-003 | P2 | Low | PATCH aceita `null` para campos que o banco exige preencher |

### AUD-001 — Sincronização Google pode duplicar eventos e perder atualização

- **Local:** [google_calendar_service.py:297](C:/Users/ed/Documents/projetos/korus-one-api/app/services/google_calendar_service.py:297), fila em [linha 190](C:/Users/ed/Documents/projetos/korus-one-api/app/services/google_calendar_service.py:190); chamadores em `app/api/v1/google_calendar.py`, `appointments.py`, `appointment_responses.py` e `worker.py`.
- **CWE / ASVS:** CWE-362, condição de corrida; [V11.1.6](https://github.com/OWASP/ASVS/blob/v4.0.3/4.0/en/0x19-V11-BusLogic.md), proteção contra condições de corrida na lógica de negócio.
- **Evidência:** `db.get(GoogleCalendarSyncRecord, record_id)` não toma lock; o dispatcher aceita `queued`, `failed` e também `processing`, grava `processing` e faz commit antes do I/O remoto. A criação usa busca por propriedade seguida de POST, sem ID remoto determinístico. Ao terminar, grava `synced` sem verificar se o snapshot foi substituído.
- **Impacto:** duas execuções podem buscar o mesmo evento ainda inexistente e criar dois eventos Google. Em outro interleaving, uma edição/cancelamento enfileira V2 enquanto V1 está em voo; V1 pode concluir e marcar a linha como sincronizada, ou sobrescrever o evento remoto depois de V2. A constraint local por `appointment_id` não torna a criação remota exclusiva.
- **Correção mínima:** claim atômico por linha; processamento ativo só pode ser retomado após expiração da posse. A conclusão precisa ser condicionada à versão reclamada, preservando trabalho reenfileirado. Usar identificação remota determinística quando aplicável para retries de resultado incerto.
- **Verificação necessária:** teste PostgreSQL com dois dispatchers e barreira no provedor simulado; outro teste reenfileirando cancelamento/edição durante a chamada remota. O teste atual de sincronização em `tests/test_google_calendar.py` é sequencial.
- **Status:** Open.

### SCR-001 — Erro na configuração seleciona billing simulado em produção

- **Local:** [billing/__init__.py:17](C:/Users/ed/Documents/projetos/korus-one-api/app/billing/__init__.py:17), [config.py:157](C:/Users/ed/Documents/projetos/korus-one-api/app/core/config.py:157) e [config.py:247](C:/Users/ed/Documents/projetos/korus-one-api/app/core/config.py:247).
- **CWE / ASVS:** CWE-20, validação inadequada; V5.1.3, validação por valores permitidos.
- **Evidência:** o provider é uma string livre. `effective_billing_provider` devolve valores desconhecidos; o guard de produção recusa apenas o valor efetivo `stub`; o registry devolve `StubPaymentGateway()` para qualquer chave diferente de `stub` e `asaas`.
- **Condição concreta:** com as demais configurações válidas, `SENTRY_ENVIRONMENT=production`, `DEBUG=false` e `BILLING_PROVIDER=assas` passam por esse guard e selecionam o stub. O checkout usa o `provider_key` dessa instância e gera IDs/PIX simulados.
- **Impacto:** checkout indisponível para pagamento real e registros de provedor inconsistentes com a intenção operacional. Não foi encontrada, neste caminho, ativação automática por pagamento fictício: a simulação HTTP é bloqueada com debug desativado.
- **Correção mínima:** rejeitar providers desconhecidos no schema/configuração e no registry; falhar com o `PaymentGatewayConfigError` já existente. Manter stub somente por seleção explícita nos ambientes autorizados.
- **Verificação necessária:** ampliar `tests/test_debug_production_guard.py` para valor desconhecido e conferir que checkout não persiste uma sessão simulada quando a configuração é inválida.
- **Status:** Open.

### SCR-002 — O callback OAuth aceita uma autorização iniciada em outro navegador

- **Local:** [google_calendar_service.py:62](C:/Users/ed/Documents/projetos/korus-one-api/app/services/google_calendar_service.py:62), [google_calendar.py:77](C:/Users/ed/Documents/projetos/korus-one-api/app/api/v1/google_calendar.py:77).
- **CWE / ASVS:** CWE-352, CSRF; V4.2.2, proteção contra CSRF.
- **Evidência:** o `state` é um JWT com `sub`, tipo e prazo de dez minutos. Não inclui desafio específico do navegador, consumo único ou versão de sessão; a autorização não usa PKCE. O callback não recebe sessão autenticada e salva a credencial no profissional indicado pelo JWT.
- **Cenário inferido:** A inicia a conexão na própria conta Korus e transfere a URL legítima de autorização para B. Se B concede acesso à sua conta Google, o callback associa essa credencial à conta Korus A. A assinatura impede alterar o `sub`, mas não impede transferir a autorização inteira. O produto pode então escrever no calendário B por ações da conta A; não foi demonstrada leitura arbitrária do calendário pelo produto.
- **Correção mínima:** vincular a transação OAuth ao navegador/sessão iniciadora e consumi-la uma única vez, com conferência de identidade e estado da conta no callback. A proteção precisa funcionar através do domínio/proxy efetivamente usado pelo web. PKCE pode complementar a proteção; seu verificador também deve estar vinculado à transação.
- **Referência:** a exigência de `state` de uso único vinculado ao user agent, na ausência de outra proteção adequada, consta da [RFC 9700, seção 2.1](https://www.rfc-editor.org/rfc/rfc9700.html#section-2.1).
- **Verificação necessária:** dois clientes/navegadores; transferência de state entre eles deve ser recusada. Cobrir replay e callback depois de invalidação da sessão. O teste atual confirma criptografia do refresh token, mas chama o callback sem cookies.
- **Status:** Open. Cenário de abuso não executado.

### SCR-003 — Persistência manifest confia no score fornecido pelo cliente

- **Local:** [assessment_service.py:37](C:/Users/ed/Documents/projetos/korus-one-api/app/services/assessment_service.py:37); chamador [ManifestInstrumentForm.tsx:168](C:/Users/ed/Documents/projetos/korus-one-web/src/components/assessments/ManifestInstrumentForm.tsx:168).
- **CWE / ASVS:** CWE-20, validação inadequada; [V5.1.4](https://github.com/OWASP/ASVS/blob/v4.0.3/4.0/en/0x13-V5-Validation-Sanitization-Encoding.md), validação de dados estruturados e relações entre campos.
- **Evidência:** o cálculo por respostas ocorre somente em `mode == "manifest" and scores is None`. Se `scores` vem preenchido, o caminho usa `ScoringSession.from_scores(scores)`, mesmo para instrumentos manifest. Campos de resultado/percentual enviados também têm precedência quando preenchidos.
- **Impacto:** respostas e resultado calculado podem divergir e chegar como avaliação concluída ao prontuário, análises e relatórios. O formulário normal envia o preview do servidor, mas salva `answers` do estado corrente junto de `lastScores` calculado anteriormente; os inputs continuam editáveis enquanto a requisição de cálculo está em voo. Um cliente HTTP também pode enviar scores incompatíveis diretamente. Isso afeta registros acessíveis ao autor, sem demonstrar acesso entre contas.
- **Correção mínima:** para manifest, derivar scores e campos calculados das respostas no momento de salvar; preservar separadamente a interpretação profissional quando o contrato a permitir. Manter o caminho de score fornecido pelo cliente apenas para os modos que dependem dele.
- **Verificação necessária:** POST com respostas FOIS nível 1 e scores pré-calculados de nível 7 deve persistir o cálculo coerente com nível 1 ou recusar a divergência. Cobrir alteração de resposta durante o preview no web.
- **Status:** Open.

### AUD-002 — Título opcional de evolução causa erro nas ferramentas de IA

- **Local:** [ai_service.py:88](C:/Users/ed/Documents/projetos/korus-one-api/app/services/ai_service.py:88); contrato em `app/schemas/prontuario.py:11` e `app/models/evolution.py:25`.
- **CWE / ASVS:** CWE-754, tratamento inadequado de condição prevista; [V7.4.2](https://github.com/OWASP/ASVS/blob/v4.0.3/4.0/en/0x15-V7-Error-Logging.md), tratamento de condições excepcionais.
- **Evidência:** `"; ".join(e.title for e in evolutions)` exige strings, enquanto `EvolutionCreate.title` é opcional e o router persiste `None` quando não há título.
- **Impacto:** uma evolução sem título entre as três mais recentes causa `TypeError` ao construir o contexto. Atinge análise de fala por texto/áudio e a ferramenta de contexto do assistente. No fluxo de áudio, a transcrição externa ocorre antes desse ponto e pode já ter sido processada.
- **Correção mínima:** tratar o título ausente no helper compartilhado, usando o fallback já adotado para apresentação de evoluções; não adicionar guards individuais aos chamadores.
- **Verificação necessária:** integrar criação de evolução sem título com uma chamada de análise de fala/contexto, usando provedor simulado. O teste atual de título opcional não cobre esse consumidor.
- **Status:** Open.

### SCR-004 — Limite de upload é aplicado depois da alocação do corpo

- **Local:** [cloudflare-api-proxy.ts:205](C:/Users/ed/Documents/projetos/korus-one-web/src/lib/api/cloudflare-api-proxy.ts:205), [me.py:121](C:/Users/ed/Documents/projetos/korus-one-api/app/api/v1/me.py:121), [professional_branding.py:62](C:/Users/ed/Documents/projetos/korus-one-api/app/services/professional_branding.py:62).
- **CWE / ASVS:** CWE-400, consumo de recursos sem controle; V12.1.1, limites de arquivos.
- **Evidência:** o proxy usa `await request.arrayBuffer()` para todo método com corpo, antes da autorização no upstream. Branding usa `await file.read()` sem teto e só depois verifica o limite de 2 MB. Os demais leitores examinados de anexos, áudio, fotos e recursos já fazem leitura limitada.
- **Impacto:** corpos grandes consomem memória antes de serem recusados; na borda isso ocorre inclusive para requisições que serão não autenticadas. O teto e os controles de tráfego da infraestrutura podem reduzir o impacto, mas não foram verificados nesta auditoria.
- **Correção mínima:** encaminhar o stream no proxy com limite de bytes adequado à rota e independente do header declarado; ler branding com teto de `MAX_BRANDING_BYTES + 1`, reutilizando o padrão local. Evitar uma nova biblioteca.
- **Verificação necessária:** rejeição do corpo acima do limite antes da leitura integral, incluindo ausência/falsificação de Content-Length; verificar o streaming no runtime real do Worker. O teste atual de branding verifica a resposta para tamanho excedido, não a quantidade de bytes lida.
- **Status:** Open.

### AUD-003 — Campos opcionais de PATCH permitem nulos incompatíveis com o banco

- **Local:** [professional.py:46](C:/Users/ed/Documents/projetos/korus-one-api/app/schemas/professional.py:46), [me.py:68](C:/Users/ed/Documents/projetos/korus-one-api/app/api/v1/me.py:68); mesmo padrão em `schemas/patient.py:93`, `schemas/session.py:18` e seus routers.
- **CWE / ASVS:** CWE-20, validação inadequada; V5.1.4, validação de dados estruturados e relações entre campos.
- **Evidência:** `name: str | None = None` aceita `{"name": null}`. `model_dump(exclude_unset=True)` mantém o nulo explicitamente fornecido e o router aplica `setattr`. `Professional.name`, `Patient.name` e vários campos de sessão são `nullable=False`.
- **Impacto:** pedidos aceitos pelo schema falham no flush com erro de integridade, produzindo 500 em vez de validação 422. A transação é revertida; não há evidência de persistência do nulo ou perda de dados.
- **Correção mínima:** distinguir omissão de nulo explícito nos schemas dos campos obrigatórios. Preservar a limpeza com nulo de campos realmente opcionais, como endereço/notas do paciente.
- **Verificação necessária:** PATCH de perfil/paciente/sessão com nulo inválido retorna 422 e mantém os valores anteriores; omissão continua válida e limpeza de campos opcionais continua funcionando.
- **Status:** Open.

## Verificação e controles observados

Matriz de cobertura ASVS: todos os itens abaixo tiveram apenas inspeção estática parcial. “Controle observado” significa presença no código examinado, sem atestar conformidade em runtime.

| Capítulo | Cobertura e limite |
| --- | --- |
| V2 — Autenticação | Gates, hashes e limites de autenticação examinados; ataques e configuração operacional não testados |
| V3 — Sessão | Cookies, refresh e revogação examinados; validade e replay em runtime não testados |
| V4 — Acesso | Amostras de autoria/tenant e gates admin examinados; SCR-002 aberto; matriz completa de papéis não executada |
| V5 — Validação | Schemas, configuração e campos derivados examinados; SCR-001, SCR-003 e AUD-003 abertos |
| V6 — Criptografia | Criptografia de credenciais Google observada; rotação de chaves e infraestrutura não verificadas |
| V7 — Erros e logs | Scrubbing Sentry examinado; AUD-002 aberto; logs reais não consultados |
| V8 — Proteção de dados | Política no-store e isolamento SSR examinados; caches da infraestrutura não verificados |
| V11 — Lógica de negócio | Billing, financeiro, scoring e sincronização examinados; AUD-001 aberto; concorrência não reproduzida |
| V12 — Arquivos | Leitores, limites e escopo de IDs examinados; SCR-004 aberto; limites externos não verificados |
| V14 — Configuração | Guards de produção e inicialização examinados; SCR-001 aberto; deployment não inspecionado |

- AST de 334 arquivos da aplicação, 179 arquivos de testes, 76 migrations e `worker.py`: 590 arquivos sem erro sintático.
- Inventário de 407 funções de handlers HTTP. O levantamento estático dos routers `admin_*` encontrou gates `require_admin_permission` em todos os handlers, considerando aliases locais. Isso não prova isoladamente a correção de cada papel/permissão.
- Grafo de revisões Alembic analisado diretamente no texto: um head `pt20260925a`, sem `down_revision` ausente. Migrations não foram aplicadas nem comparadas ao banco.
- Cookies HttpOnly/Secure em produção, refresh opaco com hashes e rotação/revogação, isolamento SSR por Request, gates de autoria clínica, arquivos com IDs escopados, webhooks com segredo/assinatura e respostas API no-store estão presentes nos caminhos examinados.
- Cancelamento/baixa/estorno financeiros examinados usam locks; o web omite `serviceId` quando preserva o serviço agendado, permitindo usar o snapshot de preço. Esses candidatos foram descartados como achados.
- Testes, typecheck, lint, build e E2E não foram executados. Nenhuma contagem antiga de testes aprovados foi reutilizada como validação atual. A CI declara PostgreSQL descartável para os testes de concorrência, mas seu resultado remoto não foi consultado.
- Não houve avaliação atual de CVEs, infraestrutura, backups, permissões S3, compliance PCI/LGPD ou comportamento em produção. As referências ASVS organizam a revisão; não constituem certificação de conformidade.

## Simplificação — ponytail-audit

Somente candidatos de complexidade, sem aplicar remoções. Contagem conservadora dos corpos das funções por AST, excluindo imports, espaços e testes.

- `delete:` worker legado `process_ai_job` (24 linhas); os produtores atuais de IA são síncronos e nenhuma chamada de enqueue desse job foi encontrada. Remover a função e seu registro depois de verificar que não há jobs legados pendentes no Redis. [worker.py:27](C:/Users/ed/Documents/projetos/korus-one-api/worker.py:27)
- `delete:` gerador antigo de refresh JWT (11 linhas), sem chamadores na árvore pesquisada; o fluxo atual usa `create_refresh_session`. Substituição: nenhuma. [security.py:43](C:/Users/ed/Documents/projetos/korus-one-api/app/core/security.py:43)
- `delete:` método `ScoringSession.to_assessment_fields` que apenas lança erro (3 linhas), sem chamadas sobre essa classe; manter o método usado de `NormalizedScores`. [scoring_session.py:127](C:/Users/ed/Documents/projetos/korus-one-api/app/services/scoring_session.py:127)
- `delete:` wrapper não utilizado `scores_to_assessment_fields` (2 linhas); consumidores já usam `NormalizedScores.to_assessment_fields`. Substituição: nenhuma. [assessment_scoring.py:41](C:/Users/ed/Documents/projetos/korus-one-api/app/services/assessment_scoring.py:41)

net: -40 linhas, -0 dependências possíveis. Dessas, 24 dependem de confirmar o esvaziamento de jobs legados. Não remover ARQ: outros jobs e crons continuam ativos.

## Ordem sugerida e limites de remediação

Corrigir o fallback de billing e as corridas Google primeiro; em seguida vínculo OAuth, coerência de scoring e contexto com título opcional; completar limites de upload e validação de PATCH. Usar os serviços, schemas e leitores já existentes, com testes focados dos cenários acima. Mudanças de contrato devem ser espelhadas manualmente no web.

Nenhum patch foi aplicado ou preparado. A política `fixer-policy.md` referenciada pela skill não existe no caminho relativo informado (`C:/Users/ed/.agents/docs/fixer-policy.md`); portanto não houve classificação formal de autoaplicação. Para futura implementação, revisão deve avaliar contratos, locks e migrações necessárias e incluir evidência dos testes; rollback deve preservar filas, registros clínicos e financeiros existentes. Este relatório não autoriza alteração desses dados ou deploy.
