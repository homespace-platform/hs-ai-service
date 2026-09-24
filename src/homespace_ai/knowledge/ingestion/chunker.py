from dataclasses import dataclass
import re
from typing import Any
from transformers import AutoTokenizer


@dataclass
class ChunkItem:
    chunk_index: int
    heading_path: str
    content: str
    token_count: int
    passage_text: str  # content with 'passage: ' and heading prefixed for embedding
    metadata: dict[str, Any]


class MarkdownChunker:
    """Chunks markdown by headings and paragraphs, measuring token count with the embedding model tokenizer.
    Ensures 'passage: ' prefix + heading path + content fits within token constraints.
    """

    HEADING_REGEX = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)

    def __init__(
        self,
        tokenizer: AutoTokenizer,
        target_tokens: int = 350,
        overlap_tokens: int = 50,
        max_tokens: int = 450,
    ) -> None:
        self.tokenizer = tokenizer
        self.target_tokens = target_tokens
        self.overlap_tokens = overlap_tokens
        self.max_tokens = max_tokens

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    def chunk_document(
        self,
        document_id: str,
        version: str,
        title: str,
        visibility: str,
        locale: str,
        raw_markdown: str,
    ) -> list[ChunkItem]:
        # Parse into sections based on Markdown headings
        sections = self._parse_heading_sections(raw_markdown, default_title=title)
        
        chunks: list[ChunkItem] = []
        chunk_index = 0

        for section_heading, text_blocks in sections:
            # Group text blocks into chunks respecting target and max token limits
            grouped_texts = self._group_blocks(section_heading, text_blocks)
            for text in grouped_texts:
                if not text.strip():
                    continue

                heading_prefix = f"{section_heading}\n\n" if section_heading else ""
                full_passage = f"passage: {heading_prefix}{text.strip()}"
                token_count = self.count_tokens(full_passage)
                if token_count > self.max_tokens:
                    raise ValueError(
                        f"Chunk {chunk_index} exceeds the embedding token limit: "
                        f"{token_count} > {self.max_tokens}."
                    )

                # Metadata for citation
                metadata = {
                    "document_id": document_id,
                    "version": version,
                    "title": title,
                    "heading_path": section_heading,
                    "visibility": visibility,
                    "locale": locale,
                    "chunk_index": chunk_index,
                }

                chunks.append(
                    ChunkItem(
                        chunk_index=chunk_index,
                        heading_path=section_heading,
                        content=text.strip(),
                        token_count=token_count,
                        passage_text=full_passage,
                        metadata=metadata,
                    )
                )
                chunk_index += 1

        return chunks

    def _parse_heading_sections(
        self, markdown: str, default_title: str
    ) -> list[tuple[str, list[str]]]:
        """Parses markdown into hierarchical heading paths and list of paragraphs/blocks."""
        lines = markdown.splitlines()
        sections: list[tuple[str, list[str]]] = []
        
        current_heading_hierarchy: list[tuple[int, str]] = []
        current_blocks: list[str] = []
        current_block: list[str] = []

        def get_current_heading_path() -> str:
            if not current_heading_hierarchy:
                return default_title
            return " > ".join(h[1] for h in current_heading_hierarchy)

        for line in lines:
            heading_match = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
            if heading_match:
                # Flush existing block
                if current_block:
                    current_blocks.append("\n".join(current_block).strip())
                    current_block = []
                
                # Flush previous section if it had content
                if current_blocks:
                    sections.append((get_current_heading_path(), current_blocks))
                    current_blocks = []

                level = len(heading_match.group(1))
                heading_text = heading_match.group(2).strip()

                # Update hierarchy
                while current_heading_hierarchy and current_heading_hierarchy[-1][0] >= level:
                    current_heading_hierarchy.pop()
                current_heading_hierarchy.append((level, heading_text))
            else:
                if not line.strip():
                    if current_block:
                        current_blocks.append("\n".join(current_block).strip())
                        current_block = []
                else:
                    current_block.append(line)

        if current_block:
            current_blocks.append("\n".join(current_block).strip())
        if current_blocks:
            sections.append((get_current_heading_path(), current_blocks))

        if not sections and markdown.strip():
            sections.append((default_title, [markdown.strip()]))

        return sections

    def _group_blocks(self, heading_path: str, blocks: list[str]) -> list[str]:
        """Groups paragraphs/FAQ pairs together to reach target_tokens without exceeding max_tokens."""
        heading_overhead = self.count_tokens(f"passage: {heading_path}\n\n")
        available_target = max(10, self.target_tokens - heading_overhead)
        available_max = max(20, self.max_tokens - heading_overhead)

        result_chunks: list[str] = []
        current_chunk_blocks: list[str] = []
        current_token_count = 0

        for block in blocks:
            block_tokens = self.count_tokens(block)

            # If a single block alone is larger than available_max, split it by sentences
            if block_tokens > available_max:
                if current_chunk_blocks:
                    result_chunks.append("\n\n".join(current_chunk_blocks))
                    current_chunk_blocks = []
                    current_token_count = 0

                split_sub_blocks = self._split_large_block(block, available_target, available_max)
                result_chunks.extend(split_sub_blocks)
                continue

            if current_token_count + block_tokens <= available_target:
                current_chunk_blocks.append(block)
                current_token_count += block_tokens
            elif current_token_count + block_tokens <= available_max:
                current_chunk_blocks.append(block)
                result_chunks.append("\n\n".join(current_chunk_blocks))
                current_chunk_blocks = []
                current_token_count = 0
            else:
                if current_chunk_blocks:
                    result_chunks.append("\n\n".join(current_chunk_blocks))
                current_chunk_blocks = [block]
                current_token_count = block_tokens

        if current_chunk_blocks:
            result_chunks.append("\n\n".join(current_chunk_blocks))

        return result_chunks

    def _split_large_block(self, block: str, target: int, max_tokens: int) -> list[str]:
        """Splits an oversized block by sentence delimiters while maintaining overlap."""
        sentences: list[str] = []
        for sentence in re.split(r"(?<=[.!?\n])\s+", block):
            if self.count_tokens(sentence) <= max_tokens:
                sentences.append(sentence)
                continue
            # A paragraph may contain no sentence boundaries at all. Split it
            # by words rather than silently truncating it in the E5 tokenizer.
            words: list[str] = []
            for word in sentence.split():
                candidate = " ".join([*words, word])
                if self.count_tokens(candidate) <= max_tokens:
                    words.append(word)
                else:
                    if not words:
                        raise ValueError("A single word exceeds the embedding token limit.")
                    sentences.append(" ".join(words))
                    words = [word]
            if words:
                sentences.append(" ".join(words))
        sub_chunks: list[str] = []
        current: list[str] = []
        current_count = 0

        for sentence in sentences:
            s_count = self.count_tokens(sentence)
            if current_count + s_count <= target:
                current.append(sentence)
                current_count += s_count
            else:
                if current:
                    sub_chunks.append(" ".join(current))
                    # Overlap: keep last few sentences up to overlap_tokens
                    overlap: list[str] = []
                    overlap_count = 0
                    for prev_s in reversed(current):
                        cnt = self.count_tokens(prev_s)
                        if overlap_count + cnt <= self.overlap_tokens:
                            overlap.insert(0, prev_s)
                            overlap_count += cnt
                        else:
                            break
                    if self.count_tokens(" ".join([*overlap, sentence])) > max_tokens:
                        overlap = []
                        overlap_count = 0
                    current = overlap + [sentence]
                    current_count = overlap_count + s_count
                else:
                    sub_chunks.append(sentence)
                    current = []
                    current_count = 0

        if current:
            sub_chunks.append(" ".join(current))

        return sub_chunks
