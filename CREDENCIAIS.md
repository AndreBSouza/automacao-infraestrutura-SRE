# Credenciais — o que preencher no `.env`

Tudo que não depende de você já está pronto: dependências instaladas, Postgres
no ar com pgvector, schema aplicado, `APP_SECRET_KEY` gerada.

Falta apenas preencher as credenciais abaixo. A qualquer momento, rode para ver
o que ainda está pendente:

```bash
.venv\Scripts\python.exe scripts\check_setup.py
```

E, quando achar que terminou, valide a conectividade real:

```bash
.venv\Scripts\python.exe scripts\check_setup.py --connect
```

---

## Obrigatórias — sem elas a aplicação não sobe

### 1. `ANTHROPIC_API_KEY`

Console da Anthropic → Settings → API Keys → Create Key.
https://console.anthropic.com/settings/keys

> Esta é a chave que a parte automatizada consome. O plano de assinatura do
> Claude não cobre uso automatizado — por isso o vigia precisa da API paga.

### 2. `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`

Portal Azure → **Microsoft Entra ID** → App registrations → New registration:

- **Name**: `SAI — Copiloto de Infraestrutura`
- **Supported account types**: Single tenant
- **Redirect URI**: tipo `Web`, valor exatamente igual ao `ENTRA_REDIRECT_URI`
  do `.env` (padrão: `http://localhost:8000/auth/callback`)

Depois de criar:

| Onde achar | Vai para |
|---|---|
| Overview → Directory (tenant) ID | `ENTRA_TENANT_ID` |
| Overview → Application (client) ID | `ENTRA_CLIENT_ID` |
| Certificates & secrets → New client secret → copie o **Value** | `ENTRA_CLIENT_SECRET` |

> O **Value** do secret só aparece uma vez. Se perder, gere outro.

Em **API permissions**, garanta `User.Read` (Delegated) — costuma já vir por padrão.

Pelo CLI:

```bash
az ad app create --display-name "SAI" \
  --web-redirect-uris "http://localhost:8000/auth/callback"
```

---

## Opcionais — cada uma habilita um sistema

A aplicação sobe sem elas; cada ausência apenas desativa as tools daquele
sistema. Comece pelo que você mais usa no dia a dia.

### Azure (VMs, App Services, SQL, métricas)

```bash
# Leitura — comece só por aqui
az ad sp create-for-rbac --name sai-reader --role Reader \
  --scopes /subscriptions/<SUBSCRIPTION_ID>
```

A saída traz `tenant`, `appId` e `password` → `AZURE_TENANT_ID`,
`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`. O `AZURE_SUBSCRIPTION_ID` você já tem.

Workspace do Log Analytics (é um GUID **diferente** do da subscription):

```bash
az monitor log-analytics workspace show -g <rg> -n <nome> --query customerId -o tsv
```

Para **ações de escrita**, crie um segundo service principal com papel restrito
apenas aos recursos que ele pode alterar — nunca `Contributor` na subscription
inteira:

```bash
az ad sp create-for-rbac --name sai-writer --role "Virtual Machine Contributor" \
  --scopes /subscriptions/<id>/resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/<vm>
```

→ `AZURE_WRITE_CLIENT_ID`, `AZURE_WRITE_CLIENT_SECRET`

### Azure DevOps

`https://dev.azure.com/<org>/_usersSettings/tokens` → New Token, com escopos:
`Code (Read)`, `Build (Read & execute)`, `Release (Read & execute)`,
`Work Items (Read)`.

→ `AZURE_DEVOPS_ORG`, `AZURE_DEVOPS_PROJECT`, `AZURE_DEVOPS_PAT`

### SQL Server

Dois logins separados. O de leitura:

```sql
CREATE LOGIN sai_reader WITH PASSWORD = '<senha forte>';
GRANT VIEW SERVER STATE TO sai_reader;   -- necessário para as DMVs
USE [SuaBase];
CREATE USER sai_reader FOR LOGIN sai_reader;
ALTER ROLE db_datareader ADD MEMBER sai_reader;
```

O de escrita (só se for usar restore/manutenção de índices) recebe permissões
próprias, e **nunca** as mesmas do de leitura.

```
SQL_READ_CONN_STRING=DRIVER={ODBC Driver 18 for SQL Server};SERVER=sql-prod-01;DATABASE=master;UID=sai_reader;PWD=<senha>;TrustServerCertificate=yes
```

> Precisa do [ODBC Driver 18](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)
> instalado na máquina onde o SAI roda.

### Grafana

Administration → Service accounts → Add service account (papel `Viewer`) → Add
token. → `GRAFANA_BASE_URL`, `GRAFANA_API_TOKEN`

### Zabbix

User settings → API tokens → Create token, com um usuário que tenha leitura nos
host groups relevantes. → `ZABBIX_BASE_URL`, `ZABBIX_API_TOKEN`

### Linux / Nginx

```bash
ssh-keygen -t ed25519 -f ~/.ssh/sai_automation -C "sai-automation" -N ""
```

→ `LINUX_SSH_USER`, `LINUX_SSH_PRIVATE_KEY_PATH`, `KNOWN_LINUX_HOSTS`
(separados por vírgula)

> **Antes de apontar para qualquer host de produção, leia o [SUDOERS.md](SUDOERS.md).**
> A conta SSH precisa de sudo restrito a uma lista explícita de comandos. Sem
> isso, a segurança do sistema depende só do código da aplicação.

### F5 / WAF

Crie um usuário de API dedicado no BIG-IP com papel limitado (não `Administrator`).
→ `F5_BASE_URL`, `F5_API_USER`, `F5_API_PASSWORD`

### Embeddings (base de conhecimento)

https://dash.voyageai.com/ → API Keys. → `EMBEDDINGS_API_KEY`

Depois, coloque seus runbooks e scripts em `docs/` e rode:

```bash
.venv\Scripts\python.exe -m sai.rag.ingest --docs-dir docs/
```

### Teams / Slack (aprovação pelo chat)

Opcional — as aprovações funcionam normalmente pelo web app sem isso.

**Teams**: crie um Azure Bot, aponte o messaging endpoint para
`https://<seu-host>/webhooks/teams`. → `TEAMS_BOT_APP_ID`,
`TEAMS_BOT_APP_PASSWORD`, `TEAMS_WEBHOOK_URL`

**Slack**: crie um app em https://api.slack.com/apps, habilite Interactivity
apontando para `https://<seu-host>/webhooks/slack`. → `SLACK_BOT_TOKEN`,
`SLACK_SIGNING_SECRET`

> Enquanto essas variáveis estiverem vazias, as aprovações por chat ficam
> **desabilitadas** — nunca abertas. Não existe modo "pular verificação".

---

## Depois de preencher

**1. Confirme que está tudo certo:**

```bash
.venv\Scripts\python.exe scripts\check_setup.py --connect
```

**2. Crie o primeiro admin.** O login via Entra ID cria todo usuário novo como
`viewer` — promoção de papel nunca é self-service. Sem um admin, nenhuma ação
`high`/`critical` pode ser aprovada:

```bash
az ad user show --id voce@empresa.com --query id -o tsv   # pega o Object ID

.venv\Scripts\python.exe scripts\seed_admin.py \
  --email voce@empresa.com \
  --entra-object-id <object-id> \
  --name "Seu Nome"
```

**3. Revise o `allowlist.yaml`.** Ele vem com exemplos fictícios e nada
auto-executa até você preenchê-lo com hosts e serviços reais. Só entram aí
ações de baixo risco que você aceite ver executadas sem aprovação prévia.

**4. Suba:**

```bash
.venv\Scripts\python.exe -m uvicorn sai.api.main:app --reload
```

```bash
cd frontend && npm run dev
```

Acesse http://localhost:5173.
