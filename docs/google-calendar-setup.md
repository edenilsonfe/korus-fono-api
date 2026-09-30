# Google Agenda — configuração OAuth

O KorusFono usa OAuth 2.0 server-side para espelhar a agenda no calendário
principal do profissional. A plataforma continua sendo a fonte da verdade: cria,
atualiza e remove apenas eventos vinculados a agendamentos Korus. Alterações feitas
diretamente no Google não são importadas.

## 1. Criar o projeto e habilitar a API

1. No [Google Cloud Console](https://console.cloud.google.com/), crie um projeto
   separado para desenvolvimento/testes. Para a publicação, prefira outro projeto
   de produção.
2. Abra **APIs e serviços > Biblioteca**, procure **Google Calendar API** e clique
   em **Ativar**.

## 2. Configurar o Google Auth Platform

Em **Google Auth Platform**:

1. **Branding**: use `KorusFono`, um e-mail de suporte monitorado, a página inicial
   pública e a política de privacidade (`<URL_DO_WEB>/privacidade`). O domínio das
   URLs precisa estar verificado para publicação.
2. **Audience**: escolha **External**. Durante o desenvolvimento, mantenha
   **Testing** e adicione os e-mails que testarão a integração. Tokens de apps
   externos em Testing expiram em 7 dias; isso é esperado e não serve para produção.
3. **Data Access > Add or remove scopes**: adicione somente:

   ```text
   https://www.googleapis.com/auth/calendar.events.owned
   ```

   Não adicione `calendar`, `calendar.readonly`, `calendarlist` ou escopos de perfil:
   a implementação não precisa deles.

## 3. Criar o cliente OAuth

Em **Clients > Create client**:

- Tipo: **Web application**.
- Nome sugerido: `KorusFono Calendar Web`.
- **Authorized JavaScript origins**: não é necessário para este fluxo server-side.
- **Authorized redirect URIs** (a correspondência é exata, inclusive protocolo e
  barra final):

  ```text
  http://localhost:8080/api/v1/google-calendar/oauth/callback
  https://<URL_DO_WEB>/api/v1/google-calendar/oauth/callback
  ```

Copie o Client ID e o Client Secret. Nunca coloque o Client Secret no frontend.

O callback usa `FRONTEND_URL`, passa pelo proxy `/api` do web e recebe o cookie
HttpOnly do navegador que iniciou a conexão. Configure a porta/origem real do web
em desenvolvimento. Antes de publicar a revisão `gc20260930a`, cadastre a URL do
web no cliente Google; a URL antiga da API não atende ao novo vínculo de navegador.
O `state` expira em dez minutos, tem consumo único no banco e exige a versão
vigente da conta. Esse vínculo segue a [orientação OAuth do Google](https://developers.google.com/identity/protocols/oauth2/web-server#creatingclient).

## 4. Configurar API e worker

Gere uma chave Fernet exclusiva:

```powershell
py -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Defina no serviço `api` do Railway:

```text
APP_PUBLIC_URL=https://<URL_PUBLICA_DA_API>
FRONTEND_URL=https://<URL_DO_WEB>
GOOGLE_CALENDAR_CLIENT_ID=<client-id>
GOOGLE_CALENDAR_CLIENT_SECRET=<client-secret>
GOOGLE_CALENDAR_CREDENTIAL_ENCRYPTION_KEY=<chave-fernet>
```

Repita as três variáveis `GOOGLE_CALENDAR_*` no serviço `worker`, com os mesmos
valores. A API despacha imediatamente; o worker recupera filas interrompidas a cada
15 minutos. Não troque a chave Fernet enquanto conexões estiverem ativas — se for
necessário rotacioná-la, os profissionais terão de reconectar o Google.

Antes de iniciar os processos novos, aplique as migrations até `gc20260930a` e
confirme que API e worker usam a mesma revisão. Na atualização, drene/interrompa
os dispatchers antigos antes de iniciar os novos: o código antigo não conhece a
posse e a versão da fila. As credenciais Google existentes são preservadas.

A posse de processamento expira após dez minutos; reenfileirar uma edição ou um
cancelamento preserva essa posse e incrementa a versão. A conclusão antiga não
marca a nova versão como sincronizada. O ID de criação remoto é salvo antes do
POST e reutilizado em retries de resultado incerto, conforme o suporte a
[IDs de eventos definidos pelo cliente](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert).

## 5. Testar e publicar

1. Em Testing, adicione seu Gmail como test user.
2. No KorusFono, abra **Configurações > Google Agenda > Conectar**.
3. Autorize o acesso, crie um agendamento, reagende e cancele; confira cada mudança
   no calendário principal.
4. Antes de liberar para clientes, mude a audiência para **In production** e envie
   a verificação OAuth. O escopo de eventos é sensível e usuários externos verão
   alerta/limitação enquanto a verificação não estiver aprovada.

Texto sugerido para a justificativa do escopo:

> O KorusFono solicita acesso somente para criar, atualizar e excluir no calendário
> principal do profissional os eventos correspondentes aos agendamentos gerenciados
> por ele na plataforma. O KorusFono é a fonte da verdade, não importa eventos do
> Google e identifica apenas os eventos que ele próprio criou. O nome do paciente
> permanece oculto por padrão e só é incluído mediante opção explícita do usuário.

Na gravação para revisão, mostre: login no KorusFono, botão Conectar, tela de
consentimento, criação/reagendamento/cancelamento e desconexão. A política de
privacidade deve explicar finalidade, armazenamento criptografado do refresh token,
revogação e exclusão da conexão.
