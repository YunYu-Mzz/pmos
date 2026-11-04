"""Preserve native annual network-maintenance plan table rows and anomalies."""
import collections,csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber

def compact(x):return re.sub(r'\s+','',x) if isinstance(x,str) else ''
HEAD=['序号','工作单位','','电压等级','停电设备','工作开始时间','工作结束时间','工作时长','工作内容概述','调管范围','工作性质']
FIELDS=['catalogueDate','planYearNative','sequenceNative','workUnitNative','workUnitCandidate','workUnitCandidateBasis','workUnitSecondColumnNative','voltageNative','voltageUnitNative','equipmentNative','startMonthDayNative','endMonthDayNative','durationNative','durationUnitNative','workDescriptionNative','dispatchScopeNative','workNatureNative','sourcePage','sourceTable','sourceRow','sourceRowBbox','nativeCellsJson','sourceSha256','review']
def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as e:raise ValueError('invalid_catalogue_date') from e
 rows=[];headers=[];title_year=None;issues=[]
 with pdfplumber.open(pdf) as doc:
  if not doc.pages:raise ValueError('empty_pdf')
  for page_no,page in enumerate(doc.pages,1):
   matches=re.findall(r'陕西电网(20\d{2})年主网输变电设备检修计划',compact(page.extract_text() or ''))
   if len(matches)!=1:raise ValueError('unrecognized_or_ambiguous_page_title')
   year=int(matches[0]);dt.date(year,1,1)
   if title_year is None:title_year=year
   if year!=title_year:raise ValueError('page_title_year_changed')
   tables=page.find_tables()
   if len(tables)!=1:raise ValueError('unexpected_table_count')
   t=tables[0];matrix=t.extract();seen_header=False
   if not matrix or len(matrix)!=len(t.rows):raise ValueError('unexpected_table_geometry')
   for idx,raw in enumerate(matrix,1):
    c=[compact(x) for x in raw]
    if len(c)!=11:raise ValueError('unexpected_column_count')
    if not any(c):continue
    if c==HEAD:
     if seen_header:raise ValueError('duplicate_header')
     seen_header=True;headers.append({'page':page_no,'nativeCells':raw});continue
    if c[0]==f'陕西电网{year}年主网输变电设备检修计划' and not any(c[1:]):continue
    if page_no==1 and c[:2]==['附','件1'] and not any(c[2:]):continue
    if not seen_header or not c[0].isdigit():raise ValueError('unexpected_nondata_row')
    if not all(c[i] for i in (0,1,2,3,4,5,6,8)):raise ValueError('missing_required_native_cell')
    if not re.fullmatch(r'\d{1,2}月\d{1,2}日',c[5]) or not re.fullmatch(r'\d{1,2}月\d{1,2}日',c[6]):raise ValueError('unrecognized_month_day')
    review=[]
    candidate='国网陕西超高压公司' if c[1] in ('司国网陕西超高压公','国网司陕西超高压公','司国网陕西超高压公司') else ''
    if candidate:review.append('单位文字跨行错序，候选名称按可见原文整理，待核')
    if any(not c[i] for i in (7,9,10)):review.append('原表部分单元格空白或合并，未推断')
    if any(re.search(r'\(cid:\d+\)',z) for z in c):review.append('原文字体缺字编码，待核')
    rows.append({'catalogueDate':catalogue,'planYearNative':str(year),'sequenceNative':c[0],'workUnitNative':c[1],'workUnitCandidate':candidate,'workUnitCandidateBasis':'原文字体跨行，候选待核' if candidate else '','workUnitSecondColumnNative':c[2],'voltageNative':c[3],'voltageUnitNative':'原表未注明','equipmentNative':c[4],'startMonthDayNative':c[5],'endMonthDayNative':c[6],'durationNative':c[7],'durationUnitNative':'原表未注明','workDescriptionNative':raw[8] or '','dispatchScopeNative':c[9],'workNatureNative':c[10],'sourcePage':page_no,'sourceTable':1,'sourceRow':idx,'sourceRowBbox':json.dumps(t.rows[idx-1].bbox,separators=(',',':')),'nativeCellsJson':json.dumps(raw,ensure_ascii=False,separators=(',',':')),'sourceSha256':digest,'review':'；'.join(review)})
   if not seen_header:raise ValueError('missing_page_header')
 if not rows:raise ValueError('no_native_rows')
 numbers=[int(r['sequenceNative']) for r in rows];duplicates={n for n,k in collections.Counter(numbers).items() if k>1}
 for i,row in enumerate(rows):
  n=numbers[i];r=[]
  if n in duplicates:r.append('原表序号重复，待核')
  if i==0 and n!=1:r.append('原表首次观察序号非1，待核')
  if i and n>numbers[i-1]+1:r.append('原表序号跳跃，待核')
  if r:row['review']='；'.join(filter(None,[row['review'],*r]))
 if numbers[0]!=1:issues.append({'reason':'first_observed_sequence_not_one','first':numbers[0]})
 if duplicates:issues.append({'reason':'duplicate_sequence','values':sorted(duplicates)})
 missing=sorted(set(range(min(numbers),max(numbers)+1))-set(numbers))
 if missing:issues.append({'reason':'missing_sequence_in_observed_range','values':missing})
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'annual_network_maintenance_values.csv';tmp=target.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows);f.flush()
 tmp.replace(target);out={'status':'parsed_with_review' if issues or any(x['review'] for x in rows) else 'parsed','catalogueDate':catalogue,'planYearNative':str(title_year),'sourceSha256':digest,'rows':rows,'sourceHeaders':headers,'issues':issues,'limitations':['原文是年度检修计划，非实际执行。','原表工作单位横跨两列，第二列无独立表头，按第二列原值保留。','日期仅月日，保留原值，不从标题补年份。','电压等级与工作时长表头无单位，不推断。','原空白或合并单元格不填充；原序号重复、跳号如实标记。','跨行错序的单位名仅提供候选，原单元格和待核标记保留。']};parsed=dest/'annual_network_maintenance_parsed.json';tmp=parsed.with_suffix('.json.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(parsed)
 return {'status':out['status'],'catalogueDate':catalogue,'planYearNative':str(title_year),'rows':len(rows),'issueCount':len(issues),'sourceSha256':digest,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(parsed)}
