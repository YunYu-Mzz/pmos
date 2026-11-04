"""Extract observed annual capacity-table cells without inventing units."""
import csv
import hashlib
import re
from html.parser import HTMLParser

class Reader(HTMLParser):
 def __init__(self):
  super().__init__(convert_charrefs=True);self.tables=[];self.table=None;self.row=None;self.cell=None
 def handle_starttag(self,tag,attrs):
  if tag=='table':
   if self.table is not None:raise ValueError('nested_table')
   self.table=[]
  elif tag=='tr' and self.table is not None:self.row=[]
  elif tag in ('td','th') and self.row is not None:
   if any(k in ('rowspan','colspan') and v not in ('1',1) for k,v in attrs):raise ValueError('merged_cells_require_review')
   self.cell=[]
 def handle_data(self,data):
  if self.cell is not None:self.cell.append(data)
 def handle_endtag(self,tag):
  if tag in ('td','th') and self.cell is not None:self.row.append(''.join(self.cell).strip());self.cell=None
  elif tag=='tr' and self.row is not None:self.table.append(self.row);self.row=None
  elif tag=='table' and self.table is not None:self.tables.append(self.table);self.table=None

def parse_table(title,html):
 match=re.fullmatch(r'(2022|2023)年净增机组容量及分类构成(?:（完善数据）)?',title or '')
 if not match:return {'status':'unsupported_title','values':[]}
 year=match.group(1);p=Reader();p.feed(html if isinstance(html,str) else '')
 if len(p.tables)!=1:return {'status':'unsupported_table_count','values':[]}
 table=p.tables[0]
 headers={'2022':['','2022年净增容量','2022年底装机容量'], '2023':['','2023年新增容量','2023年期末容量']}
 header_index=0 if year=='2022' else 1
 if len(table)<=header_index or table[header_index]!=headers[year] or year=='2023' and table[0]!=['装机容量（万千瓦）','','']:
  return {'status':'unsupported_header','values':[]}
 if len(table)<=header_index+1 or any(len(row)!=3 for row in table[header_index:]):return {'status':'unsupported_shape','values':[]}
 categories=[row[0] for row in table[header_index+1:]]
 if any(not c for c in categories):return {'status':'missing_category','values':[]}
 values=[];problems=[];parent=''
 for i,row in enumerate(table[header_index+1:],header_index+2):
  if re.match(r'^\d+\.',row[0]):parent=row[0]
  for col in (1,2):
   native=row[col];state='numeric' if re.fullmatch(r'-?\d+(?:\.\d+)?',native) else 'source_missing_marker' if native in ('—','-','--') else 'empty' if not native else 'unrecognized'
   if state=='unrecognized':problems.append({'row':i,'column':col+1,'state':state})
   values.append({'titleYear':year,'categoryNative':row[0],'precedingNumberedCategory':parent if row[0].startswith('其中') else '',
                  'columnNative':headers[year][col],'valueNative':native,'valueState':state,
                  'unitNative':'万千瓦' if year=='2023' else '原文未注明','sourceRow':i,'sourceColumn':col+1})
 return {'status':'parsed_with_cell_review' if problems else 'parsed','values':values,'cellIssues':problems,
         'limitations':['原文分类可能包含总计、小计和子项，不应直接相加。','2022年表未注明单位，未从其他年份推断。']}

def export_page(result,destination):
 records=result.get('rows');decoded=result.get('site_decoded_rows')
 if not isinstance(records,list) or not isinstance(decoded,list) or len(records)!=len(decoded):raise ValueError('listing_and_site_decoded_rows_mismatch')
 rows=[];statuses=[]
 for record,shown in zip(records,decoded):
  guid=record.get('guid')
  if not guid or guid!=shown.get('native_guid') or shown.get('provenance')!='original_site_component_after_this_completed_native_query':raise ValueError('native_decoded_row_identity_mismatch')
  content=shown.get('site_content_value')
  try:p=parse_table(record.get('title'),content)
  except ValueError as exc:p={'status':'requires_review','values':[],'cellIssues':[{'error':str(exc)}]}
  digest=hashlib.sha256((content if isinstance(content,str) else '').encode()).hexdigest()
  statuses.append({'nativeGuid':guid,'status':p['status'],'cellIssues':p.get('cellIssues',[]),'contentSha256':digest})
  rows.extend({**x,'nativeGuid':guid,'title':record.get('title'),'contentSha256':digest,'responseSha256':result.get('sha256'),'provenance':'site_component_transformed_value'} for x in p['values'])
 destination.mkdir(parents=True,exist_ok=True);target=destination/'generation_capacity_table_values.csv';tmp=destination/'generation_capacity_table_values.csv.tmp'
 fields=['titleYear','categoryNative','precedingNumberedCategory','columnNative','valueNative','valueState','unitNative','sourceRow','sourceColumn','nativeGuid','title','contentSha256','responseSha256','provenance']
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
 tmp.replace(target)
 return {'status':'completed_with_review' if any(s['status']!='parsed' for s in statuses) else 'parsed','records':len(records),'values':len(rows),'recordsStatus':statuses,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
