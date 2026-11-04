"""Validate native disclosure listings and retain site-decoded fields separately."""
import hashlib
import json
from .spot_tables import ValidationError


def native_listing(body, dataset, turn, params, http_status):
    if http_status != 200 or type(body.get('status')) is not int or body['status'] != 0 or body.get('message') != 'Success':
        raise ValidationError('disclosure_listing_business_failure')
    data = body.get('data')
    null_zero_list = (isinstance(data, dict) and 'list' in data and data['list'] is None
                      and type(data.get('total')) is int and data['total'] == 0)
    if not isinstance(data, dict) or not (isinstance(data.get('list'), list) or null_zero_list):
        raise ValidationError('disclosure_listing_shape_changed')
    # This native component explicitly displays [] when total is zero. Keep
    # data.list=null in the archived bytes and record the derived empty view.
    records = [] if null_zero_list else data['list']
    page, size, total = data.get('pageNum'), data.get('pageSize'), data.get('total')
    if any(type(x) is not int for x in (page, size, total)) or total < 0:
        raise ValidationError('disclosure_listing_pagination_missing')
    if page != params['page'] or size != params['size'] or len(records) != min(size, max(0, total-(page-1)*size)):
        raise ValidationError('disclosure_listing_page_incomplete')
    source = dataset['nativeListing']
    post = turn.get('postRead', {})
    components = post.get('components', [])
    if len(components) != 1:
        raise ValidationError('one_completed_native_listing_contract_required')
    c = components[0]
    state = c.get('state', c)
    actual_query = {'data': state['queryParam']['data'],
                    'pageInfo': {k: state['queryParam']['pageInfo'][k] for k in ('pageNum', 'pageSize')}}
    expected_query = {'data': source['filters'], 'pageInfo': {'pageNum': page, 'pageSize': size}}
    if (state['infoCode'] != source['infoCode'] or (source.get('requiresViewId', True) and state.get('viewId') != source['guid'])
            or state['marketInfoTwo'] != source['marketId'] or actual_query != expected_query):
        raise ValidationError('disclosure_listing_native_context_changed')
    invoked = turn.get('invokeResult', {})
    if invoked.get('nativeQuery') != expected_query and invoked.get('nativeMenu') != source['guid']:
        raise ValidationError('disclosure_listing_query_binding_missing')
    if post.get('columns') != [dataset['validation']['native_columns']]:
        raise ValidationError('disclosure_listing_native_columns_changed')
    visible = state['resultValue']
    if state['total'] != total or len(visible) != len(records):
        raise ValidationError('disclosure_listing_native_result_incomplete')
    keys, derived = [], []
    for raw, shown in zip(records, visible):
        guid = raw.get('guid')
        if not isinstance(guid, str) or not guid or guid != shown.get('guid') or guid in keys:
            raise ValidationError('disclosure_listing_identity_missing_or_duplicated')
        keys.append(guid)
        for k, value in raw.items():
            if k not in ('attachment', 'content') and shown.get(k) != value:
                raise ValidationError('disclosure_listing_native_field_changed:'+k)
        attachment = raw.get('attachment')
        parsed = json.loads(attachment) if attachment else (attachment if source.get('emptyAttachmentMode') == 'preserve' else [])
        if parsed != shown.get('attachment'):
            raise ValidationError('disclosure_listing_attachment_decode_mismatch')
        if raw.get('content') != shown.get('content') and not isinstance(shown.get('content'), str):
            raise ValidationError('disclosure_listing_site_decode_not_text')
        derived.append({'native_guid': guid, 'site_content_value': shown.get('content'),
                        'content_representation': 'site_component_transformed_value' if raw.get('content') != shown.get('content') else 'unchanged_from_original_response',
                        'site_decoded_attachment': shown.get('attachment'),
                        'content_differs_from_original': raw.get('content') != shown.get('content'),
                        'provenance': 'original_site_component_after_this_completed_native_query',
                        'ui_only_fields': {k: v for k, v in shown.items() if k not in raw}})
    native_pages = data.get('pages')
    derived_pages = (total + size - 1) // size if total else 0
    anomalies = []
    if native_pages is not None and native_pages != derived_pages:
        anomalies.append({'field': 'pages', 'native_value': native_pages,
                          'derived_from_total_and_page_size': derived_pages,
                          'action': 'native_value_preserved_not_used_as_page_limit'})
    return {'outcome': 'saved' if keys else 'empty', 'kind': 'native_disclosure_listing',
            'rows': records, 'saved_rows': len(keys), 'server_total_rows': total,
            'source_list_is_null': null_zero_list,
            'empty_rows_representation': 'derived_from_native_zero_total_and_site_empty_result' if null_zero_list else None,
            'keys': [hashlib.sha256(k.encode()).hexdigest() for k in keys],
            'native_columns': dataset['validation']['native_columns'],
            'source_identity_fields': ['guid'], 'source_date_field': 'operateDate',
            'date_provenance': 'native_operateDate_preserved_without_conversion',
            'native_query': actual_query, 'native_pagination_fields': {k: data[k] for k in ('pageNum', 'pageSize', 'pages', 'total', 'size') if k in data},
            'source_pagination_anomalies': anomalies, 'derived_page_count': derived_pages,
            'native_value_dictionaries': state.get('nativeDictionaries', {}), 'snapshot_date': params['snapshotDate'],
            'snapshot_scope': 'native_listing_with_original_filters_no_historical_date_changes',
            'numeric_points': 0, 'points_per_row': 0, 'derived_field_schema_version': 2, 'site_decoded_rows': derived,
            'attachments_downloaded_by_this_listing': 0, 'warnings': turn.get('warnings', [])}
