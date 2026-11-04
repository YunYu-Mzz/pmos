"""Strict offline adapters for observed Shaanxi native tabular responses."""
import hashlib,json,re
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation

class ValidationError(ValueError): pass

def time_label(column):
    label=re.sub(r'\s+','',column.get('label',''))
    if re.match(r'^\d{2}:\d{2}',label):return label[:5]
    prop=column.get('prop','')
    if label==prop and re.fullmatch(r'p\d{4}',prop):return prop[1:3]+':'+prop[3:5]
    return None

def successful(raw,http_status,protocol='code200'):
    if http_status!=200: raise ValidationError('http_failure')
    obj=json.loads(raw)
    if not isinstance(obj,dict):raise ValidationError('business_failure')
    if protocol=='status0':
        if type(obj.get('status')) is not int or obj['status']!=0 or obj.get('success') is False or obj.get('message') not in ('Success','操作成功','查询成功'):raise ValidationError('business_failure')
    elif protocol=='code200':
        if type(obj.get('code')) is not int or obj['code']!=200 or obj.get('errorCode') is not None:raise ValidationError('business_failure')
    else:raise ValidationError('unreviewed_envelope_protocol')
    return obj

def columns(raw,http_status=200):
    cs=successful(raw,http_status).get('data')
    if not isinstance(cs,list) or not cs: raise ValidationError('missing_native_columns')
    props=[c.get('prop') for c in cs]
    if any(not isinstance(p,str) or not p for p in props) or len(set(props))!=len(props):raise ValidationError('ambiguous_native_columns')
    return cs

def native_table(raw,cs,*,http_status=200,expected_date=None,require_96=False,identity_fields=None,date_field='date',protocol='code200',date_encoding='iso_date',period_minutes=None,shape='paged_table'):
    obj=successful(raw,http_status,protocol);data=obj.get('data')
    if shape=='array':
        if not isinstance(data,list):raise ValidationError('unexpected_native_array_shape')
        rows=data;total=None
    elif shape=='paged_table':
        if not isinstance(data,dict) or type(data.get('total')) is not int or data['total']<0 or not isinstance(data.get('list'),list):raise ValidationError('unexpected_native_table_shape')
        rows=data['list'];total=data['total']
        if len(rows)>total:raise ValidationError('row_count_exceeds_total')
    else:raise ValidationError('unreviewed_native_table_shape')
    keys=[c['prop'] for c in cs];times=[c for c in cs if time_label(c)]
    if date_encoding not in ('iso_date','iso8601_instant_asia_shanghai'):raise ValidationError('unreviewed_source_date_encoding')
    if period_minutes is not None:
        if period_minutes not in (15,60) or len(times)!=1440//period_minutes:raise ValidationError('incomplete_native_period_mapping')
        for i,c in enumerate(times,1):
            h,m=divmod(i*period_minutes,60)
            if time_label(c)!=f'{h:02d}:{m:02d}' and not (i*period_minutes==1440 and time_label(c)=='00:00'):raise ValidationError('native_time_label_mismatch')
    if require_96:
        if len(times)!=96 or len({c['prop'] for c in times})!=96:raise ValidationError('incomplete_or_reordered_96_point_mapping')
        for i,c in enumerate(times,1):
            label=time_label(c);h,m=divmod(i*15,60)
            if not label.startswith(f'{h:02d}:{m:02d}') and not (i==96 and label.startswith('00:00')):raise ValidationError('native_time_label_mismatch')
    identities=set();points=0;missing=[];dates=set()
    for ri,row in enumerate(rows):
        if not isinstance(row,dict):raise ValidationError('invalid_row')
        identity=tuple(row.get(k) for k in identity_fields) if identity_fields else row.get('id',row.get('ID'))
        if identity_fields:
            if any(not isinstance(v,str) or not v for v in identity):raise ValidationError('missing_natural_key_field')
        elif isinstance(identity,bool) or not isinstance(identity,(str,int)) or identity=='':raise ValidationError('missing_or_duplicate_source_identity')
        if identity in identities:raise ValidationError('missing_or_duplicate_source_identity')
        identities.add(identity)
        if date_field in keys or date_field in row:
            date=row.get(date_field)
            try:
                if date_encoding=='iso_date':datetime.strptime(date,'%Y-%m-%d')
                else:
                    if not isinstance(date,str):raise ValueError('Source instant must be text')
                    instant=datetime.fromisoformat(date.replace('Z','+00:00'))
                    if instant.tzinfo is None:raise ValueError('Source instant lacks timezone')
                    date=instant.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
            except (ValueError,TypeError):raise ValidationError('invalid_source_date')
            if expected_date is not None and date!=expected_date:raise ValidationError('query_date_mismatch')
            dates.add(date)
        for c in times:
            prop=c['prop'];value=row.get(prop)
            if prop not in row:raise ValidationError('missing_source_field:'+prop)
            if value is None or value=='':missing.append({'row':ri,'prop':prop});continue
            try:
                if isinstance(value,bool) or not Decimal(str(value)).is_finite():raise InvalidOperation
            except (InvalidOperation,ValueError):raise ValidationError('non_numeric_time_series_value')
            points+=1
    return {'outcome':'empty' if not rows else 'saved','response_sha256':hashlib.sha256(raw).hexdigest(),'saved_rows':len(rows),'server_total_rows':total,'all_rows_saved':shape=='array' or len(rows)==total,'completion_scope':'entire_native_array_response_no_server_pagination' if shape=='array' else 'native_paged_result','unique_dates':len(dates),'first_date':min(dates) if dates else None,'last_date':max(dates) if dates else None,'points_per_row':len(times),'numeric_points':points,'missing_values_preserved':missing,'native_columns':cs,'source_identity_fields':list(identity_fields) if identity_fields else None,'source_date_field':date_field,'source_date_encoding':date_encoding,'date_comparison_timezone':'Asia/Shanghai' if date_encoding=='iso8601_instant_asia_shanghai' else None,'rows':rows}
