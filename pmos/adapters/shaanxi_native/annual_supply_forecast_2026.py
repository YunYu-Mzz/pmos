"""Parse the observed 2026 annual supply forecast without correcting it."""
import csv,hashlib,re
from decimal import Decimal
from html.parser import HTMLParser
N=r'\d+(?:\.\d+)?'
class Text(HTMLParser):
 def __init__(self):super().__init__(convert_charrefs=True);self.parts=[]
 def handle_data(self,data):self.parts.append(data)
def parse_forecast(title,html):
 if title!='2026年度电力电量供需预测':return {'status':'unrecognized_title','values':[]}
 p=Text();p.feed(html if isinstance(html,str) else '');text=re.sub(r'\s+','', ''.join(p.parts))
 patterns=[
  r'预测2026年投产发电装机('+N+r')万千瓦，其中火电('+N+r')万千瓦（([^）]+)），风电('+N+r')万千瓦，太阳能('+N+r')万千瓦（其中集中式('+N+r')万千瓦、分布式('+N+r')万千瓦），新型储能('+N+r')万千瓦。',
  r'按省发改委批复，2026年退役([^，]+)，合计('+N+r')万千瓦。',
  r'预计2026年底发电装机('+N+r')万千瓦，其中水电('+N+r')万千瓦、火电('+N+r')万千瓦、风电('+N+r')万千瓦、太阳能('+N+r')万千瓦、新型储能('+N+r')万千瓦。',
  r'预测2026年全社会用电量('+N+r')亿千瓦时，同比增长('+N+r')%。']
 matches=[list(re.finditer(pattern,text)) for pattern in patterns]
 if any(len(z)!=1 for z in matches):return {'status':'requires_review_unrecognized_or_ambiguous_sections','values':[]}
 a,b,c,d=[z[0] for z in matches];projects=[]
 for fragment in (a.group(3),b.group(1)):
  parts=re.split(r'[、，]',fragment);items=[re.fullmatch(r'([^、，（）]+?)('+N+r')万千瓦',z) for z in parts]
  if len(items)!=3 or any(z is None for z in items):return {'status':'requires_review_plant_list','values':[]}
  projects.append([(z.group(1),z.group(2)) for z in items])
 if any(len({name for name,_ in z})!=len(z) for z in projects):return {'status':'requires_review_duplicate_plant','values':[]}
 issue=Decimal(a.group(2))+Decimal(a.group(4))+Decimal(a.group(5))>Decimal(a.group(1))
 rows=[]
 def add(metric,category,plant,parent,value,unit,scope,source,review=''):
  rows.append({'period':'2026年','metric':metric,'categoryNative':category,'plantNative':plant,'parentNative':parent,
               'valueNative':value,'unitNative':unit,'scopeNative':scope,'sourceText':source,'review':review})
 note='原文火电、风电和太阳能分项之和超过投产总量，待核对' if issue else ''
 for cat,val,parent in [('合计',a.group(1),''),('火电',a.group(2),''),('风电',a.group(4),''),('太阳能',a.group(5),''),('集中式',a.group(6),'太阳能'),('分布式',a.group(7),'太阳能'),('新型储能',a.group(8),'')]:
  add('预测投产发电装机',cat,'',parent,val,'万千瓦','原文：预测投产',a.group(0),note)
 for plant,val in projects[0]:add('预测投产发电装机','火电',plant,'火电',val,'万千瓦','原文：预测投产',a.group(0),note)
 add('退役发电装机','合计','','',b.group(2),'万千瓦','原文：按省发改委批复',b.group(0))
 for plant,val in projects[1]:add('退役发电装机','',plant,'',val,'万千瓦','原文：按省发改委批复',b.group(0))
 for cat,val in zip(('合计','水电','火电','风电','太阳能','新型储能'),c.groups()):add('预计年底发电装机',cat,'','',val,'万千瓦','原文：预计年底',c.group(0))
 add('预测全社会用电量','合计','','',d.group(1),'亿千瓦时','原文：预测',d.group(0))
 add('预测全社会用电量同比增长','合计','','',d.group(2),'%','原文：预测',d.group(0))
 return {'status':'parsed_with_source_consistency_review' if issue else 'parsed','values':rows,
         'warnings':[note] if issue else [],'limitations':['Forecast, expectation and approval text are retained as published and are not actual operating outcomes.',
          'Solar subtypes are explicitly labeled as a subset; they are not assumed exhaustive.']}
def export_page(result,destination):
 records=result.get('rows');decoded=result.get('site_decoded_rows')
 if not isinstance(records,list) or not isinstance(decoded,list) or len(records)!=len(decoded):raise ValueError('listing_and_site_decoded_rows_mismatch')
 rows=[];statuses=[]
 for record,shown in zip(records,decoded):
  guid=record.get('guid')
  if not guid or guid!=shown.get('native_guid') or shown.get('provenance')!='original_site_component_after_this_completed_native_query':raise ValueError('native_decoded_row_identity_mismatch')
  content=shown.get('site_content_value');p=parse_forecast(record.get('title'),content);digest=hashlib.sha256((content if isinstance(content,str) else '').encode()).hexdigest()
  statuses.append({'nativeGuid':guid,'status':p['status'],'warnings':p.get('warnings',[]),'contentSha256':digest})
  rows.extend({**x,'nativeGuid':guid,'title':record.get('title'),'contentSha256':digest,'responseSha256':result.get('sha256'),'provenance':'site_component_transformed_value'} for x in p['values'])
 destination.mkdir(parents=True,exist_ok=True);target=destination/'annual_supply_forecast_values.csv';tmp=destination/'annual_supply_forecast_values.csv.tmp'
 fields=['period','metric','categoryNative','plantNative','parentNative','valueNative','unitNative','scopeNative','sourceText','review','nativeGuid','title','contentSha256','responseSha256','provenance']
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
 tmp.replace(target)
 return {'status':'completed_with_review' if any(s['status']!='parsed' for s in statuses) else 'parsed','records':len(records),'values':len(rows),'recordsStatus':statuses,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
