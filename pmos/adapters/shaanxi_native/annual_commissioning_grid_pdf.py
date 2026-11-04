"""Preserve annual grid plan clauses in source-native sections and regions."""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber
FIELDS=['catalogueDate','planYear','sectionNative','regionNative','clauseOrdinal','clauseNative','sourcePage','sourceSha256','review']
def split_clauses(text):
 parts=[];start=0;depth=0
 for i,ch in enumerate(text):
  if ch in '（(':depth+=1
  elif ch in '）)':
   depth-=1
   if depth<0:raise ValueError('unbalanced_parentheses')
  if depth==0 and ch in '；。':
   part=text[start:i+1]
   if not part:raise ValueError('empty_clause')
   parts.append(part);start=i+1
 if depth!=0 or start!=len(text):raise ValueError('incomplete_clause_or_parentheses')
 if ''.join(parts)!=text:raise ValueError('clause_reconstruction_failed')
 return parts
def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:date=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as exc:raise ValueError('invalid_catalogue_date') from exc
 with pdfplumber.open(pdf) as f:
  if len(f.pages)!=1:raise ValueError('unrecognized_page_count')
  text=re.sub(r'\s+','',f.pages[0].extract_text() or '')
 headers=list(re.finditer(r'(20\d{2})年，750千伏电网方面，',text))
 if len(headers)!=1:raise ValueError('unrecognized_750_section')
 year=headers[0].group(1);head=headers[0].end();marker='330千伏电网方面，'
 if text.count(marker)!=1:raise ValueError('unrecognized_330_section')
 mid=text.index(marker,head);s750=text[head:mid];s330=text[mid+len(marker):]
 if not s750 or not s330:raise ValueError('empty_grid_section')
 regions=('陕北地区','关中地区','陕南地区');positions=[s330.find(r) for r in regions]
 if all(i>=0 for i in positions):
  if positions!=sorted(positions) or positions[0]!=0 or any(s330.count(r)!=1 for r in regions):raise ValueError('ambiguous_region_headings')
  groups=[(regions[i],s330[positions[i]+len(regions[i]):positions[i+1] if i+1<len(regions) else len(s330)]) for i in range(len(regions))]
 elif all(i<0 for i in positions):groups=[('',s330)]
 else:raise ValueError('partial_region_headings')
 sections=[('750千伏电网方面','',s750)]+[('330千伏电网方面',region,part) for region,part in groups];rows=[]
 for section,region,body in sections:
  if not body:raise ValueError('empty_region')
  for clause in split_clauses(body):rows.append({'catalogueDate':date,'planYear':year,'sectionNative':section,'regionNative':region,'clauseOrdinal':len(rows)+1,'clauseNative':clause,'sourcePage':1,'sourceSha256':digest,'review':'原文为计划；段落标题并不保证句中每个工程电压相同；并列工程不拆分'})
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'annual_commissioning_grid_clauses.csv';tmp=target.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
 tmp.replace(target);out={'status':'parsed_with_review','catalogueDate':date,'planYear':year,'sourceSha256':digest,'sections':[{'sectionNative':sec,'regionNative':reg,'bodyNative':body} for sec,reg,body in sections],'rows':rows,'limitations':['保留原生段落标题、地区标记及完整原句，并列工程不拆分。','标题为段落范围，不能作为句中每项工程的电压断言。','年度计划不等于实际投运。']};p=dest/'annual_commissioning_grid_parsed.json';tmp=p.with_suffix('.json.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(p)
 return {'status':out['status'],'catalogueDate':date,'planYear':year,'rows':len(rows),'sourceSha256':digest,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(p)}
