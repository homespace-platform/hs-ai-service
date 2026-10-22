"""Build a compact, packaged resolver snapshot from the SAME JSON pair used by Next.js.

Run after hs-web-app's npm run sync:data. No network access or API key is needed.
"""
import hashlib
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
source = root.parent / 'hs-web-app/data/vietmap'
target = root / 'src/homespace_ai/data/vietmap'
province_text = (source / 'province.json').read_bytes()
ward_text = (source / 'ward.json').read_bytes()
provinces, wards = json.loads(province_text), json.loads(ward_text)
assert len(provinces) == 34 and len(wards) >= 3000, 'Incomplete admin_new snapshot'
assert all(key == row['code'] and isinstance(key, str) for key, row in provinces.items())
assert all(key == row['code'] and row['parent_code'] in provinces for key, row in wards.items())
data = {
    'source': 'vietmap-company/vietnam_administrative_address/admin_new',
    'province_sha256': hashlib.sha256(province_text).hexdigest(),
    'ward_sha256': hashlib.sha256(ward_text).hexdigest(),
    'cities': [[key, row['name_with_type']] for key, row in provinces.items()],
    'wards': [[key, row['parent_code'], row['name_with_type']] for key, row in wards.items()],
}
files = {'admin_new.json': json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n',
         'LICENSE.txt': (source / 'LICENSE.txt').read_text(encoding='utf-8'),
         'SOURCE.md': (source / 'SOURCE.md').read_text(encoding='utf-8')}
for name, text in files.items():
    path = target / name
    if '--check' in sys.argv:
        assert path.read_text(encoding='utf-8') == text, 'Stale AI location snapshot; run scripts/sync_location_catalog.py'
    else:
        target.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')
print(f'OK: {len(provinces)} cities, {len(wards)} wards. Only resolved IDs, not the catalog, go to the model.')
