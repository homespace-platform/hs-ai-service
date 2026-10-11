"""Filename identity and server-managed sequential knowledge versions.

Revision ID: a71c9e205b44
Revises: 919dda8816a3
"""
from collections import Counter
import unicodedata

from alembic import op
import sqlalchemy as sa

revision = "a71c9e205b44"
down_revision = "919dda8816a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_documents", sa.Column("source_filename", sa.String(255), nullable=True))
    op.add_column("knowledge_document_versions", sa.Column("version_number", sa.Integer(), nullable=True))
    op.execute("""
        WITH numbered AS (
            SELECT id, row_number() OVER (PARTITION BY document_id ORDER BY created_at, id) AS n
            FROM knowledge_document_versions
        )
        UPDATE knowledge_document_versions v
        SET version_number = numbered.n, version = numbered.n::text,
            metadata_snapshot = metadata_snapshot || jsonb_build_object('version', numbered.n::text)
        FROM numbered WHERE v.id = numbered.id
    """)
    # Old documents already record their uploaded filename in version metadata.
    # Ambiguous legacy names remain unmapped; the API reports a conflict rather than merging them.
    connection = op.get_bind()
    rows = connection.execute(sa.text("""
        SELECT DISTINCT ON (document_id) document_id, metadata_snapshot->>'original_filename' AS filename
        FROM knowledge_document_versions ORDER BY document_id, version_number DESC
    """)).mappings().all()
    candidates = [(row["document_id"], unicodedata.normalize("NFC", row["filename"].strip()).casefold())
                  for row in rows if row["filename"] and len(row["filename"].strip()) <= 255
                  and "/" not in row["filename"] and "\\" not in row["filename"]]
    counts = Counter(filename for _, filename in candidates)
    for document_id, filename in candidates:
        if counts[filename] == 1:
            connection.execute(sa.text("UPDATE knowledge_documents SET source_filename=:filename WHERE document_id=:id"),
                               {"filename": filename, "id": document_id})
    op.create_unique_constraint("uq_knowledge_source_filename", "knowledge_documents", ["source_filename"])
    op.alter_column("knowledge_document_versions", "version_number", nullable=False)
    op.create_unique_constraint("uq_knowledge_version_number", "knowledge_document_versions", ["document_id", "version_number"])


def downgrade() -> None:
    op.drop_constraint("uq_knowledge_version_number", "knowledge_document_versions", type_="unique")
    op.drop_column("knowledge_document_versions", "version_number")
    op.drop_constraint("uq_knowledge_source_filename", "knowledge_documents", type_="unique")
    op.drop_column("knowledge_documents", "source_filename")
