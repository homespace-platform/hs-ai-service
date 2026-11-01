"""Explicit street names are address predicates, never administrative units or POI targets."""
import re
import unicodedata

from homespace_ai.application.vietnamese_text import fold_text

_PREFIX = re.compile(r"\b(?:(?:đường|duong)\s+|[đd]\.\s*)", re.IGNORECASE)
_END = re.compile(
    r"[,;!?\n]|\b(?:cho\s+(?:\d+|mot|hai|ba|bon|nam)\s+nguoi|gia|ngan sach|duoi|tren|toi da|"
    r"it nhat|tu\s+\d|tam|khoang|phuong|xa|quan|huyen|thanh pho|tp\.?\s|tinh|hcm|tphcm|"
    r"(?:tai|o)\s+(?=(?:phuong|xa|tp|thanh pho|tinh|hcm|tphcm)\b)|"
    r"co\s+(?:wc|ban cong|thang may|bep|noi that)|khong\s+(?:chung|noi that)|va\s+(?:gia|co|cho))\b")


def street_mentions(question: str) -> list[tuple[str, int, int]]:
    text = unicodedata.normalize("NFC", question)
    folded = fold_text(text)
    found = []
    for prefix in _PREFIX.finditer(text):
        # Do not fold the prefix: 'điều dưỡng' becomes 'dieu duong' but is not a road.
        if re.search(r"\b(?:chi|tieu|huyet|luong)\s+$", folded[:prefix.start()]):
            continue
        if re.match(r"(?:di|truyen|chim|bo)\b", folded[prefix.end():]):
            continue
        end = _END.search(folded, prefix.end())
        stop = end.start() if end else len(text)
        name = text[prefix.end():stop].strip(" .:-")
        if name and len(name) <= 120 and len(name.split()) <= 12:
            found.append((name, prefix.start(), stop))
    return found


def administrative_text(question: str) -> str:
    """Keep real ward/city text, mask names embedded in 'đường Hạnh Thông', etc."""
    text = unicodedata.normalize("NFC", question)
    for _, start, end in reversed(street_mentions(text)):
        text = text[:start] + " " * (end - start) + text[end:]
    return text
