import argparse
import asyncio
from pathlib import Path
import sys

import structlog

from homespace_ai.application.use_cases.document_use_cases import DocumentUseCases
from homespace_ai.core.config import get_settings
from homespace_ai.core.database import async_session_maker

logger = structlog.get_logger(__name__)


async def import_knowledge_directory(directory_path: str) -> None:
    p = Path(directory_path).resolve()
    if not p.is_dir():
        print(f"Error: Directory '{p}' does not exist.")
        sys.exit(1)

    md_files = sorted(
        [f for f in p.glob("*.md") if f.name.lower() != "readme.md" and not f.name.startswith(".")]
    )
    if not md_files:
        print(f"No markdown documents found in '{p}'.")
        return

    print(f"Found {len(md_files)} knowledge markdown documents in '{p}'.")
    settings = get_settings()

    created_count = 0
    error_count = 0

    async with async_session_maker() as session:
        use_cases = DocumentUseCases(session=session, settings=settings)

        for f in md_files:
            try:
                content = f.read_bytes()
                result = await use_cases.upload_document(
                    filename=f.name,
                    content=content,
                    created_by="system-bootstrap",
                )
                created_count += 1
                print(f" [QUEUED] {f.name} -> {result.document_id} (version: {result.version}, job: {result.job_id})")
            except Exception as e:
                await session.rollback()
                error_count += 1
                print(f" [ERROR] {f.name}: {e}")

    print("\n--- IMPORT SUMMARY ---")
    print(f"Total scanned: {len(md_files)}")
    print(f"New versions queued: {created_count}")
    print(f"Errors: {error_count}")
    print("New versions activate automatically when the worker succeeds. Re-running increments every file's version.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create next knowledge versions and queue automatic ingestion.")
    parser.add_argument(
        "--dir",
        "-d",
        default="../hs-infrastructure/documents/knowledge",
        help="Path to folder containing knowledge .md files (default: ../hs-infrastructure/documents/knowledge)",
    )
    args = parser.parse_args()
    asyncio.run(import_knowledge_directory(args.dir))


if __name__ == "__main__":
    main()
