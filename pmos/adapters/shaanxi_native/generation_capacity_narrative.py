"""Conservatively extract explicitly dated installed-capacity categories."""
import csv
import hashlib
import json
import re
from html.parser import HTMLParser

NUM = r'\d+(?:\.\d+)?'
HEAD = re.compile(r'(?:截至|截止)(\d{4})年(\d{1,2})月底，陕西全省发电装机达到(' + NUM + r')万千瓦，其中([^。]+)。')
ITEM = re.compile(r'(水电|火电|风电|太阳能|新型储能)(' + NUM + r')万千瓦')

class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

def parse_capacity(title, html):
    """Return only exact known capacity sentences; retain unresolved text separately."""
    p = _Text()
    p.feed(html if isinstance(html, str) else '')
    text = re.sub(r'\s+', '', ''.join(p.parts))
    title_match = re.fullmatch(r'(\d{4})年(\d{1,2})月(?:发电机组装机及发电总体情况|发电装机及分类型总体情况)', title or '')
    annual = re.fullmatch(r'(\d{4})年发电装机及分类型总体情况(?:（含新增规模）|年度)', title or '')
    match = HEAD.search(text)
    if not (title_match or annual) or not match:
        return {'status': 'requires_review_unrecognized_format', 'values': [], 'visibleText': text}
    year, month, total, fragment = match.groups()
    title_period = (title_match.group(1), int(title_match.group(2))) if title_match else (annual.group(1), 12)
    if (year, int(month)) != title_period or not 1 <= int(month) <= 12:
        return {'status': 'requires_review_period_mismatch', 'values': [], 'visibleText': text}
    chunks = fragment.split('、')
    pairs = [ITEM.fullmatch(chunk) for chunk in chunks]
    if any(z is None for z in pairs):
        return {'status': 'requires_review_category_format', 'values': [], 'visibleText': text}
    pairs = [(z.group(1), z.group(2)) for z in pairs]
    names = [z[0] for z in pairs]
    if len(names) != len(set(names)) or not {'水电', '火电', '风电', '太阳能'}.issubset(names):
        return {'status': 'requires_review_categories', 'values': [], 'visibleText': text}
    source = match.group(0)
    values = [{'period': f'{year}-{int(month):02d}', 'metric': '月末发电装机', 'category': category,
               'value': value, 'unit': '万千瓦', 'sourceText': source} for category, value in [('全省合计', total)] + pairs]
    # This parser only models the capacity sentence. It never changes or
    # derives the year of other statements in the same native disclosure.
    return {'status': 'parsed_capacity_sentence_only', 'values': values, 'visibleText': text}

def export_page(result, destination):
    """Write a derivative of already captured website-decoded listing rows."""
    records = result.get('rows')
    decoded = result.get('site_decoded_rows')
    if not isinstance(records, list) or not isinstance(decoded, list) or len(records) != len(decoded):
        raise ValueError('listing_and_site_decoded_rows_mismatch')
    values, statuses = [], []
    for record, shown in zip(records, decoded):
        guid = record.get('guid')
        if not guid or guid != shown.get('native_guid') or shown.get('provenance') != 'original_site_component_after_this_completed_native_query':
            raise ValueError('native_decoded_row_identity_mismatch')
        content = shown.get('site_content_value')
        parsed = parse_capacity(record.get('title'), content)
        digest = hashlib.sha256((content if isinstance(content, str) else '').encode()).hexdigest()
        statuses.append({'nativeGuid': guid, 'status': parsed['status'], 'contentSha256': digest})
        for row in parsed['values']:
            values.append({**row, 'nativeGuid': guid, 'title': record.get('title'), 'contentSha256': digest,
                           'responseSha256': result.get('sha256'), 'provenance': 'site_component_transformed_value'})
    destination.mkdir(parents=True, exist_ok=True)
    fields = ['period', 'metric', 'category', 'value', 'unit', 'sourceText', 'nativeGuid', 'title',
              'contentSha256', 'responseSha256', 'provenance']
    target = destination / 'generation_capacity_values.csv'
    tmp = destination / 'generation_capacity_values.csv.tmp'
    with tmp.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(values)
    tmp.replace(target)
    return {'status': 'completed_with_review' if any(x['status'] != 'parsed_capacity_sentence_only' for x in statuses) else 'parsed',
            'records': len(records), 'values': len(values), 'recordsStatus': statuses,
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'limitation': 'Only recognized capacity sentences are structured; all other content remains in original site-decoded rows.'}
