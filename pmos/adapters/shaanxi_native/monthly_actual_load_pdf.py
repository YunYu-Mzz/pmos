"""Extract explicit monthly load extrema while preserving source scope and dates."""
import csv,datetime as dt,hashlib,json,re
from decimal import Decimal
from pathlib import Path
import pdfplumber
FIELDS=['catalogueDate','sourceYear','sourceMonth','metricNative','scopeNative','valueNative','unitNative','eventMonthDayNative','eventDateDerived','eventDateBasis','sourceSentence','sourcePage','sourceSha256','review']
def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as exc:raise ValueError('invalid_catalogue_date') from exc
 with pdfplumber.open(pdf) as f:
  pages=[re.sub(r'\s+','',p.extract_text() or '') for p in f.pages]
 text=''.join(pages);title=list(re.finditer(r'(20\d{2})年(\d{1,2})月陕西电网负荷情况',text))
 if len(title)!=1:raise ValueError('unrecognized_or_ambiguous_title')
 year,month=map(int,title[0].groups());dt.date(year,month,1);pattern=r'(最大(?:用电)?负荷|最小(?:用电)?负荷)(-?\d+(?:\.\d+)?)万千瓦[（(](\d{1,2})月(\d{1,2})日[）)]';hits=list(re.finditer(pattern,text));mentioned=re.findall(r'最大(?:用电)?负荷|最小(?:用电)?负荷',text)
 if not hits or len(hits)!=len(mentioned) or len(hits)>2:raise ValueError('unrecognized_or_ambiguous_load_extrema')
 kinds=['最大' if m.group(1).startswith('最大') else '最小' for m in hits]
 if len(kinds)!=len(set(kinds)):raise ValueError('duplicate_extremum')
 rows=[]
 for hit in hits:
  metric,value,event_month,event_day=hit.groups();event_month=int(event_month);event_day=int(event_day);sentence_start=text.rfind('。',0,hit.start())+1;sentence_end=text.find('。',hit.end())
  if sentence_end<0:raise ValueError('source_sentence_incomplete')
  sentence=text[sentence_start:sentence_end+1];warnings=[];scope=''
  if '全月晚峰' in sentence:
   if metric.startswith('最大'):scope='全月晚峰'
   else:scope='同句含全月晚峰，最小值范围待核';warnings.append('最小值是否限于晚峰需核实')
  missing_month=re.search(r'陕西电网(\d{1,2})(?=最大)',sentence)
  if missing_month:warnings.append('原句月份后缺“月”字，保留原句')
  if event_month!=month:event_date='';basis='原文事件月份与标题月份不同';warnings.append('事件月份与标题月份不同，未补年份')
  else:
   try:event_date=dt.date(year,event_month,event_day).isoformat()
   except ValueError:event_date='';warnings.append('事件日期无效，保留原文')
   basis='标题年度＋原文月日' if event_date else '原文日期待核'
  page=next((i for i,p in enumerate(pages,1) if hit.group(0) in p),None)
  if page is None:raise ValueError('extremum_crosses_page_boundary')
  rows.append({'catalogueDate':catalogue,'sourceYear':str(year),'sourceMonth':str(month),'metricNative':metric,'scopeNative':scope,'valueNative':value,'unitNative':'万千瓦','eventMonthDayNative':f'{hit.group(3)}月{hit.group(4)}日','eventDateDerived':event_date,'eventDateBasis':basis,'sourceSentence':sentence,'sourcePage':page,'sourceSha256':digest,'review':'；'.join(warnings)})
 issues=[]
 if len(rows)!=2:issues.append('原文未识别到最大、最小两项，不填补')
 if len(rows)==2 and Decimal(next(x['valueNative'] for x in rows if x['metricNative'].startswith('最大')))<Decimal(next(x['valueNative'] for x in rows if x['metricNative'].startswith('最小'))):issues.append('原文最大值小于最小值，待核')
 if any(x['review'] for x in rows):issues.append('部分源文范围或日期需核实')
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);p=dest/'monthly_actual_load_values.csv';tmp=p.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
 tmp.replace(p);out={'status':'parsed_with_review' if issues else 'parsed','catalogueDate':catalogue,'sourceYear':str(year),'sourceMonth':str(month),'sourceSha256':digest,'rows':rows,'issues':issues,'narrativeNative':text,'limitations':['目录日期与正文月份分开保存；完整日期仅用标题年度与同月原文月日组合。','原文只提供一个极值时不推断另一项；限定晚峰或原文不完整处保留待核。','不将月极值当逐日时间序列。']};q=dest/'monthly_actual_load_parsed.json';tmp=q.with_suffix('.json.tmp');tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(q)
 return {'status':out['status'],'catalogueDate':catalogue,'sourceYear':str(year),'sourceMonth':str(month),'rows':len(rows),'sourceSha256':digest,'csvSha256':hashlib.sha256(p.read_bytes()).hexdigest(),'path':str(q)}
