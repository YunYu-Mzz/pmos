"""Extract native monthly predicted load bounds, preserving date differences."""
import csv,datetime as dt,hashlib,json,re
from decimal import Decimal
from pathlib import Path
import pdfplumber
N=r'-?\d+(?:\.\d+)?'

def parse_text(text,source_date,source_sha256):
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as exc:raise ValueError('invalid_catalogue_date') from exc
 compact=re.sub(r'\s+','',text);titles=list(re.finditer(r'(\d{4})年(\d{1,2})月陕西电网用电负荷预测',compact))
 if len(titles)!=1:raise ValueError('unrecognized_or_ambiguous_title')
 year,month=map(int,titles[0].groups());dt.date(year,month,1);period=f'{year:04d}-{month:02d}'
 pattern=r'预测(\d{4})年(\d{1,2})月陕西电网最大负荷('+N+r')万千瓦，最小负荷('+N+r')万千瓦。'
 matches=list(re.finditer(pattern,compact[titles[0].end():]))
 if len(matches)!=1:raise ValueError('unrecognized_or_ambiguous_prediction')
 m=matches[0]
 if (int(m[1]),int(m[2]))!=(year,month):raise ValueError('title_and_prediction_period_mismatch')
 issues=[]
 if catalogue[:7]!=period:issues.append('目录月份与正文月份不一致')
 if Decimal(m[3])<Decimal(m[4]):issues.append('原文最大负荷小于最小负荷')
 rows=[{'catalogueDate':catalogue,'titlePeriod':period,'metricNative':metric,'valueNative':value,'unitNative':'万千瓦',
        'scopeNative':'原文预测','sourceTextCompact':m.group(0),'review':'; '.join(issues),'sourceSha256':source_sha256}
       for metric,value in [('最大负荷',m[3]),('最小负荷',m[4])]]
 return {'status':'parsed_with_review' if issues else 'parsed','catalogueDate':catalogue,'titlePeriod':period,'rows':rows,'issues':issues,
         'limitations':['目录日期和正文所属月份分别保存，不自动修正。','预测值不是实际用电负荷。']}

def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);raw=pdf.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=source_sha256:raise ValueError('pdf_hash_mismatch')
 with pdfplumber.open(pdf) as document:layout='\n'.join(page.extract_text() or '' for page in document.pages)
 result=parse_text(layout,source_date,source_sha256);dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'monthly_load_forecast_values.csv';tmp=target.with_suffix('.csv.tmp');fields=['catalogueDate','titlePeriod','metricNative','valueNative','unitNative','scopeNative','sourceTextCompact','review','sourceSha256']
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(result['rows']);f.flush()
 tmp.replace(target);out=dest/'monthly_load_forecast_parsed.json';tmp=out.with_suffix('.json.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');tmp.replace(out)
 return {'status':result['status'],'titlePeriod':result['titlePeriod'],'rows':len(result['rows']),'issues':result['issues'],'sourceSha256':source_sha256,'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'path':str(out)}
