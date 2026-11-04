"""Extract observed source rows of monthly transmission maintenance PDFs."""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber

def clean(v):return re.sub(r'\s*\n\s*','',v).strip() if isinstance(v,str) else ''
HEADERS=[['序号','','电压等级','停电设备','工作开始时间','工作结束时间'],['序号','工作单位','','电压等级','停电设备','工作开始时间','工作结束时间'],['序号','工作单位','','电压等级','停电设备','工作开始时间','工作结束时间','工作时长'],['序号','场站','电压等级','停电设备','工作开始时间','工作结束时间','工作时长'],['序号','工作单位','电压等级','停电设备','工作开始时间','工作结束时间'],['序号','工作单位','电压等级','停电设备','工作开始时间','工作结束时间','工作时长']]
FIELDS=['catalogueDate','nativeTitle','titlePeriod','rowOrdinal','sequenceNative','unitColumn1Header','unitColumn1Native','unitColumn2Header','unitColumn2Native','unitColumn2Present','voltageNative','voltageUnitNative','equipmentNative','startNative','endNative','durationNative','durationFieldPresent','durationUnitNative','sourcePage','sourceTable','sourceRowOnTable','sourceRowBbox','nativeCells','review','sourceSha256']
def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as e:raise ValueError('invalid_catalogue_date') from e
 rows=[];issues=[];head=None;last_seq=0
 with pdfplumber.open(pdf) as doc:
  texts=[''.join((pg.extract_text() or '').split()) for pg in doc.pages]
  matches=list(re.finditer(r'陕西电网(\d{4})年(\d{1,2})月份主网输变电设备检修(?:计划)?',''.join(texts)))
  periods={(int(m[1]),int(m[2])) for m in matches}
  if len(periods)!=1:raise ValueError('missing_or_ambiguous_title')
  year,month=next(iter(periods));dt.date(year,month,1);period=f'{year:04d}-{month:02d}';title=matches[0].group(0)
  for pi,page in enumerate(doc.pages,1):
   tables=page.find_tables()
   if len(tables)!=1:raise ValueError('unsupported_page_table_count')
   table=tables[0];matrix=table.extract()
   if not matrix or len(matrix)!=len(table.rows):raise ValueError('invalid_table_geometry')
   h=[clean(x) for x in matrix[0]]
   if h not in HEADERS or head is not None and head!=h:raise ValueError('unsupported_or_changed_header')
   head=h;two_units=len(h)>2 and h[1]=='工作单位' and h[2]=='';voltage_ix=3 if two_units else 2;duration='工作时长' in h
   for ri,rawrow in enumerate(matrix[1:],2):
    c=[clean(x) for x in rawrow]
    if len(c)!=len(h):raise ValueError('row_column_count_mismatch')
    if not any(c):continue
    seq=c[0];review=[]
    if not seq:review.append('原文序号为空')
    elif not seq.isdigit():review.append('原文序号非数字')
    else:
     n=int(seq)
     if n!=last_seq+1:review.append('原文序号不连续')
     last_seq=n
    if not c[1]:review.append('原文工作单位或场站为空')
    if two_units and not c[2]:review.append('原文第二工作单位子列为空')
    if not c[voltage_ix]:review.append('原文电压等级为空')
    if not c[voltage_ix+1]:review.append('原文停电设备为空')
    if not c[voltage_ix+2] or not c[voltage_ix+3]:review.append('原文工作日期为空')
    if duration and not c[-1]:review.append('原文工作时长为空')
    if catalogue[:7]!=period:review.append('目录月份与原文标题月份不一致')
    row={'catalogueDate':catalogue,'nativeTitle':title,'titlePeriod':period,'rowOrdinal':len(rows)+1,'sequenceNative':seq,'unitColumn1Header':h[1],'unitColumn1Native':c[1],'unitColumn2Header':h[2] if two_units else '', 'unitColumn2Native':c[2] if two_units else '', 'unitColumn2Present':two_units,'voltageNative':c[voltage_ix],'voltageUnitNative':'原文未注明','equipmentNative':c[voltage_ix+1],'startNative':c[voltage_ix+2],'endNative':c[voltage_ix+3],'durationNative':c[-1] if duration else '', 'durationFieldPresent':duration,'durationUnitNative':'原文未注明','sourcePage':pi,'sourceTable':1,'sourceRowOnTable':ri,'sourceRowBbox':table.rows[ri-1].bbox,'nativeCells':rawrow,'review':'；'.join(review),'sourceSha256':digest}
    rows.append(row)
    if review:issues.append({'page':pi,'rowOnTable':ri,'reasons':review})
 if not rows:raise ValueError('no_rows_requires_review')
 out={'status':'parsed_with_review' if issues else 'parsed','catalogueDate':catalogue,'nativeTitle':title,'titlePeriod':period,'rows':rows,'issues':issues,'limitations':['工作单位/场站及停电设备按原生单元格保存，不推断机组ID。','原文开始结束日期只有月日时保留原文，不推断年份。','电压等级及工作时长表头未声明单位，不自行补单位。','原文空序号不填补，页面位置与全文件行号可追溯。']}
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'monthly_transmission_maintenance_values.csv';tmp=target.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows);f.flush()
 tmp.replace(target);parsed=dest/'monthly_transmission_maintenance_parsed.json';tmp=parsed.with_suffix('.json.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(parsed)
 return {'status':out['status'],'titlePeriod':period,'rows':len(rows),'issues':issues,'sourceSha256':digest,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(parsed)}
