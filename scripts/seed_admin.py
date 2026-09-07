"""Cria (ou promove) o primeiro usuário admin.

Necessário porque o login via Entra ID cria todo usuário novo como `viewer` —
promoção de papel nunca é self-service (SPEC 10.4). Alguém precisa ser admin
antes de qualquer ação `high`/`critical` poder ser aprovada, e esse alguém é
definido aqui, fora da aplicação.

Uso:
    python scripts/seed_admin.py --email andre@empresa.com \
        --entra-object-id 00000000-0000-0000-0000-000000000000 \
        --name "Andre" [--slack-user-id U012ABC]

O `entra-object-id` é o Object ID do usuário no Entra ID:
    az ad user show --id andre@empresa.com --query id -o tsv

Rodar de novo com o mesmo e-mail promove o usuário existente a admin em vez
de duplicar — útil depois que a pessoa já entrou uma vez e virou `viewer`.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from sai.db.models import AuditLog, User  # noqa: E402
from sai.db.session import session_scope  # noqa: E402


async def seed(email: str, entra_object_id: str, name: str, slack_user_id: str | None) -> None:
    async with session_scope() as session:
        existing = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()

        if existing is not None:
            previous = existing.role
            existing.role = "admin"
            existing.entra_object_id = entra_object_id
            if slack_user_id:
                existing.slack_user_id = slack_user_id
            session.add(
                AuditLog(
                    actor="script:seed_admin",
                    event_type="user_role_changed",
                    entity_type="user",
                    entity_id=existing.id,
                    payload={"email": email, "from": previous, "to": "admin"},
                )
            )
            print(f"Usuário {email} promovido de '{previous}' para 'admin'.")
            return

        user = User(
            email=email,
            display_name=name,
            role="admin",
            entra_object_id=entra_object_id,
            slack_user_id=slack_user_id,
        )
        session.add(user)
        await session.flush()
        session.add(
            AuditLog(
                actor="script:seed_admin",
                event_type="user_created",
                entity_type="user",
                entity_id=user.id,
                payload={"email": email, "role": "admin"},
            )
        )
        print(f"Admin criado: {email} ({user.id})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cria ou promove o primeiro admin do SAI.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--entra-object-id", required=True,
                        help="Object ID do usuário no Entra ID (az ad user show --id <email> --query id -o tsv)")
    parser.add_argument("--name", default=None, help="Nome de exibição (padrão: o e-mail)")
    parser.add_argument("--slack-user-id", default=None,
                        help="Member ID do Slack (ex.: U012ABC), se for aprovar pelo Slack")
    args = parser.parse_args()

    asyncio.run(seed(args.email, args.entra_object_id, args.name or args.email, args.slack_user_id))


if __name__ == "__main__":
    main()
