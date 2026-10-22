"""Small search previews; explicit detail/follow-up tools retain full current public facts."""
import json

MAX_SEARCH_LISTING_BYTES = 4096


def compact_search_listing(item: dict) -> dict:
    # Search predicates have already been applied by Core. The model is NOT the filtering engine.
    result = {name: item[name] for name in ('id', 'title', 'category', 'status', 'availableFrom', 'areaM2',
              'maxMotorbikeCount', 'maxCarCount', 'pricing') if name in item}
    if 'title' in result:
        result['title'] = str(result['title'])[:200]
    address = item.get('address') or {}
    result['address'] = {name: str(address[name])[:255] for name in ('display', 'cityId', 'city', 'wardId', 'ward', 'street')
                         if address.get(name) is not None}
    detail = {'ROOM': 'roomDetail', 'HOUSE': 'houseDetail', 'APARTMENT': 'apartmentDetail'}.get(item.get('category'))
    if detail and item.get(detail):
        result[detail] = {name: value for name, value in item[detail].items()
                          if value is None or isinstance(value, (bool, int, float)) or isinstance(value, str) and len(value) <= 100}
    for name, allowed in {
        'charges': ('chargeType', 'amount', 'unit', 'includedInRent', 'billingMethod'),
        'amenities': ('code', 'name'), 'furnishings': ('itemCode', 'assetName', 'quantity'),
        'policies': ('code', 'value', 'policyCode', 'policyValue'),
    }.items():
        if isinstance(item.get(name), list):
            result[name] = [{key: (str(row[key])[:100] if isinstance(row[key], str) else row[key])
                             for key in allowed if key in row} for row in item[name][:24] if isinstance(row, dict)]
    # Explicitly mark a preview: omissions are unknown, not a negative fact.
    result['evidenceMode'] = 'search_preview'
    result['omittedFields'] = ['description', 'handover_notes', 'viewing_details']
    for name in ('policies', 'furnishings', 'amenities', 'charges'):
        if name in result and len(result[name]) < len(item.get(name) or []):
            result['omittedFields'].append(name + '_remaining_rows')
    for name in ('policies', 'furnishings', 'amenities', 'charges', detail, 'address', 'pricing'):
        if len(json.dumps(result, ensure_ascii=False, default=str).encode('utf-8')) <= MAX_SEARCH_LISTING_BYTES:
            break
        if name in result:
            result.pop(name)
            result['omittedFields'].append(name)
    if len(json.dumps(result, ensure_ascii=False, default=str).encode('utf-8')) > MAX_SEARCH_LISTING_BYTES:
        raise ValueError('Search evidence exceeds its byte budget')
    return result
