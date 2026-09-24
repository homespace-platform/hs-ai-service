from typing import Any
import uuid

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from homespace_ai.knowledge.ingestion.chunker import ChunkItem
from homespace_ai.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)


class ChunkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save_chunks(
        self,
        version_id: uuid.UUID,
        document_id: str,
        chunk_items: list[ChunkItem],
        embeddings: list[list[float]],
    ) -> list[KnowledgeChunk]:
        # Delete existing chunks for this version if reindexing
        await self.delete_chunks_by_version(version_id)

        saved: list[KnowledgeChunk] = []
        for item, emb in zip(chunk_items, embeddings, strict=True):
            chunk = KnowledgeChunk(
                version_id=version_id,
                document_id=document_id,
                chunk_index=item.chunk_index,
                heading_path=item.heading_path,
                content=item.content,
                token_count=item.token_count,
                embedding=emb,
                chunk_metadata=item.metadata,
            )
            self.session.add(chunk)
            saved.append(chunk)

        await self.session.flush()
        return saved

    async def delete_chunks_by_version(self, version_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.version_id == version_id)
        )
        await self.session.flush()

    async def search_chunks(
        self,
        query_vector: list[float],
        top_k: int = 5,
        allowed_visibilities: list[str] | None = None,
        locale: str | None = None,
        min_similarity: float = 0.55,
        filter_active_only: bool = True,
    ) -> list[tuple[KnowledgeChunk, float]]:
        """Exact cosine similarity search using pgvector.
        Enforces strict ACL: only active, approved, published documents matching visibility & locale.
        """
        visibilities = allowed_visibilities or ["public"]

        similarity_expr = (1.0 - KnowledgeChunk.embedding.cosine_distance(query_vector)).label("similarity")

        stmt = select(KnowledgeChunk, similarity_expr)
        stmt = stmt.join(
            KnowledgeDocumentVersion,
            KnowledgeChunk.version_id == KnowledgeDocumentVersion.id,
        )
        stmt = stmt.join(
            KnowledgeDocument,
            KnowledgeDocumentVersion.document_id == KnowledgeDocument.document_id,
        )

        if filter_active_only:
            stmt = stmt.where(
                KnowledgeDocument.active_version_id == KnowledgeDocumentVersion.id,
                KnowledgeDocument.status == "APPROVED",
                KnowledgeDocumentVersion.status == "PUBLISHED",
            )

        stmt = stmt.where(KnowledgeDocument.visibility.in_(visibilities))

        if locale:
            stmt = stmt.where(KnowledgeDocument.locale == locale)

        stmt = stmt.where(similarity_expr >= min_similarity)
        stmt = stmt.order_by(similarity_expr.desc()).limit(top_k)

        result = await self.session.execute(stmt)
        rows = result.all()
        return [(row[0], float(row[1])) for row in rows]

    async def search_chunks_lexical(
        self,
        query_text: str,
        top_k: int = 20,
        allowed_visibilities: list[str] | None = None,
        locale: str | None = None,
        filter_active_only: bool = True,
    ) -> list[tuple[KnowledgeChunk, float]]:
        """Full-text lexical search using PostgreSQL 'simple' dictionary (preserves Vietnamese accents/words).
        Weights: Title ('A'), Heading Path ('B'), Content ('C').
        Enforces identical strict ACL as vector search.
        """
        clean_query = query_text.strip()
        if not clean_query:
            return []

        visibilities = allowed_visibilities or ["public"]

        w_a = text("'A'")
        w_b = text("'B'")
        w_c = text("'C'")
        ts_vector = func.setweight(
            func.to_tsvector("simple", func.coalesce(KnowledgeDocument.title, "")), w_a
        ).op("||")(
            func.setweight(
                func.to_tsvector("simple", func.coalesce(KnowledgeChunk.heading_path, "")), w_b
            )
        ).op("||")(
            func.setweight(
                func.to_tsvector("simple", func.coalesce(KnowledgeChunk.content, "")), w_c
            )
        )

        ts_query = func.plainto_tsquery("simple", clean_query)
        ts_rank = func.ts_rank_cd(ts_vector, ts_query).label("rank")

        stmt = select(KnowledgeChunk, ts_rank)
        stmt = stmt.join(
            KnowledgeDocumentVersion,
            KnowledgeChunk.version_id == KnowledgeDocumentVersion.id,
        )
        stmt = stmt.join(
            KnowledgeDocument,
            KnowledgeDocumentVersion.document_id == KnowledgeDocument.document_id,
        )

        if filter_active_only:
            stmt = stmt.where(
                KnowledgeDocument.active_version_id == KnowledgeDocumentVersion.id,
                KnowledgeDocument.status == "APPROVED",
                KnowledgeDocumentVersion.status == "PUBLISHED",
            )

        stmt = stmt.where(KnowledgeDocument.visibility.in_(visibilities))

        if locale:
            stmt = stmt.where(KnowledgeDocument.locale == locale)

        stmt = stmt.where(ts_vector.op("@@")(ts_query))
        stmt = stmt.order_by(ts_rank.desc()).limit(top_k)

        result = await self.session.execute(stmt)
        rows = result.all()
        return [(row[0], float(row[1])) for row in rows]

    async def search_chunks_hybrid(
        self,
        query_text: str,
        query_vector: list[float],
        top_k: int = 5,
        allowed_visibilities: list[str] | None = None,
        locale: str | None = None,
        min_vector_similarity: float = 0.55,
        candidate_pool_size: int = 20,
    ) -> list[tuple[KnowledgeChunk, float]]:
        """Hybrid search combining pgvector exact cosine search and PostgreSQL lexical FTS via
        Reciprocal Rank Fusion (RRF), intent-based domain calibration, heading deduplication,
        and diversity constraints.
        """
        # 1. Fetch vector candidates
        vector_candidates = await self.search_chunks(
            query_vector=query_vector,
            top_k=candidate_pool_size,
            allowed_visibilities=allowed_visibilities,
            locale=locale,
            min_similarity=min_vector_similarity,
            filter_active_only=True,
        )

        # 2. Fetch lexical candidates
        lexical_candidates = await self.search_chunks_lexical(
            query_text=query_text,
            top_k=candidate_pool_size,
            allowed_visibilities=allowed_visibilities,
            locale=locale,
            filter_active_only=True,
        )

        if not vector_candidates and not lexical_candidates:
            return []

        # 3. Reciprocal Rank Fusion (k=60 standard)
        rrf_constant = 60
        scores: dict[uuid.UUID, float] = {}
        chunk_map: dict[uuid.UUID, KnowledgeChunk] = {}
        vector_sim_map: dict[uuid.UUID, float] = {}

        for rank, (chunk, sim) in enumerate(vector_candidates):
            cid = chunk.id
            chunk_map[cid] = chunk
            vector_sim_map[cid] = sim
            scores[cid] = scores.get(cid, 0.0) + (1.0 / (rrf_constant + rank + 1))

        for rank, (chunk, _rank_score) in enumerate(lexical_candidates):
            cid = chunk.id
            chunk_map[cid] = chunk
            scores[cid] = scores.get(cid, 0.0) + (1.0 / (rrf_constant + rank + 1))

        # 4. Domain & Intent Weighting
        q_lower = query_text.lower()
        is_overview_query = any(k in q_lower for k in [
            "là gì", "dành cho ai", "nền tảng gì", "giới thiệu", "tổng quan",
            "về homespace", "ai dùng", "mục đích", "được thành lập", "vai trò"
        ])
        is_policy_query = any(k in q_lower for k in [
            "điều khoản", "chính sách", "quy định", "quyền riêng tư", "bảo mật", "vi phạm"
        ])

        for cid, chunk in chunk_map.items():
            doc_id = chunk.document_id.lower()
            category = (chunk.chunk_metadata.get("category") or "").lower()
            heading = (chunk.heading_path or "").lower()
            title = (chunk.chunk_metadata.get("title") or "").lower()

            # Boost overview documents on overview questions
            if is_overview_query:
                if "overview" in doc_id or "overview" in category or "giới thiệu" in title or "tổng quan" in heading:
                    scores[cid] *= 1.45
                elif "policy" in doc_id or "terms" in doc_id:
                    # Slightly dampen tangential policies when user explicitly asked for overview
                    scores[cid] *= 0.85

            # Boost policy / terms documents on policy questions
            if is_policy_query:
                if "policy" in doc_id or "terms" in doc_id or "chính sách" in title:
                    scores[cid] *= 1.35

        # 5. Heading Deduplication and Document Diversity
        # Sort all candidates by fusion score
        sorted_cids = sorted(scores.keys(), key=lambda c: scores[c], reverse=True)

        selected: list[tuple[KnowledgeChunk, float]] = []
        seen_headings: set[str] = set()
        doc_count: dict[str, int] = {}

        for cid in sorted_cids:
            chunk = chunk_map[cid]
            doc_id = chunk.document_id
            heading = chunk.heading_path or ""
            heading_key = f"{doc_id}:{heading}"

            # Deduplicate multiple chunks from the exact same heading
            if heading_key in seen_headings:
                continue

            # Diversity limit: max 2 chunks per document in context
            if doc_count.get(doc_id, 0) >= 2:
                continue

            seen_headings.add(heading_key)
            doc_count[doc_id] = doc_count.get(doc_id, 0) + 1

            sim = vector_sim_map.get(cid, 0.70)
            selected.append((chunk, sim))

            if len(selected) >= top_k:
                break

        return selected

