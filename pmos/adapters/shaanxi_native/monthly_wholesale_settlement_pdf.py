"""Extract observed wholesale monthly settlement tables without inventing cells."""
import csv
import hashlib
import json
import re
from pathlib import Path

import pdfplumber

TRADE_FIELDS = ['month', 'cycle', 'variety', 'cumulativeVolume', 'cumulativePrice',
                'monthVolume', 'monthPrice', 'volumeUnit', 'priceUnit', 'page', 'sourceSha256']
SUBJECT_FIELDS = ['month', 'side', 'subject', 'maxPrice', 'minPrice', 'priceUnit', 'page', 'sourceSha256']
NUMBER = re.compile(r'^-?\d+(?:\.\d+)?$')


def _number(value):
    value = (value or '').strip()
    if value and not NUMBER.fullmatch(value):
        raise ValueError('unrecognized_number:' + value)
    return value


def parse_pdf(pdf, source_sha256, output_dir):
    pdf = Path(pdf)
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if digest != source_sha256:
        raise ValueError('pdf_hash_mismatch')
    trades, subjects = [], []
    with pdfplumber.open(pdf) as doc:
        pages = [(p.extract_text() or '', p.extract_tables()) for p in doc.pages]
    title = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*省内批发侧电力交易',
                      '\n'.join(text for text, _ in pages))
    if not title or not 1 <= int(title.group(2)) <= 12:
        raise ValueError('unsupported_document_title')
    month = f'{title.group(1)}-{int(title.group(2)):02d}'
    trade_tables = subject_tables = 0
    for page_no, (text, tables) in enumerate(pages, 1):
        compact = re.sub(r'\s+', '', text)
        for table in tables:
            headers = table[0] if table else []
            trade_header = headers in (['周期', '交易品种', '累计成交情况', None, '当月成交情况', None],
                                       ['交易品种', '周期', '累计成交情况', None, '当月成交情况', None])
            if len(table) >= 3 and trade_header and table[1] == [None, None, '电量', '电价', '电量', '电价']:
                trade_tables += 1
                if '单位：亿千瓦时、元/兆瓦时' not in compact:
                    raise ValueError('unrecognized_trade_units')
                primary = None
                for row in table[2:]:
                    if len(row) != 6:
                        raise ValueError('unexpected_trade_columns')
                    if row[0] is not None:
                        primary = row[0].strip()
                    if not primary:
                        raise ValueError('missing_trade_group')
                    secondary = (row[1] or '').strip()
                    if primary == '合计':
                        if secondary:
                            raise ValueError('unexpected_total_identity')
                        cycle, variety = '合计', ''
                    else:
                        if not secondary:
                            raise ValueError('missing_trade_dimension')
                        cycle, variety = (primary, secondary) if headers[0] == '周期' else (secondary, primary)
                    trades.append(dict(month=month, cycle=cycle, variety=variety,
                        cumulativeVolume=_number(row[2]), cumulativePrice=_number(row[3]),
                        monthVolume=_number(row[4]), monthPrice=_number(row[5]),
                        volumeUnit='亿千瓦时', priceUnit='元/兆瓦时', page=page_no, sourceSha256=digest))
            elif table and table[0] == ['', '', '最大值', '最小值']:
                subject_tables += 1
                if '各经营主体月均价情况表' not in compact or '单位：元/兆瓦时' not in compact:
                    raise ValueError('unrecognized_subject_table')
                side = None
                for row in table[1:]:
                    if len(row) != 4:
                        raise ValueError('unexpected_subject_columns')
                    if row[0] is not None:
                        side = row[0].strip()
                    subject = (row[1] or '').strip()
                    if not side or not subject:
                        raise ValueError('missing_subject_identity')
                    subjects.append(dict(month=month, side=side, subject=subject,
                        maxPrice=_number(row[2]), minPrice=_number(row[3]),
                        priceUnit='元/兆瓦时', page=page_no, sourceSha256=digest))
    if trade_tables != 1 or subject_tables != 1 or not trades or not subjects:
        raise ValueError('unsupported_or_incomplete_tables')
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    for name, fields, rows in [('trade.csv', TRADE_FIELDS, trades), ('subjects.csv', SUBJECT_FIELDS, subjects)]:
        target = dest / name
        tmp = target.with_suffix('.csv.tmp')
        with tmp.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        tmp.replace(target)
    result = {'status': 'parsed_with_review', 'month': month, 'sourceSha256': digest,
              'tradeRows': len(trades), 'subjectRows': len(subjects),
              'limitations': ['空白保留为空，负值及精度按原表保留。', '只识别已核对表头与单位；其他格式待核。',
                              '本解析不证明来源下载HTTP状态或其他月份已采集。']}
    (dest / 'parsed.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result
