"""Extract dated period/section rows from supported monthly source PDFs."""
import csv,datetime as dt,decimal,hashlib,json,re
from collections import defaultdict
from pathlib import Path
import pdfplumber

HEAD=['序号','断面名称','电压等级','断面限额','预计最大潮流','预计最大负载率']
CAP=re.compile(r'(\d{1,2})月(\d{1,2})日{1,2}(?:[-—至](\d{1,2})月(\d{1,2})日{1,2})?关键断面的输电能力[（(](高峰|平段|低谷)[）)]')
FIELDS=['catalogueDate','nativeTitle','nativeBodyTitle','titlePeriod','captionNative','startDate','endDate','periodNative','sequenceNative','sectionNative','voltageNative','voltageUnitNative','limitNative','flowNative','powerUnitNative','loadRateNative','sourcePage','sourceTable','sourceRowOnTable','sourceRowBbox','nativeCells','review','sourceSha256']
def clean(x):return re.sub(r'\s+','',x) if isinstance(x,str) else ''
def near(a,b):return len(a)==len(b) and all(abs(x-y)<1 for x,y in zip(a,b))
def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);raw=pdf.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as exc:raise ValueError('invalid_catalogue_date') from exc
 with pdfplumber.open(pdf) as doc:
  if not doc.pages:raise ValueError('empty_pdf')
  first=clean(doc.pages[0].extract_text() or '')
  mt=re.search(r'(\d{1,2})月现货开展期间关键断面的输电能力',first);mb=re.search(r'陕西电网(\d{4})年(\d{1,2})月主要断面及送电能力如下所示',first)
  if not mt or not mb:raise ValueError('unrecognized_titles')
  titlemonth=int(mt[1]);year=int(mb[1]);bodymonth=int(mb[2]);dt.date(year,titlemonth,1);period=f'{year:04d}-{titlemonth:02d}'
  base_review=[]
  if titlemonth!=bodymonth:base_review.append('原文标题月份与正文月份不一致')
  if catalogue[:7]!=period:base_review.append('目录月份与原文标题月份不一致')
  blocks=[];current=None;full_columns=None
  def finish():
   if current is not None:
    if not current['rows'] or not current['header'] or not current['units']:raise ValueError('incomplete_caption_table')
    blocks.append(current)
  for pi,page in enumerate(doc.pages,1):
   events=[]
   for line in page.extract_text_lines():
    text=clean(line['text']);m=CAP.fullmatch(text)
    if m:events.append((line['top'],0,'caption',m,text))
    elif '关键断面的输电能力' in text and ('高峰' in text or '平段' in text or '低谷' in text):raise ValueError('unrecognized_period_caption')
   for ti,table in enumerate(page.find_tables(),1):events.append((table.bbox[1],1,'table',table,ti))
   for _,_,kind,item,extra in sorted(events,key=lambda e:(e[0],e[1])):
    if kind=='caption':
     finish();m=item;sm,sd=int(m[1]),int(m[2]);em,ed=(int(m[3]),int(m[4])) if m[3] else (sm,sd);start=dt.date(year,sm,sd);end=dt.date(year,em,ed)
     if start>end:raise ValueError('caption_date_range_reversed')
     review=list(base_review)
     if '日日' in extra:review.append('原文日期出现重复日字')
     if sm!=titlemonth or em!=titlemonth:review.append('原文日期范围与标题月份不一致')
     current={'caption':extra,'start':start.isoformat(),'end':end.isoformat(),'period':m[5],'rows':[],'header':False,'units':False,'partial':False,'review':review};continue
    if current is None:raise ValueError('table_without_caption')
    table=item;table_index=extra;matrix=table.extract()
    if not matrix or len(matrix)!=len(table.rows):raise ValueError('invalid_table')
    for ri,rawrow in enumerate(matrix,1):
     cells=[clean(x) for x in rawrow];geoms=table.rows[ri-1].cells
     if cells==HEAD:
      if current['header']:raise ValueError('duplicate_header')
      current['header']=True
      xs=[(c[0],c[2]) for c in geoms if c]
      if len(xs)!=6:raise ValueError('header_geometry_missing')
      shape=[v for pair in xs for v in pair]
      if full_columns is not None and not near(shape,full_columns):raise ValueError('header_geometry_changed')
      full_columns=shape;continue
     if cells==['电压等级','断面限额','预计最大潮流']:
      if current['header'] or full_columns is None or len(geoms)!=3:raise ValueError('unproven_split_header')
      shape=[v for c in geoms for v in (c[0],c[2])]
      if not near(shape,full_columns[4:10]):raise ValueError('split_header_geometry_changed')
      current['header']=True;current['partial']=True;continue
     if len(cells)==6 and cells[:2]==['',''] and cells[2:] in (['（kV）','（万千瓦）','(万千瓦)',''],['(kV)','(万千瓦)','(万千瓦)','']):
      if not current['header'] or current['units']:raise ValueError('unit_row_without_header')
      current['units']=True;continue
     if len(cells)!=6 or not current['header'] or not current['units'] or cells[0]!=str(len(current['rows'])+1) or not all(cells):raise ValueError('unrecognized_or_missing_native_row')
     if not all(re.fullmatch(r'\d+(?:\.\d+)?',cells[k]) for k in (2,3,4)) or not re.fullmatch(r'\d+(?:\.\d+)?%',cells[5]):raise ValueError('invalid_native_number')
     if len(geoms)!=6 or any(c is None for c in geoms):raise ValueError('native_row_geometry_missing')
     shape=[v for c in geoms for v in (c[0],c[2])]
     if full_columns is None or not near(shape,full_columns):raise ValueError('native_row_geometry_changed')
     review=list(current['review'])
     limit=decimal.Decimal(cells[3]);flow=decimal.Decimal(cells[4]);rate=decimal.Decimal(cells[5][:-1])
     if limit and abs(flow*100/limit-rate)>decimal.Decimal('1'):review.append('潮流除以限额与原文负载率差异超过1个百分点，口径待核')
     current['rows'].append({'catalogueDate':catalogue,'nativeTitle':mt.group(0),'nativeBodyTitle':mb.group(0),'titlePeriod':period,'captionNative':current['caption'],'startDate':current['start'],'endDate':current['end'],'periodNative':current['period'],'sequenceNative':cells[0],'sectionNative':cells[1],'voltageNative':cells[2],'voltageUnitNative':'kV','limitNative':cells[3],'flowNative':cells[4],'powerUnitNative':'万千瓦','loadRateNative':cells[5],'sourcePage':pi,'sourceTable':table_index,'sourceRowOnTable':ri,'sourceRowBbox':table.rows[ri-1].bbox,'nativeCells':rawrow,'review':'；'.join(review),'sourceSha256':source_sha256})
  finish()
  if not blocks:raise ValueError('no_caption_tables')
  groups=defaultdict(set)
  for b in blocks:
   key=(b['start'],b['end'])
   if b['period'] in groups[key]:raise ValueError('duplicate_period_caption')
   groups[key].add(b['period'])
  if any(v!={'高峰','平段','低谷'} for v in groups.values()):raise ValueError('missing_period_caption')
  rows=[r for b in blocks for r in b['rows']];issues=[{'page':r['sourcePage'],'caption':r['captionNative'],'sequence':r['sequenceNative'],'review':r['review']} for r in rows if r['review']]
 out={'status':'parsed_with_review' if issues else 'parsed','catalogueDate':catalogue,'nativeTitle':mt.group(0),'nativeBodyTitle':mb.group(0),'titlePeriod':period,'rows':rows,'issues':issues,'captionTables':len(blocks),'splitHeaderTables':sum(b['partial'] for b in blocks),'limitations':['仅为原文分日期区间与时段的预测或限额，不逐日构造新数值。','保留原文负载率，算术差异不自动更正。','目录日期、标题与正文月份分别保存。']}
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'monthly_transmission_section_values.csv';tmp=target.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows);f.flush()
 tmp.replace(target);parsed=dest/'monthly_transmission_section_parsed.json';tmp=parsed.with_suffix('.json.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(parsed)
 return {'status':out['status'],'titlePeriod':period,'rows':len(rows),'issues':issues,'captionTables':len(blocks),'splitHeaderTables':out['splitHeaderTables'],'sourceSha256':source_sha256,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(parsed)}
