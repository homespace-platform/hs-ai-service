from abc import ABC, abstractmethod
import os
from typing import Any
import numpy as np

import structlog

logger = structlog.get_logger(__name__)


class BaseEmbeddingAdapter(ABC):
    @property
    @abstractmethod
    def model_id(self) -> str:
        pass

    @property
    @abstractmethod
    def dimension(self) -> int:
        pass

    @abstractmethod
    def embed_passages(self, passages: list[str]) -> list[list[float]]:
        """Embeds document chunks with 'passage: ' prefix and L2 normalization."""
        pass

    @abstractmethod
    def embed_query(self, query: str) -> list[float]:
        """Embeds a user search query with 'query: ' prefix and L2 normalization."""
        pass


class LocalE5EmbeddingAdapter(BaseEmbeddingAdapter):
    """SentenceTransformers implementation of intfloat/multilingual-e5-small.
    Runs locally on CPU, with persistent cache directory.
    """

    def __init__(
        self,
        model_id: str = "intfloat/multilingual-e5-small",
        revision: str | None = None,
        cache_dir: str = "./models_cache",
    ) -> None:
        self._model_id = model_id
        self._revision = revision
        self._cache_dir = cache_dir
        self._model: Any = None
        self._tokenizer: Any = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return

        from sentence_transformers import SentenceTransformer
        from transformers import AutoTokenizer

        os.makedirs(self._cache_dir, exist_ok=True)
        logger.info(
            "loading_embedding_model",
            model_id=self._model_id,
            revision=self._revision,
            cache_dir=self._cache_dir,
        )

        kwargs: dict[str, Any] = {
            "cache_folder": self._cache_dir,
            "device": "cpu",
        }
        if self._revision:
            kwargs["revision"] = self._revision

        self._model = SentenceTransformer(self._model_id, **kwargs)
        self._tokenizer = AutoTokenizer.from_pretrained(
            self._model_id,
            cache_dir=self._cache_dir,
            revision=self._revision,
        )
        logger.info("embedding_model_loaded", model_id=self._model_id)

    @property
    def tokenizer(self) -> Any:
        self._ensure_loaded()
        return self._tokenizer

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return 384

    def embed_passages(self, passages: list[str]) -> list[list[float]]:
        if not passages:
            return []
        self._ensure_loaded()
        
        # Ensure 'passage: ' prefix
        prefixed = [
            p if p.startswith("passage: ") else f"passage: {p}"
            for p in passages
        ]
        embeddings = self._model.encode(
            prefixed,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return embeddings.tolist()

    def embed_query(self, query: str) -> list[float]:
        self._ensure_loaded()
        clean_q = query.strip()
        prefixed = clean_q if clean_q.startswith("query: ") else f"query: {clean_q}"
        embedding = self._model.encode(
            [prefixed],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )[0]
        return embedding.tolist()


class MockEmbeddingAdapter(BaseEmbeddingAdapter):
    """Deterministic mock embedding adapter for fast unit testing without downloading 470MB weights."""

    def __init__(self, model_id: str = "mock-multilingual-e5-small", dimension: int = 384) -> None:
        self._model_id = model_id
        self._dimension = dimension

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    def _hash_to_vector(self, text: str) -> list[float]:
        # Generate deterministic vector from text
        rng = np.random.RandomState(abs(hash(text)) % (2**32))
        vec = rng.randn(self._dimension)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    def embed_passages(self, passages: list[str]) -> list[list[float]]:
        return [
            self._hash_to_vector(p if p.startswith("passage: ") else f"passage: {p}")
            for p in passages
        ]

    def embed_query(self, query: str) -> list[float]:
        clean_q = query.strip()
        prefixed = clean_q if clean_q.startswith("query: ") else f"query: {clean_q}"
        return self._hash_to_vector(prefixed)
