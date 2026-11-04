"""Extract only explicitly dated, category-specific installed-capacity changes."""
import csv,hashlib,re
from html.parser import HTMLParser
NUM=r'-?\d+(?:\.\d+)?'
TITLE=re.compile(r'(\d{4})年(\d{1,2})月(?:发电机组装机及发电总体情况|发电装机及分类型总体情况)')
ANNUAL=re.compile(r'(\d{4})年发电装机及分类型总体情况(?:（含新增规模）|年度)')
HEAD=re.compile(r'(\d{4})年(?:(1[-—至]\d{1,2}月)|(\d{1,2}月))?，?(?:陕西全省发电装机(净增|增加|减少)|(净增)装机)('+NUM+r')万千瓦，其中([^。]+)。')
ITEM=re.compile(r'(水电|火电|风电|太阳能|新型储能)(增加|减少|净增)?('+NUM+r')万千瓦')
class Text(HTMLParser):
 def __init__(self):super().__init__(convert_charrefs=True);self.parts=[]
 def handle_data(self,data):self.parts.append(data)
def parse_changes(title,html):
 p=Text();p.feed(html if isinstance(html,str) else '');text=re.sub(r'\s+','', ''.join(p.parts));tm=TITLE.fullmatch(title or '');ann=ANNUAL.fullmatch(title or '')
 if not (tm or ann):return {'values':[],'status':'unrecognized_title','visibleText':text}
 year=tm.group(1) if tm else ann.group(1);title_month=int(tm.group(2)) if tm else 12
 candidates=list(HEAD.finditer(text))
 if not candidates:return {'values':[],'status':'change_sentence_not_found','visibleText':text}
 if len(candidates)!=1:return {'values':[],'status':'ambiguous_change_sentences','visibleText':text}
 m=candidates[0];source_year,span,single,action,action2,total,fragment=m.groups()
 if source_year!=year:return {'values':[],'status':'source_year_differs_from_title','sourceText':m.group(0),'visibleText':text}
 if not 1<=title_month<=12:return {'values':[],'status':'invalid_title_month','visibleText':text}
 period=source_year+'年'+(span or single or '');end_month=int(re.search(r'\d+(?=月)',span or single).group()) if (span or single) else None
 if end_month is not None and not 1<=end_month<=12:return {'values':[],'status':'invalid_source_month','visibleText':text}
 pairs=[ITEM.fullmatch(c) for c in re.split(r'[、，]',fragment)]
 if any(z is None for z in pairs):return {'values':[],'status':'unrecognized_category_format','visibleText':text}
 groups=[z.groups() for z in pairs];categories=[x[0] for x in groups]
 if len(categories)!=len(set(categories)) or not {'水电','火电','风电','太阳能'}.issubset(categories):return {'values':[],'status':'missing_or_ambiguous_categories','visibleText':text}
 status='source_period_differs_from_title' if end_month is not None and end_month!=title_month else 'parsed'
 rows=[]
 for category,direction,value in [('全省合计',action or action2,total)]+groups:
  rows.append({'titlePeriod':f'{year}-{title_month:02d}','sourcePeriod':period,'metric':'发电装机变动',
               'category':category,'nativeDirection':direction or '原文未标明','nativeValue':value,'unit':'万千瓦',
               'periodStatus':status,'sourceText':m.group(0)})
 return {'values':rows,'status':status,'visibleText':text}
def export_page(result,destination):
 records=result.get('rows');decoded=result.get('site_decoded_rows')
 if not isinstance(records,list) or not isinstance(decoded,list) or len(records)!=len(decoded):raise ValueError('listing_and_site_decoded_rows_mismatch')
 rows=[];statuses=[]
 for record,shown in zip(records,decoded):
  guid=record.get('guid')
  if not guid or guid!=shown.get('native_guid') or shown.get('provenance')!='original_site_component_after_this_completed_native_query':raise ValueError('native_decoded_row_identity_mismatch')
  content=shown.get('site_content_value');parsed=parse_changes(record.get('title'),content);digest=hashlib.sha256((content if isinstance(content,str) else '').encode()).hexdigest()
  statuses.append({'nativeGuid':guid,'status':parsed['status'],'contentSha256':digest})
  rows.extend({**x,'nativeGuid':guid,'title':record.get('title'),'contentSha256':digest,'responseSha256':result.get('sha256'),'provenance':'site_component_transformed_value'} for x in parsed['values'])
 destination.mkdir(parents=True,exist_ok=True);target=destination/'net_capacity_values.csv';tmp=destination/'net_capacity_values.csv.tmp'
 fields=['titlePeriod','sourcePeriod','metric','category','nativeDirection','nativeValue','unit','periodStatus','sourceText','nativeGuid','title','contentSha256','responseSha256','provenance']
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
 tmp.replace(target)
 return {'status':'completed_with_review' if any(s['status']!='parsed' for s in statuses) else 'parsed','records':len(records),'values':len(rows),'recordsStatus':statuses,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
