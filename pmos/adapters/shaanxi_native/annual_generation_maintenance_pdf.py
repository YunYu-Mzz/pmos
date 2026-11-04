"""Extract observed annual generation-maintenance plan rows from native PDFs."""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber

def compact(x):return re.sub(r'\s+','',x) if isinstance(x,str) else ''
HEAD=['序号','申请单位','设备名称','容量','申请开工时间','申请完工时间','工期','检修内容','检修类(cid:2047)']
CATEGORIES={'火电','抽蓄','水电'}
FIELDS=['catalogueDate','planYearNative','categoryNative','sequenceNative','applicantNative','equipmentNative','capacityNative','capacityUnitNative','startDateNative','endDateNative','durationNative','durationUnitNative','maintenanceContentNative','maintenanceTypeNative','lastHeaderNative','sourcePage','sourceTable','sourceRow','sourceRowBbox','nativeCellsJson','sourceSha256','review']

def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as e:raise ValueError('invalid_catalogue_date') from e
 rows=[];category=None;seq={};heads=[]
 with pdfplumber.open(pdf) as doc:
  if not doc.pages:raise ValueError('empty_pdf')
  title=compact(doc.pages[0].extract_text() or '');titles=re.findall(r'陕西电网(20\d{2})年统调发电设备年度检修计划',title)
  if len(titles)!=1:raise ValueError('unrecognized_or_ambiguous_title')
  year=int(titles[0]);dt.date(year,1,1)
  for page_no,page in enumerate(doc.pages,1):
   tables=page.find_tables()
   if len(tables)!=1:raise ValueError('unexpected_table_count_on_page')
   for table_no,table in enumerate(tables,1):
    matrix=table.extract()
    if not matrix or [compact(c) for c in matrix[0]]!=HEAD or len(matrix)!=len(table.rows):raise ValueError('unexpected_table_header_or_geometry')
    heads.append({'page':page_no,'nativeCells':matrix[0]})
    for idx,raw in enumerate(matrix[1:],2):
     c=[compact(x) for x in raw]
     if len(c)!=9:raise ValueError('unexpected_column_count')
     if c[0] in CATEGORIES and not any(c[1:]):
      if c[0] in seq:raise ValueError('repeated_category_header')
      category=c[0];seq[category]=0;continue
     if not category or not c[0].isdigit() or int(c[0])!=seq[category]+1:raise ValueError('invalid_category_or_sequence')
     if not all(c):raise ValueError('missing_required_native_cell')
     try:start=dt.date.fromisoformat(c[4]);end=dt.date.fromisoformat(c[5])
     except ValueError as e:raise ValueError('invalid_native_date') from e
     if end<start:raise ValueError('end_before_start')
     review=[]
     if start.year!=year or end.year!=year:review.append('起止日期与标题年份不同，待核')
     if any(re.search(r'\(cid:\d+\)',cell) for cell in c):review.append('原文字体缺字编码，待核')
     review='；'.join(review)
     seq[category]+=1
     rows.append({'catalogueDate':catalogue,'planYearNative':str(year),'categoryNative':category,'sequenceNative':c[0],'applicantNative':c[1],'equipmentNative':c[2],'capacityNative':c[3],'capacityUnitNative':'原表未注明','startDateNative':c[4],'endDateNative':c[5],'durationNative':c[6],'durationUnitNative':'原表未注明','maintenanceContentNative':c[7],'maintenanceTypeNative':c[8],'lastHeaderNative':HEAD[8],'sourcePage':page_no,'sourceTable':table_no,'sourceRow':idx,'sourceRowBbox':json.dumps(table.rows[idx-1].bbox,separators=(',',':')),'nativeCellsJson':json.dumps(raw,ensure_ascii=False,separators=(',',':')),'sourceSha256':digest,'review':review})
 if not rows:raise ValueError('no_native_rows')
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'annual_generation_maintenance_values.csv';temp=target.with_suffix('.csv.tmp')
 with temp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows);f.flush()
 temp.replace(target);out={'status':'parsed_with_review' if any(x['review'] for x in rows) else 'parsed','catalogueDate':catalogue,'planYearNative':str(year),'rows':rows,'sourceHeaders':heads,'sourceSha256':digest,'limitations':['原文为年度检修计划，不代表实际检修或执行结果。','容量与工期表头均未标单位，不推断。','设备名称可能包含多台机组，原文整格保留，不按标点拆分。','检修末列表头存在PDF缺字编码，原样保留，不修复原文。']};parsed=dest/'annual_generation_maintenance_parsed.json';temp=parsed.with_suffix('.json.tmp');temp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');temp.replace(parsed)
 return {'status':out['status'],'catalogueDate':catalogue,'planYearNative':str(year),'rows':len(rows),'sourceSha256':digest,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(parsed)}
