"""Resolve explicit location predicates locally; never ask the LLM to invent Vietmap codes."""
from functools import lru_cache
from importlib.resources import files
import json
import re
import unicodedata

from homespace_ai.application.vietnamese_text import fold_text
from homespace_ai.application.listing_street import administrative_text


class LocationClarification(ValueError):
    """A human location choice is required, not a provider failure or an empty search."""

    def __init__(self, message: str, location_query: dict | None = None):
        super().__init__(message)
        self.location_query = location_query or {}


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


@lru_cache(maxsize=1)
def _ward_index():
    index = {}
    for code, (_, name) in catalog()[1].items():
        for alias in {key(name), short(name)}:
            index.setdefault(alias, set()).add(code)
    return index


@lru_cache(maxsize=1)
def _ward_pattern():
    names = {key(name) for _, name in catalog()[1].values()}
    return re.compile(r'\b(?:' + '|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r')\b')


def explicit_names(question: str) -> tuple[set[str], set[str]]:
    """Read independent admin phrases, excluding street/landmark names and ward-name substrings."""
    raw = re.sub(r'[^a-z0-9,;?!\n]+', ' ', fold_text(administrative_text(question)))
    protected = [(m.start(), m.end()) for m in _ward_pattern().finditer(raw)]
    text = key(re.sub(r'\b(?:gan|canh|sat|quanh)\b[^,;?!\n]*?(?=\b(?:o|tai)\s+(?:phuong|xa|dac khu|tinh|thanh pho)\b|[,;?!\n]|$)',
                      lambda m: m.group() if any(start <= m.start() < end for start, end in protected) else ' ', raw))
    ward_keys = {m.group() for m in _ward_pattern().finditer(text)}
    # A province name contained inside a ward name is not a second province request.
    city_text = _ward_pattern().sub(' ', text)
    cities, wards, aliases = catalog()
    # Thạnh An / Thanh An fold to the same key. This is one spoken ward choice;
    # disambiguate by its parent city later, rather than treating it as two requests.
    names = {min(wards[code][1] for code in _ward_index()[name]) for name in ward_keys}
    return names, {code for alias, code in aliases.items() if mentioned(alias, city_text)}


def location_proposal(question: str, proposed: dict, state: dict | None = None) -> dict:
    """Customer's explicit phrases override model slot errors; pending names survive short replies."""
    names, city_ids = explicit_names(question)
    cities, wards, _ = catalog()
    location = {slot: value for slot, value in proposed.items() if slot in {'ward', 'city'} and isinstance(value, str) and value.strip()}
    if len(names) > 1:
        raise LocationClarification('Bạn muốn tìm tại phường/xã nào trước? Mình sẽ giữ các nhu cầu còn lại.')
    if len(city_ids) > 1:
        raise LocationClarification('Bạn muốn tìm tại tỉnh/thành phố nào trước? Mình sẽ lọc từng khu vực để không trộn kết quả.')
    if names:
        location['ward'] = next(iter(names))
    if city_ids:
        location['city'] = cities[next(iter(city_ids))]
    from homespace_ai.application.renter_search_state import RESET
    if RESET.search(question):
        state = None
    pending = (state or {}).get('pending') or {}
    if pending.get('type') == 'LOCATION':
        for slot, value in (pending.get('locationQuery') or {}).items():
            if slot not in location or (slot == 'ward' and not names and not mentioned(short(location[slot]), key(question))):
                location[slot] = value
    # New ward, same confirmed city: retain that city only if this ward exists there.
    if location.get('ward') and not location.get('city'):
        previous = (state or {}).get('location') or {}
        if previous.get('cityId') and any(parent == previous['cityId'] and key(location['ward']) in {key(name), short(name)}
                                         for parent, name in wards.values()):
            location['city'] = cities[previous['cityId']]
    return location


def recover_location_query(state: dict | None, history: list[dict]) -> dict | None:
    """Upgrade old pending conversations from customer messages, never from assistant choices."""
    pending = (state or {}).get('pending') or {}
    if pending.get('type') != 'LOCATION' or pending.get('locationQuery'):
        return state
    from copy import deepcopy
    from homespace_ai.application.renter_search_state import RESET
    for message in reversed(history):
        if message.get('role') != 'user':
            continue
        content = message.get('content') or ''
        if RESET.search(content):
            break
        names, _ = explicit_names(content)
        if names:
            if len(names) != 1:
                break
            try:
                query = location_proposal(content, {})
            except LocationClarification:
                return state
            repaired = deepcopy(state)
            repaired['pending']['locationQuery'] = query
            return repaired
    return state


def resolve_location_filters(filters: list[dict], question: str, history: list[dict], fields: dict) -> list[dict]:
    cities, wards, aliases = catalog()
    geo = {'city', 'city_id', 'ward', 'ward_id'}
    requested = [item for item in filters if item['field'] in geo]
    current = key(question)
    # A city name inside a landmark is not a separately requested administrative restriction.
    location_text = key(administrative_text(question))
    named_wards, explicit_cities = explicit_names(question)
    if len(named_wards) > 1:
        raise LocationClarification('Bạn muốn tìm tại phường/xã nào trước? Mình sẽ giữ các nhu cầu còn lại.')
    # Native tool callers sometimes put the entire address in ward or omit that slot.
    # The catalog-backed phrase in the customer's current message is authoritative.
    if named_wards:
        requested = [item for item in requested if item['field'] not in {'ward', 'ward_id'}]
        requested.append({'field': 'ward', 'op': 'eq', 'value': next(iter(named_wards))})
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
        candidates = {value} if item['field'] == 'ward_id' and value in wards else set(
            _ward_index().get(key(value), set())) if item['field'] == 'ward' else set()
        # Diacritics may distinguish two real wards within the same merged province.
        original = unicodedata.normalize('NFC', question).casefold()
        exact_names = {wards[code][1] for code in candidates
                       if re.search(r'\b' + re.escape(wards[code][1].casefold()) + r'\b', original)}
        if len(exact_names) == 1:
            candidates = {code for code in candidates if wards[code][1] in exact_names}
        if city_id:
            candidates = {code for code in candidates if wards[code][0] == city_id}
        if not candidates:
            raise LocationClarification('Mình chưa ghép được phường/xã với tỉnh/thành phố theo danh mục mới. Bạn kiểm tra lại hai tên này nhé.',
                                        {'ward': value, **({'city': cities[city_id]} if city_id else {})})
        # Drop a numeric administrative guess derived only from a landmark, but preserve a named ward.
        if item['field'] == 'ward_id' and not any(mentioned(short(wards[code][1]), source)
                or mentioned(key(wards[code][1]), current) or mentioned(code, source) for code in candidates):
            continue
        ward_ids = candidates if ward_ids is None else ward_ids & candidates
    if ward_ids is not None and len(ward_ids) != 1:
        choices = sorted({f'{wards[code][1]}, {cities[wards[code][0]]}' for code in ward_ids})[:4]
        names = {wards[code][1] for code in ward_ids}
        raise LocationClarification('Tên phường/xã chưa xác định duy nhất. Bạn chọn tỉnh/thành phố hoặc một địa chỉ cụ thể nhé.'
                                    + (' Các kết quả: ' + '; '.join(choices) if choices else ''),
                                    {'ward': next(iter(names))} if len(names) == 1 else {})
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
