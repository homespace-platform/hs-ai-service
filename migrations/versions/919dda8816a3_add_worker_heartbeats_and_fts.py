"""add_worker_heartbeats_and_fts

Revision ID: 919dda8816a3
Revises: 93c564133fbd
Create Date: 2026-09-25 02:39:45.319202

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector


# revision identifiers, used by Alembic.
revision: str = '919dda8816a3'
down_revision: Union[str, None] = '93c564133fbd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'worker_heartbeats',
        sa.Column('worker_id', sa.String(length=100), nullable=False),
        sa.Column('hostname', sa.String(length=100), nullable=False),
        sa.Column('pid', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('last_heartbeat', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('worker_id'),
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_fts "
        "ON knowledge_chunks "
        "USING gin (to_tsvector('simple', coalesce(heading_path, '') || ' ' || coalesce(content, '')));"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_fts;")
    op.drop_table('worker_heartbeats')

