from homespace_ai import main
from homespace_ai.api.v1 import admin_knowledge


def test_preload_embedding_model_runs_first_query(monkeypatch):
    calls: list[str] = []

    class FakeEmbedder:
        def embed_query(self, question: str) -> list[float]:
            calls.append(question)
            return [1.0]

    monkeypatch.setattr(admin_knowledge, "get_embedder", lambda settings: FakeEmbedder())

    main.preload_embedding_model()

    assert calls == ["HomeSpace"]
