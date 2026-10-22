"""Resolve explicit location predicates locally; never ask the LLM to invent Vietmap codes."""
from functools import lru_cache
from importlib.resources import files
import json
import re

from homespace_ai.application.vietnamese_text import fold_text


class LocationClarification(ValueError):
    """A human location choice is required, not a provider failure or an empty search."""


def key(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', fold_text(value)).strip()


def short(value: str) -> str:
    return re.sub(r'^(?:thanh pho|tinh|phuong|xa|dac khu) ', '', key(value))


def mentioned(value: str, text: str) -> bool:
    return bool(re.search(r'\b' + re.escape(value) + r'\b', text))


@lru_cache(maxsize=1)
def catalog():
    data = json.loads(files('homespace_ai').joinpath('data/vietmap/admin_new.json').read_text(encoding='utf-8'))
    cities = dict(data['cities'])
    wards = {code: (city, name) for code, city, name in data['wards']}
    aliases = {key(name): code for code, name in cities.items()}
    aliases.update({short(name): code for code, name in cities.items()})
    hcm = aliases['ho chi minh']
    aliases.update({alias: hcm for alias in ['hcm', 'tp hcm', 'tphcm', 'sai gon', 'saigon']})
    return cities, wards, aliases


def resolve_location_filters(filters: list[dict], question: str, history: list[dict], fields: dict) -> list[dict]:
    cities, wards, aliases = catalog()
    geo = {'city', 'city_id', 'ward', 'ward_id'}
    requested = [item for item in filters if item['field'] in geo]
    current = key(question)
    # A city name inside a landmark is not a separately requested administrative restriction.
    location_text = key(re.sub(r'\b(?:gan|canh|sat|quanh)\b[^,;?!\n]*', ' ', fold_text(question)))
    explicit_cities = {code for alias, code in aliases.items() if mentioned(alias, location_text)}
    # A district is not the new ward with a similar name. No unverified legacy mapping.
    if re.search(r'\b(?:quan(?!\s+(?:ly|tam|trong)\b)|huyen)\s+\S+', current) and not re.search(r'\b(?:phuong|xa|dac khu)\s+\S+', current):
        raise LocationClarification('Bạn đang nói khu vực quận/huyện cũ. Bạn cho mình biết phường/xã hiện tại hoặc địa chỉ cụ thể để lọc đúng sau sáp nhập nhé.')
    if len(explicit_cities) > 1:
        raise LocationClarification('Bạn muốn tìm tại tỉnh/thành phố nào trước? Mình sẽ lọc từng khu vực để không trộn kết quả.')
    city_id = next(iter(explicit_cities), None)
    source = location_text + ' ' + ' '.join(key(item.get('content', '')) for item in history[-8:] if item.get('role') == 'user')
    for item in requested:
        if item['field'] not in {'city', 'city_id'}:
            continue
        if item['op'] not in {'eq', 'contains'}:
            raise LocationClarification('Bạn cho biết tên tỉnh/thành phố muốn tìm nhé.')
        candidate = str(item['value']) if item['field'] == 'city_id' else aliases.get(key(str(item['value'])))
        if explicit_cities and candidate != city_id:
            continue
        if candidate not in cities:
            raise LocationClarification('Mình chưa xác định được tỉnh/thành phố theo địa chỉ mới. Bạn cho biết tên đầy đủ nhé.')
        # An explicit current city supersedes a stale/hallucinated model code.
        if city_id is None and any(code == candidate and mentioned(alias, source) for alias, code in aliases.items()):
            city_id = candidate
    ward_ids = None
    for item in requested:
        if item['field'] not in {'ward', 'ward_id'}:
            continue
        if item['op'] not in {'eq', 'contains'}:
            raise LocationClarification('Bạn muốn tìm tại phường/xã nào?')
        value = str(item['value'])
        candidates = {value} if item['field'] == 'ward_id' and value in wards else {
            code for code, (parent, name) in wards.items() if key(value) in {key(name), short(name)}
        } if item['field'] == 'ward' else set()
        if city_id:
            candidates = {code for code in candidates if wards[code][0] == city_id}
        if not candidates:
            raise LocationClarification('Mình chưa ghép được phường/xã với tỉnh/thành phố theo danh mục mới. Bạn kiểm tra lại hai tên này nhé.')
        # Drop a numeric administrative guess derived only from a landmark, but preserve a named ward.
        if item['field'] == 'ward_id' and not any(mentioned(short(wards[code][1]), source)
                or mentioned(key(wards[code][1]), current) or mentioned(code, source) for code in candidates):
            continue
        ward_ids = candidates if ward_ids is None else ward_ids & candidates
    if ward_ids is not None and len(ward_ids) != 1:
        choices = sorted({f'{wards[code][1]}, {cities[wards[code][0]]}' for code in ward_ids})[:4]
        raise LocationClarification('Tên phường/xã chưa xác định duy nhất. Bạn chọn tỉnh/thành phố hoặc một địa chỉ cụ thể nhé.'
                                    + (' Các kết quả: ' + '; '.join(choices) if choices else ''))
    result = [item for item in filters if item['field'] not in geo]
    if ward_ids:
        ward_id = next(iter(ward_ids))
        city_id = wards[ward_id][0]
        field = 'ward_id' if 'ward_id' in fields else 'ward'
        result.append({'field': field, 'op': 'eq', 'value': ward_id if field == 'ward_id' else wards[ward_id][1]})
    if city_id and ('city_id' in fields or 'city' in fields):
        field = 'city_id' if 'city_id' in fields else 'city'
        result.append({'field': field, 'op': 'eq', 'value': city_id if field == 'city_id' else cities[city_id]})
    return result
