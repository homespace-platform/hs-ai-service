"""Stable, path-free filename identity shared by uploads and legacy matching."""
import unicodedata

from fastapi import HTTPException


def normalize_filename(filename: str) -> str:
    name = unicodedata.normalize("NFC", filename.strip())
    if (not name or len(name) > 255 or "/" in name or "\\" in name
            or any(unicodedata.category(char).startswith("C") for char in name)
            or not name.casefold().endswith(".md")):
        raise HTTPException(400, "Only .md files with path-free filenames of at most 255 characters are accepted.")
    return name.casefold()
