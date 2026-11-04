"""Extract the observed monthly retail purchase-price table and shared value."""
import csv
import hashlib
import json
import re
from pathlib import Path

import pdfplumber

HEADER = ['时段t', 'Q中长期合同电量', 'Q日前市场出清电量', 'Q实际用电量', 'K1', 'K2',
          'P中长期电价', 'P现货日前出清电价', 'P现货实时出清电价', 'P批发购电分时均价', 'P批发购电加权均价']
FIELDS = ['month', 'periodOrdinal', 'contractVolume', 'dayAheadClearedVolume', 'actualVolume',
          'k1', 'k2', 'contractPrice', 'dayAheadPrice', 'realTimePrice', 'periodPurchasePrice',
          'volumeUnit', 'priceUnit', 'page', 'sourceSha256']
SUMMARY_FIELDS = ['month', 'metricNative', 'valueNative', 'priceUnit', 'page', 'sourceSha256']
NUMBER = re.compile(r'^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$')


def number(value):
    value = (value or '').strip()
    if not NUMBER.fullmatch(value):
        raise ValueError('unrecognized_numeric_cell')
    return value


def parse_pdf(pdf, source_sha256, output_dir):
    raw = Path(pdf).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != source_sha256:
        raise ValueError('pdf_hash_mismatch')
    periods, summary, notes, found = [], [], [], 0
    with pdfplumber.open(Path(pdf)) as doc:
        full_text = '\n'.join(page.extract_text() or '' for page in doc.pages)
        match = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*批发购电分时均价信息披露', full_text)
        if not match or not 1 <= int(match.group(2)) <= 12:
            raise ValueError('unsupported_document_title')
        month = f'{match.group(1)}-{int(match.group(2)):02d}'
        for page_no, page in enumerate(doc.pages, 1):
            text = page.extract_text() or ''
            for table in page.find_tables():
                rows = table.extract()
                if not rows or [re.sub(r'\s+', '', x or '') for x in rows[0]] != HEADER:
                    continue
                found += 1
                if '单位：兆瓦时、元/兆瓦时' not in re.sub(r'\s+', '', text):
                    raise ValueError('unrecognized_units')
                if len(rows) != 25 or any(len(row) != 11 for row in rows):
                    raise ValueError('unsupported_table_shape')
                if [row[0] for row in rows[1:]] != [str(i) for i in range(1, 25)]:
                    raise ValueError('unexpected_native_periods')
                weighted = number(rows[1][10])
                if any(row[10] not in (None, '') for row in rows[2:]):
                    raise ValueError('weighted_price_not_single_merged_cell')
                cell = table.rows[1].cells[10]
                first, last = table.rows[1].cells[0], table.rows[24].cells[0]
                if not cell or not first or not last or abs(cell[1] - first[1]) > .5 or abs(cell[3] - last[3]) > .5:
                    raise ValueError('weighted_price_cell_not_full_table_span')
                for row in rows[1:]:
                    vals = [number(v) for v in row[1:10]]
                    periods.append(dict(zip(FIELDS, [month, row[0], *vals, '兆瓦时', '元/兆瓦时', page_no, digest])))
                summary.append(dict(month=month, metricNative='P批发购电加权均价', valueNative=weighted,
                                    priceUnit='元/兆瓦时', page=page_no, sourceSha256=digest))
                pos = text.find('备注')
                if pos >= 0:
                    notes.append({'page': page_no, 'textExtracted': text[pos:]})
    if found != 1 or len(periods) != 24 or len(summary) != 1:
        raise ValueError('unsupported_or_incomplete_table')
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    for name, fields, rows in [('periods.csv', FIELDS, periods), ('summary.csv', SUMMARY_FIELDS, summary)]:
        target = dest / name
        temp = target.with_suffix('.csv.tmp')
        with temp.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        temp.replace(target)
    result = {'status': 'parsed_with_review', 'month': month, 'sourceSha256': digest,
              'periodRows': len(periods), 'summaryRows': len(summary), 'notes': notes,
              'limitations': ['时段只保存原表序号，不推断钟点。', '合并单元格的加权均价只保存一条，不复制到各时段。',
                              '数字保留原文，包括千位分隔符；备注为PDF文本提取，仍需参考原件。']}
    (dest / 'parsed.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result
