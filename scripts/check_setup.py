"""Diagnóstico de configuração: diz exatamente o que ainda falta preencher.

Uso:
    python scripts/check_setup.py            # verifica o .env
    python scripts/check_setup.py --connect  # também testa conectividade real

Sem `--connect`, nada de rede acontece: apenas verifica quais variáveis estão
vazias e classifica o impacto de cada ausência.

Códigos de saída:
    0 = pronto para subir (o núcleo obrigatório está preenchido)
    1 = falta algo obrigatório
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Cores só quando o terminal suporta; sem isso o log de CI fica ilegível.
_TTY = sys.stdout.isatty()
GREEN = "\033[32m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
BOLD = "\033[1m" if _TTY else ""
OFF = "\033[0m" if _TTY else ""

OK, MISSING, WARN = f"{GREEN}OK{OFF}", f"{RED}FALTA{OFF}", f"{YELLOW}--{OFF}"


@dataclass(frozen=True)
class Var:
    name: str
    required: bool
    consequence: str
    how: str = ""


@dataclass(frozen=True)
class Group:
    title: str
    vars: tuple[Var, ...]
    note: str = ""


GROUPS: tuple[Group, ...] = (
    Group(
        "Núcleo (obrigatório para a aplicação subir)",
        (
            Var("APP_SECRET_KEY", True, "os tokens de sessão não podem ser assinados",
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'),
            Var("DATABASE_URL", True, "sem banco, nada funciona",
                "docker compose up -d postgres  (o padrão do .env já aponta para ele)"),
            Var("ANTHROPIC_API_KEY", True, "o chat e o diagnóstico do vigia não funcionam",
                "https://console.anthropic.com/settings/keys"),
        ),
    ),
    Group(
        "Login dos usuários (Entra ID)",
        (
            Var("ENTRA_TENANT_ID", True, "ninguém consegue entrar na aplicação"),
            Var("ENTRA_CLIENT_ID", True, "ninguém consegue entrar na aplicação"),
            Var("ENTRA_CLIENT_SECRET", True, "ninguém consegue entrar na aplicação"),
        ),
        note="Registre um app em Entra ID > App registrations, com redirect URI "
             "igual a ENTRA_REDIRECT_URI.",
    ),
    Group(
        "Base de conhecimento (RAG)",
        (
            Var("EMBEDDINGS_API_KEY", False,
                "a ingestão de runbooks não roda; o agente responde sem o contexto da empresa",
                "https://dash.voyageai.com/"),
        ),
    ),
    Group(
        "Azure",
        (
            Var("AZURE_TENANT_ID", False, "as tools de Azure ficam indisponíveis"),
            Var("AZURE_CLIENT_ID", False, "as tools de Azure ficam indisponíveis"),
            Var("AZURE_CLIENT_SECRET", False, "as tools de Azure ficam indisponíveis"),
            Var("AZURE_SUBSCRIPTION_ID", False, "as tools de Azure ficam indisponíveis"),
            Var("AZURE_LOG_ANALYTICS_WORKSPACE_ID", False,
                "azure_get_activity_log falha (é o GUID do workspace, não da subscription)"),
            Var("AZURE_WRITE_CLIENT_ID", False,
                "sem credencial de escrita separada; ações de escrita no Azure ficam indisponíveis"),
            Var("AZURE_WRITE_CLIENT_SECRET", False, "idem acima"),
        ),
        note="az ad sp create-for-rbac --name sai-reader --role Reader "
             "--scopes /subscriptions/<id>",
    ),
    Group(
        "Azure DevOps",
        (
            Var("AZURE_DEVOPS_ORG", False, "as tools de DevOps ficam indisponíveis"),
            Var("AZURE_DEVOPS_PROJECT", False, "as tools de DevOps ficam indisponíveis"),
            Var("AZURE_DEVOPS_PAT", False, "as tools de DevOps ficam indisponíveis"),
        ),
        note="PAT com escopos Code:Read, Build:Read&Execute, Release:Read&Execute, Work Items:Read.",
    ),
    Group(
        "SQL Server",
        (
            Var("SQL_READ_CONN_STRING", False, "diagnóstico de SQL Server indisponível"),
            Var("SQL_WRITE_CONN_STRING", False,
                "restore/manutenção indisponíveis (use login separado do de leitura)"),
        ),
        note="Login de leitura precisa de db_datareader + VIEW SERVER STATE.",
    ),
    Group(
        "Monitoramento",
        (
            Var("GRAFANA_BASE_URL", False, "tools de Grafana indisponíveis"),
            Var("GRAFANA_API_TOKEN", False, "tools de Grafana indisponíveis"),
            Var("ZABBIX_BASE_URL", False, "o vigia não detecta nada via Zabbix"),
            Var("ZABBIX_API_TOKEN", False, "o vigia não detecta nada via Zabbix"),
        ),
    ),
    Group(
        "Linux / Nginx",
        (
            Var("LINUX_SSH_USER", False, "tools de Linux/Nginx indisponíveis"),
            Var("LINUX_SSH_PRIVATE_KEY_PATH", False, "tools de Linux/Nginx indisponíveis"),
            Var("KNOWN_LINUX_HOSTS", False, "o inventário não coleta dados dos hosts Linux"),
        ),
        note="ATENÇÃO: o sudoers de cada host deve limitar essa conta a uma lista "
             "explícita de comandos. Veja SUDOERS.md.",
    ),
    Group(
        "F5 / WAF",
        (
            Var("F5_BASE_URL", False, "tools de F5 e WAF indisponíveis"),
            Var("F5_API_USER", False, "tools de F5 e WAF indisponíveis"),
            Var("F5_API_PASSWORD", False, "tools de F5 e WAF indisponíveis"),
        ),
    ),
    Group(
        "Aprovação via chat",
        (
            Var("TEAMS_WEBHOOK_URL", False, "o card de aprovação não é enviado ao Teams"),
            Var("TEAMS_BOT_APP_ID", False,
                "aprovações pelo Teams ficam DESABILITADAS (fail-closed, nunca abertas)"),
            Var("SLACK_BOT_TOKEN", False, "a mensagem de aprovação não é enviada ao Slack"),
            Var("SLACK_SIGNING_SECRET", False,
                "aprovações pelo Slack ficam DESABILITADAS (fail-closed, nunca abertas)"),
        ),
        note="Sem isso as aprovações continuam funcionando normalmente pelo web app.",
    ),
)


def load_env(path: Path) -> dict[str, str]:
    """Lê o .env sem depender de libs — o ponto é diagnosticar antes de
    qualquer import pesado funcionar."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def is_placeholder(value: str) -> bool:
    """Um valor de exemplo não preenchido conta como ausente — senão o
    diagnóstico daria 'OK' para algo que vai falhar na primeira chamada."""
    v = value.strip().lower()
    return not v or "example.com" in v or v in {"changeme", "todo", "xxx", "<preencher>"}


def report(env: dict[str, str]) -> int:
    missing_required: list[Var] = []
    print(f"\n{BOLD}Verificação de configuração do SAI{OFF}\n")

    for group in GROUPS:
        print(f"{BOLD}{group.title}{OFF}")
        for var in group.vars:
            value = env.get(var.name, os.environ.get(var.name, ""))
            if is_placeholder(value):
                status = MISSING if var.required else WARN
                if var.required:
                    missing_required.append(var)
                detail = f"{DIM}→ {var.consequence}{OFF}"
            else:
                status = OK
                detail = ""
            print(f"  [{status:^14}] {var.name:38} {detail}")
        if group.note:
            print(f"  {DIM}{group.note}{OFF}")
        print()

    if missing_required:
        print(f"{RED}{BOLD}Falta preencher {len(missing_required)} variável(is) obrigatória(s):{OFF}")
        for var in missing_required:
            print(f"  • {BOLD}{var.name}{OFF} — {var.consequence}")
            if var.how:
                print(f"    {DIM}{var.how}{OFF}")
        print()
        return 1

    print(f"{GREEN}{BOLD}O núcleo obrigatório está preenchido.{OFF}")
    print(f"{DIM}As variáveis marcadas com -- são opcionais: cada uma apenas desativa{OFF}")
    print(f"{DIM}as tools daquele sistema, sem impedir a aplicação de subir.{OFF}\n")
    return 0


async def check_connectivity() -> None:
    """Testa a conectividade real de cada conector configurado."""
    from sai.config import get_settings
    from sai.connectors.registry import ToolRegistry

    print(f"{BOLD}Conectividade dos conectores{OFF}")
    registry = ToolRegistry(get_settings())
    results = await registry.healthcheck_all()
    labels = {
        "ok": OK,
        "error": MISSING,
        "timeout": f"{RED}TIMEOUT{OFF}",
        "not_configured": f"{DIM}nao config.{OFF}",
    }
    for name, state in sorted(results.items()):
        print(f"  [{labels.get(state, state):^14}] {name}")
    print()

    print(f"{BOLD}Banco de dados{OFF}")
    try:
        from sqlalchemy import text

        from sai.db.session import session_scope

        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
            has_vector = (
                await session.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'"))
            ).scalar()
        print(f"  [{OK:^14}] conexão")
        print(f"  [{OK if has_vector else MISSING:^14}] extensão pgvector")
    except Exception as exc:  # noqa: BLE001 - diagnóstico, mostra a causa
        print(f"  [{MISSING:^14}] {exc}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verifica o que falta configurar no SAI.")
    parser.add_argument("--connect", action="store_true",
                        help="também testa conectividade real com cada sistema")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    args = parser.parse_args()

    env_path = Path(args.env_file)
    if not env_path.exists():
        print(f"\n{RED}Arquivo {env_path} não existe.{OFF}")
        print(f"Crie com: {BOLD}copy .env.example .env{OFF} (Windows) ou "
              f"{BOLD}cp .env.example .env{OFF} (Linux/macOS)\n")
        return 1

    code = report(load_env(env_path))

    if args.connect:
        if code != 0:
            print(f"{YELLOW}Pulando o teste de conectividade: preencha o obrigatório antes.{OFF}\n")
        else:
            asyncio.run(check_connectivity())

    return code


if __name__ == "__main__":
    raise SystemExit(main())
