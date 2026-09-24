from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib
import re
from typing import Any
import yaml

from fastapi import HTTPException, status


@dataclass(frozen=True)
class ParsedDocument:
    document_id: str
    title: str
    audience: list[str]
    visibility: str  # "public" or "admin"
    locale: str
    version: str
    raw_markdown: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)
    category: str | None = None


class DocumentParser(ABC):
    """Abstract interface for parsing documents into standard ParsedDocument.
    Allows plugging in PDF parser in the future without changing API or business schema.
    """

    @abstractmethod
    def parse(self, filename: str, content: bytes, max_size_bytes: int = 1048576) -> ParsedDocument:
        pass


class MarkdownDocumentParser(DocumentParser):
    FRONT_MATTER_REGEX = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n(.*)$", re.DOTALL)

    def parse(self, filename: str, content: bytes, max_size_bytes: int = 1048576) -> ParsedDocument:
        if not filename.lower().endswith(".md"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Only .md Markdown files are supported, got '{filename}'.",
            )

        if len(content) > max_size_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File size {len(content)} bytes exceeds limit of {max_size_bytes} bytes.",
            )

        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File is not valid UTF-8 encoded text.",
            )

        match = self.FRONT_MATTER_REGEX.match(text)
        if not match:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Markdown document must include valid YAML front matter bounded by '---'.",
            )

        yaml_text, raw_markdown = match.group(1), match.group(2)

        try:
            front_matter = yaml.safe_load(yaml_text)
            if not isinstance(front_matter, dict):
                raise ValueError("Front matter is not a YAML dictionary.")
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid YAML front matter: {e}",
            )

        document_id = str(front_matter.get("document_id") or "").strip()
        title = str(front_matter.get("title") or "").strip()
        locale = str(front_matter.get("locale") or "vi-VN").strip()
        visibility = str(front_matter.get("visibility") or "public").strip().lower()
        version = str(front_matter.get("version") or "0.1.0").strip()
        raw_audience = front_matter.get("audience", ["public"])

        if isinstance(raw_audience, list):
            audience = [str(a).strip().lower() for a in raw_audience if a]
        elif isinstance(raw_audience, str):
            audience = [a.strip().lower() for a in raw_audience.split(",") if a.strip()]
        else:
            audience = ["public"]

        if not document_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing required 'document_id' in YAML front matter.",
            )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", document_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="document_id must be a URL-safe identifier (letters, numbers, dot, underscore or hyphen).",
            )
        if not title:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing required 'title' in YAML front matter.",
            )

        if len(document_id) > 100 or len(title) > 255 or len(version) > 30 or len(locale) > 20:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Document metadata exceeds allowed field lengths.",
            )

        if visibility not in ("public", "admin"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid visibility '{visibility}', must be 'public' or 'admin'.",
            )

        category = front_matter.get("category")
        if category:
            category = str(category).strip()
            if len(category) > 100:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Category exceeds 100 characters.",
                )

        # Calculate content sha256 hash
        content_hash = hashlib.sha256(content).hexdigest()

        # Build clean metadata snapshot - never use front matter 'status' to bypass approval!
        last_reviewed = front_matter.get("last_reviewed")
        clean_metadata = {
            "document_id": document_id,
            "title": title,
            "audience": audience,
            "visibility": visibility,
            "locale": locale,
            "version": version,
            "category": category,
            "last_reviewed": str(last_reviewed) if last_reviewed is not None else None,
            "original_filename": filename,
        }

        return ParsedDocument(
            document_id=document_id,
            title=title,
            audience=audience,
            visibility=visibility,
            locale=locale,
            version=version,
            raw_markdown=raw_markdown.strip(),
            content_hash=content_hash,
            metadata=clean_metadata,
            category=category,
        )
