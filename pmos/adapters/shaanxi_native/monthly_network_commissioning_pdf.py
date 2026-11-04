"""Parse labelled substation and transmission tables in archived monthly PDFs."""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber

def clean(v):return re.sub(r'\s*\n\s*','',v).strip() if isinstance(v,str) else ''
def compact(v):return re.sub(r'\s+','',v or '')
STATEMENT_PATTERN=re.compile(r'(?P<year>20\d{2})年(?P<month>\d{1,2})月，陕西电网(?P<statement>无新建330千伏及以上变电设备|无330千伏及以上变电设备投产|无330千伏及以上输电设备投产|新建330千伏及以上输电线路)。')
STATEMENT_TYPES={'无新建330千伏及以上变电设备':('变电设备','无新建'),'无330千伏及以上变电设备投产':('变电设备','无投产'),'无330千伏及以上输电设备投产':('输电设备','无投产'),'新建330千伏及以上输电线路':('输电线路','新建，原句未给数量')}
STATEMENT_FIELDS=['catalogueDate','sourcePeriod','kindNative','statusNative','voltageScopeNative','sourceSentence','sourcePage','sourceSha256','review']
def explicit_statement_values(text,page,catalogue,source_sha):
 result=[]
 for m in STATEMENT_PATTERN.finditer(compact(text)):
  year,month=int(m['year']),int(m['month']);dt.date(year,month,1);kind,status=STATEMENT_TYPES[m['statement']]
  result.append({'catalogueDate':catalogue,'sourcePeriod':f'{year:04d}-{month:02d}','kindNative':kind,'statusNative':status,'voltageScopeNative':'330千伏及以上','sourceSentence':m.group(0),'sourcePage':page,'sourceSha256':source_sha,'review':''})
 return result
GEN=re.compile(r'表\d+(\d{4})年(\d{1,2})月陕西10千伏及以上新增归调发电工程明细')
CAP=re.compile(r'表(\d+(?:-\d+)?)(\d{4})年(\d{1,2})月陕西(新增|退运)330千伏及以上(变电|输电)工程明细')
SUB6=['序号','电压等级(千伏)','变电站名称','主变编号','设备容量(兆伏安)','归调时间']
SUB7=SUB6+['工程作用']
LINEBASE=['序号','电压等级(千伏)','两端厂站','','线路名称','线路长度(千米)']
FIELDS=['catalogueDate','sourcePeriod','captionNative','tableActionNative','kindNative','sequenceNative','voltageNative','voltageUnitNative','stationNative','transformerNative','capacityNative','capacityUnitNative','impactNative','impactOrigin','impactSourceRow','impactContinuation','endpoint1Native','endpoint2Native','lineNameNative','lineLengthNative','lineLengthUnitNative','timeHeaderNative','eventDateNative','sourcePage','sourceTable','sourceRowOnTable','sourceRowBbox','nativeCells','review','sourceSha256']
def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as e:raise ValueError('invalid_catalogue_date') from e
 rows=[];issues=[];statements=[];explicit=[];captions=[];current=None;prior_table=None;source_period=None;seen_generation=False
 def finish_current():
  if current and current['kind']!='generation' and not current['rows']:
   issues.append({'captionNative':current['caption'],'reason':'原声明细表标题下未捕获数据行，需核对'})
 with pdfplumber.open(pdf) as doc:
  for pi,page in enumerate(doc.pages,1):
   explicit.extend(explicit_statement_values(page.extract_text() or '',pi,catalogue,digest))
   events=[]
   for line in page.extract_text_lines():
    text=compact(line['text']);m=CAP.fullmatch(text);g=GEN.fullmatch(text)
    if m:events.append((line['top'],0,'network_caption',m,text))
    elif g:events.append((line['top'],0,'generation_caption',g,text))
    elif re.search(r'表\d',text) and ('变电工程明细' in text or '输电工程明细' in text):raise ValueError('unrecognized_network_caption')
    if ('变电设备' in text or '输电设备' in text or '输电线路' in text) and ('无' in text or '新建' in text):statements.append({'page':pi,'textNative':line['text']})
   for ti,table in enumerate(page.find_tables(),1):events.append((table.bbox[1],1,'table',table,ti))
   for _,_,kind,item,extra in sorted(events,key=lambda x:(x[0],x[1])):
    if kind=='generation_caption':
     finish_current()
     seen_generation=True;source_period=f'{int(item[1]):04d}-{int(item[2]):02d}';dt.date(int(item[1]),int(item[2]),1);current={'kind':'generation'};prior_table=None;continue
    if kind=='network_caption':
     finish_current()
     y,m=int(item[2]),int(item[3]);dt.date(y,m,1);period=f'{y:04d}-{m:02d}'
     if source_period!=period:raise ValueError('network_period_differs_from_generation_title')
     current={'kind':item[5],'action':item[4],'caption':extra,'period':period,'head':None,'lastSeq':0,'rows':0};captions.append({'page':pi,'captionNative':extra,'kind':item[5],'action':item[4]});prior_table=None;continue
    if current is None:raise ValueError('table_without_known_caption')
    t=item;ti=extra;matrix=t.extract()
    if not matrix or len(matrix)!=len(t.rows):raise ValueError('invalid_table_geometry')
    h=[compact(x) for x in matrix[0]]
    if current['kind']=='generation':
     if '电厂名称' not in h and not (len(h)==8 and h[0].isdigit()):raise ValueError('unknown_generation_table')
     continue
    if current['kind']=='变电':
     expected=(SUB6,SUB7);header_ok=h in expected
    else:
     header_ok=len(h)==7 and h[:6]==LINEBASE and h[6] in ('归调时间','退运时间')
    if header_ok:
     if current['head'] is not None and current['head']!=h:raise ValueError('source_table_header_changed')
     if current['kind']=='输电' and ((current['action']=='退运') != (h[6]=='退运时间')):raise ValueError('caption_time_column_conflict')
     current['head']=h;start=1
    elif current['head'] is not None and len(h)==len(current['head']) and h[0].isdigit():start=0
    elif current['head'] is not None and current['kind']=='变电' and len(current['head'])==7 and pi>1 and ti==1 and len(matrix)==1 and len(h)==7 and all(not z for z in h[:6]) and h[6]:
     if not rows or prior_table is None or rows[-1]['sourcePage']!=pi-1 or rows[-1]['kindNative']!='变电':raise ValueError('unproven_cross_page_continuation')
     oldcell=prior_table.rows[-1].cells[6];newcell=t.rows[0].cells[6]
     if not oldcell or not newcell or abs(oldcell[0]-newcell[0])>2 or abs(oldcell[2]-newcell[2])>2:raise ValueError('cross_page_column_geometry_differs')
     rows[-1]['impactNative']+=clean(matrix[0][6]);rows[-1]['impactContinuation']=json.dumps({'sourcePage':pi,'sourceTable':ti,'sourceRowOnTable':1,'nativeCells':matrix[0],'bbox':t.rows[0].bbox},ensure_ascii=False,separators=(',',':'));rows[-1]['review']='；'.join(filter(None,[rows[-1]['review'],'跨页工程作用按原列位置衔接，待核']));issues.append({'page':pi,'reason':'跨页工程作用按原列位置衔接，待核'});continue
    else:raise ValueError('unsupported_or_unbound_network_table')
    for ri,raw in enumerate(matrix[start:],start+1):
     c=[clean(x) for x in raw]
     if len(c)!=len(current['head']) or not any(c):raise ValueError('invalid_network_row')
     review=[];seq=c[0]
     if not seq.isdigit() or int(seq)!=current['lastSeq']+1:review.append('原文序号缺失或不连续')
     if seq.isdigit():current['lastSeq']=int(seq)
     current['rows']+=1
     row={'catalogueDate':catalogue,'sourcePeriod':current['period'],'captionNative':current['caption'],'tableActionNative':current['action'],'kindNative':current['kind'],'sequenceNative':seq,'voltageNative':c[1],'voltageUnitNative':'千伏','stationNative':'','transformerNative':'','capacityNative':'','capacityUnitNative':'','impactNative':'','impactOrigin':'','impactSourceRow':'','impactContinuation':'','endpoint1Native':'','endpoint2Native':'','lineNameNative':'','lineLengthNative':'','lineLengthUnitNative':'','timeHeaderNative':'','eventDateNative':'','sourcePage':pi,'sourceTable':ti,'sourceRowOnTable':ri,'sourceRowBbox':t.rows[ri-1].bbox,'nativeCells':raw,'review':'','sourceSha256':digest}
     if current['kind']=='变电':
      row.update({'stationNative':c[2],'transformerNative':c[3],'capacityNative':c[4],'capacityUnitNative':'兆伏安','timeHeaderNative':current['head'][5],'eventDateNative':c[5]})
      if len(c)==7:
       row['impactNative']=c[6];row['impactOrigin']='native_cell' if c[6] else ''
       if raw[6] is None:
        seqcell=t.rows[ri-1].cells[0];y=(seqcell[1]+seqcell[3])/2 if seqcell else None;candidates=[]
        for pri in range(start,ri-1):
         cell=t.rows[pri].cells[6]
         if cell and y is not None and cell[1]<=y<=cell[3] and clean(matrix[pri][6]):candidates.append((pri,cell,clean(matrix[pri][6])))
        if len(candidates)==1:
         parent,bbox,value=candidates[0];row['impactNative']=value;row['impactOrigin']='merged_cell';row['impactSourceRow']=str(parent+1)
        else:review.append('原文合并工程作用单元格无法唯一匹配')
       elif not c[6]:review.append('原文工程作用为空')
     else:
      row.update({'endpoint1Native':c[2],'endpoint2Native':c[3],'lineNameNative':c[4],'lineLengthNative':c[5],'lineLengthUnitNative':'千米','timeHeaderNative':current['head'][6],'eventDateNative':c[6]})
      if '改建' in c[4]:review.append('线路名称原文标记改建，表题同时为'+current['action'])
      if c[5].startswith(('-','−')):review.append('原文线路长度为负，口径待核')
     if not c[1] or not row['eventDateNative']:review.append('原文电压等级或日期为空')
     row['review']='；'.join(review)
     if review:issues.append({'page':pi,'rowOnTable':ri,'reasons':review})
     rows.append(row)
    prior_table=t
 finish_current()
 if not seen_generation or source_period is None:raise ValueError('missing_generation_context')
 if not captions:issues.append({'reason':'原文未提供可识别的变电或输电明细表，保留正文供复核'})
 for item in explicit:
  if item['sourcePeriod']!=source_period:item['review']='正文月份与发电工程表月份不同，待核'
 out={'status':'parsed_with_review' if issues else 'parsed','catalogueDate':catalogue,'sourcePeriod':source_period,'rows':rows,'issues':issues,'captions':captions,'sourceStatements':statements,'explicitStatements':explicit,'limitations':['仅解析原文有明确表题的变电、输电工程表；未提供表格的正文不推断明细或数值零。','按原文区分新增表、退运表及行内改建标记；负长度不自动解释。','工程作用的原表合并单元格按真实几何关联，跨页续格单独保留并标待核。','不覆盖发电工程表，该表由独立解析器处理。']}
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'monthly_network_commissioning_values.csv';tmp=target.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows);f.flush()
 tmp.replace(target)
 statement_target=dest/'monthly_network_commissioning_statement_values.csv';statement_tmp=statement_target.with_suffix('.csv.tmp')
 with statement_tmp.open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=STATEMENT_FIELDS);w.writeheader();w.writerows(explicit);f.flush()
 statement_tmp.replace(statement_target);parsed=dest/'monthly_network_commissioning_parsed.json';tmp=parsed.with_suffix('.json.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(parsed)
 return {'status':out['status'],'sourcePeriod':source_period,'rows':len(rows),'issues':issues,'captionTables':len(captions),'sourceSha256':digest,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'statementRows':len(explicit),'statementCsvSha256':hashlib.sha256(statement_target.read_bytes()).hexdigest(),'path':str(parsed)}
