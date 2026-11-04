"""Export source-provided non-time fields as a compact row-keyed sidecar."""
import csv,hashlib,json,os,uuid
from pathlib import Path
from .spot_tables import time_label

def write_native_dimensions(result,dest):
 rows=result.get('rows');columns=result.get('native_columns') or []
 if not isinstance(rows,list):return None
 time_props={c['prop'] for c in columns if c.get('prop') and time_label(c)}
 if not time_props:return None
 fields=list(dict.fromkeys([c['prop'] for c in columns if c.get('prop') and c['prop'] not in time_props]+[k for row in rows for k in row if k not in time_props]))
 if 'derived_row_ordinal' in fields:raise ValueError('native_dimension_ordinal_name_collision')
 dest=Path(dest);target=dest/'native_dimensions.csv';tmp=dest/('.native_dimensions.'+uuid.uuid4().hex+'.tmp')
 try:
  with tmp.open('x',encoding='utf-8-sig',newline='') as f:
   w=csv.DictWriter(f,fieldnames=['derived_row_ordinal']+fields);w.writeheader()
   for i,row in enumerate(rows,1):
    w.writerow({'derived_row_ordinal':i,**{k:json.dumps(v,ensure_ascii=False,separators=(',',':')) if isinstance(v,(dict,list)) else v for k,v in row.items() if k in fields}})
   f.flush();os.fsync(f.fileno())
  if target.exists():
   if target.read_bytes()!=tmp.read_bytes():raise ValueError('existing_native_dimensions_changed')
   tmp.unlink()
  else:os.replace(tmp,target)
 finally:
  if tmp.exists():tmp.unlink()
 return {'path':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'rows':len(rows),'fields':fields,'joinKey':'derived_row_ordinal','sourceResponseSha256':result.get('response_sha256') or result.get('sha256'),'nullAndMissingStates':'native_field_states.csv'}
