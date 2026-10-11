import pytest

from homespace_ai.core import schema_health
from homespace_ai.core.database import Base


class FakeInspector:
    def __init__(self, missing_tables=(), missing_columns=()):
        self.missing_tables = missing_tables
        self.missing_columns = missing_columns

    def has_table(self, name):
        return name not in self.missing_tables

    def get_columns(self, name):
        return [{"name": column.name} for column in Base.metadata.tables[name].columns
                if (name, column.name) not in self.missing_columns]


def test_current_schema_is_ready(monkeypatch):
    monkeypatch.setattr(schema_health, "inspect", lambda _: FakeInspector())
    schema_health.check_schema(object())


@pytest.mark.parametrize("missing_tables,missing_columns,expected", [
    (("worker_heartbeats",), (), "worker_heartbeats"),
    ((), (("knowledge_documents", "source_filename"),), "knowledge_documents.source_filename"),
    ((), (("knowledge_document_versions", "version_number"),), "knowledge_document_versions.version_number"),
])
def test_missing_schema_has_actionable_error(monkeypatch, missing_tables, missing_columns, expected):
    monkeypatch.setattr(schema_health, "inspect", lambda _: FakeInspector(missing_tables, missing_columns))
    with pytest.raises(schema_health.SchemaNotReadyError) as error:
        schema_health.check_schema(object())
    assert expected in str(error.value)
    assert "uv run alembic upgrade head" in str(error.value)
    assert "AI_DATABASE_URL" in str(error.value)


@pytest.mark.asyncio
async def test_worker_fails_before_starting_loop_if_schema_missing(monkeypatch):
    from homespace_ai.knowledge.ingestion import worker as module

    async def missing(_):
        raise schema_health.SchemaNotReadyError("missing worker_heartbeats")

    monkeypatch.setattr(module, "ensure_schema_ready", missing)
    worker = module.IngestionWorker(embedder=object())
    with pytest.raises(schema_health.SchemaNotReadyError):
        await worker.run()
    assert not worker.running


@pytest.mark.asyncio
async def test_api_does_not_register_without_schema(monkeypatch):
    from homespace_ai import main

    async def missing(_):
        raise schema_health.SchemaNotReadyError("missing worker_heartbeats")

    monkeypatch.setattr(main, "ensure_schema_ready", missing)
    with pytest.raises(schema_health.SchemaNotReadyError):
        async with main.lifespan(main.app):
            pytest.fail("API must not serve requests on an incomplete schema")
    assert main.app.state.eureka_registered is False
