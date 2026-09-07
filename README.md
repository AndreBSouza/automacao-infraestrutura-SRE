# SAI — Sistema de Automação e Copiloto de Infraestrutura

Internal infrastructure copilot implementing `SPEC.md`: continuous
discovery/inventory of Azure, Azure DevOps, SQL Server, Grafana, Zabbix,
Linux, Nginx, and F5(WAF); a conversational diagnostic chat backed by
Claude (Anthropic API) with tool use; and a governed remediation
workflow where every write action requires human approval unless it
matches a small, versioned, low-risk allowlist.

This README covers setup, credentials, running locally, testing, and the
security posture. See `SPEC.md` for the full technical specification.

---

## Início rápido

Um comando prepara tudo que não depende de credenciais — venv, dependências,
Postgres com pgvector, migrations, `.env` com `APP_SECRET_KEY` já gerada — e
termina listando exatamente o que falta preencher:

```powershell
.\scripts\bootstrap.ps1
```

Depois, preencha as credenciais seguindo o **[CREDENCIAIS.md](CREDENCIAIS.md)**
e confira:

```powershell
.venv\Scripts\python.exe scripts\check_setup.py --connect
```

Só o núcleo (chave da Anthropic + registro no Entra ID) é obrigatório. Cada
sistema — Azure, Zabbix, Grafana, SQL Server, F5 — é opcional e independente:
sem credencial, apenas as tools daquele sistema ficam indisponíveis.

> **Antes de apontar para hosts de produção**, leia o **[SUDOERS.md](SUDOERS.md)**.
> A conta SSH precisa de sudo restrito a uma lista explícita de comandos; sem
> isso, a segurança depende apenas do código da aplicação.

---

## 1. Prerequisites

- Python 3.12+
- PostgreSQL 16 with the `pgvector` extension available (the bundled
  `pgvector/pgvector:pg16` Docker image already has it)
- Docker + Docker Compose (optional, but the fastest path to a working
  Postgres instance)

## 2. Database setup

### Option A — Docker Compose (recommended for local dev)

```bash
docker compose up -d postgres
```

This starts Postgres 16 with pgvector pre-installed, exposed on
`localhost:5432`, user/db `sai`/`sai` (change in `docker-compose.yml` for
anything beyond local dev).

### Option B — existing Postgres instance

```sql
CREATE DATABASE sai;
\c sai
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
```

(The pgcrypto/vector extensions are also created automatically by the
first Alembic migration, but your DB role needs `CREATE EXTENSION`
privileges for that to succeed — a superuser may need to run the above
once.)

### Run migrations

```bash
pip install -r requirements.txt
alembic upgrade head
```

Alembic reads the real connection string from `DATABASE_URL` (via
`sai/config.py`), not from `alembic.ini` directly — see
`sai/db/migrations/env.py`.

## 3. Configure `.env`

```bash
cp .env.example .env
```

Fill in every value you need for the connectors you intend to use. You do
**not** need every credential to start the app — connectors fail their own
`healthcheck()` (visible at `GET /health`) gracefully rather than crashing
the process. `.env` is for **local development only**; it is already
gitignored. **Never commit it.**

### Obtaining each credential

| Credential | How to obtain |
|---|---|
| **Anthropic API key** | https://console.anthropic.com/settings/keys |
| **Voyage AI key** (embeddings) | https://dash.voyageai.com/ — see "Why Voyage AI" below |
| **Azure Service Principal (read)** | `az ad sp create-for-rbac --name sai-reader --role Reader --scopes /subscriptions/<sub-id>` — copy `appId`/`password`/`tenant` into `AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET`/`AZURE_TENANT_ID` |
| **Azure Service Principal (write)** | Create a second SP; grant only the specific roles needed on the specific resources where you enable write actions (e.g. `Virtual Machine Contributor` scoped to one resource group) — never `Owner`/`Contributor` at subscription scope |
| **Entra ID app registration** (login) | Azure Portal → Entra ID → App registrations → New registration; add a Web redirect URI matching `ENTRA_REDIRECT_URI`; create a client secret |
| **Azure DevOps PAT** | Azure DevOps → User settings → Personal Access Tokens; scopes: `Code (Read)`, `Build (Read & Execute)`, `Release (Read & Execute)`, `Work Items (Read)` |
| **SQL Server accounts** | Create two logins: one with `db_datareader` + `VIEW SERVER STATE` for `SQL_READ_CONN_STRING`, one with write/restore permissions for `SQL_WRITE_CONN_STRING` |
| **Zabbix API token** | Zabbix UI → Administration → Users → (your user) → API tokens |
| **Grafana Service Account token** | Grafana UI → Administration → Service accounts → Add service account token |
| **F5 API user** | BIG-IP → Access → Users → create a user with a restricted role (not `admin`) for iControl REST |
| **Teams Incoming Webhook** | Teams channel → Connectors → Incoming Webhook |
| **Slack Bot token / signing secret** | https://api.slack.com/apps → your app → OAuth & Permissions / Basic Information |

### Why Voyage AI for embeddings?

Anthropic does not currently offer a first-party embeddings endpoint.
Anthropic's own documentation recommends Voyage AI as an embeddings
partner, so `sai/rag/ingest.py::get_embedding()` calls Voyage AI's HTTP API
via `httpx`. The function is intentionally isolated so swapping to any
other OpenAI-compatible embeddings provider only requires changing
`EMBEDDINGS_API_BASE` / `EMBEDDINGS_API_KEY` / `EMBEDDINGS_MODEL` (and, if
the response shape differs, that one function).

## 4. Run locally

```bash
uvicorn sai.api.main:app --reload
```

This starts the FastAPI app, which on startup also starts the APScheduler
jobs (vigia threshold polling + periodic inventory sync) in-process — see
"Deviations from SPEC" below for why APScheduler instead of Celery.

`GET http://localhost:8000/health` reports per-connector connectivity.

### Ingest your runbooks (RAG)

```bash
python -m sai.rag.ingest --docs-dir docs/
```

Drop `.md`/`.txt`/`.py`/`.sh`/`.sql` files into `docs/` first — see
`docs/README.md`.

### Frontend (chat, aprovações, alertas, inventário)

```bash
cd frontend && npm install && npm run dev
```

Abre em `http://localhost:5173` e faz proxy de `/api` para o backend em
`localhost:8000`, de modo que o navegador enxerga uma única origem e CORS não
entra em jogo no ambiente local. Em produção, aponte `VITE_API_BASE` para a URL
pública da API e sirva `frontend/dist` como conteúdo estático.

O login redireciona para o Entra ID; o token volta no *fragmento* da URL
(`#access_token=...`), que os navegadores nunca enviam ao servidor — assim ele
não aparece em logs de acesso, logs de proxy nem em cabeçalhos `Referer`. É
guardado em `sessionStorage` (não `localStorage`), de modo que expira ao fechar
a aba, e não fica em uma estação compartilhada.

### Run the scheduler standalone

The scheduler (`sai/scheduler/vigia.py::start_scheduler`) is started
automatically by `sai.api.main`'s lifespan handler. If you want a
dedicated process instead of running it inside the API process, import
and call `start_scheduler(...)` from a small standalone script — see the
docstring in `sai/scheduler/vigia.py` for the exact call signature.

## 5. Run tests

```bash
pytest
```

Frontend:

```bash
cd frontend && npm test
```

Cobertura por área:

| Arquivo | O que trava |
|---|---|
| `test_approval_engine.py` | SPEC 13.3 — tool `high`/`critical` nunca executa sem `status=='approved'` |
| `test_webhook_security.py` | Autenticidade + atribuição das aprovações vindas de Teams/Slack |
| `test_api_webhook_endpoints.py` | Os guards acima estão realmente ligados nas rotas HTTP |
| `test_prompt_injection.py` | SPEC 13.4 — conteúdo externo não vira instrução |
| `test_rollback.py` | SPEC 13.6 — toda tool de alto risco tem posição de rollback, e ela reverte de fato |
| `test_scheduler_dedup.py` | SPEC 13.5 — deduplicação sob rajada; nada roda fora do horário comercial |
| `test_reports.py` | Relatórios geram XLSX/PDF válidos, com bordas de período |
| `frontend/src/__tests__/` | Gate de confirmação em ações críticas; parsing NDJSON do stream |

Tests never make real network/SSH/SQL/Anthropic calls — connectors are
constructed with dummy settings and their I/O is mocked/monkeypatched (see
`sai/tests/conftest.py`). The mandatory regression test from SPEC.md
section 13.3 lives in `sai/tests/test_approval_engine.py` — it asserts a
`high`/`critical` tool can never execute without `Action.status ==
'approved'`. `sai/tests/test_prompt_injection.py` covers SPEC 13.4.

## 6. SECURITY (read this before deploying anywhere beyond a laptop)

1. **Least privilege for every service account.** The Azure Reader SP, the
   SQL read login, the Zabbix/Grafana tokens, and the F5 API user should
   all have the minimum permissions listed in `SPEC.md` section 5 — never
   broader "just in case" access.
2. **Never commit `.env`.** It's gitignored; keep it that way. In
   staging/prod, inject secrets via **Azure Key Vault + Managed Identity**,
   not via a checked-in file or a plaintext env var set by a human.
3. **Segregate read and write credentials** wherever the platform allows
   it (Azure, SQL Server) — `AZURE_WRITE_CLIENT_ID`/`SQL_WRITE_CONN_STRING`
   are deliberately separate config keys from their read counterparts.
4. **Review `allowlist.yaml` before enabling any auto-execute tool.** The
   example entries are clearly marked `EXAMPLE — DO NOT USE IN PRODUCTION
   WITHOUT REVIEW` and reference fake hostnames. Only low-risk tools are
   ever eligible for this file, and it is not editable via chat/API by
   design.
5. **RBAC**: `viewer` (read-only), `operator` (approve low/medium),
   `admin` (approve high/critical, and the only role that should have
   write access to `allowlist.yaml` in your deployment pipeline).
6. **MFA** is enforced by your Entra ID tenant's Conditional Access
   policies — this app relies on Entra ID for that, it does not implement
   its own MFA.
7. **Audit log is append-only.** In production, grant the application's
   database role `INSERT` only on `audit_log` — no `UPDATE`/`DELETE` — at
   the database permission level, not just in application code.
8. **Quarterly permission review** of every service account is a process
   requirement (SPEC 10.10), not something this codebase can enforce by
   itself — put it on a calendar.
9. Sanitize before you extend: any new tool added to the connectors layer
   must avoid putting secrets, connection strings, or customer PII into
   text sent to the LLM (`ToolResult.data`/`.error`).
10. **Aprovações via Teams/Slack exigem identidade mapeada.** Um clique em
    "Aprovar" só é aceito quando (a) a requisição é comprovadamente da
    plataforma — JWT do Bot Framework para Teams, assinatura HMAC para Slack
    — e (b) a pessoa que clicou é resolvida para um usuário real:
    - Teams → `users.entra_object_id` deve ser igual ao `from.aadObjectId`
    - Slack → `users.slack_user_id` deve ser igual ao member id (ex.: `U012ABC`)

    Ambos falham fechado: sem `TEAMS_BOT_APP_ID` ou sem
    `SLACK_SIGNING_SECRET` configurados, as aprovações por aquele canal ficam
    **desabilitadas** — não existe modo "pular verificação". Uma identidade
    não mapeada recebe 403. O RBAC continua valendo: aprovar uma ação
    `critical` exige `admin`, venha ela do web app ou do chat.

    > Isso corrige uma falha em que qualquer requisição que alcançasse
    > `/webhooks/teams` podia aprovar uma ação crítica (ex.: restore de banco),
    > com a auditoria registrando um admin arbitrário. Coberto por
    > `sai/tests/test_webhook_security.py` e
    > `sai/tests/test_api_webhook_endpoints.py`.

## 7. Deviations from SPEC.md (and why)

- **Celery+Redis → APScheduler.** SPEC section 3 suggests "Celery + Redis,
  ou Azure Functions com Timer Trigger". This build uses APScheduler
  (`AsyncIOScheduler`) running in-process with the FastAPI app instead —
  it needs no extra broker/worker infrastructure and is sufficient for the
  polling cadence described in SPEC section 9 (every 2-5 minutes) and the
  inventory sync cadence (every N hours). `docker-compose.yml` still
  includes a `redis` service as an optional component for anyone who later
  wants to move to Celery for higher-throughput job distribution.
- **Qdrant → pgvector only.** SPEC section 2's diagram lists
  "Vector DB (pgvector/Qdrant)"; this build uses pgvector exclusively (as
  SPEC section 3 itself recommends: "Evita mais um serviço"), so there is
  no separate vector database service to run.
- **Teams/Slack identity resolution está implementada e falha fechado.**
  As aprovações vindas do chat são autenticadas (JWT do Bot Framework /
  assinatura HMAC do Slack) e atribuídas à pessoa real que clicou, via
  `users.entra_object_id` / `users.slack_user_id` — ver seção 6, item 10.
  O que permanece como configuração de ambiente (fora deste repositório) é o
  registro do bot em si: criar o Azure Bot / app do Slack, apontar o
  messaging endpoint para `/webhooks/teams` (ou a Interactivity Request URL
  para `/webhooks/slack`) e preencher `TEAMS_BOT_APP_ID` /
  `SLACK_SIGNING_SECRET`. Enquanto isso não for feito, as aprovações por
  chat ficam desabilitadas — nunca abertas.
- **App Service (Web App) scaling** (`azure_scale_app_service`) uses
  `azure-mgmt-web`'s `WebSiteManagementClient`, imported lazily inside the
  method — this keeps the otherwise-required heavy dependency optional for
  deployments that don't scale App Service Plans, while still being real,
  working code once the package is installed (already listed in
  `requirements.txt`).
- **Known Linux hosts / SQL databases for inventory sync** are configured
  via simple comma-separated env vars (`KNOWN_LINUX_HOSTS`,
  `KNOWN_SQL_DATABASES`) rather than a dynamic CMDB lookup — SPEC section 8
  doesn't mandate a specific host-discovery mechanism, and a static list is
  the simplest correct implementation; swapping in dynamic discovery later
  only touches `sai/inventory/sync.py`.

## 8. Repository layout

```
sai/                  Backend (FastAPI + conectores + IA + governança)
sai/reports/          Geração de relatórios XLSX/PDF (SPEC 14)
scripts/bootstrap.ps1 Prepara todo o ambiente que não depende de credenciais
scripts/check_setup.py Diz exatamente o que falta configurar (--connect testa)
scripts/seed_admin.py Cria/promove o primeiro admin (login cria só 'viewer')
CREDENCIAIS.md        Onde obter cada credencial, uma a uma
SUDOERS.md            Sudo restrito nos hosts gerenciados — leia antes de prod
frontend/             SPA React/TypeScript (chat, aprovações, alertas,
                      inventário, relatórios)
infra/main.bicep      Infraestrutura do próprio SAI (Container Apps, Postgres,
                      Key Vault, ACR, App Insights) com RBAC de menor privilégio
azure-pipelines.yml   CI/CD — lint, testes, gate de governança, build, deploy
.azuredevops/         Template de stage de deploy reutilizado por ambiente
allowlist.yaml        Ações de baixo risco pré-aprovadas (versionado, revisar!)
SPEC.md               Especificação técnica completa
```

See the module docstrings — every package under `sai/` opens with a
docstring pointing back to the relevant `SPEC.md` section.

## 9. Deploy da infraestrutura

```bash
az deployment group create -g <resource-group> -f infra/main.bicep \
  -p environmentName=dev adminGroupObjectId=<object-id-do-grupo-admin> \
     postgresAdminPassword=<senha>
```

O Bicep provisiona tudo com Managed Identity: o Container App recebe apenas
`Key Vault Secrets User` (leitura de segredos, nunca escrita/exclusão) e
`AcrPull`. A aprovação manual para produção **não** está declarada no YAML,
e sim no Environment `sai-prod` do Azure DevOps (Pipelines > Environments >
Approvals and checks) — de propósito: assim a exigência de aprovação não pode
ser removida editando um arquivo em um PR.
