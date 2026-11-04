"""Extract observed monthly spot-trading quantities and prices from PDFs."""
import csv,datetime as dt,hashlib,json,re
from pathlib import Path
import pdfplumber
N=r'-?\d+(?:\.\d+)?'

def parse_text(text,source_date,source_sha256):
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as exc:raise ValueError('invalid_catalogue_date') from exc
 compact=re.sub(r'\s+','',text);titles=list(re.finditer(r'(\d{4})年(\d{1,2})月省内现货交易总体情况',compact))
 if len(titles)!=1:raise ValueError('unrecognized_or_ambiguous_title')
 year,month=map(int,titles[0].groups());dt.date(year,month,1);period=f'{year:04d}-{month:02d}';body=compact[titles[0].end():]
 lead=re.match(r'(\d{4})年(\d{1,2})月省内现货交易(\d{1,2})天(?:[（(]([^）)]+)[）)])?[，,]',body)
 if not lead or (int(lead[1]),int(lead[2]))!=(year,month) or not 1<=int(lead[3])<=31:raise ValueError('source_period_or_trading_days_mismatch')
 main=body[lead.end():];supp=''
 if '补充：' in main:
  if main.count('补充：')!=1:raise ValueError('ambiguous_supplement')
  main,supp=main.split('补充：',1)
 rows=[]
 def add(period,section,market,category,metric,value,unit,source):
  rows.append({'catalogueDate':catalogue,'titlePeriod':f'{year:04d}-{month:02d}','valuePeriod':period,'sectionNative':section,'sideNative':'发电侧' if '发电侧' in source else '原文未注明',
               'marketNative':market,'categoryNative':category,'metricNative':metric,'valueNative':value,'unitNative':unit,'sourceTextCompact':source,'sourceSha256':source_sha256})
 add(period,'正文','','','交易天数',lead[3],'天',lead.group(0))
 modern=re.fullmatch(r'发电侧日前市场日均出清电量('+N+r')亿千瓦时，出清均价('+N+r')元/MWh。发电侧实时市场日均出清电量('+N+r')亿千瓦时，出清均价('+N+r')元/MWh，其中火电实时出清均价('+N+r')元/MWh，风电实时出清均价('+N+r')元/MWh，光伏实时出清均价('+N+r')元/MWh。',main)
 weighted=re.fullmatch(r'发电侧日前加权均价('+N+r')元/MWh，日均出清电量('+N+r')亿千瓦时。发电侧实时加权均价('+N+r')元/MWh，日均出清电量('+N+r')亿千瓦时。',main)
 legacy=re.fullmatch(r'日前出清均价('+N+r')元/MWh，日均出清电量('+N+r')亿千瓦时。日内出清均价('+N+r')元/MWh，日均出清电量('+N+r')亿千瓦时。',main)
 if modern:
  for market,q,p in [('日前',modern[1],modern[2]),('实时',modern[3],modern[4])]:
   add(period,'正文',market,'原文未分类型','日均出清电量',q,'亿千瓦时',main);add(period,'正文',market,'原文未分类型','出清均价',p,'元/MWh',main)
  for category,value in zip(('火电','风电','光伏'),modern.groups()[4:]):add(period,'正文','实时',category,'出清均价',value,'元/MWh',main)
  template='modern_monthly_clearing'
 elif weighted or legacy:
  match=weighted or legacy;markets=('日前','实时') if weighted else ('日前','日内');price='加权均价' if weighted else '出清均价'
  for market,p,q in [(markets[0],match[1],match[2]),(markets[1],match[3],match[4])]:
   add(period,'正文',market,'原文未分类型',price,p,'元/MWh',main);add(period,'正文',market,'原文未分类型','日均出清电量',q,'亿千瓦时',main)
  template='weighted_average' if weighted else 'day_ahead_intraday'
 else:raise ValueError('unrecognized_monthly_trade_template')
 if supp:
  pattern=re.compile(r'(\d{4})年(\d{1,2})月火电实时出清均价('+N+r')元/MWh，风电实时出清均价('+N+r')元/MWh，光伏实时出清均价('+N+r')元/MWh。')
  matches=list(pattern.finditer(supp))
  if not matches or ''.join(m.group(0) for m in matches)!=supp:raise ValueError('unrecognized_supplement')
  for m in matches:
   y,mo=int(m[1]),int(m[2]);dt.date(y,mo,1);supp_period=f'{y:04d}-{mo:02d}'
   for category,value in zip(('火电','风电','光伏'),m.groups()[2:]):add(supp_period,'补充','实时',category,'出清均价',value,'元/MWh',m.group(0))
 return {'status':'parsed','template':template,'catalogueDate':catalogue,'titlePeriod':period,'tradingDaysNative':lead[3],'tradingWindowNative':lead[4] or '', 'rows':rows,
         'limitations':['目录日期和正文月份分别保存，不能互相替代。','补充段按其原文月份独立保存，不合并或累加。','原文日内不自动改称实时，出清数据不等于结算数据。']}

def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);raw=pdf.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=source_sha256:raise ValueError('pdf_hash_mismatch')
 with pdfplumber.open(pdf) as document:layout='\n'.join(page.extract_text() or '' for page in document.pages)
 result=parse_text(layout,source_date,source_sha256);dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True)
 target=dest/'monthly_trade_values.csv';tmp=target.with_suffix('.csv.tmp');fields=['catalogueDate','titlePeriod','valuePeriod','sectionNative','sideNative','marketNative','categoryNative','metricNative','valueNative','unitNative','sourceTextCompact','sourceSha256']
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(result['rows']);f.flush()
 tmp.replace(target);out=dest/'monthly_trade_parsed.json';tmp=out.with_suffix('.json.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');tmp.replace(out)
 return {'status':result['status'],'template':result['template'],'titlePeriod':result['titlePeriod'],'rows':len(result['rows']),'sourceSha256':source_sha256,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(out)}
