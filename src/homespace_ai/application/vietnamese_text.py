"""Accent-insensitive intent matching without changing the user's original message."""
import re
import unicodedata


def fold_text(value: str) -> str:
    return ''.join(char for char in unicodedata.normalize('NFD', value.replace('đ', 'd').replace('Đ', 'D'))
                   if unicodedata.category(char) != 'Mn').lower()


class VietnamesePattern:
    def __init__(self, pattern: str) -> None:
        self.pattern = re.compile(fold_text(pattern), re.IGNORECASE)

    def search(self, value: str):
        return self.pattern.search(fold_text(value))

    def match(self, value: str):
        return self.pattern.match(fold_text(value))

    def findall(self, value: str):
        return self.pattern.findall(fold_text(value))
