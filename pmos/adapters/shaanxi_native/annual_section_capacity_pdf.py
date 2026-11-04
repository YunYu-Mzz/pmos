"""Extract source-specific annual section capacity tables without inventing periods."""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber
HEAD=['序号','断面名称','电压等级（kV）','断面限额（万千瓦）','预计最大潮流(万千瓦)','预计最大负载率']
FIELDS=['catalogueDate','yearNative','sequenceNative','sectionNative','voltageNative','voltageUnitNative','limitNative','limitUnitNative','forecastFlowNative','forecastFlowUnitNative','forecastLoadRateNative','sourcePage','sourceTable','sourceRow','sourceRowBbox','nativeCellsJson','sourceSha256','review']
def compact(x):return re.sub(r'\s+','',x) if isinstance(x,str) else ''
def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as exc:raise ValueError('invalid_catalogue_date') from exc
 rows=[];headers=[];year=None
 with pdfplumber.open(pdf) as doc:
  if not doc.pages:raise ValueError('empty_pdf')
  for page_no,page in enumerate(doc.pages,1):
   matches=re.findall(r'(20\d{2})年关键断面的输电能力',compact(page.extract_text() or ''))
   if len(matches)!=1:raise ValueError('unrecognized_or_ambiguous_title')
   if year is None:year=matches[0]
   if matches[0]!=year:raise ValueError('changed_title_year')
   tables=page.find_tables()
   if len(tables)!=1:raise ValueError('unexpected_table_count')
   table=tables[0];matrix=table.extract()
   if not matrix or len(matrix)!=len(table.rows) or [compact(c) for c in matrix[0]]!=HEAD:raise ValueError('unrecognized_table_header')
   headers.append({'page':page_no,'nativeCells':matrix[0]})
   for i,raw in enumerate(matrix[1:],2):
    c=[compact(x) for x in raw]
    if len(c)!=6 or not all(c) or not c[0].isdigit() or not re.fullmatch(r'\d+(?:\.\d+)?(?:\+\d+(?:\.\d+)?)*',c[2]) or not all(re.fullmatch(r'\d+(?:\.\d+)?',c[k]) for k in (3,4)) or not re.fullmatch(r'\d+(?:\.\d+)?%',c[5]):raise ValueError('unexpected_data_row')
    review=[]
    if re.search(r'\(cid:\d+\)', ''.join(c)):review.append('原文字体缺字编码，待核')
    rows.append(dict(catalogueDate=catalogue,yearNative=year,sequenceNative=c[0],sectionNative=c[1],voltageNative=c[2],voltageUnitNative='kV',limitNative=c[3],limitUnitNative='万千瓦',forecastFlowNative=c[4],forecastFlowUnitNative='万千瓦',forecastLoadRateNative=c[5],sourcePage=page_no,sourceTable=1,sourceRow=i,sourceRowBbox=json.dumps(table.rows[i-1].bbox,separators=(',',':')),nativeCellsJson=json.dumps(raw,ensure_ascii=False,separators=(',',':')),sourceSha256=digest,review='；'.join(review)))
 if not rows:raise ValueError('no_rows')
 numbers=[int(r['sequenceNative']) for r in rows]
 if len(numbers)!=len(set(numbers)) or numbers!=list(range(1,len(numbers)+1)):raise ValueError('unexpected_sequence')
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);csvpath=dest/'annual_section_capacity_values.csv';tmp=csvpath.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
 tmp.replace(csvpath);out={'status':'parsed_with_review' if any(r['review'] for r in rows) else 'parsed','catalogueDate':catalogue,'yearNative':year,'sourceSha256':digest,'rows':rows,'sourceHeaders':headers,'limitations':['原表为年度预计最大潮流及负载率，不代表实际运行。','不从年度数据推断月度、日度或时点数据。','复合电压等级和断面名称保留原值，不拆分。']};p=dest/'annual_section_capacity_parsed.json';tmp=p.with_suffix('.json.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(p)
 return {'status':out['status'],'catalogueDate':catalogue,'yearNative':year,'rows':len(rows),'sourceSha256':digest,'csvSha256':hashlib.sha256(csvpath.read_bytes()).hexdigest(),'path':str(p)}
