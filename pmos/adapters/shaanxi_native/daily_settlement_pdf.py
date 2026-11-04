"""Extract native daily-settlement PDF tables; retain units, missing cells and provenance."""
import csv
import datetime as dt
import hashlib
import json
import re
import subprocess
from pathlib import Path

NUM = r'-?\d+(?:\.\d+)?'
NUMBER = re.compile(r'^' + NUM + r'$')
SCHEMA = 'shaanxi_daily_settlement_v1'


class SettlementParseError(ValueError):
    pass


def source_qualifications(text):
    """Retain observed source notes verbatim after removing PDF line wraps."""
    compact=re.sub(r'\s+','',text)
    phrases=('上述中长期合同结算均价计算时暂未纳入中长期结算参考点，待系统功能完善后予以更新。',
             '表中结算均价计算时已纳入中长期结算参考点。',
             '表中数据统计范围为纳入现货市场参与范围的经营主体。',
             '表中价格仅为电能量结算均价，不包含各类市场运营费用。',
             '表中日前/实时统一结算点电价计算时均考虑发电企业厂用电率。',
             '表中日均价为日加权平均电价。')
    found=[phrase for phrase in phrases if phrase in compact]
    # Some documents state the data batch and an update date in the same
    # sentence. Retain that entire original sentence without inventing a year.
    batch_notes=re.findall(r'表中数据根据电网企业推送的最新批次现货日分时电量数据计算(?:，数据更新时间\d{1,2}月\d{1,2}日)?。',compact)
    return list(dict.fromkeys(found+batch_notes))


def _narrative(text, date):
    compact = re.sub(r'\s+', '', text)
    start = compact.find('一、发电侧结算情况')
    end = compact.find('表1', start)
    start2 = compact.find('二、用户侧结算情况', end)
    end2 = compact.find('表2', start2)
    if min(start, end, start2, end2) < 0:
        return [], 'requires_review_sections'
    sections = [('发电侧', compact[start:end]), ('用户侧', compact[start2:end2])]
    rows = []
    for side, section in sections:
        def take(pattern, category, fields):
            matches = list(re.finditer(pattern, section))
            if len(matches) != 1:
                raise SettlementParseError('narrative_pattern_'+side+'_'+category)
            for field, value in zip(fields, matches[0].groups()):
                metric, unit = field
                rows.append({'date':date,'side':side,'category':category,'metric':metric,
                             'value':value,'unit':unit,'sourceText':matches[0].group(0)})
        q=('结算电量','亿千瓦时'); p=('结算均价','元/兆瓦时')
        if side == '发电侧':
            take(r'共结算发电企业上网电量('+NUM+r')亿千瓦时，结算均价('+NUM+r')元/兆瓦时','整体情况',[q,p])
            take(r'中长期合同、省间现货、备用市场电量('+NUM+r')亿千瓦时，结算均价('+NUM+r')元/兆瓦时','中长期合同、省间现货、备用市场',[q,p])
        else:
            if '共结算批发交易用户实际用电量' in section:
                take(r'共结算批发交易用户实际用电量('+NUM+r')亿千瓦时，结算均价('+NUM+r')元/兆瓦时','整体情况',[('实际用电量','亿千瓦时'),p])
            else:
                take(r'共结算批发交易用户上网电量('+NUM+r')亿千瓦时，结算均价('+NUM+r')元/兆瓦时','整体情况',[('上网电量','亿千瓦时'),p])
            take(r'中长期合同电量('+NUM+r')亿千瓦时，结算均价('+NUM+r')元/兆瓦时','中长期合同',[q,p])
            if '中长期结算参考点日均价' in section:
                take(r'中长期结算参考点日均价('+NUM+r')元/兆瓦时','中长期结算参考点',[('日均价','元/兆瓦时')])
        for market in ('日前','实时'):
            take(market+r'偏差电量('+NUM+r')亿千瓦时','%s偏差'%market,[q])
            match = re.search(market+r'偏差电量'+NUM+r'亿千瓦时[（(]正偏差('+NUM+r')亿千瓦时，结算均价('+NUM+r')元/兆瓦时；负偏差('+NUM+r')亿千瓦时，结算均价('+NUM+r')元/兆瓦时[）)]',section)
            if not match:
                raise SettlementParseError('narrative_bias_pattern_'+side+'_'+market)
            for category, quantity, price in ((market+'正偏差',match[1],match[2]),(market+'负偏差',match[3],match[4])):
                for (metric,unit),value in ((q,quantity),(p,price)):
                    rows.append({'date':date,'side':side,'category':category,'metric':metric,
                                 'value':value,'unit':unit,'sourceText':match.group(0)})
    return rows, 'parsed_known_template'


def _date(value):
    try:
        return dt.datetime.strptime(str(value), '%Y%m%d').date().isoformat()
    except ValueError as exc:
        raise SettlementParseError('invalid_catalogue_date') from exc


def parse_text(text, source_date, source_sha256, verified_page_count=None):
    date = _date(source_date)
    title = re.search(r'现货日结算总体情况及分类构成情况\s*[（(]\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*[）)]', text)
    if not title:
        raise SettlementParseError('unsupported_title_or_date')
    try:
        document_date = dt.date(*(int(x) for x in title.groups())).isoformat()
    except ValueError as exc:
        raise SettlementParseError('invalid_document_date') from exc
    if date != document_date:
        raise SettlementParseError('catalogue_and_document_date_mismatch')
    headings = ('发电侧现货日结算总体情况及分类构成情况',
                '用户侧现货日结算总体情况及分类构成情况',
                '用户侧现货日结算相关基础数据情况')
    markers = [re.search(r'(?m)^[ \t\f]*表[ \t]*' + str(i) + r'[ \t]*' + re.escape(heading) + r'[ \t]*$', text)
               for i, heading in enumerate(headings, 1)]
    if any(x is None for x in markers[:2]) or not markers[0].start() < markers[1].start():
        raise SettlementParseError('table_headers_missing_or_reordered')
    if markers[2] is not None and markers[2].start() < markers[1].start():
        raise SettlementParseError('table_headers_missing_or_reordered')
    if markers[2] is None and ('时点' in text[markers[1].end():] or re.search(r'表\s*[3-9]',text[markers[1].end():])):
        raise SettlementParseError('unrecognized_additional_table')
    if markers[2] is None:
        compact = re.sub(r'\s+','',text)
        older = '上述中长期合同结算均价计算时暂未纳入中长期结算参考点，待系统功能完善后予以更新。' in compact
        newer = ('表中结算均价计算时已纳入中长期结算参考点。' in compact and verified_page_count == 2)
        if not (older or newer):raise SettlementParseError('unverified_two_table_variant')
    tables = [text[markers[0].end():markers[1].start()], text[markers[1].end():markers[2].start() if markers[2] else len(text)]]
    labels = [('发电侧', {'整体情况','火电','风电','光伏'}), ('用户侧', {'整体情况','批发用户','售电公司'})]
    values = []
    for index, ((side, required), section) in enumerate(zip(labels, tables), 1):
        if not all(x in section for x in ('亿千瓦时','元/兆瓦时','结算电量','结算均价')):
            raise SettlementParseError('table_units_or_headers_changed')
        # Limit row discovery to the table, excluding its notes and the next
        # narrative. Preserve new native categories instead of silently dropping
        # them, while refusing unfamiliar row/cell layouts for review.
        if '统计项目' not in section or not re.search(r'注\s*[：:]',section):
            raise SettlementParseError('category_table_boundaries_changed')
        body=re.split(r'注\s*[：:]',section.split('统计项目',1)[1],maxsplit=1)[0]
        lines=body.splitlines()[1:];seen=set()
        for line in lines:
            line=re.sub(r'^\s*'+re.escape(side)+r'\s+','',line)
            line=re.sub(r'^\s*其中\s*[：:]\s*','',line).strip()
            if not line or line in (side,'其中：','其中:'):
                continue
            match=re.fullmatch(r'([\u4e00-\u9fffA-Za-z0-9（）()·、\-]+)\s+(.+?)\s*',line)
            if not match:raise SettlementParseError('unrecognized_category_row_'+side)
            category=match.group(1)
            if category in seen:raise SettlementParseError('ambiguous_category_'+side+'_'+category)
            seen.add(category);cells=match.group(2).split()
            if len(cells)==1 and cells[0] in ('\\','/','—','-','--'):
                amount=price=None; marker=cells[0]
            elif len(cells)==2 and all(NUMBER.fullmatch(v) for v in cells):
                amount,price=cells;marker=None
            else:
                raise SettlementParseError('unexpected_category_cells_'+side+'_'+category)
            values.append({'date':date,'table':index,'side':side,'category':category,
                           'quantity':amount,'quantityUnit':'亿千瓦时','price':price,'priceUnit':'元/兆瓦时',
                           'sourceMissingMarker':marker})
        if not required.issubset(seen):
            raise SettlementParseError('required_categories_missing_'+side)
    periods=[];averages=[]
    if markers[2]:
        tail=text[markers[2].end():]
        if not all(x in tail for x in ('中长期结算参考点现货价格','日前统一结算点电价','实时统一结算点电价','元/兆瓦时')):
            raise SettlementParseError('period_price_headers_changed')
        row_re = re.compile(r'^\s*(\d{1,3})\s+('+NUM+r')\s+('+NUM+r')\s+('+NUM+r')\s*$')
        prices=[]
        for line in tail.splitlines():
            m=row_re.match(line)
            if m:prices.append(m.groups())
        if [int(x[0]) for x in prices] != list(range(1,97)):
            raise SettlementParseError('period_rows_not_exactly_1_through_96')
        average=re.findall(r'(?m)^\s*日平均\s+('+NUM+r')\s+('+NUM+r')\s+('+NUM+r')\s*$',tail)
        if len(average)!=1: raise SettlementParseError('daily_average_missing_or_ambiguous')
        types=['中长期结算参考点现货价格（发电侧实时市场出清均价）','日前统一结算点电价（发电侧日前市场情况）','实时统一结算点电价（发电侧实时市场情况）']
        periods=[{'date':date,'table':3,'periodOrdinal':int(row[0]),'category':types[i],
                  'price':row[i+1],'priceUnit':'元/兆瓦时'} for row in prices for i in range(3)]
        averages=[{'date':date,'table':3,'category':types[i],'price':value,'priceUnit':'元/兆瓦时','label':'日平均'} for i,value in enumerate(average[0])]
    try:
        narrative, narrative_status = _narrative(text, date)
    except SettlementParseError as exc:
        narrative, narrative_status = [], str(exc)
    qualifications=source_qualifications(text)
    return {'schema':SCHEMA,'date':date,'sourceSha256':source_sha256,
            'categories':values,'periodPrices':periods,'dailyAveragePrices':averages,
            'narrativeValues':narrative,'narrativeStatus':narrative_status,
            'availableTables':[1,2,3] if markers[2] else [1,2], 'qualifications':qualifications,
            'periodMapping':'source ordinal only; clock time not inferred',
            'narrativeStructured':narrative_status == 'parsed_known_template',
            'notes':['Original missing-cell markers are preserved; missing is not zero.',
                     'Original PDF narrative remains available in layout text.']}


def parse_pdf(pdf, source_date, source_sha256, output_dir):
    pdf=Path(pdf); output_dir=Path(output_dir)
    if hashlib.sha256(pdf.read_bytes()).hexdigest()!=source_sha256:
        raise SettlementParseError('pdf_hash_mismatch')
    extracted=subprocess.run(['pdftotext','-layout',str(pdf),'-'],check=True,capture_output=True,timeout=30)
    text=extracted.stdout.decode('utf-8')
    try:
        result=parse_text(text,source_date,source_sha256)
    except SettlementParseError as exc:
        if str(exc)!='unverified_two_table_variant':raise
        # A two-page PDF can legitimately have the newer reference-point note
        # without the later period table. Check the original PDF's page count;
        # a truncated text extraction must never be treated as proof.
        info=subprocess.run(['pdfinfo',str(pdf)],check=True,capture_output=True,timeout=30).stdout.decode('utf-8')
        page_matches=re.findall(r'(?m)^Pages:\s*(\d+)\s*$',info)
        if len(page_matches)!=1:raise SettlementParseError('pdf_page_count_unavailable')
        result=parse_text(text,source_date,source_sha256,verified_page_count=int(page_matches[0]))
    output_dir.mkdir(parents=True,exist_ok=True)
    _atomic(output_dir/'settlement_layout.txt',text)
    _atomic(output_dir/'settlement_parsed.json',json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    fields=['date','table','side','category','quantity','quantityUnit','price','priceUnit','sourceMissingMarker','periodOrdinal','label','metric','value','unit','sourceText']
    tmp=output_dir/'settlement_values.csv.tmp'
    with tmp.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(result['categories']+result['periodPrices']+result['dailyAveragePrices']+result['narrativeValues']);f.flush()
    tmp.replace(output_dir/'settlement_values.csv')
    try:
        from .settlement_note_scopes import source_qualifications_by_scope
        from .settlement_classification import normalize,FIELDS
        normalized=normalize(result['categories'],source_qualifications_by_scope(text),source_sha256)
        target=output_dir/'settlement_classification_values.csv';tmp=target.with_suffix('.csv.tmp')
        with tmp.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=FIELDS);writer.writeheader();writer.writerows(normalized);f.flush()
        tmp.replace(target)
        classification={'status':'parsed','rows':len(normalized),'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest()}
    except Exception as exc:
        classification={'status':'requires_review','errorType':type(exc).__name__,'error':str(exc)}
        _atomic(output_dir/'settlement_classification_parse_failure.json',json.dumps(classification,ensure_ascii=False,indent=2)+'\n')
    return {'classification':classification,'status':'parsed_tables','schema':SCHEMA,'categoryRows':len(result['categories']),
            'periodPriceValues':len(result['periodPrices']),'dailyAveragePriceValues':len(result['dailyAveragePrices']),
            'narrativeValues':len(result['narrativeValues']),'narrativeStatus':result['narrativeStatus'],'availableTables':result['availableTables'],
            'narrativeStructured':result['narrativeStructured'],'path':str(output_dir/'settlement_parsed.json'),'sourceSha256':source_sha256}


def _atomic(path,text):
    tmp=path.with_name(path.name+'.tmp');tmp.write_text(text,encoding='utf-8');tmp.replace(path)
