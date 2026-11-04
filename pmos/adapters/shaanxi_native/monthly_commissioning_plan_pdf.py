"""Extract a strictly observed single-page generation commissioning plan table."""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber
HEAD=['项目名称','性质','装机','电压等级','所在地市','预计归调时间']
HEAD_NO_CITY=['项目名称','性质','装机','电压等级','预计归调时间']
FIELDS=['catalogueDateNative','catalogueDate','nativeTitle','nativeTableTitle','titleMonthNative','projectNative','categoryNative','capacityNative','capacityUnitNative','voltageNative','voltageUnitNative','cityNative','cityFieldPresent','plannedDispatchNative','plannedDispatchISO','sourcePage','sourceTable','sourceRowOnTable','sourceRowBbox','nativeCells','review','sourceSha256']
def clean(x):return re.sub(r'\s+','',x) if isinstance(x,str) else ''
def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);raw=pdf.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=source_sha256:raise ValueError('pdf_hash_mismatch')
 native_date=str(source_date)
 try:catalogue=dt.datetime.strptime(native_date,'%Y%m%d').date().isoformat()
 except ValueError:catalogue=''
 with pdfplumber.open(pdf) as doc:
  if len(doc.pages)!=1:raise ValueError('unsupported_page_count')
  page=doc.pages[0];text=clean(page.extract_text() or '')
  title=re.search(r'(\d{1,2})月发电、电网设备新建或改建投产计划',text);tabletitle=re.search(r'表1陕西电网(\d{1,2})月预计新增归调发电装机情况',text)
  if not title or not tabletitle or title[1]!=tabletitle[1]:raise ValueError('unrecognized_or_conflicting_titles')
  titlemonth=int(title[1]);dt.date(2000,titlemonth,1);tables=page.find_tables()
  if len(tables)!=1:raise ValueError('unexpected_table_count')
  matrix=tables[0].extract()
  header=[clean(x) for x in matrix[0]] if matrix else []
  if header not in (HEAD,HEAD_NO_CITY):raise ValueError('unrecognized_table_header')
  city_present=header==HEAD
  rows=[];issues=[]
  for ri,rawrow in enumerate(matrix[1:],2):
   c=[clean(x) for x in rawrow]
   if len(c)!=len(header) or not all(c):raise ValueError('missing_native_cell')
   date_native=c[5] if city_present else c[4]
   try:expected=dt.date.fromisoformat(date_native)
   except ValueError as e:raise ValueError('unsupported_native_date') from e
   if not re.fullmatch(r'\d+(?:\.\d+)?',c[2]):raise ValueError('unsupported_capacity_value')
   review=[]
   if not catalogue:review.append('目录日期原文不是有效日期')
   if expected.month!=titlemonth:review.append('预计归调月份与表标题月份不一致')
   if catalogue and (expected.year!=int(catalogue[:4]) or titlemonth!=int(catalogue[5:7])):review.append('目录年月与预计归调年份或标题月份不一致')
   row={'catalogueDateNative':native_date,'catalogueDate':catalogue,'nativeTitle':title.group(0),'nativeTableTitle':tabletitle.group(0),'titleMonthNative':str(titlemonth),'projectNative':c[0],'categoryNative':c[1],'capacityNative':c[2],'capacityUnitNative':'原文未注明','voltageNative':c[3],'voltageUnitNative':'原文未注明','cityNative':c[4] if city_present else '','cityFieldPresent':city_present,'plannedDispatchNative':date_native,'plannedDispatchISO':expected.isoformat(),'sourcePage':1,'sourceTable':1,'sourceRowOnTable':ri,'sourceRowBbox':tables[0].rows[ri-1].bbox,'nativeCells':rawrow,'review':'；'.join(review),'sourceSha256':source_sha256}
   rows.append(row)
   if review:issues.append({'row':ri,'review':row['review']})
  if not rows:raise ValueError('empty_plan_table_requires_review')
 out={'status':'parsed_with_review' if issues else 'parsed','catalogueDateNative':native_date,'catalogueDate':catalogue,'nativeTitle':title.group(0),'nativeTableTitle':tabletitle.group(0),'rows':rows,'issues':issues,'limitations':['预计归调计划非实际投运或退役情况。','项目名称保留原文，不将光伏或风电场名称拆成虚构机组。','装机和电压等级原表未标单位，不猜测。','来源无所在地市列或目录日期无效时保留原始情况，不根据文件名补日期。']}
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'monthly_commissioning_plan_values.csv';tmp=target.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows);f.flush()
 tmp.replace(target);parsed=dest/'monthly_commissioning_plan_parsed.json';tmp=parsed.with_suffix('.json.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(parsed)
 return {'status':out['status'],'rows':len(rows),'issues':issues,'sourceSha256':source_sha256,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(parsed)}
