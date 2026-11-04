"""Parse observed green settlement PDF native tables without invented totals."""
import csv,hashlib,json,re
from pathlib import Path
import pdfplumber
FIELDS=['year','month','marketNative','rightsNative','periodNative','quantityNative','quantityUnit','priceNative','priceUnit','page','sourceSha256']
NUMBER=re.compile(r'^-?\d+(?:\.\d+)?$')
def parse_pdf(pdf,source_sha256,output_dir):
 pdf=Path(pdf);digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 rows=[];observed=set()
 with pdfplumber.open(pdf) as doc:
  texts=[p.extract_text() or '' for p in doc.pages];title=re.search(r'信息披露\s*[-－—]\s*绿电交易结算情况\s*[（(]\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*[）)]','\n'.join(texts))
  if not title or not 1<=int(title.group(2))<=12:raise ValueError('unsupported_document_title')
  year=title.group(1);month=int(title.group(2));expected={f'{month}月',f'累计1-{month}月'}
  for page_no,page in enumerate(doc.pages,1):
   text=re.sub(r'\s+','',texts[page_no-1]);tables=page.extract_tables()
   if tables and text.count('单位：亿千瓦时、元/兆瓦时')<len(tables):raise ValueError('source_units_not_confirmed')
   for table in tables:
    if len(table)!=4 or any(len(r)!=3 for r in table):raise ValueError('unsupported_table_shape')
    hdr=[re.sub(r'\s+','',x or '') for x in table[0]];sub=[re.sub(r'\s+','',x or '') for x in table[1]]
    if hdr[0]!='月份' or hdr[2] or sub!=['','结算电量','结算均价']:raise ValueError('unsupported_native_header')
    rights=hdr[1];m=re.fullmatch(r'环境权益[（(](批发市场|零售市场)[）)]',rights)
    if not m or m.group(1) in observed:raise ValueError('unsupported_or_duplicate_market')
    market=m.group(1);observed.add(market);periods=set()
    for native in table[2:]:
     period=re.sub(r'\s+','',native[0] or '');quantity=(native[1] or '').strip();price=(native[2] or '').strip()
     if period not in expected or period in periods or not NUMBER.fullmatch(quantity) or not NUMBER.fullmatch(price):raise ValueError('unsupported_period_or_numeric_value')
     periods.add(period);rows.append({'year':year,'month':f'{month:02d}','marketNative':market,'rightsNative':rights,'periodNative':period,'quantityNative':quantity,'quantityUnit':'亿千瓦时','priceNative':price,'priceUnit':'元/兆瓦时','page':page_no,'sourceSha256':digest})
    if periods!=expected:raise ValueError('native_periods_incomplete')
 if observed!={'批发市场','零售市场'} or len(rows)!=4:raise ValueError('native_markets_incomplete')
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'green_settlement.csv';tmp=target.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
 tmp.replace(target);result={'status':'parsed_with_review','year':year,'month':f'{month:02d}','rows':4,'sourceSha256':digest,'limitations':['保持环境权益、批发和零售原文分类；当月与累计不相加。','原表适用范围与历史完整性需结合原件。']};(dest/'parsed.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');return result
