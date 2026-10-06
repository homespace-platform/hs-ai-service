from dataclasses import dataclass
import re
from typing import Any
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from homespace_ai.clients.generative import BaseGenerativeClient, GenerationUnavailableError
from homespace_ai.core.config import Settings
from homespace_ai.knowledge.ingestion.embedder import BaseEmbeddingAdapter
from homespace_ai.repositories.chunk_repo import ChunkRepository

logger = structlog.get_logger(__name__)


@dataclass
class Citation:
    document_id: str
    version: str
    title: str
    heading: str
    chunk_id: str


@dataclass
class AskResult:
    answer: str
    status: str  # ANSWERED, GENERAL_ANSWER, NO_EVIDENCE, OUT_OF_SCOPE, GENERATION_UNAVAILABLE
    citations: list[dict[str, Any]]
    request_id: str


# Patterns detecting requests for personal / real-time / dynamic account data
REALTIME_PATTERNS = [
    re.compile(r"\b(tin đăng|bài đăng)\s+.*(của tôi|được duyệt chưa|trạng thái)\b", re.IGNORECASE),
    re.compile(r"\b(tiền thuê|thanh toán|hóa đơn)\s+.*(tháng này|của tôi|kỳ này)\b", re.IGNORECASE),
    re.compile(r"\b(hợp đồng|chữ ký|lịch hẹn)\s+.*(của tôi|đã ký chưa|sắp tới)\b", re.IGNORECASE),
    re.compile(r"\b(số dư|ví|tiền cọc)\s+.*(của tôi|còn bao nhiêu)\b", re.IGNORECASE),
]

GREETINGS = {"chào", "chào bạn", "xin chào", "hi", "hello", "hey", "chào ai", "chào homespace"}
PLATFORM_TERMS = (
    "homespace", "trợ lý", "nền tảng", "ứng dụng", "tin đăng", "bài đăng",
    "nhà thuê", "phòng trọ", "chủ nhà", "người thuê", "thuê nhà", "đặt lịch",
    "yêu cầu thuê", "hóa đơn", "hoá đơn", "hợp đồng", "tiền cọc",
    "thanh toán", "đăng nhập", "tài khoản", "kiểm duyệt", "rag",
)


class AskUseCases:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        embedder: BaseEmbeddingAdapter,
        generative_client: BaseGenerativeClient,
    ) -> None:
        self.session = session
        self.settings = settings
        self.embedder = embedder
        self.generative_client = generative_client
        self.chunk_repo = ChunkRepository(session)

    def is_realtime_or_personal_query(self, question: str) -> bool:
        q = question.strip()
        for pattern in REALTIME_PATTERNS:
            if pattern.search(q):
                return True
        return False

    async def ask(
        self,
        question: str,
        user_role: str | None = None,
        user_name: str | None = None,
        locale: str = "vi-VN",
        conversation_id: str | None = None,
        request_id: str | None = None,
    ) -> AskResult:
        req_id = request_id or str(uuid.uuid4())
        audience = "ADMIN" if (user_role or "").upper() == "ADMIN" else "USER"

        normalized_question = question.strip().lower().strip(" .!?…")
        if normalized_question in GREETINGS:
            name = " ".join((user_name or "").split())[:80]
            greeting = f"Chào {name}!" if name else "Chào bạn!"
            return AskResult(
                answer=(f"{greeting} Mình là trợ lý HomeSpace. "
                        "Bạn muốn tìm hiểu về thuê nhà, cho thuê, hợp đồng hay thanh toán?"
                        if audience == "USER" else
                        f"{greeting} Mình là trợ lý HomeSpace. Bạn có thể hỏi mình về HomeSpace hoặc bất kỳ chủ đề nào khác."),
                status="ANSWERED", citations=[], request_id=req_id,
            )

        # 1. Check for real-time / personal dynamic queries
        if self.is_realtime_or_personal_query(question):
            return AskResult(
                answer=(
                    "Mình chưa xem được dữ liệu tài khoản hoặc trạng thái cập nhật theo thời gian thực "
                    "(như tin đăng, hóa đơn, hợp đồng hay lịch hẹn của bạn). "
                    "Bạn hãy kiểm tra mục tương ứng trên HomeSpace; mình vẫn có thể hướng dẫn cách sử dụng nhé."
                ),
                status="OUT_OF_SCOPE",
                citations=[],
                request_id=req_id,
            )

        # ADMIN may ask general questions; do not force unrelated topics through HomeSpace RAG.
        if audience == "ADMIN" and not any(term in normalized_question for term in PLATFORM_TERMS):
            return await self._answer_general(question, req_id)

        # 2. Strict ACL enforcement on visibility
        # Users only get "public". Only explicit ADMIN role gets "admin" docs.
        allowed_visibilities = ["public"]
        if audience == "ADMIN":
            allowed_visibilities.append("admin")

        # 3. Embed user question locally with 'query: ' prefix
        import asyncio
        query_vector = await asyncio.to_thread(self.embedder.embed_query, question)

        # 4. Search matching chunks via Hybrid Search (Vector + Lexical FTS via RRF)
        matching_chunks = await self.chunk_repo.search_chunks_hybrid(
            query_text=question,
            query_vector=query_vector,
            top_k=self.settings.retrieval_top_k,
            allowed_visibilities=allowed_visibilities,
            locale=locale,
            min_vector_similarity=self.settings.retrieval_min_similarity,
            candidate_pool_size=20,
        )

        # 5. Check if evidence is sufficient
        if not matching_chunks:
            return AskResult(
                answer="Mình chưa tìm thấy tài liệu HomeSpace phù hợp với câu hỏi này. Bạn có thể hỏi về cách thuê, cho thuê, hợp đồng hoặc thanh toán; nếu cần thông tin tài khoản cụ thể, hãy liên hệ bộ phận hỗ trợ nhé.",
                status="NO_EVIDENCE",
                citations=[],
                request_id=req_id,
            )

        # Format candidates with explicit source identifiers [C1], [C2]...
        context_chunks: list[dict[str, Any]] = []
        for i, (chunk, similarity) in enumerate(matching_chunks, 1):
            heading = chunk.heading_path or ""
            doc_id = chunk.document_id
            title = chunk.chunk_metadata.get("title", "")
            version = chunk.chunk_metadata.get("version", "")

            context_chunks.append({
                "source_id": f"C{i}",
                "chunk_id": str(chunk.id),
                "document_id": doc_id,
                "title": title,
                "heading": heading,
                "version": version,
                "content": chunk.content,
                "similarity": similarity,
            })
            if len(context_chunks) >= 4:
                break

        # 6. Generate final answer with LLM
        try:
            raw_answer = await self.generative_client.generate_answer(
                question=question,
                context_chunks=context_chunks,
                audience=audience,
            )

            # 7. Parse and strictly verify model citations
            answer_text, verified_citations = self._parse_and_verify_citations(
                raw_answer=raw_answer,
                context_chunks=context_chunks,
            )

            # Detect if model concluded there was no sufficient evidence
            is_no_evidence = (
                not verified_citations
                and any(phrase in answer_text.lower() for phrase in [
                    "chưa có thông tin", "không có thông tin", "chưa được cung cấp",
                    "tài liệu hiện tại chưa", "không tìm thấy thông tin"
                ])
            )

            final_status = "NO_EVIDENCE" if is_no_evidence else "ANSWERED"
            final_citations = [] if is_no_evidence else verified_citations

            return AskResult(
                answer=answer_text,
                status=final_status,
                citations=final_citations,
                request_id=req_id,
            )
        except GenerationUnavailableError as e:
            logger.warn("generation_unavailable", error=str(e), request_id=req_id)
            fallback_citations = [
                {
                    "documentId": c["document_id"],
                    "version": c["version"],
                    "title": c["title"],
                    "heading": c["heading"],
                    "chunkId": c["chunk_id"],
                    "snippet": c["content"][:200].replace("\n", " ").strip() + "...",
                }
                for c in context_chunks[:2]
            ]
            return AskResult(
                answer=(
                    "Hiện tại dịch vụ tổng hợp câu trả lời tự động đang tạm thời gián đoạn. "
                    "Bạn có thể tham khảo các tài liệu liên quan bên dưới."
                ),
                status="GENERATION_UNAVAILABLE",
                citations=fallback_citations,
                request_id=req_id,
            )

    async def _answer_general(self, question: str, request_id: str) -> AskResult:
        try:
            answer = await self.generative_client.generate_answer(
                question=question, context_chunks=[], audience="ADMIN", mode="general"
            )
            return AskResult(answer=answer, status="GENERAL_ANSWER", citations=[], request_id=request_id)
        except GenerationUnavailableError as error:
            logger.warn("general_generation_unavailable", error=str(error), request_id=request_id)
            return AskResult(
                answer="Mình chưa thể trả lời câu hỏi này lúc này vì dịch vụ AI đang gián đoạn. Bạn thử lại sau ít phút nhé.",
                status="GENERATION_UNAVAILABLE", citations=[], request_id=request_id,
            )

    def _parse_and_verify_citations(
        self,
        raw_answer: str,
        context_chunks: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Extracts [C1], [C2] citations specified by the model, verifies against retrieved context,
        and strips internal citation syntax from the final user answer.
        """
        citation_match = re.search(r"\n?\s*TRÍCH\s+DẪN:\s*(.*?)$", raw_answer, re.IGNORECASE | re.DOTALL)
        clean_answer = raw_answer
        cited_indices: set[int] = set()

        if citation_match:
            citation_str = citation_match.group(1).strip()
            clean_answer = raw_answer[:citation_match.start()].strip()

            if "KHÔNG" not in citation_str.upper():
                found_numbers = re.findall(r"C(\d+)", citation_str, re.IGNORECASE)
                for num_str in found_numbers:
                    try:
                        idx = int(num_str)
                        if 1 <= idx <= len(context_chunks):
                            cited_indices.add(idx)
                    except ValueError:
                        pass

        # If model did not emit formal TRÍCH DẪN: tag at all, fall back to checking if specific
        # document titles or sections were explicitly discussed in text
        if not citation_match and not cited_indices:
            for idx, c in enumerate(context_chunks, 1):
                title = c["title"].lower()
                heading = c["heading"].lower()
                if (title and title in clean_answer.lower()) or (heading and len(heading) > 10 and heading in clean_answer.lower()):
                    cited_indices.add(idx)

            # If still none but answer is confident and vector similarity is high (>0.75), use top 1
            if not cited_indices and len(context_chunks) > 0 and context_chunks[0]["similarity"] >= 0.75:
                if not any(neg in clean_answer.lower() for neg in ["chưa có thông tin", "không có thông tin"]):
                    cited_indices.add(1)


        verified: list[dict[str, Any]] = []
        for idx in sorted(cited_indices):
            c = context_chunks[idx - 1]
            raw_content = c["content"].strip()
            # Clean snippet for client display
            snippet = raw_content[:200].replace("\n", " ").strip()
            if len(raw_content) > 200:
                snippet += "..."

            verified.append({
                "documentId": c["document_id"],
                "version": c["version"],
                "title": c["title"],
                "heading": c["heading"],
                "chunkId": c["chunk_id"],
                "snippet": snippet,
            })

        return clean_answer, verified
