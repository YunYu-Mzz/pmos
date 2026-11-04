"""Conservative extraction of explicit monthly generation commissioning totals."""
import datetime as dt,re
NUMBER=r'[+-]?\d+(?:\.\d+)?'
PREFIX=re.compile(rf'(?P<year>20\d{{2}})年(?P<month>\d{{1,2}})月，陕西新增调度口径电厂(?P<plants>\d+)座(?:，机组(?P<units>\d+)台)?，装机容量(?P<capacity>{NUMBER})兆瓦(?=[。，])')
WIND=re.compile(rf'(?P<year>20\d{{2}})年(?P<month>\d{{1,2}})月，陕西调度口径新增归调风电厂(?P<plants>\d+)座，装机容量(?P<capacity>{NUMBER})兆瓦。')
CATEGORY=re.compile(rf'(?P<action>新投（扩建）|新增归调|新投|扩建)(?P<kind>水电厂|火电厂|风电场|光伏电站)(?P<plants>\d+)座(?:，机组(?P<units>\d+)台)?，新增容量(?P<capacity>{NUMBER})兆瓦')
SHARED=re.compile(rf'新投(?P<kind>水电厂|火电厂|风电场|光伏电站)(?P<new>\d+)座，扩建(?P=kind)(?P<expanded>\d+)座，新增容量(?P<capacity>{NUMBER})兆瓦')

def extract(text):
 text=re.sub(r'\s+','',text);matches=list(PREFIX.finditer(text))+list(WIND.finditer(text))
 if len(matches)!=1:return {'status':'requires_review','reason':'summary_missing_or_ambiguous','rows':[]}
 m=matches[0];year,month=int(m['year']),int(m['month']);dt.date(year,month,1);period=f'{year:04d}-{month:02d}';is_wind=m.re is WIND
 rows=[{'sourcePeriod':period,'scopeNative':'风电厂' if is_wind else '新增调度口径电厂','actionNative':'新增归调' if is_wind else '新增','plantCountNative':m['plants'],'unitCountNative':None if is_wind else m['units'],'capacityNative':m['capacity'],'capacityUnitNative':'兆瓦','sourceText':m.group(0),'review':'原文装机容量为负，口径待核' if m['capacity'].startswith('-') else ''}]
 if is_wind:return {'status':'parsed','rows':rows,'limitations':['机组数未出现时保留空值。']}
 # Limit category extraction to the first explicit 其中 clause before the table.
 rest=text[m.end():].split('工程明细',1)[0]; clauses=re.split(r'其中',rest,maxsplit=1)
 all_solar=re.match(r'，均为光伏电站。',rest)
 if all_solar:
  rows.append({'sourcePeriod':period,'scopeNative':'光伏电站','actionNative':'新增','plantCountNative':m['plants'],'unitCountNative':m['units'],'capacityNative':m['capacity'],'capacityUnitNative':'兆瓦','sourceText':m.group(0)+all_solar.group(0),'review':'由原文“均为光伏电站”对应总数及容量'})
 if len(clauses)==2:
  for clause in re.split(r'[；。]',clauses[1]):
   clause=clause.strip('，');c=CATEGORY.fullmatch(clause);shared=SHARED.fullmatch(clause)
   if c:
    rows.append({'sourcePeriod':period,'scopeNative':c['kind'],'actionNative':c['action'],'plantCountNative':c['plants'],'unitCountNative':c['units'],'capacityNative':c['capacity'],'capacityUnitNative':'兆瓦','sourceText':c.group(0),'review':'原文新增容量为负，口径待核' if c['capacity'].startswith('-') else ''})
   elif shared:
    for action,count in (('新投',shared['new']),('扩建',shared['expanded'])):
     rows.append({'sourcePeriod':period,'scopeNative':shared['kind'],'actionNative':action,'plantCountNative':count,'unitCountNative':None,'capacityNative':None,'capacityUnitNative':'','sourceText':clause,'review':'容量为新投与扩建合计，未拆分'})
    rows.append({'sourcePeriod':period,'scopeNative':shared['kind'],'actionNative':'新投及扩建合计','plantCountNative':None,'unitCountNative':None,'capacityNative':shared['capacity'],'capacityUnitNative':'兆瓦','sourceText':clause,'review':'原文新投与扩建的合计容量'})
 if len(rows)>1 and sum(int(x['plantCountNative']) for x in rows[1:] if x['plantCountNative'] is not None)>int(rows[0]['plantCountNative']):
  for row in rows:row['review']='；'.join(filter(None,[row['review'],'已识别分类电厂数之和大于原文总数，口径待核']))
 return {'status':'parsed_with_review' if any(x['review'] for x in rows) else 'parsed','rows':rows,'limitations':['未出现的机组数保留空值。','仅提取完全匹配的分类句；多类别共用数量或容量时不拆分。','原文总数与分类不一致时保留原值并标待核。']}

def parse_pdf(pdf,source_date,source_sha256,output_dir):
 import csv,hashlib,json
 from pathlib import Path
 import pdfplumber
 path=Path(pdf);digest=hashlib.sha256(path.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as e:raise ValueError('invalid_catalogue_date') from e
 with pdfplumber.open(path) as doc:result=extract('\n'.join(page.extract_text() or '' for page in doc.pages))
 rows=[{'catalogueDate':catalogue,**row,'sourceSha256':digest} for row in result['rows']]
 fields=['catalogueDate','sourcePeriod','scopeNative','actionNative','plantCountNative','unitCountNative','capacityNative','capacityUnitNative','sourceText','review','sourceSha256']
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'monthly_generation_summary_values.csv';temp=target.with_suffix('.csv.tmp')
 with temp.open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows);f.flush()
 temp.replace(target);data={'status':result['status'],'catalogueDate':catalogue,'rows':rows,'reason':result.get('reason'),'limitations':result.get('limitations',[]),'sourceSha256':digest};parsed=dest/'monthly_generation_summary_parsed.json';temp=parsed.with_suffix('.json.tmp');temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');temp.replace(parsed)
 return {'status':data['status'],'catalogueDate':catalogue,'rows':len(rows),'reason':data['reason'],'sourceSha256':digest,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(parsed)}
