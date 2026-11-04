"""Retain a specific archived 2020 generation table and its inferred mapping."""
import csv,hashlib,re
from html.parser import HTMLParser

class TableReader(HTMLParser):
 def __init__(self):
  super().__init__(convert_charrefs=True);self.tables=[];self.table=None;self.row=None;self.cell=None
 def handle_starttag(self,tag,attrs):
  if tag=='table':
   if self.table is not None:raise ValueError('nested_table')
   self.table=[]
  elif tag=='tr' and self.table is not None:self.row=[]
  elif tag in ('td','th') and self.row is not None:self.cell=[]
 def handle_data(self,data):
  if self.cell is not None:self.cell.append(data)
 def handle_endtag(self,tag):
  if tag in ('td','th') and self.cell is not None:self.row.append(''.join(self.cell).strip());self.cell=None
  elif tag=='tr' and self.row is not None:self.table.append(self.row);self.row=None
  elif tag=='table' and self.table is not None:self.tables.append(self.table);self.table=None

TITLES={'2020年底发电装机及分类型总体情况年度','2020年发电设备利用小时及分类','2020年净增机组容量及分类构成','2020年发电上网电量及分类构成'}
def parse_table(title,html):
 if title not in TITLES:raise ValueError('unrecognized_title')
 p=TableReader();p.feed(html if isinstance(html,str) else '')
 if len(p.tables)!=1:raise ValueError('unexpected_table_count')
 rows=p.tables[0]
 if len(rows)!=13 or any(len(r)!=7 for r in rows):raise ValueError('unexpected_table_shape')
 if rows[0][0]!='陕西电网发电生产综合情况' or rows[1][0]!='2020年':raise ValueError('table_identity_mismatch')
 if rows[2]!=['指标名称','期末发电设备容量','发电量(万千瓦时)','上网电量(万千瓦时)','设备平均利用小时（小时）','','']:
  raise ValueError('unexpected_main_header')
 if rows[4]!=['（千瓦）','累计','增长率','累计','增长率','累计','']:
  raise ValueError('unexpected_subheader')
 expected=['合计','其中：清洁能源','可再生能源','新能源','水电','火电','风电','太阳能']
 if [r[0] for r in rows[5:]]!=expected:raise ValueError('unexpected_category_rows')
 if any(not re.fullmatch(r'-?\d+(?:\.\d+)?',value) for r in rows[5:] for value in r[1:]):raise ValueError('unexpected_numeric_cell')
 # The website's captured HTML does not preserve merged header spans. Mapping
 # the ordered headings/subheadings to the six value columns is an inference;
 # both this mapping and the unmodified table are returned for user review.
 mapping=[('期末发电设备容量','千瓦','原文未注明'),('发电量','万千瓦时','累计'),('发电量增长率','原文未注明','增长率'),
          ('上网电量','万千瓦时','累计'),('上网电量增长率','原文未注明','增长率'),('设备平均利用小时','小时','累计')]
 values=[]
 for row_num,row in enumerate(rows[5:],6):
  for col,(metric,unit,scope) in enumerate(mapping,1):
   values.append({'period':'2020年','categoryNative':row[0],'metricInferred':metric,'valueNative':row[col],
                  'unitNative':unit,'subheaderNative':scope,'sourceRow':row_num,'sourceColumn':col+1,
                  'mappingStatus':'表头合并关系缺失，按顺序推断待核对'})
 return {'rows':rows,'values':values,'mappingStatus':'表头合并关系缺失，按顺序推断待核对',
         'limitations':['Captured HTML lacks merged-header spans; metric-to-column mapping is inferred from ordered headings and subheadings.',
                        'Growth-rate units are not stated and are not inferred.',
                        'Categories overlap and cannot be added as disjoint groups.']}

def export_page(result,destination):
 records=result.get('rows');decoded=result.get('site_decoded_rows')
 if not isinstance(records,list) or not isinstance(decoded,list) or len(records)!=len(decoded):raise ValueError('listing_and_site_decoded_rows_mismatch')
 cells=[];values=[];statuses=[]
 for record,shown in zip(records,decoded):
  guid=record.get('guid');title=record.get('title')
  if not guid or guid!=shown.get('native_guid') or shown.get('provenance')!='original_site_component_after_this_completed_native_query':raise ValueError('native_decoded_row_identity_mismatch')
  content=shown.get('site_content_value');content=content if isinstance(content,str) else '';digest=hashlib.sha256(content.encode()).hexdigest()
  if title not in TITLES:
   statuses.append({'nativeGuid':guid,'status':'not_target_title','contentSha256':digest});continue
  try:parsed=parse_table(title,content)
  except ValueError as exc:
   statuses.append({'nativeGuid':guid,'status':'requires_review','reason':str(exc),'contentSha256':digest});continue
  provenance={'nativeGuid':guid,'title':title,'contentSha256':digest,'responseSha256':result.get('sha256'),'provenance':'site_component_transformed_value'}
  for row_no,row in enumerate(parsed['rows'],1):
   for col_no,value in enumerate(row,1):cells.append({'sourceRow':row_no,'sourceColumn':col_no,'nativeText':value,**provenance})
  values.extend({**x,**provenance} for x in parsed['values'])
  statuses.append({'nativeGuid':guid,'status':'parsed_cells_inferred_mapping_requires_review','contentSha256':digest,'rawCells':sum(map(len,parsed['rows'])),'values':len(parsed['values'])})
 destination.mkdir(parents=True,exist_ok=True);files={}
 for filename,rows,fields in [
  ('generation_2020_native_cells.csv',cells,['sourceRow','sourceColumn','nativeText','nativeGuid','title','contentSha256','responseSha256','provenance']),
  ('generation_2020_inferred_values.csv',values,['period','categoryNative','metricInferred','valueNative','unitNative','subheaderNative','sourceRow','sourceColumn','mappingStatus','nativeGuid','title','contentSha256','responseSha256','provenance'])]:
  target=destination/filename;tmp=target.with_name(target.name+'.tmp')
  with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
  tmp.replace(target);files[filename]=hashlib.sha256(target.read_bytes()).hexdigest()
 return {'status':'completed_with_review' if any(x['status']!='not_target_title' for x in statuses) else 'no_matching_2020_record','records':len(records),'nativeCells':len(cells),'inferredValues':len(values),'recordsStatus':statuses,'sha256':files,
         'limitations':['列对应关系是推断，网页未保留合并表头。','相同内容可能出现在不同披露来源，按内容哈希识别重复，不应把其数据相加。','净增容量标题的相同原表不含可确认的净增容量列，不伪造净增值。']}
