"""Validate observed Shaanxi attachment directories without website requests."""
import hashlib,json,re
from datetime import datetime
OBSERVED_FILE_PREFIX_ALIASES={'JDFPYZQDFF':('JDFPYZJQDFF_','JDFPYZJQDFFZ_'),
                            'UnsInfoDis':('现货日结算总体情况及分类构成情况_',)}
# Original receipt 0b71d6b9...: this exact undated PDF is in the native directory.
OBSERVED_UNDATED_PDF_IDS={'XHSCDSFJYBG':{'XHSCDSFJYBG.pdf':'n7409b43e5f7447e8a3b766c354ce3006'}}

def validate_directory(raw, *, http_status, file_name):
    if http_status != 200: raise ValueError('http_failure')
    body=json.loads(raw)
    if not isinstance(body,dict) or type(body.get('code')) is not int or body['code']!=200 or body.get('errorCode') is not None: raise ValueError('business_failure')
    rows=body.get('data')
    if not isinstance(rows,list): raise ValueError('unexpected_data_shape')
    identities=set();dates=set();files=[];anomalies=[]
    for row in rows:
        if not isinstance(row,dict) or row.get('name')!=file_name: raise ValueError('wrong_directory')
        try:
            if not isinstance(row.get('date'),str) or not re.fullmatch(r'\d{8}',row['date']): raise ValueError('invalid_source_date')
            datetime.strptime(row['date'],'%Y%m%d');dates.add(row['date'])
        except ValueError: anomalies.append({'fileId':row.get('fileId'),'date':row.get('date'),'reason':'invalid_source_date_preserved'})
        ident=row.get('fileId')
        if not isinstance(ident,str) or not ident or ident in identities: raise ValueError('duplicate_or_missing_file_id')
        filename=row.get('fileName')
        if not isinstance(filename,str):raise ValueError('wrong_file_name')
        if not filename.startswith(file_name+'_'):
            if OBSERVED_UNDATED_PDF_IDS.get(file_name,{}).get(filename)==ident:
                anomalies.append({'fileId':ident,'fileName':filename,'reason':'observed_undated_filename_preserved','nativeDirectory':file_name})
            else:
                if not any(filename.startswith(p) for p in OBSERVED_FILE_PREFIX_ALIASES.get(file_name,())):raise ValueError('wrong_file_name')
                anomalies.append({'fileId':ident,'fileName':filename,'reason':'observed_filename_alias_preserved','nativeDirectory':file_name})
        if not isinstance(row.get('missionName'),str) or not row['missionName']: raise ValueError('missing_title')
        identities.add(ident);files.append({k:row[k] for k in ('fileId','date','fileName','missionName')})
    return {'outcome':'saved' if rows else 'empty','scope':'attachment_directory_only','sha256':hashlib.sha256(raw).hexdigest(),'rows':len(rows),'unique_file_ids':len(identities),'unique_source_dates':len(dates),'first_source_date':min(dates) if dates else None,'last_source_date':max(dates) if dates else None,'downloaded_attachments':0,'source_anomalies':anomalies,'files':files,'records':rows,'source_identity_fields':['fileId'],'source_date_field':'date','native_columns':[]}
