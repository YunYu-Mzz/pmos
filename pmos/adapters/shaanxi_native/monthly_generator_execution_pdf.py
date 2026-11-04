"""Extract explicit monthly generator maintenance plan/execution counts."""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber
NORMAL=re.compile(r'(\d{4})年(\d{1,2})月(?:份)?安排机组计划检修(\d+)项，按计划执行(\d+)项(?:，取消(\d+)项)?。')
NONE=re.compile(r'(\d{4})年(\d{1,2})月(?:份)?未安排机组计划检修。')
FIELDS=['catalogueDate','sourcePeriod','metricNative','valueNative','unitNative','sourceTextNative','sourceTextCompact','sourcePage','sourceSha256']
def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as e:raise ValueError('invalid_catalogue_date') from e
 with pdfplumber.open(pdf) as doc:
  if len(doc.pages)!=1:raise ValueError('unsupported_page_count')
  text=doc.pages[0].extract_text() or '';compact=re.sub(r'\s+','',text);m=NORMAL.fullmatch(compact);empty=NONE.fullmatch(compact)
  if not m and not empty:raise ValueError('unrecognized_execution_narrative')
  found=m or empty;year,month=int(found[1]),int(found[2]);dt.date(year,month,1);period=f'{year:04d}-{month:02d}';rows=[]
  metrics=[('未安排机组计划检修','','')] if empty else [('安排机组计划检修',m[3],'项'),('按计划执行',m[4],'项')]+([('取消',m[5],'项')] if m[5] is not None else [])
  for label,value,unit in metrics:rows.append({'catalogueDate':catalogue,'sourcePeriod':period,'metricNative':label,'valueNative':value,'unitNative':unit,'sourceTextNative':text,'sourceTextCompact':compact,'sourcePage':1,'sourceSha256':digest})
  issues=[]
  if m and m[5] is not None and int(m[3])!=int(m[4])+int(m[5]):issues.append('原文安排数与执行数加取消数不一致，需核对口径')
 out={'status':'parsed_with_review' if issues else 'parsed','catalogueDate':catalogue,'sourcePeriod':period,'rows':rows,'issues':issues,'limitations':['计划检修项数不是机组数量或具体机组名单。','未安排记录保留原文状态，不构造数字零。','未出现的取消项不推断为零；目录日期和正文月份分别保存。']}
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'monthly_generator_execution_values.csv';tmp=target.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows);f.flush()
 tmp.replace(target);parsed=dest/'monthly_generator_execution_parsed.json';tmp=parsed.with_suffix('.json.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(parsed)
 return {'status':out['status'],'sourcePeriod':period,'rows':len(rows),'issues':issues,'sourceSha256':digest,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(parsed)}
