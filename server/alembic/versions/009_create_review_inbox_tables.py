"""Create the Review Inbox tables

Revision ID: 009
Revises: 008
Create Date: 2026-08-15

Candidate heads/versions (design §7, RFC 0050): untrusted proposals that
stay out of retrieval until an atomic approve creates the entry in the
same transaction. Drives mem0.context.tables.build_candidate_metadata.
"""

from typing import Sequence, Union

from alembic import op

from db import TABLE_PREFIX

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CTX_PREFIX = f"{TABLE_PREFIX}ctx_"


def upgrade() -> None:
    from mem0.context.tables import build_candidate_metadata

    build_candidate_metadata(CTX_PREFIX).create_all(bind=op.get_bind())


def downgrade() -> None:
    from mem0.context.tables import build_candidate_metadata

    build_candidate_metadata(CTX_PREFIX).drop_all(bind=op.get_bind())
