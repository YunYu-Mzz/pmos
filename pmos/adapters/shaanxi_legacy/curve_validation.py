"""Validate an original native curve without inventing dates or source IDs."""
import hashlib
import math
from .spot_tables import ValidationError


def native_curve(body, dataset, turn, params, http_status):
    # The native page explicitly clears its curve when this precise successful
    # response says the requested date has no data. Other messages remain
    # errors, including a no-data message paired with unexpected data.
    explicit_empty = (body.get('message') == '查询无数据！' and 'data' in body and body['data'] is None)
    if http_status != 200 or type(body.get('status')) is not int or body.get('status') != 0 or (body.get('message') != 'Success' and not explicit_empty):
        raise ValidationError('native_curve_business_error')
    source = dataset['nativeCurve']
    invocation = turn.get('invokeResult') or {}
    actual = invocation.get('nativeQuery')
    if actual is None:
        # Admission reuses the first completed native menu operation. Its passive
        # contract is retained separately; it never issues the query a second time.
        if not params.get('admissionContract') or invocation.get('nativeMenu') != source['guid']:
            raise ValidationError('native_curve_query_evidence_missing')
        actual = params['admissionContract']
    wanted = {'dateTime': params['date'], 'infoCode': source['infoCode'], 'marketId': source['marketId']}
    if actual != wanted:
        raise ValidationError('native_curve_query_context_mismatch')
    cs = dataset['validation']['native_columns']
    props = [c['prop'] for c in cs]
    if len(cs) != 96 or len(set(props)) != 96:
        raise ValidationError('native_curve_period_contract_missing')
    data = body.get('data')
    if data is None or data == {}:
        rows = []
    elif not isinstance(data, dict) or list(data) != props:
        raise ValidationError('native_curve_period_order_changed')
    else:
        for v in data.values():
            if v is None or v == '':
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValidationError('native_curve_unreviewed_value')
        rows = [data]
    numeric = sum(isinstance(v, (int, float)) and not isinstance(v, bool)
                  for row in rows for v in row.values())
    return {'outcome': 'saved' if rows else 'empty', 'kind': 'native_curve',
            'empty_reason': 'native_explicit_no_data_message' if explicit_empty else None,
            'rows': rows, 'saved_rows': len(rows), 'server_total_rows': len(rows),
            'numeric_points': numeric, 'native_columns': cs, 'points_per_row': 96,
            'source_date_field': None, 'source_identity_fields': [],
            'date_provenance': 'native_query_parameter_only_no_date_field_in_response',
            'native_query': wanted, 'query_identity': wanted,
            'keys': [hashlib.sha256((source['infoCode'] + ':' + params['date']).encode()).hexdigest()] if rows else [],
            'warnings': turn.get('warnings', [])}
