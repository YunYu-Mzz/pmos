"""Preserve native sector, column and cell values in consumption tables."""
import csv,hashlib,re
from html.parser import HTMLParser
HEADER=['行业','用电量本月','用电量上年同月','本月同比','用电量本年','用电量上年','本年同比']
MONTHLY=re.compile(r'(\d{4})年(?:(1[-—至]\d{1,2})|(\d{1,2}))月全社会(?:及|以及)分产业用电量信息')
ANNUAL=re.compile(r'(\d{4})年全社会用电量重点行业用电量等')
class Reader(HTMLParser):
 def __init__(self):super().__init__(convert_charrefs=True);self.tables=[];self.table=None;self.row=None;self.cell=None
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
def parse_table(title,html):
 title_match=MONTHLY.fullmatch(title or '');annual=ANNUAL.fullmatch(title or '')
 if not (title_match or annual):return {'status':'unrecognized_title','values':[]}
 year=title_match.group(1) if title_match else annual.group(1)
 month=int((title_match.group(2).split('-')[-1].split('—')[-1].split('至')[-1] if title_match.group(2) else title_match.group(3))) if title_match else None
 if month is not None and not 1<=month<=12:return {'status':'invalid_title_month','values':[]}
 p=Reader();p.feed(html if isinstance(html,str) else '')
 if len(p.tables)!=1:return {'status':'unsupported_table_count','values':[]}
 table=p.tables[0]
 if not table or table[0]!=HEADER:return {'status':'unsupported_header','values':[]}
 if len(table)<2 or any(len(row)!=len(HEADER) for row in table):return {'status':'unsupported_table_shape','values':[]}
 categories=[row[0] for row in table[1:]]
 if any(not x for x in categories) or len(categories)!=len(set(categories)):return {'status':'missing_or_duplicate_category','values':[]}
 rows=[];problems=[]
 for row_no,row in enumerate(table[1:],2):
  for col in range(1,len(HEADER)):
   native=row[col];state='numeric' if re.fullmatch(r'-?\d+(?:\.\d+)?',native) else 'empty' if not native else 'non_numeric'
   if state!='numeric':problems.append({'row':row_no,'column':col+1,'state':state})
   rows.append({'titlePeriod':f'{year}-{month:02d}' if month else year,'titlePeriodPrecision':'月' if month else '年（月份未注明）',
                'categoryNative':row[0],'columnNative':HEADER[col],'valueNative':native,'valueState':state,
                'unitNative':'原文未注明','sourceRow':row_no,'sourceColumn':col+1})
 return {'status':'parsed_with_cell_review' if problems else 'parsed','values':rows,'cellIssues':problems,
         'limitations':['Source table does not state units; values and rate units are not inferred.',
                        'Totals, sector subtotals and residential categories overlap.']}
def export_page(result,destination):
 records=result.get('rows');decoded=result.get('site_decoded_rows')
 if not isinstance(records,list) or not isinstance(decoded,list) or len(records)!=len(decoded):raise ValueError('listing_and_site_decoded_rows_mismatch')
 rows=[];statuses=[]
 for record,shown in zip(records,decoded):
  guid=record.get('guid')
  if not guid or guid!=shown.get('native_guid') or shown.get('provenance')!='original_site_component_after_this_completed_native_query':raise ValueError('native_decoded_row_identity_mismatch')
  content=shown.get('site_content_value');p=parse_table(record.get('title'),content);digest=hashlib.sha256((content if isinstance(content,str) else '').encode()).hexdigest()
  statuses.append({'nativeGuid':guid,'status':p['status'],'cellIssues':p.get('cellIssues',[]),'contentSha256':digest})
  rows.extend({**x,'nativeGuid':guid,'title':record.get('title'),'contentSha256':digest,'responseSha256':result.get('sha256'),'provenance':'site_component_transformed_value'} for x in p['values'])
 destination.mkdir(parents=True,exist_ok=True);target=destination/'industry_consumption_values.csv';tmp=destination/'industry_consumption_values.csv.tmp'
 fields=['titlePeriod','titlePeriodPrecision','categoryNative','columnNative','valueNative','valueState','unitNative','sourceRow','sourceColumn','nativeGuid','title','contentSha256','responseSha256','provenance']
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
 tmp.replace(target)
 return {'status':'completed_with_review' if any(s['status']!='parsed' for s in statuses) else 'parsed','records':len(records),'values':len(rows),'recordsStatus':statuses,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
