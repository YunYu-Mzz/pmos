"""Extract explicitly stated annual generation commissioning plans from PDF."""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber
FIELDS=['catalogueDate','planYear','categoryNative','assetNameNative','unitCountNative','unitCapacityNative','capacityUnitNative','sourceScaleNative','approximationNative','sourcePhrase','sourcePage','sourceSha256','review']
def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:date=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as exc:raise ValueError('invalid_catalogue_date') from exc
 with pdfplumber.open(pdf) as f:
  if len(f.pages)!=1:raise ValueError('unrecognized_page_count')
  text=re.sub(r'\s+','',f.pages[0].extract_text() or '')
 pattern=r'(20\d{2})年，电源方面，将有(.+?)投产，约有(\d+(?:\.\d+)?)万千瓦储能投运，以及一大批新能源投产。'
 matches=list(re.finditer(pattern,text))
 if len(matches)!=1:raise ValueError('unrecognized_generation_plan')
 m=matches[0];year=m.group(1);phrase=m.group(0);segments=m.group(2).split('、');rows=[]
 for segment in segments:
  station=re.fullmatch(r'([\u4e00-\u9fffA-Za-z0-9#]+)（(\d+)×(\d+(?:\.\d+)?)万千瓦）(?:机组)?',segment)
  if not station:raise ValueError('unrecognized_unit_expression')
  name,count,capacity=station.groups();rows.append({'catalogueDate':date,'planYear':year,'categoryNative':'电源方面','assetNameNative':name,'unitCountNative':count,'unitCapacityNative':capacity,'capacityUnitNative':'万千瓦','sourceScaleNative':segment,'approximationNative':'','sourcePhrase':phrase,'sourcePage':1,'sourceSha256':digest,'review':'年度计划，不代表实际投产；机组编号原文未给'})
 rows.append({'catalogueDate':date,'planYear':year,'categoryNative':'储能','assetNameNative':'','unitCountNative':'','unitCapacityNative':'','capacityUnitNative':'万千瓦','sourceScaleNative':m.group(3),'approximationNative':'约','sourcePhrase':phrase,'sourcePage':1,'sourceSha256':digest,'review':'原文为约数、未给项目及设备数量；年度计划'})
 rows.append({'catalogueDate':date,'planYear':year,'categoryNative':'新能源','assetNameNative':'','unitCountNative':'','unitCapacityNative':'','capacityUnitNative':'','sourceScaleNative':'一大批','approximationNative':'','sourcePhrase':phrase,'sourcePage':1,'sourceSha256':digest,'review':'原文未给数值及设备名单；年度计划'})
 grid=text.find(year+'年，750千伏电网方面，',m.end())
 if grid<0:raise ValueError('unrecognized_grid_section')
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'annual_commissioning_plan_values.csv';tmp=target.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
 tmp.replace(target);out={'status':'parsed_with_review','catalogueDate':date,'planYear':year,'sourceSha256':digest,'rows':rows,'gridNarrativeNative':text[grid:],'gridStructured':False,'limitations':['年度投产计划不等于实际投产，未推断机组编号或容量总和。','储能规模为约数；新能源未给数值。','电网工程部分保留原文，尚未逐工程结构化。']};p=dest/'annual_commissioning_plan_parsed.json';tmp=p.with_suffix('.json.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(p)
 return {'status':out['status'],'catalogueDate':date,'planYear':year,'rows':len(rows),'sourceSha256':digest,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(p),'gridStructured':False}
