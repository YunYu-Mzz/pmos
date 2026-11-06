"""Offline analysis of committed watch files. No browser or network imports.

Run separately from the acquisition owner. Field observations prove only the
listed file/scope, never all dimensions, final validity or platform first-publication.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
import zipfile

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
# Reviewed against retained SHA-verified originals, not inferred for other pages.
KEYS = {
    'queryMarketClearingRT': ['日期'],
    'queryFrequencyRegulationCapacityMileagePrice': ['日期', '时段'],
    'queryGridActual96': ['日期', '时点'],
    'queryRealtimeSourceClearedEnergy': ['日期', '时点'],
    'queryRealtimeMarketInformationDisclosure': ['日期', '时点'],
    'queryRealtimeTieLineOutput': ['通道', '日期', '时点'],
    'queryRealtimeNodeMarginalPriceExport': ['节点名称', '日期', '时点'],
    'candidate_22277879895b': ['日期', '时点'],
    'candidate_2e1f8d6d3b00': ['通道', '日期', '时点'],
    'candidate_47630fcb2723': ['业务日期', '时段'],
    'candidate_50921de23c83': ['日期', '时点'],
    'candidate_a4e0f23b313b': ['日期', '时点'],
    'candidate_b556a4714d2e': ['售电公司名称', '票据类别', '开具单位', '签发日期', '到期日'],
    'candidate_ba6db7f6d082': ['企业名称'],
    'candidate_bb69bffd311f': ['日期', '时点'],
    'candidate_c7334fde6d75': ['送出省', '接受省', '名称', '途经线路', '业务日期', '数据类型'],
    'candidate_e55f28a06368': ['日期', '时点'],
    'queryRealtimeSectionConstraints': ['断面名称', '日期', '时点'],
    'queryRealtimeReserveCapacity': ['日期'],
    'alt_tab_candidate_7440a7773d7e': ['业务日期', '线路', '数据类型', '日内时间段'],
    'alt_tab_candidate_b8c8f55fe60f': ['送出省', '接受省', '名称', '途经线路', '数据类型', '业务日期', '日内时间段'],
    'alt_tab_candidate_f7296dd65da3': ['省份', '机组类型', '身份', '业务日期', '日内时间段'],
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def value_kind(value):
    if value is None or value == '':
        return 'blank'
    if value in ('无', '--', '—', 'null', 'NULL'):
        return 'placeholder'
    try:
        number = Decimal(str(value))
        if number.is_finite():
            return 'zero' if number == 0 else 'nonzero_numeric'
    except (InvalidOperation, ValueError):
        pass
    return 'text'


def col_index(address):
    match = re.fullmatch(r'([A-Z]+)[0-9]+', address or '')
    if not match:
        raise ValueError('cell_address_unrecognized')
    result = 0
    for char in match[1]:
        result = result * 26 + ord(char) - 64
    return result - 1


def sheet_rows(archive, member, shared):
    # Deliberately ignore bogus <dimension ref="A1"> from portal exports.
    with archive.open(member) as stream:
        for _, node in ET.iterparse(stream, events=('end',)):
            if node.tag != '{' + NS['m'] + '}row':
                continue
            row = {}
            for cell in node.findall('m:c', NS):
                value = cell.findtext('m:v', default=None, namespaces=NS)
                if cell.get('t') == 's' and value is not None:
                    value = shared[int(value)]
                elif cell.get('t') == 'inlineStr':
                    item = cell.find('m:is', NS)
                    value = ''.join(item.itertext()) if item is not None else None
                if value is not None:
                    row[col_index(cell.get('r'))] = value
            if row:
                yield row
            node.clear()


def summarize(dataset, payload, extension):
    """Bounded field digest. Duplicate rows remain counted, zero is never blank."""
    fields = []
    if extension == '.json':
        body = json.loads(payload)
        def walk(item, path='', depth=0):
            if depth > 20 or len(fields) >= 100000:
                raise ValueError('json_analysis_bound_exceeded')
            if isinstance(item, dict):
                for key, child in item.items():
                    walk(child, path + '.' + str(key) if path else str(key), depth + 1)
            elif isinstance(item, list):
                # No verified stable row identity: retain array order, do not invent keys.
                fields.append({'sheet': 'json', 'column': path, 'label': path, 'digest': digest(item),
                               'counts': {'array': len(item)}, 'identity': 'array_order_preserved_unverified'})
            else:
                fields.append({'sheet': 'json', 'column': path, 'label': path, 'digest': digest(item),
                               'counts': {value_kind(item): 1}, 'identity': 'json_field_path'})
        # Envelope status/message are transport metadata, not business disclosure fields.
        walk(body.get('data') if isinstance(body, dict) and 'data' in body else body, 'data')
        return {'fields': fields, 'semantics': 'json_paths; date_requires_request_binding', 'format': 'json'}
    if extension != '.xlsx':
        return {'fields': [], 'format': extension, 'reason': 'dedicated_format_processor_required'}
    with zipfile.ZipFile(io.BytesIO(payload)) as book:
        if sum(x.file_size for x in book.infolist()) > 150 * 1024 * 1024:
            raise ValueError('uncompressed_analysis_bound_exceeded')
        if book.testzip() is not None:
            raise ValueError('workbook_crc_failed')
        shared = []
        if 'xl/sharedStrings.xml' in book.namelist():
            shared = [''.join(node.itertext()) for node in ET.fromstring(book.read('xl/sharedStrings.xml'))]
        for member in sorted(n for n in book.namelist() if re.fullmatch(r'xl/worksheets/sheet[0-9]+\.xml', n)):
            rows = iter(sheet_rows(book, member, shared))
            header = next(rows, None)
            if header is None:
                continue
            width = max(header) + 1
            labels = [header.get(i, '') for i in range(width)]
            contract = KEYS.get(dataset)
            key_columns = [labels.index(label) for label in contract] if contract and all(labels.count(label) == 1 for label in contract) else None
            cell_hashes = [[] for _ in labels]
            counts = [Counter() for _ in labels]
            seen, duplicate_keys, empty_keys, row_count = set(), 0, 0, 0
            for number, row in enumerate(rows):
                row_count += 1
                if (number + 1) * width > 2000000:
                    raise ValueError('field_cell_analysis_bound_exceeded')
                if any(i >= width for i in row):
                    raise ValueError('data_column_outside_header')
                key = [row.get(i) for i in key_columns] if key_columns else ['row_position', number]
                key_hash = digest(key)
                duplicate_keys += key_hash in seen
                empty_keys += any(value is None or value == '' for value in key)
                seen.add(key_hash)
                for i in range(width):
                    value = row.get(i)
                    cell_hashes[i].append(hashlib.sha256(encoded([key, value])).digest())
                    counts[i][value_kind(value)] += 1
            identity = ('reviewed_unique_key' if key_columns is not None and not duplicate_keys and not empty_keys else
                        'reviewed_key_ambiguous' if key_columns is not None else 'row_position_unreviewed')
            for i, label in enumerate(labels):
                if not label:
                    continue
                sequence = cell_hashes[i]
                # Sorting only the reviewed business keys removes false row-reorder changes.
                # In unreviewed tables the ordinal is in each hash, so order remains meaningful.
                sequence.sort()
                h = hashlib.sha256()
                for item in sequence:
                    h.update(item)
                fields.append({'sheet': member, 'column': i + 1, 'label': label, 'digest': h.hexdigest(),
                               'counts': dict(counts[i]), 'rows': row_count, 'identity': identity,
                               'duplicate_keys': duplicate_keys, 'empty_keys': empty_keys,
                               'unit': (re.search(r'[（(]([^（）()]*)[）)]', label).group(1)
                                        if re.search(r'[（(]([^（）()]*)[）)]', label) else None)})
    return {'fields': fields, 'format': 'xlsx', 'semantics': 'field digests retain duplicate count; raw originals unchanged'}


def verified_file(root, relative, expected):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError('original_path_outside_archive')
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError('original_sha256_mismatch')
    return path, payload


class Processor:
    def __init__(self, instance):
        self.instance = Path(instance)
        directory = self.instance / 'update_watch_v2'
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / 'fields.sqlite3')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS captures(id TEXT PRIMARY KEY,record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS field_state(key TEXT PRIMARY KEY,record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS field_events(capture TEXT,field TEXT,record TEXT NOT NULL,PRIMARY KEY(capture,field));''')

    def close(self):
        self.db.close()

    def process(self, capture):
        capture_id = digest(capture)
        if self.db.execute('SELECT 1 FROM captures WHERE id=?', (capture_id,)).fetchone():
            return False
        if capture.get('failed'):
            with self.db:
                self.db.execute('INSERT INTO captures VALUES(?,?)', (capture_id, json.dumps(capture, ensure_ascii=False)))
            return True
        path, payload = verified_file(self.instance / 'update_observations', capture['original_path'], capture['sha256'])
        analysis = summarize(capture['dataset'], payload, path.suffix)
        previous = {}
        if capture.get('previous_original_path'):
            old_path, old_payload = verified_file(self.instance / 'update_observations', capture['previous_original_path'], capture['previous_sha256'])
            old = summarize(capture['dataset'], old_payload, old_path.suffix)
            previous = {(x['sheet'], x['column'], x['label']): x for x in old['fields']}
        present = {(x['sheet'], x['column'], x['label']) for x in analysis['fields']}
        removed = [{**field, 'digest': None, 'counts': {'field_absent': 1}, 'missing_in_current': True}
                   for key, field in previous.items() if key not in present]
        with self.db:
            for field in [*analysis['fields'], *removed]:
                key = digest([capture['dataset'], capture['period'], capture.get('scope', ''), field['sheet'], field['column'], field['label']])
                old = previous.get((field['sheet'], field['column'], field['label']))
                count = field['counts']
                nonempty = sum(n for category, n in count.items() if category not in ('blank', 'placeholder', 'field_absent'))
                before_count = old['counts'] if old else {}
                before_nonempty = sum(n for category, n in before_count.items() if category not in ('blank', 'placeholder', 'field_absent'))
                event = {**field, 'dataset': capture['dataset'], 'period': capture['period'], 'scope': capture.get('scope', ''),
                         'started_at': capture['started_at'], 'finished_at': capture['finished_at'],
                         'window_seconds': capture.get('window_seconds'), 'within_900_seconds': capture.get('within_900_seconds'),
                         'state': 'field_absent' if field.get('missing_in_current') else 'first_observation_left_censored' if old is None else 'unchanged' if old['digest'] == field['digest'] else 'changed',
                         'previous_started_at': capture.get('previous_started_at'), 'sha256': capture['sha256'],
                         'previous_sha256': capture.get('previous_sha256'), 'original_path': capture['original_path'],
                         'appeared_nonempty': old is not None and not before_nonempty and bool(nonempty),
                         'became_empty': old is not None and bool(before_nonempty) and not nonempty and not field.get('missing_in_current', False),
                         'became_absent': field.get('missing_in_current', False),
                         'zero_only': bool(nonempty) and count.get('zero', 0) == nonempty,
                         'platform_published_at': None, 'coverage': capture['coverage'],
                         'validity': 'observed source values; final validity/publication not established'}
                serialized = json.dumps(event, ensure_ascii=False)
                self.db.execute('INSERT INTO field_events VALUES(?,?,?)', (capture_id, key, serialized))
                self.db.execute('INSERT OR REPLACE INTO field_state VALUES(?,?)', (key, serialized))
            self.db.execute('INSERT INTO captures VALUES(?,?)', (capture_id, json.dumps({
                **capture, 'analysis_finished_epoch': time.time(), 'analyzed_fields': len(analysis['fields']),
                'analysis_reason': analysis.get('reason')}, ensure_ascii=False)))
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--instance', type=Path, required=True)
    parser.add_argument('--max-captures', type=int, default=100)
    args = parser.parse_args()
    worker = Processor(args.instance)
    path = args.instance / 'update_watch_v2/captures.jsonl'
    completed = 0
    try:
        if path.exists():
            with path.open() as stream:
                for line in stream:
                    if not line.endswith('\n'):
                        break  # Writer is committing the last line; inspect on the next run.
                    capture = json.loads(line)
                    if worker.process(capture):
                        completed += 1
                    if completed >= args.max_captures:
                        break
        print(json.dumps({'processed_new_captures': completed, 'network_requests': 0}))
    finally:
        worker.close()


if __name__ == '__main__':
    main()
