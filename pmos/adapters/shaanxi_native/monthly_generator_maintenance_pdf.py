"""Extract original monthly generator maintenance plan rows from saved PDFs."""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber

BASE=['序号','申请单位','设备名称','检修等级','容量','申请开工时间','申请完工时间']
HEADERS=(BASE,BASE+['工期'],BASE+['工期','检修内容','备注'])

def clean(value):return re.sub(r'\s*\n\s*','',value).strip() if isinstance(value,str) else ''
def date_iso(value):
 if not re.fullmatch(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}',value):return None
 try:return dt.date(*(int(x) for x in re.split(r'[-/]',value))).isoformat()
 except ValueError:return None

def parse_tables(pages,title,source_date,source_sha256):
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as exc:raise ValueError('invalid_catalogue_date') from exc
 matches=list(re.finditer(r'陕西电网(\d{4})年(\d{1,2})月份统调发电设备检修计划',re.sub(r'\s+','',title)))
 periods={(int(m[1]),int(m[2])) for m in matches}
 if len(periods)!=1:raise ValueError('unrecognized_or_ambiguous_title')
 year,month=next(iter(periods));dt.date(year,month,1);period=f'{year:04d}-{month:02d}'
 rows=[];issues=[];header=None;expected_seq=1;empty_native=False
 if not pages or any(not p for p in pages):raise ValueError('page_without_detected_table')
 for page_no,tables in enumerate(pages,1):
  for table in tables:
   native=table['rows'];bboxes=table['rowBboxes']
   if not native or len(native)!=len(bboxes):raise ValueError('empty_or_invalid_table')
   first=[clean(x) for x in native[0]];has_header=first in HEADERS
   if has_header:
    if header is not None and first!=header:raise ValueError('header_changed_between_pages')
    header=first;start=1
   elif header is not None and len(first)==len(header) and re.fullmatch(r'\d+',first[0] or ''):start=0
   else:raise ValueError('unsupported_table_header')
   for idx in range(start,len(native)):
    raw=native[idx];cells=[clean(x) for x in raw]
    if len(cells)!=len(header):raise ValueError('row_column_count_mismatch')
    if not any(cells):continue
    if cells[0]=='1' and cells[1]=='无' and all(not x for x in cells[2:]):
     if rows or empty_native:raise ValueError('ambiguous_empty_marker')
     empty_native=True;continue
    if empty_native or not re.fullmatch(r'\d+',cells[0] or '') or int(cells[0])!=expected_seq:raise ValueError('noncontiguous_native_row_sequence')
    seq=int(cells[0]);expected_seq+=1;native_start,native_end=cells[5],cells[6];start_iso,end_iso=date_iso(native_start),date_iso(native_end);review=[]
    if not all(cells[i] for i in (1,2,3,4,5,6)):review.append('原文单元格缺失')
    if not re.fullmatch(r'\d+(?:\.\d+)?',re.sub(r'\s+','',cells[4])):review.append('容量非标准数值')
    if not start_iso or not end_iso:review.append('申请日期无法解析')
    elif end_iso<start_iso:review.append('原文申请完工时间早于开工时间')
    if catalogue[:7]!=period:review.append('目录月份与正文月份不一致')
    if review:issues.append({'page':page_no,'sequence':seq,'reasons':review})
    rows.append({'catalogueDate':catalogue,'titlePeriod':period,'sequenceNative':cells[0],'applicantNative':cells[1],'equipmentNative':cells[2],
                 'maintenanceGradeNative':cells[3],'capacityNative':cells[4],'capacityUnitNative':'原文未注明',
                 'applicationStartNative':native_start,'applicationStartISO':start_iso or '', 'applicationEndNative':native_end,'applicationEndISO':end_iso or '',
                 'durationNative':cells[7] if len(cells)>7 else '', 'durationFieldPresent':len(cells)>7,'durationUnitNative':'原文未注明',
                 'maintenanceContentNative':cells[8] if len(cells)>8 else '', 'remarksNative':cells[9] if len(cells)>9 else '',
                 'sourcePage':page_no,'sourceRowOnTable':idx+1,'sourceRowBbox':bboxes[idx],'nativeCells':raw,'review':'; '.join(review),'sourceSha256':source_sha256})
 if not rows and not empty_native:raise ValueError('no_rows_or_explicit_empty_marker')
 return {'status':'parsed_with_review' if issues else 'parsed_empty' if empty_native else 'parsed','catalogueDate':catalogue,'titlePeriod':period,'rows':rows,'issues':issues,'explicitEmptyNative':'无' if empty_native else '',
         'limitations':['原表容量及工期列未注明单位，不推断。','申请计划不代表实际检修执行；设备名称保留组合原文，不猜测机组 ID。','重复出现在不同月度计划的设备不自动去重或相加。']}

def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);raw=pdf.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=source_sha256:raise ValueError('pdf_hash_mismatch')
 with pdfplumber.open(pdf) as doc:
  pages=[];texts=[]
  for page in doc.pages:
   texts.append(page.extract_text() or '');tables=[]
   for table in page.find_tables():tables.append({'rows':table.extract(),'rowBboxes':[row.bbox for row in table.rows]})
   pages.append(tables)
 result=parse_tables(pages,'\n'.join(texts),source_date,source_sha256);dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'monthly_generator_maintenance_values.csv';tmp=target.with_suffix('.csv.tmp')
 fields=['catalogueDate','titlePeriod','sequenceNative','applicantNative','equipmentNative','maintenanceGradeNative','capacityNative','capacityUnitNative','applicationStartNative','applicationStartISO','applicationEndNative','applicationEndISO','durationNative','durationFieldPresent','durationUnitNative','maintenanceContentNative','remarksNative','sourcePage','sourceRowOnTable','sourceRowBbox','nativeCells','review','sourceSha256']
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(result['rows']);f.flush()
 tmp.replace(target);out=dest/'monthly_generator_maintenance_parsed.json';tmp=out.with_suffix('.json.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');tmp.replace(out)
 return {'status':result['status'],'titlePeriod':result['titlePeriod'],'rows':len(result['rows']),'issues':result['issues'],'explicitEmptyNative':result['explicitEmptyNative'],'sourceSha256':source_sha256,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(out)}
