"""Extract only explicit comparison direction and magnitude from native text."""
import csv,hashlib,re
try:
 from .generation_statistics_narrative import parse_statistics
except ImportError:
 from .generation_statistics_narrative import parse_statistics
NUM=r'\d+(?:\.\d+)?'
def parse_changes(title,html):
 base=parse_statistics(title,html);text=base['visibleText'];rows=[];warnings=[]
 for value in base['values']:
  metric=value['metric'];category=value['category'];source=value['sourceText'];native=value['value']
  if metric=='全口径发电量':
   indices=[m.start() for m in re.finditer(re.escape(source),text)]
   if len(indices)!=1:warnings.append('generation_anchor_ambiguous:'+value['scope']);continue
   start=indices[0];end=text.find('。',start);sentence=text[start:end+1 if end>=0 else len(text)]
   matches=list(re.finditer(r'(同比|环比)(增长|下降|提高|降低)('+NUM+r')%',sentence))
   if not matches:warnings.append('generation_comparison_not_found:'+value['scope']);continue
   if len({m.group(1) for m in matches})!=len(matches):warnings.append('generation_comparison_ambiguous:'+value['scope']);continue
   for m in matches:rows.append({'period':value['period'],'metric':'全口径发电量变化率','category':category,'comparison':m.group(1),'nativeDirection':m.group(2),'nativeValue':m.group(3),'unit':'%',
                                 'scope':value['scope'],'periodBasis':value['periodBasis'],'sourceText':sentence})
  elif metric=='分类发电量':
   expression=re.escape(category+native+'亿千瓦时')+r'[，、,](同比)(增长|下降|提高|降低)('+NUM+r')%'
   matches=list(re.finditer(expression,source))
   if len(matches)!=1:warnings.append('category_comparison_missing_or_ambiguous:'+category);continue
   m=matches[0];rows.append({'period':value['period'],'metric':'分类发电量变化率','category':category,'comparison':m.group(1),'nativeDirection':m.group(2),'nativeValue':m.group(3),'unit':'%',
                             'scope':value['scope'],'periodBasis':value['periodBasis'],'sourceText':source})
  elif metric=='发电设备平均利用小时':
   head='发电设备平均利用小时完成' if category=='全省合计' else category
   expression=re.escape(head+native+'小时')+r'[，、,](同比)(增长|下降|提高|降低)('+NUM+r')小时'
   matches=list(re.finditer(expression,source))
   if len(matches)!=1:warnings.append('utilization_comparison_missing_or_ambiguous:'+category);continue
   m=matches[0];rows.append({'period':value['period'],'metric':'平均利用小时变化','category':category,'comparison':m.group(1),'nativeDirection':m.group(2),'nativeValue':m.group(3),'unit':'小时',
                             'scope':value['scope'],'periodBasis':value['periodBasis'],'sourceText':source})
 return {'values':rows,'warnings':warnings,'baseWarnings':base['warnings'],'visibleText':text}

def export_page(result,destination):
 records=result.get('rows');decoded=result.get('site_decoded_rows')
 if not isinstance(records,list) or not isinstance(decoded,list) or len(records)!=len(decoded):raise ValueError('listing_and_site_decoded_rows_mismatch')
 rows=[];statuses=[]
 for record,shown in zip(records,decoded):
  guid=record.get('guid')
  if not guid or guid!=shown.get('native_guid') or shown.get('provenance')!='original_site_component_after_this_completed_native_query':raise ValueError('native_decoded_row_identity_mismatch')
  content=shown.get('site_content_value');parsed=parse_changes(record.get('title'),content);digest=hashlib.sha256((content if isinstance(content,str) else '').encode()).hexdigest()
  statuses.append({'nativeGuid':guid,'warnings':parsed['warnings'],'baseWarnings':parsed['baseWarnings'],'contentSha256':digest})
  rows.extend({**x,'nativeGuid':guid,'title':record.get('title'),'contentSha256':digest,'responseSha256':result.get('sha256'),'provenance':'site_component_transformed_value'} for x in parsed['values'])
 destination.mkdir(parents=True,exist_ok=True);target=destination/'generation_change_values.csv';tmp=destination/'generation_change_values.csv.tmp'
 fields=['period','metric','category','comparison','nativeDirection','nativeValue','unit','scope','periodBasis','sourceText','nativeGuid','title','contentSha256','responseSha256','provenance']
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
 tmp.replace(target)
 return {'status':'completed_with_review' if any(s['warnings'] or s['baseWarnings'] for s in statuses) else 'parsed','records':len(records),'values':len(rows),'recordsStatus':statuses,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
