import math
from homespace_ai.knowledge.ingestion.embedder import MockEmbeddingAdapter


def test_mock_embedder_dimension_and_normalization():
    adapter = MockEmbeddingAdapter(dimension=384)
    assert adapter.dimension == 384
    assert adapter.model_id == "mock-multilingual-e5-small"

    passages = ["Đoạn văn thứ nhất", "Đoạn văn thứ hai"]
    vectors = adapter.embed_passages(passages)
    assert len(vectors) == 2
    assert len(vectors[0]) == 384
    assert len(vectors[1]) == 384

    # Check vector L2 norm is ~1.0 (normalized)
    norm = math.sqrt(sum(x * x for x in vectors[0]))
    assert abs(norm - 1.0) < 1e-4

    query_vec = adapter.embed_query("câu hỏi tìm kiếm")
    assert len(query_vec) == 384
    query_norm = math.sqrt(sum(x * x for x in query_vec))
    assert abs(query_norm - 1.0) < 1e-4
