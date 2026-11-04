"""Native unpaged weighted-price array; source values and IDs stay unchanged."""
import json
import hashlib
from .spot_tables import native_table, ValidationError


def weighted_price(raw, dataset, turn, params, response):
    contract = turn.get('postRead', {})
    components = [c for c in contract.get('components', [])
                  if c.get('state', {}).get('form', {}).get('tableName') == dataset['nativeWeighted']['tableName']]
    if not contract.get('matched') or len(components) != 1:
        raise ValidationError('one_completed_weighted_price_component_required')
    component = components[0]
    form = component['state']['form']
    expected = {**dataset['nativeWeighted']['form'], 'date': params.get('date', '')}
    if form != expected or component['methods']['search'] != dataset['sampleContract']['components'][0]['methods']['search']:
        raise ValidationError('weighted_price_native_query_changed')
    invoked = turn.get('invokeResult', {})
    if invoked.get('nativeQuery') != expected and invoked.get('nativeMenu') != dataset['nativeAlias']['guid']:
        raise ValidationError('weighted_price_query_binding_missing')
    if contract['columns'] != dataset['sampleContract']['columns']:
        raise ValidationError('weighted_price_native_columns_changed')
    result = native_table(raw, dataset['validation']['native_columns'], http_status=response['status'],
                          expected_date=params.get('date') or None, require_96=True,
                          identity_fields=['date', 'area_id', 'data_type'], date_field='date',
                          period_minutes=15, shape='array')
    shown = component['state']['tableData']
    if len(shown) != result['saved_rows']:
        raise ValidationError('weighted_price_visible_row_count_changed')
    derived = []
    for original, visible in zip(result['rows'], shown):
        expected_row = {**original, 'data_type': {'1': '日前', '0': '实时'}.get(original['data_type'], original['data_type'])}
        if visible != expected_row:
            raise ValidationError('weighted_price_native_display_transformation_changed')
        derived.append({'source_identity': {k: original[k] for k in result['source_identity_fields']},
                        'site_displayed_data_type': visible['data_type'],
                        'provenance': 'native_component_after_completed_query'})
    result.update(keys=[hashlib.sha256(json.dumps([r[k] for k in result['source_identity_fields']],
                                                ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
                        for r in result['rows']],
                  native_query=form, endpoint=response['path'], responseFile=response['responseFile'],
                  sha256=response['sha256'], receipt_directory=turn.get('directory'),
                  snapshot_date=params.get('snapshotDate'), site_decoded_rows=derived,
                  native_source_id_is_not_unique=True, source_id_preserved=True,
                  server_pagination_exposed=False)
    return result
