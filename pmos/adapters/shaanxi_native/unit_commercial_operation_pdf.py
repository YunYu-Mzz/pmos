"""Extract observed unit commissioning tables from archived native PDFs."""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber

FULL=['项目名称','调度命名','电源类型','核准装机（MW）','试运行结束时间']
LEFT=FULL[:3];RIGHT=FULL[3:]

def clean(value):
 return re.sub(r'\s*\n\s*','',value).strip() if isinstance(value,str) else ''

def parse_tables(pages,source_date,source_sha256):
 try:catalog=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as exc:raise ValueError('invalid_catalogue_date') from exc
 # Each page contains entries {rows, cells, rowBboxes}, preserving geometry.
 if len(pages)==1 and len(pages[0])==1 and pages[0][0]['rows'] and [clean(x) for x in pages[0][0]['rows'][0]]==FULL:
  t=pages[0][0];pairs=[(row,t['cells'][i],t['rowBboxes'][i],None,t.get('projectRegions',{}).get(i)) for i,row in enumerate(t['rows'][1:],1)];layout='one_page_five_columns'
 elif (len(pages)==2 and all(len(p)==1 for p in pages) and pages[0][0]['rows'] and pages[1][0]['rows']
       and [clean(x) for x in pages[0][0]['rows'][0]]==LEFT and [clean(x) for x in pages[1][0]['rows'][0]]==RIGHT):
  a,b=pages[0][0],pages[1][0]
  if len(a['rows'])!=len(b['rows']) or len(a['rows'])<2:raise ValueError('split_table_row_counts_mismatch')
  pairs=[(a['rows'][i]+b['rows'][i],a['cells'][i]+b['cells'][i],a['rowBboxes'][i],b['rowBboxes'][i],None) for i in range(1,len(a['rows']))];layout='two_page_column_split_positional_link_requires_review'
 elif (len(pages)==2 and all(len(p)==1 for p in pages) and pages[0][0]['rows'] and pages[1][0]['rows']
       and [clean(x) for x in pages[0][0]['rows'][0]]==FULL[:2] and [clean(x) for x in pages[1][0]['rows'][0]]==FULL[2:]):
  a,b=pages[0][0],pages[1][0]
  if len(a['rows'])!=2 or len(b['rows'])!=2:raise ValueError('unruled_table_requires_single_row')
  pairs=[(a['rows'][1]+b['rows'][1],a['cells'][1]+b['cells'][1],a['rowBboxes'][1],b['rowBboxes'][1],None)];layout='two_page_unruled_single_row_positional_link_requires_review'
 else:raise ValueError('unsupported_table_layout')
 if not pairs:raise ValueError('no_unit_rows')
 output=[];issues=[];last_project=None
 for i,(row,cells,bbox,right_bbox,region) in enumerate(pairs,2):
  if len(row)!=5 or len(cells)!=5:raise ValueError('unexpected_row_shape')
  raw_project=row[0];project=clean(raw_project);project_resolution='direct_native_cell'
  if project and cells[0] is not None:last_project=(raw_project,cells[0])
  elif not project:
   if last_project and last_project[1][1]<=bbox[1]+0.5 and last_project[1][3]>=bbox[3]-0.5:
    raw_project=last_project[0];project=clean(raw_project);project_resolution='merged_cell_geometry'
   elif region and clean(region.get('text')):
    raw_project=region['text'];project=clean(raw_project);project_resolution='row_region_geometry_requires_review';issues.append({'row':i,'field':'project','reason':'row_region_geometry_requires_review','regionBbox':region['bbox']})
   else:project_resolution='missing_requires_review';issues.append({'row':i,'field':'project','reason':'cell_not_found_or_merged_geometry_unproven'})
  dispatch,power,capacity,end=map(clean,row[1:]);date_iso=''
  if not dispatch or not power:issues.append({'row':i,'field':'unit_or_type','reason':'missing'})
  if not re.fullmatch(r'\d+(?:\.\d+)?',capacity):issues.append({'row':i,'field':'capacity','reason':'non_numeric_or_missing'})
  try:
   nums=[int(x) for x in re.split(r'[-/]',end)]
   if len(nums)!=3:raise ValueError('date field')
   date_iso=dt.date(*nums).isoformat()
  except (ValueError,TypeError):issues.append({'row':i,'field':'trialEndDate','reason':'invalid_or_missing'})
  if layout.startswith('two_page'):issues.append({'row':i,'field':'crossPageLink','reason':'positional_link_requires_review'})
  output.append({'catalogueDate':catalog,'projectNative':project,'projectRaw':raw_project or '', 'projectResolution':project_resolution,'dispatchNameNative':dispatch,'dispatchRaw':row[1] or '',
                 'powerTypeNative':power,'approvedCapacityNative':capacity,'capacityUnitNative':'MW','trialEndDateNative':end,'trialEndDateISO':date_iso,
                 'sourceRow':i,'sourcePage':1,'secondPartPage':2 if right_bbox else None,'rowBbox':bbox,'secondPartBbox':right_bbox,
                 'layout':layout,'sourceSha256':source_sha256})
 return {'status':'completed_with_review' if issues else 'parsed','layout':layout,'rows':output,'issues':issues,
         'limitations':['调度命名是网站原文，不当作机组 ID。','试运行结束日期来自表格，目录日期来自文件元数据，两者含义不同。','跨页并列列只按行次序对应，标记待核。']}

def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);raw=pdf.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=source_sha256:raise ValueError('pdf_hash_mismatch')
 with pdfplumber.open(pdf) as doc:
  pages=[];word_pages=[]
  for page in doc.pages:
   entries=[]
   for table in page.find_tables():
    native_rows=table.extract();cells=[row.cells for row in table.rows];bboxes=[row.bbox for row in table.rows];regions={}
    if cells and len(cells[0])==5 and cells[0][0] is not None:
     header_cell=cells[0][0]
     for i in range(1,len(cells)):
      if len(cells[i])!=5 or cells[i][0] is not None or cells[i][1] is None:continue
      row_box=bboxes[i];box=(header_cell[0],row_box[1]+0.2,header_cell[2],row_box[3]-0.2)
      if box[0]<box[2] and box[1]<box[3] and abs(cells[i][1][0]-header_cell[2])<1:
       region_text=page.within_bbox(box).extract_text() or ''
       if region_text:regions[i]={'text':region_text,'bbox':box}
    entries.append({'rows':native_rows,'cells':cells,'rowBboxes':bboxes,'projectRegions':regions})
   pages.append(entries);word_pages.append(page.extract_words())
 try:result=parse_tables(pages,source_date,source_sha256)
 except ValueError as exc:
  if str(exc)!='unsupported_table_layout' or len(pages)!=2 or any(pages):raise
  synthetic=[]
  for words,headers in zip(word_pages,(FULL[:2],FULL[2:])):
   ordered=sorted(words,key=lambda x:(x['top'],x['x0']));count=len(headers)
   if len(ordered)!=2*count:raise ValueError('unruled_table_unexpected_word_count')
   head=sorted(ordered[:count],key=lambda x:x['x0']);data=sorted(ordered[count:],key=lambda x:x['x0'])
   if [x['text'] for x in head]!=headers or max(x['top'] for x in head)>=min(x['top'] for x in data) or max(x['top'] for x in data)-min(x['top'] for x in data)>4:raise ValueError('unruled_table_unrecognized_header_or_row')
   bounds=lambda x:(x['x0'],x['top'],x['x1'],x['bottom'])
   boxes=[bounds(x) for x in data];bbox=(min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes))
   synthetic.append([{'rows':[[x['text'] for x in head],[x['text'] for x in data]],'cells':[[bounds(x) for x in head],boxes],'rowBboxes':[None,bbox]}])
  result=parse_tables(synthetic,source_date,source_sha256)
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'unit_commercial_operation_values.csv';tmp=target.with_suffix('.csv.tmp')
 fields=['catalogueDate','projectNative','projectRaw','projectResolution','dispatchNameNative','dispatchRaw','powerTypeNative','approvedCapacityNative','capacityUnitNative','trialEndDateNative','trialEndDateISO','sourceRow','sourcePage','secondPartPage','rowBbox','secondPartBbox','layout','sourceSha256']
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(result['rows']);f.flush()
 tmp.replace(target);out=dest/'unit_commercial_operation_parsed.json';tmpjson=out.with_suffix('.json.tmp');tmpjson.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');tmpjson.replace(out)
 return {'status':result['status'],'rows':len(result['rows']),'layout':result['layout'],'issues':result['issues'],'sourceSha256':source_sha256,'path':str(out),'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest()}
