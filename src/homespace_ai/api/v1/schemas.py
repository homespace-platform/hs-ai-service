from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class VersionSummaryResponse(BaseModel):
    versionId: str
    version: str
    status: str
    contentHash: str
    createdAt: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    rawMarkdown: str | None = None



class JobSummaryResponse(BaseModel):
    jobId: str
    versionId: str
    documentId: str
    jobType: str
    status: str
    attempts: int
    maxAttempts: int
    errorMessage: str | None = None
    createdAt: datetime
    updatedAt: datetime


class DocumentSummaryResponse(BaseModel):
    documentId: str
    title: str
    category: str | None = None
    locale: str
    visibility: str
    audience: list[str]
    status: str
    activeVersionId: str | None = None
    createdAt: datetime
    updatedAt: datetime


class DocumentDetailResponse(DocumentSummaryResponse):
    versions: list[VersionSummaryResponse] = Field(default_factory=list)
    activeVersion: VersionSummaryResponse | None = None
    latestJob: JobSummaryResponse | None = None


class PatchDocumentRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=100)
    visibility: str | None = Field(default=None, pattern="^(public|admin)$")
    audience: list[str] | None = None


class DiagnosticSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    scope: str = Field(default="public", pattern="^(public|admin)$")
    topK: int = Field(default=5, ge=1, le=20)
    minSimilarity: float = Field(default=0.50, ge=0.0, le=1.0)


class DiagnosticChunkItem(BaseModel):
    documentId: str
    title: str
    headingPath: str
    content: str
    similarity: float
    chunkIndex: int
    visibility: str


class DiagnosticSearchResponse(BaseModel):
    query: str
    scope: str
    count: int
    results: list[DiagnosticChunkItem]


class CitationItem(BaseModel):
    documentId: str
    version: str
    title: str
    heading: str
    chunkId: str
    snippet: str | None = None



class AskRequest(BaseModel):
    question: str | None = Field(default=None, max_length=1000)
    query: str | None = Field(default=None, max_length=1000)
    conversationId: str | None = Field(default=None, max_length=200)
    conversation_id: str | None = Field(default=None, max_length=200)

    @property
    def effective_question(self) -> str:
        return (self.question or self.query or "").strip()

    @property
    def effective_conversation_id(self) -> str | None:
        return self.conversationId or self.conversation_id


class AskResponse(BaseModel):
    answer: str
    status: str  # ANSWERED, NO_EVIDENCE, OUT_OF_SCOPE, GENERATION_UNAVAILABLE
    citations: list[CitationItem] = Field(default_factory=list)
    requestId: str
