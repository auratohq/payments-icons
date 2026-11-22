#!/usr/bin/env python3
"""Download and verify the exact public asset snapshot. Python 3.11+."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from functools import lru_cache
from datetime import date
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import time
import unicodedata
from urllib.request import Request, urlopen
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageChops, ImageOps

ROOT = Path(__file__).resolve().parents[1]
SOURCE = os.environ.get('AURATO_ASSET_BASE_URL', '').rstrip('/')
PUBLIC_OBJECT_MARKER = '/storage/v1/object/public/'
NAMES = {'banks': 'Banks', 'cards': 'Cards', 'currencies': 'Currencies', 'institutions': 'Institutions', 'markets': 'Markets',
         'operators': 'Operators', 'payment-methods': 'Payment methods', 'psps': 'PSPs',
         'regulators': 'Regulators', 'schemes': 'Schemes', 'systems': 'Systems'}


def check_entry(item):
    category = item['category']
    path = item['path']
    if category not in NAMES or not isinstance(path, str):
        raise ValueError('Unknown category or invalid path')
    parts = PurePosixPath(path).parts
    if len(parts) != 2 or parts[0] != category or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*\.png', parts[1]):
        raise ValueError(f'Unsafe asset path: {path}')
    if path != '/'.join(parts) or not item.get('id') or not item.get('name'):
        raise ValueError(f'Invalid identity: {path}')
    expected = (48, 30) if category == 'cards' else (48, 48)
    if (item['width'], item['height']) != expected:
        raise ValueError(f'Unexpected dimensions in metadata: {path}')
    if not isinstance(item['bytes'], int) or not 0 < item['bytes'] <= 5_000_000:
        raise ValueError(f'Invalid byte size: {path}')
    if not re.fullmatch(r'[a-f0-9]{64}', item['sha256']):
        raise ValueError(f'Invalid SHA-256: {path}')
    if not isinstance(item.get('source'), dict) or not isinstance(item['source'].get('kind'), str):
        raise ValueError(f'Missing provenance: {path}')
    upstream = item.get('upstreamPath')
    upstream_parts = PurePosixPath(upstream).parts if isinstance(upstream, str) else ()
    if (len(upstream_parts) < 3 or upstream_parts[:2] != ('48', category) or
            any(not re.fullmatch(r'[a-zA-Z0-9_-][a-zA-Z0-9_.-]*', part) for part in upstream_parts[2:]) or
            not upstream_parts[-1].endswith('.webp') or
            upstream != '/'.join(upstream_parts)):
        raise ValueError(f'Unsafe upstream asset path: {upstream}')
    aliases = item.get('aliases', [])
    if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
        raise ValueError(f'Invalid aliases: {path}')
    url = item['source'].get('url')
    if url is not None and (not isinstance(url, str) or not re.match(r'^https?://[^\s]+$', url)):
        raise ValueError(f'Unsafe source URL: {path}')


def destination(item):
    check_entry(item)
    path = ROOT / item['path']
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f'Symlinks are not supported: {path}')
    return path


def load_catalog():
    index = json.loads((ROOT / 'catalog.json').read_text())
    schema_version = index.get('schemaVersion')
    if schema_version not in (1, 2) or 'sourceBaseUrl' in index:
        raise ValueError('Unsupported catalog schema or public upstream URL')
    entries, categories = [], set()
    for category in index['categories']:
        cid = category['id']
        if cid not in NAMES or cid in categories or category['path'] != f'catalog/{cid}.json':
            raise ValueError('Invalid catalog category')
        categories.add(cid)
        batch = json.loads((ROOT / category['path']).read_text())
        if len(batch) != category['count'] or any(x['category'] != cid for x in batch):
            raise ValueError(f'Category count or mapping mismatch: {cid}')
        entries.extend(batch)
    if categories != set(NAMES) or len(entries) != index['assetCount']:
        raise ValueError('Incomplete catalog')
    seen = set()
    for item in entries:
        if schema_version == 1:
            legacy = PurePosixPath(item.get('path', '')).parts
            if (len(legacy) != 2 or legacy[0] != item.get('category') or
                    not re.fullmatch(r'[a-zA-Z0-9_-]+\.png', legacy[1])):
                raise ValueError(f'Invalid legacy asset path: {item.get("path")}')
            item.setdefault('upstreamPath', item['path'])
            item.setdefault('aliases', [])
        else:
            check_entry(item)
        if item['path'] in seen:
            raise ValueError(f'Duplicate path: {item["path"]}')
        seen.add(item['path'])
    cards = sum(x['category'] == 'cards' for x in entries)
    if cards != index['cardCount'] or len(entries) - cards != index['primaryIconCount']:
        raise ValueError('Shape counts do not match')
    return index, entries


def check_bytes(data, item):
    if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError(f'Bytes or SHA-256 mismatch: {item["path"]}')


@lru_cache(maxsize=1)
def silhouette_masks():
    inside, outside = bytearray(), bytearray()
    radius = 43 * 48 / 256
    for y in range(48):
        for x in range(48):
            dx = max(radius - (x + 0.5), (x + 0.5) - (48-radius), 0)
            dy = max(radius - (y + 0.5), (y + 0.5) - (48-radius), 0)
            distance = (dx*dx + dy*dy) ** 0.5
            inside.append(255 if distance < radius-1.5 else 0)
            outside.append(255 if distance > radius+1.5 else 0)
    return Image.frombytes('L', (48, 48), bytes(inside)), Image.frombytes('L', (48, 48), bytes(outside))


def png_from_mini(data, category):
    """Decode the 48-pixel WebP source; fit padded cards without stretching."""
    with Image.open(BytesIO(data)) as original:
        if original.format != 'WEBP':
            raise ValueError('Expected a WebP from the 48-pixel source snapshot')
        image = original.convert('RGBA')
    if category == 'cards':
        if image.width != 48 or not 0 < image.height <= 256:
            raise ValueError(f'Unexpected card mini dimensions: {image.size}')
        if image.size != (48, 30):
            bounds = image.getchannel('A').getbbox()
            if not bounds:
                raise ValueError('Empty card mini')
            art = ImageOps.contain(image.crop(bounds), (48, 30), Image.Resampling.LANCZOS)
            image = Image.new('RGBA', (48, 30))
            image.paste(art, ((48-art.width)//2, (30-art.height)//2))
    elif image.size != (48, 48):
        raise ValueError(f'Unexpected icon mini dimensions: {image.size}')
    elif all(image.getchannel('A').getpixel(point) == 255
             for point in ((0, 0), (47, 0), (0, 47), (47, 47))):
        # A few square sources need the same baked-in rounded frame as the pack.
        radius = 43 * 48 / 256
        samples = bytearray()
        for y in range(48*4):
            for x in range(48*4):
                dx = max(radius-(x+.5)/4, (x+.5)/4-(48-radius), 0)
                dy = max(radius-(y+.5)/4, (y+.5)/4-(48-radius), 0)
                samples.append(255 if dx*dx+dy*dy <= radius*radius else 0)
        mask = Image.frombytes('L', (48*4, 48*4), bytes(samples))
        mask = mask.resize((48, 48), Image.Resampling.LANCZOS)
        image.putalpha(ImageChops.multiply(image.getchannel('A'), mask))
    stream = BytesIO()
    image.save(stream, format='PNG', optimize=True)
    return stream.getvalue()


def check_image(data, item):
    check_bytes(data, item)
    with Image.open(BytesIO(data)) as original:
        if original.format != 'PNG' or original.size != (item['width'], item['height']):
            raise ValueError(f'PNG format or dimensions mismatch: {item["path"]}')
        original.load()
        alpha = original.convert('RGBA').getchannel('A')
    width, height = alpha.size
    if item['category'] == 'cards':
        # Card artwork deliberately keeps its own frame, transparency and corner treatment.
        return
    if any(alpha.getpixel(p) != 0 for p in ((0, 0), (width-1, 0), (0, height-1), (width-1, height-1))):
        raise ValueError(f'Corners must be transparent: {item["path"]}')
    # Check the rounded-square silhouette independent of a CSS border-radius.
    # Downsampling can leave tiny alpha deviations; keep tolerance at the edge.
    alpha = alpha.point(lambda value: 0 if value <= 5 else 255 if value >= 250 else value)
    inside, outside = silhouette_masks()
    if ImageChops.multiply(ImageChops.invert(alpha), inside).getbbox():
        raise ValueError(f'Transparent rounded-square interior: {item["path"]}')
    if ImageChops.multiply(alpha, outside).getbbox():
        raise ValueError(f'Square corner outside silhouette: {item["path"]}')


def download(entries, workers):
    groups = defaultdict(list)
    for item in entries:
        groups[item['sha256']].append(item)
    cache = ROOT / '.cache' / 'sha256'
    cache.mkdir(parents=True, exist_ok=True)

    def fetch_group(group):
        first = group[0]
        cached = cache / (first['sha256'] + '.png')
        data = None
        # A shared hash may belong to many records. Reuse any already materialized
        # descriptive path before reaching out to the upstream public bucket.
        for candidate in [*(destination(item) for item in group), cached]:
            if candidate.is_file():
                candidate_data = candidate.read_bytes()
                try:
                    check_bytes(candidate_data, first)
                    data = candidate_data
                    break
                except ValueError:
                    pass
        if data is None:
            if not SOURCE:
                raise ValueError(
                    f'Missing approved bytes for {first["path"]}; '
                    'set AURATO_ASSET_BASE_URL in the private maintainer environment'
                )
            for attempt in range(3):
                try:
                    suffix = '' if attempt == 0 else '?v=' + first['sha256']
                    request = Request(f'{SOURCE}/{first["upstreamPath"]}{suffix}', headers={'User-Agent': 'Aurato-Payment-Icons/1.0'})
                    with urlopen(request, timeout=30) as response:
                        data = png_from_mini(response.read(5_000_001), first['category'])
                    check_bytes(data, first)
                    cached.write_bytes(data)
                    break
                except Exception:
                    data = None
                    if attempt == 2:
                        raise
                    time.sleep(attempt+1)
        for item in group:
            check_bytes(data, item)
            path = destination(item)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_bytes() != data:
                temp = path.with_suffix('.png.part')
                temp.write_bytes(data)
                temp.replace(path)
        return len(group)

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        tasks = [executor.submit(fetch_group, group) for group in groups.values()]
        for i, task in enumerate(as_completed(tasks), 1):
            completed += task.result()
            if i % 100 == 0 or i == len(tasks):
                print(f'{i}/{len(tasks)} unique images; {completed}/{len(entries)} files ready', flush=True)


def validate(entries):
    expected = {x['path'] for x in entries}
    actual = {p.relative_to(ROOT).as_posix() for cat in NAMES for p in (ROOT/cat).rglob('*.png') if p.is_file()}
    if expected != actual:
        raise ValueError(f'Missing files: {sorted(expected-actual)[:10]}; unlisted files: {sorted(actual-expected)[:10]}')
    unexpected = {p.relative_to(ROOT).as_posix() for cat in NAMES for p in (ROOT/cat).rglob('*')
                  if p.is_file() and p.suffix.lower() != '.png' and p.name != 'README.md'}
    if unexpected:
        raise ValueError(f'Unexpected files in category folders: {sorted(unexpected)[:10]}')
    decoded = set()
    for item in entries:
        data = destination(item).read_bytes()
        check_bytes(data, item)
        # Equal bytes only need one expensive silhouette inspection per shape.
        key = (item['sha256'], item['category'] == 'cards')
        if key not in decoded:
            check_image(data, item)
            decoded.add(key)
    print(f'Validated {len(entries):,} files: paths, SHA-256, bytes, PNG dimensions and alpha silhouettes.')


def spreadsheet_safe(value):
    text = str(value or '')
    return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text


def private_asset_path(url):
    """Return an upstream object path without retaining its private host."""
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or PUBLIC_OBJECT_MARKER not in parsed.path:
        return None
    object_path = unquote(parsed.path.split(PUBLIC_OBJECT_MARKER, 1)[1])
    if '/' not in object_path:
        return None
    _, upstream_path = object_path.split('/', 1)
    parts = PurePosixPath(upstream_path).parts
    if len(parts) < 2 or any(part in ('.', '..') for part in parts) or upstream_path != '/'.join(parts):
        return None
    return upstream_path


def redact_private_source_urls(entries):
    """Replace internal asset URLs with stable public repository references."""
    public_paths = defaultdict(list)
    for item in entries:
        public_paths[item['upstreamPath']].append(item['path'])
    changed = 0
    for item in entries:
        source = item['source']
        upstream_path = private_asset_path(source.get('url'))
        if not upstream_path:
            continue
        source['url'] = None
        matches = sorted(public_paths.get(upstream_path, []))
        if matches:
            source['reference'] = matches[0]
        changed += 1
    return changed


def check_no_private_source_urls(entries):
    exposed = [item['path'] for item in entries if private_asset_path(item['source'].get('url'))]
    if exposed:
        raise ValueError(f'Private asset source URL in public catalog: {exposed[:10]}')


def write_category_catalogs(index, entries):
    for category in index['categories']:
        batch = [item for item in entries if item['category'] == category['id']]
        (ROOT/category['path']).write_text(json.dumps(batch, indent=2, ensure_ascii=False)+'\n')


def redact_catalog(index, entries):
    changed = redact_private_source_urls(entries)
    write_category_catalogs(index, entries)
    export_csv(entries)
    print(f'Redacted {changed:,} private source URLs and refreshed public catalog exports.')


def export_csv(entries):
    check_no_private_source_urls(entries)
    with (ROOT/'catalog.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(['id', 'name', 'aliases', 'category', 'path', 'upstream_path', 'width', 'height', 'bytes', 'sha256', 'source_kind', 'source_url', 'source_reference'])
        for item in entries:
            writer.writerow([spreadsheet_safe(item['id']), spreadsheet_safe(item['name']), spreadsheet_safe('; '.join(item.get('aliases', [])))] +
                            [spreadsheet_safe(item[k]) for k in ('category', 'path', 'upstreamPath', 'width', 'height', 'bytes', 'sha256')] +
                            [spreadsheet_safe(item['source'].get(k)) for k in ('kind', 'url', 'reference')])


def slugify(value):
    value = str(value or '').replace('&', ' and ').replace('+', ' plus ')
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', value).strip('-')


def clean_aliases(values):
    aliases, seen = [], set()
    for value in values or []:
        value = ' '.join(str(value).split())
        folded = value.casefold()
        if value and folded not in seen:
            aliases.append(value)
            seen.add(folded)
    return aliases


def assign_paths(entries):
    used = defaultdict(set)
    for item in sorted(entries, key=lambda x: (x['category'], x['name'].casefold(), x['id'])):
        candidates = [item.pop('seoSlug', None), item['name'], *item.get('aliases', []), item['id']]
        base = next((slugify(value) for value in candidates if slugify(value)), 'payment-icon')[:180].strip('-')
        stem, number = base, 2
        while stem in used[item['category']]:
            stem = f'{base}-{number}'
            number += 1
        used[item['category']].add(stem)
        item['path'] = f"{item['category']}/{stem}.png"


def materialize_existing(entries, old_entries):
    old_by_id = {(item['category'], item['id']): item for item in old_entries}
    copied = 0
    for item in entries:
        old = old_by_id.get((item['category'], item['id']))
        if not old:
            continue
        source, target = ROOT / old['path'], ROOT / item['path']
        if source == target or not source.is_file() or target.exists():
            continue
        data = source.read_bytes()
        if len(data) == item['bytes'] and hashlib.sha256(data).hexdigest() == item['sha256']:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied += 1
    print(f'Reused {copied:,} unchanged PNGs at their descriptive paths.')


def markdown_text(value):
    return (str(value).replace('\\', '\\\\').replace('|', '\\|').replace('[', '\\[').replace(']', '\\]')
            .replace('\n', ' ').replace('<', '&lt;').replace('>', '&gt;'))


def export_category_readmes(entries):
    groups = defaultdict(list)
    for item in entries:
        groups[item['category']].append(item)
    for category, label in NAMES.items():
        batch = sorted(groups[category], key=lambda x: (x['name'].casefold(), x['path']))
        title = 'Payment method icons and logos' if category == 'payment-methods' else f'{label} payment icons and assets'
        lines = [f'# {title}', '',
                 f'{len(batch):,} descriptive PNG files from the [Aurato Global Payment Icon Pack](../README.md).', '']
        if category == 'cards':
            lines += ['Card filenames combine market, issuer and product information for discoverability.',
                      'Browse the complete machine-readable [card catalog](../catalog/cards.json) or use the [searchable gallery](../index.html).', '']
        else:
            lines += ['| Name | Aliases | PNG |', '| --- | --- | --- |']
            for item in batch:
                aliases = ', '.join(item.get('aliases', [])) or '—'
                filename = item['path'].split('/')[-1]
                lines.append(f"| {markdown_text(item['name'])} | {markdown_text(aliases)} | [{filename}]({filename}) |")
            lines.append('')
        (ROOT/category/'README.md').write_text('\n'.join(lines), encoding='utf-8')


def prune(entries):
    expected = {item['path'] for item in entries}
    retired = sorted(p for category in NAMES for p in (ROOT/category).rglob('*.png')
                     if p.relative_to(ROOT).as_posix() not in expected)
    for path in retired:
        path.unlink()
    print(f'Removed {len(retired):,} retired or superseded PNG paths.')


def import_snapshot(path):
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or not rows:
        raise ValueError('Expected a nonempty JSON array from scripts/export.sql')
    _, old_entries = load_catalog()
    if not SOURCE:
        raise ValueError('Set AURATO_ASSET_BASE_URL to the private entity-minis base URL before import')

    def prepare(row):
        item = {k: row[k] for k in ('id', 'name', 'category')}
        item.update(width=48, height=30 if row['category'] == 'cards' else 48,
                    bytes=1, sha256='0'*64, path=f"{row['category']}/snapshot.png")
        item['upstreamPath'] = row['upstream_path']
        item['seoSlug'] = row.get('seo_slug')
        item['aliases'] = clean_aliases(row.get('aliases'))
        item['source'] = {'kind': row['source_kind'], 'url': row['source_url']}
        if item['source']['url'] and item['source']['url'].startswith('/'):
            item['source']['reference'] = item['source']['url']
            item['source']['url'] = None
        check_entry(item)
        request = Request(f'{SOURCE}/{item["upstreamPath"]}', headers={'User-Agent': 'Aurato-Payment-Icons/1.0'})
        with urlopen(request, timeout=30) as response:
            raw = response.read(5_000_001)
        if (len(raw) != row['source_bytes'] or
                hashlib.md5(raw).hexdigest() != row['source_etag'].strip('"')):
            raise ValueError(f'Source changed since approved export: {item["id"]}')
        data = png_from_mini(raw, item['category'])
        item.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        cache = ROOT / '.cache' / 'sha256'
        cache.mkdir(parents=True, exist_ok=True)
        (cache / (item['sha256'] + '.png')).write_bytes(data)
        return item

    with ThreadPoolExecutor(max_workers=16) as executor:
        entries = list(executor.map(prepare, rows))
    present = {item['category'] for item in entries}
    missing = set(NAMES) - present
    if missing:
        raise ValueError(f'Unexpected missing categories: {sorted(missing)}')
    assign_paths(entries)
    entries.sort(key=lambda x: x['path'])
    redact_private_source_urls(entries)
    for item in entries:
        check_entry(item)
    if len({x['path'] for x in entries}) != len(entries):
        raise ValueError('Duplicate asset paths in snapshot')
    counts = Counter(x['category'] for x in entries)
    if set(counts) != set(NAMES):
        raise ValueError(f'Snapshot must contain all {len(NAMES)} categories')
    old_paths = {x['path'] for x in old_entries}
    new_paths = {x['path'] for x in entries}
    index, _ = load_catalog()
    index.update(schemaVersion=2, version=date.today().strftime('%Y.%m.%d'), updatedAt=date.today().isoformat(), assetCount=len(entries),
                 primaryIconCount=len(entries)-counts['cards'], cardCount=counts['cards'])
    materialize_existing(entries, old_entries)
    for category in index['categories']:
        category['count'] = counts[category['id']]
    write_category_catalogs(index, entries)
    (ROOT/'catalog.json').write_text(json.dumps(index, indent=2)+'\n')
    export_csv(entries)
    export_category_readmes(entries)
    print(f'Imported {len(entries)} entries. {len(new_paths-old_paths)} new paths, {len(old_paths-new_paths)} removed paths.')
    print('Review the diff, then run download and validate. Remove retired files explicitly; validation detects them.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['download', 'validate', 'export', 'import', 'prune', 'redact'])
    parser.add_argument('snapshot', nargs='?', type=Path)
    parser.add_argument('--workers', type=int, default=12)
    args = parser.parse_args()
    if args.command == 'import':
        if not args.snapshot:
            parser.error('import requires a JSON snapshot path')
        import_snapshot(args.snapshot)
        return
    index, entries = load_catalog()
    if args.command == 'download':
        if not 1 <= args.workers <= 48:
            parser.error('--workers must be between 1 and 48')
        download(entries, args.workers)
    elif args.command == 'validate':
        validate(entries)
    elif args.command == 'prune':
        prune(entries)
    elif args.command == 'export':
        export_csv(entries)
    elif args.command == 'redact':
        redact_catalog(index, entries)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
