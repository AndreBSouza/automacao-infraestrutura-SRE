"""Add users.slack_user_id for chat-platform identity mapping.

Required by the hardened approval webhooks (sai/api/routers/webhooks.py):
an approval arriving from Slack must be attributable to a specific person,
so the Slack `user.id` from the interactivity payload is mapped onto a real
`users` row. Teams needs no new column — it supplies the Entra object id
directly, which already exists as `users.entra_object_id`.

Nullable by design: users who never approve via Slack simply have no id.
An unmapped identity is refused at the endpoint, never approved anonymously.

Revision ID: 0002_user_slack_identity
Revises: 0001_initial_schema
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_user_slack_identity"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("slack_user_id", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_users_slack_user_id", "users", ["slack_user_id"])


def downgrade() -> None:
    op.drop_constraint("uq_users_slack_user_id", "users", type_="unique")
    op.drop_column("users", "slack_user_id")
