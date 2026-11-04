"""Extract explicit counts from observed monthly network-maintenance PDF text."""
import csv, datetime as dt, hashlib, json, re
from pathlib import Path

import pdfplumber

NARRATIVE = re.compile(
    r'(?P<month>\d{1,2})月(?:份)?'
    r'(?P<planLabel>安排电气检修项目|计划检修|输变电设备计划检修|安排主网计划检修|安排计划检修)'
    r'(?P<plan>\d+)项，(?:(?:取消(?P<cancelBefore>\d+)项)，)?'
    r'(?P<actualLabel>按计划执行|实际执行)(?P<actual>\d+)项'
    r'(?:，取消(?P<cancelAfter>\d+)项)?。'
)
FIELDS = ['catalogueDate','sourceMonthNative','sourceYearNative','metricNative','valueNative','unitNative',
          'sourceTextNative','sourceTextCompact','sourcePage','sourceSha256','review']

def parse_pdf(pdf, source_date, source_sha256, output_dir):
    pdf=Path(pdf); digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
    if digest != source_sha256: raise ValueError('pdf_hash_mismatch')
    try: catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
    except ValueError as exc: raise ValueError('invalid_catalogue_date') from exc
    with pdfplumber.open(pdf) as doc:
        if len(doc.pages)!=1: raise ValueError('unsupported_page_count')
        raw=doc.pages[0].extract_text() or ''
    compact=re.sub(r'\s+','',raw); m=NARRATIVE.fullmatch(compact)
    if not m: raise ValueError('unrecognized_network_execution_narrative')
    month=int(m['month'])
    if not 1 <= month <= 12: raise ValueError('invalid_source_month')
    cancel=m['cancelBefore'] if m['cancelBefore'] is not None else m['cancelAfter']
    metrics=[(m['planLabel'],m['plan']), (m['actualLabel'],m['actual'])]
    if cancel is not None: metrics.append(('取消',cancel))
    issues=['原文仅写月份，未披露年份；不能由目录日期推定正文年份']
    if cancel is not None and int(m['plan']) != int(m['actual'])+int(cancel):
        issues.append('原文计划项数与执行项数加取消项数不一致，口径待核')
    rows=[{'catalogueDate':catalogue,'sourceMonthNative':str(month),'sourceYearNative':'',
           'metricNative':label,'valueNative':value,'unitNative':'项','sourceTextNative':raw,
           'sourceTextCompact':compact,'sourcePage':1,'sourceSha256':digest,'review':'；'.join(issues)}
          for label,value in metrics]
    out={'status':'parsed_with_review','catalogueDate':catalogue,'sourceMonthNative':str(month),
         'sourceYearNative':'','rows':rows,'issues':issues,
         'limitations':['原文只披露项数，不等于设备数量或具体名单。','未披露年份或取消项时不推断。']}
    dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True)
    target=dest/'monthly_network_maintenance_execution_values.csv'; tmp=target.with_suffix('.csv.tmp')
    with tmp.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows);f.flush()
    tmp.replace(target); parsed=dest/'monthly_network_maintenance_execution_parsed.json';tmp=parsed.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(parsed)
    return {'status':out['status'],'sourceMonthNative':str(month),'sourceYearNative':'','rows':len(rows),
            'issues':issues,'sourceSha256':digest,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(parsed)}
