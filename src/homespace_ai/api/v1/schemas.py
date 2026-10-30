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
    sourceFilename: str | None = None
    latestVersion: str | None = None
    latestVersionStatus: str | None = None


class UpdateDocumentContentRequest(BaseModel):
    markdown: str = Field(min_length=1, max_length=1048576)
    expectedVersionId: str = Field(min_length=1, max_length=36)


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


class ContractCardItem(BaseModel):
    """Compact card built by the application from a verified Core projection. No link: the web app
    builds the role-correct route from contractId + askerRole."""
    contractId: str
    contractNumber: str
    property: str | None = None
    askerRole: str
    roleLabel: str
    status: str
    statusLabel: str
    leasePhase: str
    phaseLabel: str
    leaseStart: str | None = None
    leaseEndExclusive: str | None = None
    lastRentalDay: str | None = None
    readAt: str | None = None
    source: str = "CORE"


class AppointmentCardItem(BaseModel):
    """Compact card built by the application from a verified Core projection. No link: the web app builds the
    role-correct route and the exact-open parameter from appointmentId + requesterRole. The stored status and the
    time-derived phase stay separate, and a proposed schedule never replaces the current one."""
    appointmentId: str
    requesterRole: str
    roleLabel: str
    listingId: str | None = None
    listingTitle: str
    listingAddress: str | None = None
    date: str | None = None
    startTime: str | None = None
    endTime: str | None = None
    status: str
    statusLabel: str
    temporalPhase: str | None = None
    phaseLabel: str | None = None
    statusSyncPending: bool = False
    rescheduleRequested: bool = False
    proposedDate: str | None = None
    proposedStartTime: str | None = None
    proposedEndTime: str | None = None
    readAt: str | None = None
    source: str = "CORE"


class AskResponse(BaseModel):
    answer: str
    # ANSWERED, GENERAL_ANSWER, NO_EVIDENCE, OUT_OF_SCOPE, GENERATION_UNAVAILABLE, TOOL_UNAVAILABLE,
    # NO_RESULTS, NEEDS_CLARIFICATION, LOGIN_REQUIRED, NOT_FOUND
    status: str
    citations: list[CitationItem] = Field(default_factory=list)
    requestId: str
    listingIds: list[str] = Field(default_factory=list)
    listingPositions: dict[str, int] = Field(default_factory=dict)
    contractCards: list[ContractCardItem] = Field(default_factory=list)
    appointmentCards: list[AppointmentCardItem] = Field(default_factory=list)
    dataSources: list[str] = Field(default_factory=list)
    suggestedQuestions: list[str] = Field(default_factory=list, max_length=4)


class GuestStateRequest(BaseModel):
    guestState: str = Field(min_length=1, max_length=2097152)


class GuestAskRequest(AskRequest):
    guestState: str = Field(min_length=1, max_length=2097152)


class GuestAskResponse(AskResponse):
    guestState: str
    remainingQuestions: int


class ConversationMessage(BaseModel):
    id: str
    role: str
    content: str
    status: str | None = None
    createdAt: datetime
    listingIds: list[str] = Field(default_factory=list)
    listingPositions: dict[str, int] = Field(default_factory=dict)
    contractCards: list[ContractCardItem] = Field(default_factory=list)
    appointmentCards: list[AppointmentCardItem] = Field(default_factory=list)
    dataSources: list[str] = Field(default_factory=list)
    suggestedQuestions: list[str] = Field(default_factory=list, max_length=4)


class ConversationSummary(BaseModel):
    id: str
    title: str
    isPinned: bool
    createdAt: datetime
    updatedAt: datetime


class ConversationDetail(ConversationSummary):
    messages: list[ConversationMessage]


class GuestConversationDetail(ConversationDetail):
    guestState: str
    remainingQuestions: int


class SetConversationPinnedRequest(BaseModel):
    isPinned: bool
