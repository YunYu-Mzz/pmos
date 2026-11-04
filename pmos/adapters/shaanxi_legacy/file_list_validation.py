"""Validate native intervention-log metadata; listing is distinct from PDF retrieval."""
import hashlib
import json
from datetime import datetime
from .spot_tables import ValidationError


def file_listing(raw, dataset, turn, params, response):
    body = json.loads(raw)
    if response['status'] != 200 or body.get('status') != 0 or body.get('message') != 'Success':
        raise ValidationError('native_file_listing_business_failure')
    data = body.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('list'), list):
        raise ValidationError('native_file_listing_shape_changed')
    page, size, total = (data.get(k) for k in ('pageNum', 'pageSize', 'total'))
    if any(type(x) is not int for x in (page, size, total)) or total < 0 or page != params['page'] or size != params['size']:
        raise ValidationError('native_file_listing_pagination_changed')
    if len(data['list']) != min(size, max(0, total-(page-1)*size)):
        raise ValidationError('native_file_listing_page_incomplete')
    source = dataset['nativeFileList']
    cs = turn.get('postRead', {}).get('components', [])
    if not turn.get('postRead', {}).get('matched') or len(cs) != 1:
        raise ValidationError('one_completed_native_file_component_required')
    c = cs[0]
    state = c['state']
    query = {'fileName': c['routeName'], 'createdTime': state['offlineInformation']['createdTime'],
             'pageNum': state['offlineInformation']['pageNum'], 'pageSize': state['offlineInformation']['pageSize']}
    expected = {'fileName': source['routeName'], 'createdTime': params.get('createdTime', source['createdTime']),
                'pageNum': page, 'pageSize': size}
    if query != expected or state['resultTotal'] != total or len(state['tableData']) != len(data['list']):
        raise ValidationError('native_file_listing_query_or_result_changed')
    known = dataset['sampleContract']['components'][0]
    if any(c['methods'].get(k) != known['methods'].get(k) for k in ('onSearch', 'assemableDate')):
        raise ValidationError('native_file_listing_method_changed')
    if turn['postRead']['columns'] != dataset['sampleContract']['columns']:
        raise ValidationError('native_file_listing_columns_changed')
    invoked = turn.get('invokeResult', {})
    if invoked.get('nativeQuery') != expected and not (params.get('defaultNavigationScope') and invoked.get('nativeMenu') == dataset['nativeAlias']['guid']):
        raise ValidationError('native_file_listing_invocation_missing')
    ids, derivatives = [], []
    for record, visible in zip(data['list'], state['tableData']):
        identity = record.get('id')
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValidationError('native_file_listing_id_missing_or_duplicated')
        if any(not isinstance(record.get(k), str) or not record[k] for k in ('fileName', 'filePath', 'createdTime')):
            raise ValidationError('native_file_listing_source_fields_missing')
        try:
            stamp = datetime.fromisoformat(record['createdTime'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('Missing source timezone')
        except ValueError:
            raise ValidationError('native_file_listing_created_time_changed')
        if visible != {**record, 'details': '详情'}:
            raise ValidationError('native_file_listing_site_transformation_changed')
        ids.append(identity)
        derivatives.append({'native_id': identity, 'site_details_label': visible['details'],
                            'provenance': 'native_assemableDate_after_this_completed_query'})
    return {'outcome': 'saved' if ids else 'empty', 'kind': 'native_file_listing', 'rows': data['list'],
            'saved_rows': len(ids), 'server_total_rows': total, 'all_rows_saved': len(ids) == total,
            'keys': [hashlib.sha256(x.encode()).hexdigest() for x in ids], 'numeric_points': 0, 'points_per_row': 0,
            'native_columns': dataset['validation']['native_columns'], 'source_identity_fields': ['id'],
            'ui_only_columns': ['details'],
            'source_date_field': 'createdTime', 'source_date_encoding': 'native_timezone_aware_instant_preserved',
            'file_storage_id_field': 'filePath', 'native_query': query, 'snapshot_date': params.get('snapshotDate'),
            'files_listed': len(ids), 'pdf_files_downloaded': 0, 'site_decoded_rows': derivatives,
            'endpoint': response['path'], 'responseFile': response['responseFile'], 'sha256': response['sha256'],
            'receipt_directory': turn.get('directory')}
