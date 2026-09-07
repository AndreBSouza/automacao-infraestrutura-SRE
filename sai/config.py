"""Centralized configuration for SAI, loaded from environment variables.

All secrets/endpoints are read via pydantic-settings from the process
environment (populated from a local `.env` in dev, or injected at runtime
by Azure Key Vault + Managed Identity in production — see README.md
"SECURITY" section). Nothing here should ever contain a real credential;
every field either has a safe non-secret default or is required and must
be supplied via the environment.

Per SPEC.md section 10.3: in production, secrets must come from Key Vault
via Managed Identity, not from a committed `.env`. `.env` files are for
local development only and MUST NOT be committed (see .gitignore).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BusinessHoursConfig(BaseSettings):
    """Business-hours window used to gate the proactive scheduler (vigia).

    Per SPEC.md section 9: the vigia only runs within this window; outside
    of it the job is paused so it doesn't burn LLM budget or create noise.
    """

    model_config = SettingsConfigDict(env_prefix="BUSINESS_HOURS_", extra="ignore")

    start_hour: int = Field(default=8, ge=0, le=23)
    end_hour: int = Field(default=18, ge=0, le=23)
    timezone: str = Field(default="America/Sao_Paulo")
    # Comma-separated weekday numbers, Monday=0 .. Sunday=6
    weekdays: str = Field(default="0,1,2,3,4")

    @property
    def weekday_set(self) -> set[int]:
        return {int(d) for d in self.weekdays.split(",") if d.strip() != ""}


class Settings(BaseSettings):
    """Top-level application settings.

    Every field is documented in `.env.example` at the project root.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- App/general ----
    app_env: Literal["dev", "staging", "prod"] = Field(default="dev", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    secret_key: str = Field(
        default="dev-only-change-me",
        alias="APP_SECRET_KEY",
        description="Used to sign app-issued JWTs. Must be set to a strong random value in prod.",
    )
    cors_allowed_origins: str = Field(default="http://localhost:5173", alias="CORS_ALLOWED_ORIGINS")
    # Where /auth/callback sends the browser after a successful login. The
    # token is handed over in the URL *fragment* (#access_token=...), which
    # browsers never send to the server — so it stays out of access logs,
    # proxy logs and Referer headers, unlike a query string. Leave blank to
    # keep returning JSON (useful for CLI/API-only usage).
    frontend_url: str = Field(default="http://localhost:5173", alias="FRONTEND_URL")

    # ---- Postgres ----
    database_url: str = Field(
        default="postgresql+asyncpg://sai:sai@localhost:5432/sai",
        alias="DATABASE_URL",
        description="Async SQLAlchemy connection string for the primary Postgres DB (with pgvector).",
    )

    # ---- Anthropic / LLM ----
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-5", alias="ANTHROPIC_MODEL")
    anthropic_max_tokens: int = Field(default=8192, alias="ANTHROPIC_MAX_TOKENS")
    # Modelo usado pelo vigia (triagem automática de alertas), separado do
    # modelo do chat. Triagem é trabalho de volume e relativamente rotineiro;
    # o chat é onde se lê o raciocínio em profundidade.
    #
    # ATENÇÃO ao custo: caches de prompt são por modelo. Usar dois modelos
    # cria dois caches independentes — cada um paga a própria escrita e recebe
    # metade das leituras. Com volume baixo isso pode custar mais do que
    # economiza. Deixe igual a ANTHROPIC_MODEL para manter um cache só.
    anthropic_vigia_model: str = Field(default="claude-sonnet-5", alias="ANTHROPIC_VIGIA_MODEL")
    # Modelo de escalonamento: acionado pontualmente, por pedido explícito do
    # operador, quando a resposta do modelo padrão não convence. Não há
    # escalonamento automático — a decisão é sempre humana, para que o custo
    # extra seja sempre uma escolha consciente.
    anthropic_escalation_model: str = Field(
        default="claude-opus-5", alias="ANTHROPIC_ESCALATION_MODEL"
    )
    # TTL do cache do prompt (system prompt + definições de tools — a parte
    # fixa e cara de cada requisição).
    #
    # A escolha depende do intervalo entre requisições que compartilham esse
    # prefixo, medido de início a início:
    #   < 5 min   -> "5m": cada requisição renova o cache; estritamente mais barato
    #   5-60 min  -> "1h": a única janela em que a escrita 2x se paga
    #   > 1 h     -> nenhum ajuda muito; aceite o miss
    #
    # O padrão aqui é "1h" porque o perfil esperado (poucos incidentes por dia
    # + perguntas esparsas no chat, dentro do horário comercial) cai na faixa
    # do meio. Se o volume subir a ponto de as requisições ficarem a menos de
    # 5 min umas das outras, troque para "5m": aí o TTL longo só custa a
    # escrita dobrada, sem benefício.
    anthropic_cache_ttl: Literal["5m", "1h"] = Field(default="1h", alias="ANTHROPIC_CACHE_TTL")

    # ---- Embeddings (Voyage AI — see rag/ingest.py docstring for rationale) ----
    embeddings_api_base: str = Field(default="https://api.voyageai.com/v1", alias="EMBEDDINGS_API_BASE")
    embeddings_api_key: str = Field(default="", alias="EMBEDDINGS_API_KEY")
    embeddings_model: str = Field(default="voyage-3-large", alias="EMBEDDINGS_MODEL")
    embeddings_dimensions: int = Field(default=1536, alias="EMBEDDINGS_DIMENSIONS")

    # ---- Azure ----
    azure_tenant_id: str = Field(default="", alias="AZURE_TENANT_ID")
    azure_client_id: str = Field(default="", alias="AZURE_CLIENT_ID")
    azure_client_secret: str = Field(default="", alias="AZURE_CLIENT_SECRET")
    azure_subscription_id: str = Field(default="", alias="AZURE_SUBSCRIPTION_ID")
    # Separate, more privileged credential used ONLY for approved write actions
    # (SPEC 10.2 — segregation of read/write credentials).
    azure_write_client_id: str = Field(default="", alias="AZURE_WRITE_CLIENT_ID")
    azure_write_client_secret: str = Field(default="", alias="AZURE_WRITE_CLIENT_SECRET")
    # Log Analytics workspace GUID backing `azure_get_activity_log`. This is a
    # DIFFERENT identifier from the subscription id — find it under the
    # workspace's "Workspace ID" in the portal, or via
    # `az monitor log-analytics workspace show --query customerId`.
    azure_log_analytics_workspace_id: str = Field(default="", alias="AZURE_LOG_ANALYTICS_WORKSPACE_ID")

    # ---- Entra ID (OIDC login for the app itself) ----
    entra_tenant_id: str = Field(default="", alias="ENTRA_TENANT_ID")
    entra_client_id: str = Field(default="", alias="ENTRA_CLIENT_ID")
    entra_client_secret: str = Field(default="", alias="ENTRA_CLIENT_SECRET")
    entra_redirect_uri: str = Field(default="http://localhost:8000/auth/callback", alias="ENTRA_REDIRECT_URI")
    # Bootstrap do primeiro admin.
    #
    # Existe para resolver o ovo-e-galinha: promoção de papel exige um admin,
    # e no banco vazio não há nenhum. Com isso ligado, o PRIMEIRO usuário a
    # fazer login vira admin; todos os seguintes entram como 'viewer' normal.
    #
    # Só tem efeito com a tabela `users` VAZIA — não é um interruptor que
    # transforma qualquer login em admin. Ainda assim, desligue depois de
    # criar o seu usuário: é a diferença entre uma porta aberta uma vez e uma
    # porta destrancada. Toda promoção por esse caminho vai para a auditoria.
    bootstrap_first_admin: bool = Field(default=False, alias="BOOTSTRAP_FIRST_ADMIN")

    # ---- Azure DevOps ----
    devops_org: str = Field(default="", alias="AZURE_DEVOPS_ORG")
    devops_project: str = Field(default="", alias="AZURE_DEVOPS_PROJECT")
    devops_pat: str = Field(default="", alias="AZURE_DEVOPS_PAT")

    # ---- SQL Server ----
    sql_read_conn_string: str = Field(default="", alias="SQL_READ_CONN_STRING")
    sql_write_conn_string: str = Field(default="", alias="SQL_WRITE_CONN_STRING")

    # ---- Grafana ----
    grafana_base_url: str = Field(default="", alias="GRAFANA_BASE_URL")
    grafana_api_token: str = Field(default="", alias="GRAFANA_API_TOKEN")

    # ---- Zabbix ----
    zabbix_base_url: str = Field(default="", alias="ZABBIX_BASE_URL")
    zabbix_api_token: str = Field(default="", alias="ZABBIX_API_TOKEN")

    # ---- Linux/SSH ----
    linux_ssh_user: str = Field(default="sai-automation", alias="LINUX_SSH_USER")
    linux_ssh_private_key_path: str = Field(default="/run/secrets/sai_ssh_key", alias="LINUX_SSH_PRIVATE_KEY_PATH")

    # ---- F5 BIG-IP ----
    f5_base_url: str = Field(default="", alias="F5_BASE_URL")
    f5_api_user: str = Field(default="", alias="F5_API_USER")
    f5_api_password: str = Field(default="", alias="F5_API_PASSWORD")

    # ---- Teams / Slack notifications ----
    teams_webhook_url: str = Field(default="", alias="TEAMS_WEBHOOK_URL")
    teams_bot_app_id: str = Field(default="", alias="TEAMS_BOT_APP_ID")
    teams_bot_app_password: str = Field(default="", alias="TEAMS_BOT_APP_PASSWORD")
    slack_bot_token: str = Field(default="", alias="SLACK_BOT_TOKEN")
    slack_signing_secret: str = Field(default="", alias="SLACK_SIGNING_SECRET")

    # ---- Approval engine ----
    approval_timeout_minutes_low: int = Field(default=60, alias="APPROVAL_TIMEOUT_MINUTES_LOW")
    approval_timeout_minutes_medium: int = Field(default=30, alias="APPROVAL_TIMEOUT_MINUTES_MEDIUM")
    approval_timeout_minutes_high: int = Field(default=15, alias="APPROVAL_TIMEOUT_MINUTES_HIGH")
    approval_timeout_minutes_critical: int = Field(default=10, alias="APPROVAL_TIMEOUT_MINUTES_CRITICAL")
    allowlist_path: str = Field(default="allowlist.yaml", alias="ALLOWLIST_PATH")

    # ---- Inventory discovery targets ----
    # Comma-separated lists of known hosts/databases the inventory sync job
    # (sai/inventory/sync.py) walks each cycle. In production these would
    # typically be discovered dynamically (e.g. from a CMDB or from Azure
    # Resource Graph results) rather than hardcoded — kept simple here.
    known_linux_hosts: str = Field(default="", alias="KNOWN_LINUX_HOSTS")
    known_sql_databases: str = Field(default="", alias="KNOWN_SQL_DATABASES")

    @property
    def known_linux_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.known_linux_hosts.split(",") if h.strip()]

    @property
    def known_sql_databases_list(self) -> list[str]:
        return [d.strip() for d in self.known_sql_databases.split(",") if d.strip()]

    # ---- Scheduler / vigia ----
    vigia_poll_interval_seconds: int = Field(default=180, alias="VIGIA_POLL_INTERVAL_SECONDS")
    inventory_sync_interval_hours: int = Field(default=6, alias="INVENTORY_SYNC_INTERVAL_HOURS")

    business_hours: BusinessHoursConfig = Field(default_factory=BusinessHoursConfig)

    @field_validator("anthropic_max_tokens")
    @classmethod
    def _clamp_max_tokens(cls, v: int) -> int:
        return max(1024, min(v, 64000))


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()
