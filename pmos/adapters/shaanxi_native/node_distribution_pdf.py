"""Extract PDF-native branch/node sensitivity rows using source geometry.

Column text is compacted from native words; exact positioned fragments remain
available for review. Values are source sensitivity, not price or power.
"""
import bisect,collections,csv,datetime as dt,hashlib,json,re,subprocess,tempfile,xml.etree.ElementTree as ET
from pathlib import Path
FIELDS=['catalogueDate','branchCompact','nodeCompact','sensitivityNative','sensitivityUnitNative','sourcePage','anchorY','branchFragments','nodeFragments','sensitivityFragments','sourceSha256']
LABELS={'支路名称','节点号','灵敏度'}
def group_page(words,page_no):
 cols=[[],[],[]];headers=set()
 for w in words:
  text=w['text'];x=float(w['x']);y=round(float(w['y']),1)
  if text in LABELS:
   headers.add(text);continue
  cols[0 if x<245 else 1 if x<380 else 2].append((y,x,text))
 vals=collections.defaultdict(list)
 for y,x,text in cols[2]:vals[y].append((x,text))
 anchors=sorted(vals)
 if not anchors:raise ValueError(f'page_{page_no}_without_numeric_anchors')
 rows=[{'anchorY':y,'branch':[],'node':[],'sensitivity':sorted(vals[y])} for y in anchors]
 for row in rows:
  if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?',''.join(t for _,t in row['sensitivity'])):raise ValueError(f'page_{page_no}_invalid_sensitivity')
 for index,name in enumerate(('branch','node')):
  for y,x,text in cols[index]:
   pos=bisect.bisect_left(anchors,y);choices=[k for k in (pos-1,pos) if 0<=k<len(anchors)]
   if not choices:raise ValueError(f'page_{page_no}_missing_anchor')
   choices.sort(key=lambda k:abs(anchors[k]-y));nearest=choices[0]
   if abs(anchors[nearest]-y)>8 or len(choices)>1 and abs(anchors[choices[0]]-y)==abs(anchors[choices[1]]-y):raise ValueError(f'page_{page_no}_ambiguous_word_position')
   rows[nearest][name].append((y,x,text))
 for row in rows:
  if not row['branch'] or not row['node']:raise ValueError(f'page_{page_no}_missing_branch_or_node')
 return rows,headers

def parse_pdf(pdf,source_date,source_sha256,output_dir):
 pdf=Path(pdf);digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
 if digest!=source_sha256:raise ValueError('pdf_hash_mismatch')
 try:catalogue=dt.datetime.strptime(str(source_date),'%Y%m%d').date().isoformat()
 except ValueError as e:raise ValueError('invalid_catalogue_date') from e
 rows=[];pages=0;ns='{http://www.w3.org/1999/xhtml}'
 with tempfile.TemporaryDirectory(prefix='node-distribution-') as td:
  xml=Path(td)/'bbox.xml'
  proc=subprocess.run(['pdftotext','-bbox-layout',str(pdf),str(xml)],capture_output=True,text=True,timeout=120)
  if proc.returncode or proc.stderr:raise ValueError('source_pdf_text_extraction_failed: '+proc.stderr[:300])
  for _,page in ET.iterparse(xml,events=('end',)):
   if page.tag!=ns+'page':continue
   pages+=1
   if abs(float(page.attrib['width'])-595.28)>1 or abs(float(page.attrib['height'])-841.89)>1:raise ValueError('unsupported_page_size')
   words=[{'text':w.text or '', 'x':w.attrib['xMin'],'y':w.attrib['yMin']} for w in page.iter(ns+'word')]
   data,headers=group_page(words,pages)
   if pages==1 and headers!=LABELS:raise ValueError('source_header_missing')
   for r in data:
    branch=sorted(r['branch']);node=sorted(r['node']);value=r['sensitivity'];frags=lambda x:json.dumps([{'y':y,'x':xpos,'text':t} for y,xpos,t in x],ensure_ascii=False,separators=(',',':'))
    rows.append({'catalogueDate':catalogue,'branchCompact':''.join(t for _,_,t in branch),'nodeCompact':''.join(t for _,_,t in node),'sensitivityNative':''.join(t for _,t in value),'sensitivityUnitNative':'原文未注明','sourcePage':pages,'anchorY':r['anchorY'],'branchFragments':frags(branch),'nodeFragments':frags(node),'sensitivityFragments':json.dumps([{'x':x,'text':t} for x,t in value],ensure_ascii=False,separators=(',',':')),'sourceSha256':digest})
   page.clear()
 if not pages or not rows:raise ValueError('source_pdf_empty')
 dest=Path(output_dir);dest.mkdir(parents=True,exist_ok=True);target=dest/'node_distribution_values.csv';tmp=target.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows);f.flush()
 tmp.replace(target);parsed=dest/'node_distribution_parsed.json';tmp=parsed.with_suffix('.json.tmp');out={'status':'parsed','catalogueDate':catalogue,'sourceSha256':digest,'pages':pages,'rows':len(rows),'csvSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'limitations':['名称以PDF文本词块和坐标无空白拼接，原词块位置随每行保存。','为支路、节点和灵敏度原值，不能当作电价、电量。','目录日期不是已证实的实际生效时间。']};tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');tmp.replace(parsed)
 return {**out,'path':str(parsed)}
