"""A default page size is not a hard result cap. Keep query, cards and references aligned."""
import re

from homespace_ai.application.vietnamese_text import fold_text

DEFAULT_RESULTS = 5
DEFAULT_MAX_RESULTS = 20
ABSOLUTE_MAX_RESULTS = 50


def max_results(settings=None) -> int:
    return min(max(int(getattr(settings, 'ai_listing_max_results', DEFAULT_MAX_RESULTS)), 1), ABSOLUTE_MAX_RESULTS)


def requested_count(question: str) -> int | None:
    text = fold_text(question)
    units = r'(?:tin(?:\s+dang)?|bai\s+dang|ket\s+qua|phong(?!\s+(?:ngu|tam|khach|bep)\b)(?:\s+tro)?|can(?:\s+ho)?|nha(?!\s+ve sinh\b)(?:\s+nguyen\s+can)?)\b'
    match = re.search(r'\b(\d{1,6})\s+' + units, text) or re.search(r'\btop\s+(\d{1,6})\b', text)
    if match:
        return max(1, int(match[1]))
    # Explicit quantity phrases only: bedrooms, occupants, prices and ordinals are not result counts.
    words = {'mot':1,'hai':2,'ba':3,'bon':4,'nam':5,'sau':6,'bay':7,'tam':8,'chin':9,'muoi':10,'hai muoi':20}
    words.update({'muoi ' + name:10 + number for name, number in list(words.items()) if number < 10})
    words.update({'muoi lam':15, 'muoi tu':14, 'ba muoi':30, 'bon muoi':40, 'nam muoi':50})
    for phrase in sorted(words, key=len, reverse=True):
        if re.search(r'\b' + phrase + r'\s+' + units, text):
            return words[phrase]
    return None


def search_limit(question: str, settings=None) -> int:
    return min(requested_count(question) or DEFAULT_RESULTS, max_results(settings))
