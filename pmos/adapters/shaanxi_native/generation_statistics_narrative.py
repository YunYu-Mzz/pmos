"""Extract explicitly scoped generation and utilization values from disclosure text."""
import csv,hashlib,re
from html.parser import HTMLParser
NUM=r'\d+(?:\.\d+)?'
TITLE=re.compile(r'(\d{4})年(\d{1,2})月(?:发电机组装机及发电总体情况|发电装机及分类型总体情况)')
ANNUAL=re.compile(r'(\d{4})年发电装机及分类型总体情况(?:（含新增规模）|年度)')
class Text(HTMLParser):
 def __init__(self):super().__init__(convert_charrefs=True);self.parts=[]
 def handle_data(self,data):self.parts.append(data)
def parse_statistics(title,html):
 p=Text();p.feed(html if isinstance(html,str) else '');text=re.sub(r'\s+','', ''.join(p.parts));tm=TITLE.fullmatch(title or '');ann=ANNUAL.fullmatch(title or '')
 if not (tm or ann):return {'values':[],'warnings':['unrecognized_title'],'visibleText':text}
 year=tm.group(1) if tm else ann.group(1);month=int(tm.group(2)) if tm else 12
 if not 1<=month<=12:return {'values':[],'warnings':['invalid_title_month'],'visibleText':text}
 period=f'{year}-{month:02d}';values=[];warnings=[]
 def add(metric,category,value,unit,scope,basis,source):
  values.append({'period':period,'metric':metric,'category':category,'value':value,'unit':unit,'scope':scope,'periodBasis':basis,'sourceText':source})
 monthly=re.search(re.escape(year)+r'年'+str(month)+r'月陕西省全口径发电量('+NUM+r')亿千瓦时',text)
 if monthly:add('全口径发电量','全省合计',monthly.group(1),'亿千瓦时','当月','正文明确年月',monthly.group(0))
 else:warnings.append('monthly_generation_missing_or_unrecognized')
 cumulative=re.search(r'(?:(\d{4})年)?1[-—至](\d{1,2})月(?:陕西省全口径|累计)发电量('+NUM+r')亿千瓦时',text)
 scope=None;basis=None;anchor=None
 if cumulative:
  cy,cm=cumulative.group(1),int(cumulative.group(2))
  if cm==month and (cy is None or cy==year):
   scope='1至'+str(month)+'月累计';basis='正文明确年份及区间' if cy else '标题年份及正文区间';anchor=cumulative
   add('全口径发电量','全省合计',cumulative.group(3),'亿千瓦时',scope,basis,cumulative.group(0))
  else:warnings.append('cumulative_period_mismatch')
 elif month==1 and monthly:
  scope='当月';basis='正文明确年月';anchor=monthly
 else:warnings.append('cumulative_generation_missing_or_unrecognized')
 if anchor:
  end=text.find('发电设备平均利用小时',anchor.end());section=text[anchor.end():end if end>=0 else len(text)]
  start=section.find('其中：')
  if start>=0:
   section=section[start:];pairs=[(c,list(re.finditer(re.escape(c)+r'('+NUM+r')亿千瓦时',section))) for c in ('水电','火电','风电','太阳能')]
   if all(len(matches)==1 for _,matches in pairs):
    context=text[anchor.start():end if end>=0 else len(text)]
    for category,matches in pairs:add('分类发电量',category,matches[0].group(1),'亿千瓦时',scope,basis,context)
   else:warnings.append('generation_categories_unrecognized_or_ambiguous')
  else:warnings.append('generation_categories_not_found')
 # The source gives a title month but does not explicitly state the start date
 # of the utilization-hour window, so preserve that distinction in each row.
 hours=re.search(r'发电设备平均利用小时完成('+NUM+r')小时([^。]*)。',text)
 if hours:
  section=hours.group(0);pairs=[(c,list(re.finditer(re.escape(c)+r'('+NUM+r')小时',section))) for c in ('水电','火电','风电','太阳能')]
  if all(len(matches)==1 for _,matches in pairs):
   add('发电设备平均利用小时','全省合计',hours.group(1),'小时','原文未明确起始日期','标题月份',section)
   for category,matches in pairs:add('发电设备平均利用小时',category,matches[0].group(1),'小时','原文未明确起始日期','标题月份',section)
  else:warnings.append('utilization_categories_unrecognized_or_ambiguous')
 else:warnings.append('utilization_missing_or_unrecognized')
 return {'values':values,'warnings':warnings,'visibleText':text}

def export_page(result,destination):
 records=result.get('rows');decoded=result.get('site_decoded_rows')
 if not isinstance(records,list) or not isinstance(decoded,list) or len(records)!=len(decoded):raise ValueError('listing_and_site_decoded_rows_mismatch')
 rows=[];statuses=[]
 for record,shown in zip(records,decoded):
  guid=record.get('guid')
  if not guid or guid!=shown.get('native_guid') or shown.get('provenance')!='original_site_component_after_this_completed_native_query':raise ValueError('native_decoded_row_identity_mismatch')
  content=shown.get('site_content_value');p=parse_statistics(record.get('title'),content);digest=hashlib.sha256((content if isinstance(content,str) else '').encode()).hexdigest()
  statuses.append({'nativeGuid':guid,'warnings':p['warnings'],'contentSha256':digest})
  rows.extend({**x,'nativeGuid':guid,'title':record.get('title'),'contentSha256':digest,'responseSha256':result.get('sha256'),'provenance':'site_component_transformed_value'} for x in p['values'])
 destination.mkdir(parents=True,exist_ok=True);target=destination/'generation_statistics_values.csv';tmp=destination/'generation_statistics_values.csv.tmp'
 fields=['period','metric','category','value','unit','scope','periodBasis','sourceText','nativeGuid','title','contentSha256','responseSha256','provenance']
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
 tmp.replace(target)
 return {'status':'completed_with_review' if any(s['warnings'] for s in statuses) else 'parsed','records':len(records),'values':len(rows),'recordsStatus':statuses,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
