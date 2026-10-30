"""Distinguish facilities inside a property from places outside it."""
from homespace_ai.application.vietnamese_text import VietnamesePattern

NEARBY_INTENT = VietnamesePattern(
    r"quét.{0,40}(?:xung quanh|tiện ích)|bán kính|khoảng cách (?:đến|tới)|"
    r"tiện ích[^.!?;\n]{0,45}(?:xung quanh|lân cận|bên ngoài|gần đó|quanh đó)|"
    r"(?:xung quanh|lân cận|gần (?:đây|đó)|quanh (?:đây|đó|các phòng|những phòng))[^.!?;\n]{0,45}(?:tiện ích|có gì|có những gì|có những chỗ)|"
    r"(?:gần|quanh|xung quanh).{0,60}(?:trường|đại học|siêu thị|bệnh viện|nhà thuốc|công viên|chợ|gym|nơi làm|xe buýt)")
BUILDING_INTENT = VietnamesePattern(
    r"tiện ích[^.!?;\n]{0,30}(?:tòa nhà|toà nhà|nội khu|bên trong)|"
    r"tiện nghi|thang máy|wifi|wi-fi|camera|bảo vệ|giặt phơi|hồ bơi|khu bếp")
AMENITY_WORD = VietnamesePattern(r"tiện ích")


def ambiguous_amenities(question: str) -> bool:
    return bool(AMENITY_WORD.search(question) and not NEARBY_INTENT.search(question)
                and not BUILDING_INTENT.search(question))
