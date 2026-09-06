"""Reversible capture-clock corrections; original files and filesystem dates stay intact."""
from datetime import datetime, timedelta, timezone
import math
import re

MAX_BATCH = 5000


def parse_timestamp(value: str) -> datetime:
    text = str(value or '').strip()
    if re.match(r'^\d{4}:\d{2}:\d{2}', text):
        text = text[:10].replace(':', '-') + text[10:]
    if not re.match(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}', text):
        raise ValueError('A complete capture date and time is required')
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('Invalid capture date or time') from error


def normalized_timestamp(value: str | None) -> str | None:
    return None if value is None else parse_timestamp(value).isoformat(timespec='seconds')


def zone(value):
    if value == 'remove':
        return None
    if not re.fullmatch(r'[+-]\d{2}:\d{2}', str(value)):
        raise ValueError('Time zone must be an offset such as -04:00 or +05:30')
    hours, minutes = map(int, value[1:].split(':'))
    if minutes > 59 or hours > 14 or (hours == 14 and minutes):
        raise ValueError('Time zone must be between -14:00 and +14:00')
    return timezone(timedelta(minutes=(hours * 60 + minutes) * (-1 if value[0] == '-' else 1)))


def corrected_timestamp(value, shift_seconds=0, time_zone=''):
    try:
        shift = float(shift_seconds)
    except (ValueError, TypeError) as error:
        raise ValueError('Clock shift must be a number of seconds') from error
    if not math.isfinite(shift) or abs(shift) > 366 * 86400 * 100:
        raise ValueError('Clock shift must be within 100 years')
    stamp = parse_timestamp(value)
    try:
        stamp += timedelta(seconds=shift)
    except OverflowError as error:
        raise ValueError('Corrected capture time is outside the supported date range') from error
    if time_zone:
        # Assign the known camera zone; changing this does not also move the clock.
        stamp = stamp.replace(tzinfo=zone(time_zone))
    return stamp.isoformat(timespec='seconds')


def exif_fields(value):
    stamp = parse_timestamp(value)
    offset = stamp.strftime('%z')
    return {'DateTimeOriginal': stamp.strftime('%Y:%m:%d %H:%M:%S'),
            'OffsetTimeOriginal': offset[:3] + ':' + offset[3:] if offset else ''}


def preview(cat, names, resolve_id, metadata, *, shift_seconds=0, time_zone='', include_pairs=False, reset=False):
    if not isinstance(names, list) or not names or len(names) > MAX_BATCH:
        raise ValueError(f'Select between 1 and {MAX_BATCH} photos')
    # Validate even an empty/missing-date selection before any partial proposal.
    corrected_timestamp('2000-01-01T00:00:00', shift_seconds, time_zone)
    selected = list(dict.fromkeys(map(str, names)))
    if include_pairs:
        for name in tuple(selected):
            image_id = resolve_id(name)
            if image_id is not None:
                selected.extend(cat.paired_image_names(image_id))
    changes, skipped, seen = [], [], set()
    for name in dict.fromkeys(selected):
        image_id = resolve_id(name)
        info = cat.capture_details(image_id) if image_id is not None else None
        if not info:
            skipped.append({'name': name, 'reason': 'Photo is not in the catalog'})
            continue
        if info['fileId'] in seen:
            continue
        seen.add(info['fileId'])
        try:
            if reset:
                after = None
                before = info['override'] or info['original']
                if info['override'] is None:
                    continue
            else:
                camera = metadata(name) if info['override'] is None else {}
                before = info['override'] or camera.get('DateTimeOriginal') or info['original']
                stamp = parse_timestamp(before)
                if not info['override'] and stamp.tzinfo is None and camera.get('OffsetTimeOriginal'):
                    stamp = stamp.replace(tzinfo=zone(camera['OffsetTimeOriginal']))
                before = stamp.isoformat(timespec='seconds')
                after = corrected_timestamp(before, shift_seconds, time_zone)
                if after == before:
                    continue
            changes.append({'name': name, 'fileId': info['fileId'], 'before': before,
                            'after': after, 'beforeOverride': info['override'],
                            'original': info['original']})
        except ValueError as error:
            skipped.append({'name': name, 'reason': str(error)})
    return {'ok': True, 'changes': changes, 'skipped': skipped,
            'count': len(changes), 'selectionCount': len(names)}
