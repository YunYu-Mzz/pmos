"""Extract the explicitly labelled generation project table from saved PDFs.

Only the observed table-1 layout is supported. Other tables in the PDF are
preserved in the original and are deliberately outside this parser's scope.
"""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber

HEAD=['序号','电压等级(千伏)','电厂名称','机组编号','机组类型','装机容量(兆瓦)','归调时间','首次并网日期']
FIELDS=['catalogueDate','tableTitle','titlePeriod','sequenceNative','voltageNative','voltageUnitNative','plantNative','unitNumberNative','unitTypeNative','capacityNative','capacityUnitNative','dispatchDateNative','firstGridDateNative','sourcePage','sourceTable','sourceRowOnTable','sourceRowBbox','nativeCells','sourceSha256']

def clean(x):return re.sub(r'\s+','',x) if isinstance(x,str) else ''

def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);raw=pdf.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as e:raise ValueError('invalid_catalogue_date') from e
 with pdfplumber.open(pdf) as doc:
  page_texts=[clean(p.extract_text() or '') for p in doc.pages]
  title_pattern=r'表1(\d{4})年(\d{1,2})月陕西10千伏及以上新增归调发电工程明细'
  titles=[(i+1,m) for i,text in enumerate(page_texts) for m in re.finditer(title_pattern,text)]
  if len(titles)!=1:raise ValueError('unrecognized_or_ambiguous_table_title')
  page_no,m=titles[0];year,month=int(m[1]),int(m[2]);dt.date(year,month,1);period=f'{year:04d}-{month:02d}'
  matches=[]
  for pi,page in enumerate(doc.pages,1):
   for ti,t in enumerate(page.find_tables(),1):
    matrix=t.extract()
    if matrix and [clean(x) for x in matrix[0]]==HEAD:matches.append((pi,ti,t,matrix))
  if len(matches)!=1 or matches[0][0]!=page_no:raise ValueError('missing_repeated_or_misaligned_table_header')
  pi,ti,table,matrix=matches[0];rows=[]
  page=doc.pages[pi-1]
  if table.bbox[3]+1>=page.height:raise ValueError('table_end_without_next_section_evidence')
  following=clean(page.crop((0,table.bbox[3]+1,page.width,page.height)).extract_text() or '')
  if not re.match(rf'{year}年{month}月[，,]陕西电网',following):raise ValueError('table_end_without_next_section_evidence')
  for i,rawrow in enumerate(matrix[1:],2):
   values=[clean(x) for x in rawrow]
   if len(values)!=8 or values[0]!=str(i-1) or not all(values[k] for k in (1,2,4,5,6,7)):raise ValueError('unexpected_generation_row')
   rows.append(dict(zip(FIELDS,[catalogue,m.group(0),period,values[0],values[1],'千伏',values[2],values[3],values[4],values[5],'兆瓦',values[6],values[7],pi,ti,i,table.rows[i-1].bbox,rawrow,source_sha256])))
  if not rows:raise ValueError('empty_generation_table_requires_review')
  # Scope explicitly identifies only the observed ruled table; there may be
  # other project types or further content elsewhere in the source PDF.
  out={'status':'parsed_table1_only','catalogueDate':catalogue,'tableTitle':m.group(0),'titlePeriod':period,'rows':rows,'scope':'原 PDF 表1新增归调发电工程；下方检测到另一工程段落，不涵盖其他工程表。','limitations':['机组编号为空、横线或斜杠时保留原文，不推断机组。','目录日期、归调日期和首次并网日期分别保留。','电厂、工程行或空机组编号不能直接计作实际机组台数。']}
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'monthly_commissioning_generation_values.csv';temp=target.with_suffix('.csv.tmp')
 with temp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows);f.flush()
 temp.replace(target);parsed=dest/'monthly_commissioning_generation_parsed.json';tmp=parsed.with_suffix('.json.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(parsed)
 return {'status':out['status'],'catalogueDate':catalogue,'titlePeriod':period,'rows':len(rows),'scope':out['scope'],'sourceSha256':source_sha256,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(parsed)}
