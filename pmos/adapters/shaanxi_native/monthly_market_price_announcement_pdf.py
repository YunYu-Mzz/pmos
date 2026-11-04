"""Extract observed monthly wholesale-price announcement tables."""
import csv
import hashlib
import json
import re
from pathlib import Path

import pdfplumber

PERIOD_FIELDS = ['month', 'periodNative', 'volumeNative', 'priceNative', 'volumeUnit', 'priceUnit',
                 'page', 'sourceSha256', 'review']
SUMMARY_FIELDS = ['month', 'metricNative', 'priceNative', 'priceUnit', 'noteNative', 'page', 'sourceSha256']
NUMBER = re.compile(r'^-?\d+(?:\.\d+)?$')


def number(value):
    value = (value or '').strip()
    if not NUMBER.fullmatch(value):
        raise ValueError('unrecognized_number')
    return value


def parse_pdf(pdf, source_sha256, output_dir):
    pdf = Path(pdf)
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if digest != source_sha256:
        raise ValueError('pdf_hash_mismatch')
    periods, summary = [], []
    with pdfplumber.open(pdf) as doc:
        texts = [p.extract_text() or '' for p in doc.pages]
        range_title = re.search(r'关于\s*(\d{4})\s*年\s*(\d{1,2})\s*[-—–]\s*(\d{1,2})\s*月\s*电力批发市场交易价格情况的公告', '\n'.join(texts))
        if range_title:
            return parse_range(doc, texts, range_title, digest, output_dir)
        title = re.search(r'关于\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*电力批发市场交易价格情况的公告', '\n'.join(texts))
        if not title or not 1 <= int(title.group(2)) <= 12:
            raise ValueError('unsupported_document_title')
        year, month_num = title.group(1), int(title.group(2))
        month = f'{year}-{month_num:02d}'
        for page_no, page in enumerate(doc.pages, 1):
            text = texts[page_no - 1]
            compact = re.sub(r'\s+', '', text)
            for table in page.extract_tables():
                if not table: continue
                header = [re.sub(r'\s+', '', x or '') for x in table[0]]
                if header == ['时间', '中长期批发交易均价']:
                    if '单位：元/兆瓦时' not in compact or len(table) != 2 or len(table[1]) != 2:
                        raise ValueError('unsupported_summary_shape')
                    actual = re.sub(r'\s+', '', table[1][0] or '')
                    if actual != f'{year}年{month_num}月': raise ValueError('summary_month_mismatch')
                    note = re.search(r'备注[：:]([^\n]+)', text)
                    if not note: raise ValueError('summary_note_missing')
                    summary.append(dict(month=month, metricNative='中长期批发交易均价', priceNative=number(table[1][1]),
                                        priceUnit='元/兆瓦时', noteNative=note.group(0).strip(), page=page_no, sourceSha256=digest))
                elif header in (['时段', f'{month_num}月电量', f'{month_num}月电价'], ['时段', f'{month_num}月']):
                    has_volume = len(header) == 3
                    required_unit = '单位：兆瓦时、元/兆瓦时' if has_volume else '单位：元/兆瓦时'
                    if required_unit not in compact or len(table) != 25 or any(len(r) != len(header) for r in table):
                        raise ValueError('unsupported_period_shape')
                    expected = [f'{i:02d}:00-{i+1:02d}:00' for i in range(24)]
                    if [r[0] for r in table[1:]] != expected: raise ValueError('native_periods_not_complete')
                    for r in table[1:]:
                        volume, price = (number(r[1]), number(r[2])) if has_volume else ('', number(r[1]))
                        precision = len(volume.partition('.')[2])
                        review = ('原表未披露分时电量' if not has_volume else
                                  '原文电量小数位多于3位，保留原值待核' if precision > 3 else '')
                        periods.append(dict(month=month, periodNative=r[0], volumeNative=volume, priceNative=price,
                                            volumeUnit='兆瓦时' if has_volume else '', priceUnit='元/兆瓦时', page=page_no,
                                            sourceSha256=digest, review=review))
    if len(summary) != 1 or len(periods) != 24: raise ValueError('unsupported_or_incomplete_tables')
    dest = Path(output_dir); dest.mkdir(parents=True, exist_ok=True)
    for name, fields, rows in [('periods.csv', PERIOD_FIELDS, periods), ('summary.csv', SUMMARY_FIELDS, summary)]:
        target = dest / name; temp = target.with_suffix('.csv.tmp')
        with temp.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        temp.replace(target)
    result = {'status': 'parsed_with_review', 'month': month, 'sourceSha256': digest,
              'periodRows': len(periods), 'summaryRows': len(summary),
              'reviewRows': sum(bool(row['review']) for row in periods),
              'limitations': ['月份来自公告及表格原文，不能以目录年份代替。', '多位小数按原文保留，不猜测四舍五入。']}
    if any(not row['volumeUnit'] for row in periods):
        result['volumeAvailable'] = False
        result['limitations'].append('原表仅披露分时电价，未披露分时电量；空值不是零。')
    (dest / 'parsed.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result


def parse_range(doc, texts, title, digest, output_dir):
    year, start, end = title.group(1), int(title.group(2)), int(title.group(3))
    if not 1 <= start <= end <= 12:
        raise ValueError('unsupported_month_range')
    months = list(range(start, end + 1))
    periods, summaries = [], []
    found_summary = found_period = False
    for page_no, page in enumerate(doc.pages, 1):
        text = texts[page_no - 1]
        compact = re.sub(r'\s+', '', text)
        for table in page.extract_tables():
            if not table: continue
            header = [re.sub(r'\s+', '', x or '') for x in table[0]]
            if header == ['时间', '中长期批发交易均价']:
                if found_summary or '表1中长期批发市场交易均价' not in compact or '单位：元/兆瓦时' not in compact or len(table) != len(months) + 1 or any(len(r) != 2 for r in table):
                    raise ValueError('unsupported_range_summary_shape')
                note = re.search(r'备注[：:]([^\n]+)', text)
                if not note: raise ValueError('summary_note_missing')
                for m, row in zip(months, table[1:]):
                    if re.sub(r'\s+', '', row[0] or '') != f'{year}年{m}月': raise ValueError('summary_month_mismatch')
                    summaries.append(dict(month=f'{year}-{m:02d}', metricNative='中长期批发交易均价', priceNative=number(row[1]), priceUnit='元/兆瓦时', noteNative=note.group(0).strip(), page=page_no, sourceSha256=digest))
                found_summary = True
            elif header == ['时段'] + [f'{m}月' for m in months]:
                if found_period or '表2现货实时市场分时均价' not in compact or '单位：元/兆瓦时' not in compact or len(table) != 25 or any(len(r) != len(header) for r in table):
                    raise ValueError('unsupported_range_period_shape')
                expected = [f'{i:02d}:00-{i+1:02d}:00' for i in range(24)]
                if [r[0] for r in table[1:]] != expected: raise ValueError('native_periods_not_complete')
                for row in table[1:]:
                    for m, value in zip(months, row[1:]):
                        periods.append(dict(month=f'{year}-{m:02d}', periodNative=row[0], volumeNative='', priceNative=number(value), volumeUnit='', priceUnit='元/兆瓦时', page=page_no, sourceSha256=digest, review='原表未披露分时电量', metricNative='现货实时市场分时均价'))
                found_period = True
    if not found_summary or not found_period or len(summaries) != len(months) or len(periods) != 24 * len(months):
        raise ValueError('unsupported_or_incomplete_range_tables')
    dest = Path(output_dir); dest.mkdir(parents=True, exist_ok=True)
    for name, fields, rows in [('periods.csv', PERIOD_FIELDS + ['metricNative'], periods), ('summary.csv', SUMMARY_FIELDS, summaries)]:
        target = dest / name; temp = target.with_suffix('.csv.tmp')
        with temp.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        temp.replace(target)
    result = {'status':'parsed_with_review', 'monthRange':f'{year}-{start:02d}至{year}-{end:02d}', 'sourceSha256':digest, 'periodRows':len(periods), 'summaryRows':len(summaries), 'reviewRows':len(periods), 'volumeAvailable':False, 'limitations':['原表仅披露分时均价，未披露分时电量；空值不是零。','保留中长期批发交易均价原备注，分时均价为现货实时市场。']}
    (dest / 'parsed.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result
