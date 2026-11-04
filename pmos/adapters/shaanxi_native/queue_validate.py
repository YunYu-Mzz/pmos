"""Offline validation/export for the live queue. Never opens a browser."""
import csv
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

from .catalogue import validate_directory
from .spot_tables import columns, native_table, successful, time_label, ValidationError


def atomic(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def validate_weekly_native_bounds(turn, params, rows):
    """Use the actual native date bounds; a null end date is not seven days."""
    native = (turn.get('invokeResult') or {}).get('form')
    if native is not None and (native.get('startDate') != params.get('startDate')
                               or native.get('endDate') != params.get('endDate')
                               or native.get('pageNum') != params.get('page')
                               or native.get('pageSize') != params.get('size')):
        raise ValidationError('weekly_native_query_mismatch')
    if (turn.get('identity') or {}).get('action') == 'query' and native is None:
        raise ValidationError('weekly_native_query_missing')
    begin = date.fromisoformat(params['startDate'])
    end = date.fromisoformat(params['endDate']) if params.get('endDate') else None
    if end is not None and end < begin:
        raise ValidationError('weekly_invalid_native_date_bounds')
    for row in rows:
        actual = date.fromisoformat(row['datetime'])
        if actual < begin or (end is not None and actual > end):
            raise ValidationError('weekly_source_outside_native_query_bounds')
    return {'startDate': begin.isoformat(), 'endDate': end.isoformat() if end else None,
            'endBound': 'native_explicit' if end else 'not_supplied_by_native_query'}


def validate(request):
    dataset, turn, params = request['dataset'], request['turn'], request['params']
    failures = turn.get('failures', [])
    accepted_warning = (turn.get('warningPolicy', {}).get('continueWhenSdkAbortedAndBusinessCompleted') is True
                        and bool(turn.get('warnings')) and 'blockingFailures' in turn
                        and not turn['blockingFailures'] and all(
                            x.get('path') == '/px-common-authcenter/sso/token'
                            and (x.get('error') == 'net::ERR_ABORTED' or (
                                x.get('error') == 'net::ERR_NETWORK_CHANGED'
                                and turn.get('warningPolicy', {}).get('continueWhenSdkNetworkChangedAndBusinessCompleted') is True)) for x in failures))
    if turn.get('phase') == 'held' or turn.get('pending') or (failures and not accepted_warning):
        raise ValidationError('native_turn_not_complete')
    matches = [x for x in turn['responses'] if x.get('kind') == 'data'
               and x['path'] == request.get('endpoint', dataset['endpoint']) and x.get('responseFile')]
    if len(matches) != 1:
        raise ValidationError('exactly_one_primary_response_required')
    response = matches[0]
    raw = Path(response['responseFile']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != response['sha256']:
        raise ValidationError('archive_hash_mismatch')
    for failure in failures:
        if failure.get('error') == 'net::ERR_NETWORK_CHANGED' and not any(
                w.get('code') == 'native_sdk_network_changed_business_completed'
                and w.get('requestId') == failure.get('requestId')
                and w.get('error') == failure['error']
                and any(e.get('sha256') == response['sha256'] and e.get('path') == response['path']
                        for e in w.get('businessEvidence', [])) for w in turn.get('warnings', [])):
            raise ValidationError('network_warning_requires_exact_business_evidence')
    if request.get('download'):
        if response['status'] != 200 or not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-2048:]:
            raise ValidationError('unreviewed_attachment_format_or_business_error')
        return {'outcome': 'saved', 'kind': 'pdf_original', 'sha256': response['sha256'],
                'bytes': len(raw), 'responseFile': response['responseFile'], 'source': params,
                'formatCheck': 'PDF_signature_and_EOF_only', 'keys': []}
    if dataset['family'] == 'directory':
        result = validate_directory(raw, http_status=response['status'], file_name=dataset['fileName'])
        return {**result, 'keys': [], 'responseFile': response['responseFile'],
                'native_query': {'fileName': dataset['fileName'], 'date': ''},
                'snapshot_date': params.get('snapshotDate'), 'receipt_directory': turn.get('directory')}
    if dataset['family'] == 'disclosure_list':
        from .disclosure_list_validation import native_listing
        result = native_listing(json.loads(raw), dataset, turn, params, response['status'])
        result.update(endpoint=response['path'], responseFile=response['responseFile'],
                      sha256=response['sha256'], receipt_directory=turn.get('directory'))
        return result
    if dataset['family'] == 'curve':
        from .curve_validation import native_curve
        result = native_curve(json.loads(raw), dataset, turn, params, response['status'])
        result.update(endpoint=response['path'], responseFile=response['responseFile'],
                      sha256=response['sha256'], receipt_directory=turn.get('directory'))
        return result
    if dataset['family'] == 'weighted_price':
        from .weighted_price_validation import weighted_price
        return weighted_price(raw, dataset, turn, params, response)
    if dataset['family'] == 'file_list':
        from .file_list_validation import file_listing
        return file_listing(raw, dataset, turn, params, response)
    rule = dataset.get('validation', {})
    if request.get('admission'):
        col_responses = [x for x in turn['responses'] if x.get('path') == '/sn-trade-out/InformationController/selectCol' and x.get('responseFile')]
        if len(col_responses) != 1:
            raise ValidationError('one_original_native_column_response_required')
        col_response = col_responses[0]
        col_raw = Path(col_response['responseFile']).read_bytes()
        if hashlib.sha256(col_raw).hexdigest() != col_response['sha256']:
            raise ValidationError('native_column_archive_hash_mismatch')
        cs = columns(col_raw, col_response['status'])
        rule = {'native_columns': cs, 'source_identity_fields': None, 'source_date_field': 'date',
                'points_per_row': sum(bool(time_label(c)) for c in cs)}
    # An empty admission has no evidence for nonempty field mapping. Keep the
    # original response for offline review instead of promoting or exporting
    # an unverified schema when a daily historical query first returns rows.
    if dataset.get('family') == 'preview' and dataset.get('sampleOnly') and not rule.get('native_columns'):
        envelope = successful(raw, response['status'], 'status0')
        data = envelope.get('data')
        if isinstance(data, dict) and isinstance(data.get('list'), list) and data['list']:
            raise ValidationError('nonempty_empty_sample_requires_native_field_review')
    result = native_table(raw, rule['native_columns'], http_status=response['status'],
                          expected_date=params.get('date') if dataset['family'] != 'weekly' else None,
                          require_96=rule['points_per_row'] == 96,
                          identity_fields=rule['source_identity_fields'],
                          date_field=rule['source_date_field'] or 'date',
                          date_encoding=rule.get('source_date_encoding', 'iso_date'),
                          period_minutes=rule.get('period_minutes'),
                          protocol='status0' if dataset['family'] == 'preview' else 'code200')
    rows = result['rows']
    result['native_query'] = params
    result['endpoint'] = response['path']
    result['receipt_directory'] = turn.get('directory')
    result['warnings'] = turn.get('warnings', [])
    value_type = params.get('nativeFilters', {}).get('valueType')
    if value_type and any(r.get('valueType') != value_type for r in rows):
        raise ValidationError('native_value_type_filter_mismatch')
    if params.get('date') and any((rule['source_date_field'] or 'date') not in r for r in rows):
        raise ValidationError('cannot_verify_query_date_from_source_rows')
    if dataset['family'] == 'weekly':
        result['weekly_native_query_bounds'] = validate_weekly_native_bounds(turn, params, rows)
    size, page = params['size'], params['page']
    total = result['server_total_rows']
    expected_count = min(size, max(0, total - (page - 1) * size))
    # A navigation response can finish before the page widget catches up.
    # It is only a preliminary envelope check, never a completed page group.
    # The actual dated query still uses the strict exact-page check below.
    navigation = dataset['family'] == 'grid' and params.get('defaultNavigationScope') is True
    if (navigation and len(rows) > size) or (not navigation and len(rows) != expected_count):
        raise ValidationError('incomplete_or_out_of_range_native_page')
    if navigation:
        result['completion_scope'] = 'navigation_response_pagination_not_verified'
    fields = rule['source_identity_fields']
    result['keys'] = [hashlib.sha256(json.dumps([r.get(k) for k in fields] if fields else
                                              r.get('id', r.get('ID')), ensure_ascii=False,
                                              sort_keys=True).encode()).hexdigest() for r in rows]
    return result


def export(result, dest, dataset=None):
    if result.get('kind') == 'pdf_original':
        raw = Path(result['responseFile']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != result['sha256']:
            raise ValidationError('pdf_artifact_archive_hash_mismatch')
        artifact = dest / 'original.pdf'
        if artifact.exists():
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != result['sha256']:
                raise ValidationError('existing_pdf_artifact_changed')
        else:
            with artifact.open('xb') as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
        result.update(artifactFile=str(artifact), artifactSha256=result['sha256'],
                      artifactFilenameIsDerived=True)
        if (dataset or {}).get('id') == 'disclosure:388bd8af-e533-404e-bb95-16304fde2378':
            # PDF parsing is downstream of archival: an unsupported format
            # must not discard an already received original or trigger a retry.
            try:
                from .daily_settlement_pdf import parse_pdf
                result['settlementParse'] = parse_pdf(artifact, result['source']['date'],
                                                       result['sha256'], dest / 'settlement')
            except Exception as exc:
                result['settlementParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                             'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'settlement_parse_failure.json', result['settlementParse'])
        if (dataset or {}).get('id') == 'disclosure:556f753a-e35a-444b-9986-8382f249856a':
            try:
                from .unit_commercial_operation_pdf import parse_pdf as parse_unit_operation_pdf
                result['unitCommercialOperationParse'] = parse_unit_operation_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'unit_commercial_operation')
            except Exception as exc:
                result['unitCommercialOperationParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                          'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'unit_commercial_operation_parse_failure.json', result['unitCommercialOperationParse'])
        if (dataset or {}).get('id') == 'disclosure:5ac919a6-6ea1-41c9-94d8-be9993a71d95':
            try:
                from .monthly_trade_summary_pdf import parse_pdf as parse_monthly_trade_pdf
                result['monthlyTradeParse'] = parse_monthly_trade_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_trade')
            except Exception as exc:
                result['monthlyTradeParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                               'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_trade_parse_failure.json', result['monthlyTradeParse'])
        if (dataset or {}).get('id') == '1643900543023845376':
            try:
                from .monthly_load_forecast_pdf import parse_pdf as parse_monthly_load_pdf
                result['monthlyLoadForecastParse'] = parse_monthly_load_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_load_forecast')
            except Exception as exc:
                result['monthlyLoadForecastParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                      'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_load_forecast_parse_failure.json', result['monthlyLoadForecastParse'])
        if (dataset or {}).get('id') == '1643902312445513728':
            try:
                from .monthly_generator_maintenance_pdf import parse_pdf as parse_generator_maintenance_pdf
                result['monthlyGeneratorMaintenanceParse'] = parse_generator_maintenance_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_generator_maintenance')
            except Exception as exc:
                result['monthlyGeneratorMaintenanceParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                              'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_generator_maintenance_parse_failure.json', result['monthlyGeneratorMaintenanceParse'])
        if (dataset or {}).get('id') == '1643899086627278848':
            try:
                from .monthly_commissioning_generation_pdf import parse_pdf as parse_commissioning_generation_pdf
                result['monthlyCommissioningGenerationParse'] = parse_commissioning_generation_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_commissioning_generation')
            except Exception as exc:
                result['monthlyCommissioningGenerationParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                                  'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_commissioning_generation_parse_failure.json', result['monthlyCommissioningGenerationParse'])
        if (dataset or {}).get('id') == '1643901949487222784':
            try:
                from .monthly_transmission_section_pdf import parse_pdf as parse_transmission_section_pdf
                result['monthlyTransmissionSectionParse'] = parse_transmission_section_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_transmission_section')
            except Exception as exc:
                result['monthlyTransmissionSectionParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                              'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_transmission_section_parse_failure.json', result['monthlyTransmissionSectionParse'])
        if (dataset or {}).get('id') == '1643902185966276608':
            try:
                from .monthly_commissioning_plan_pdf import parse_pdf as parse_commissioning_plan_pdf
                result['monthlyCommissioningPlanParse'] = parse_commissioning_plan_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_commissioning_plan')
            except Exception as exc:
                result['monthlyCommissioningPlanParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                           'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_commissioning_plan_parse_failure.json', result['monthlyCommissioningPlanParse'])
        if (dataset or {}).get('id') == '1643902468003860480':
            try:
                from .node_distribution_pdf import parse_pdf as parse_node_distribution_pdf
                result['nodeDistributionParse'] = parse_node_distribution_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'node_distribution')
            except Exception as exc:
                result['nodeDistributionParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                   'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'node_distribution_parse_failure.json', result['nodeDistributionParse'])
        if (dataset or {}).get('id') == '1643899464840253440':
            try:
                from .annual_generation_maintenance_pdf import parse_pdf as parse_annual_generation_maintenance_pdf
                result['annualGenerationMaintenanceParse'] = parse_annual_generation_maintenance_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'annual_generation_maintenance')
            except Exception as exc:
                result['annualGenerationMaintenanceParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                               'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'annual_generation_maintenance_parse_failure.json', result['annualGenerationMaintenanceParse'])
        if (dataset or {}).get('id') == '1643901849096556544':
            try:
                from .annual_network_maintenance_pdf import parse_pdf as parse_annual_network_maintenance_pdf
                result['annualNetworkMaintenanceParse'] = parse_annual_network_maintenance_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'annual_network_maintenance')
            except Exception as exc:
                result['annualNetworkMaintenanceParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                            'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'annual_network_maintenance_parse_failure.json', result['annualNetworkMaintenanceParse'])
        if (dataset or {}).get('id') == '1643901361512910848':
            try:
                from .annual_section_capacity_pdf import parse_pdf as parse_annual_section_capacity_pdf
                result['annualSectionCapacityParse'] = parse_annual_section_capacity_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'annual_section_capacity')
            except Exception as exc:
                result['annualSectionCapacityParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                        'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'annual_section_capacity_parse_failure.json', result['annualSectionCapacityParse'])
        if (dataset or {}).get('id') == '1643901746738761728':
            try:
                from .annual_commissioning_plan_pdf import parse_pdf as parse_annual_commissioning_plan_pdf
                result['annualCommissioningPlanParse'] = parse_annual_commissioning_plan_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'annual_commissioning_plan')
            except Exception as exc:
                result['annualCommissioningPlanParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                          'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'annual_commissioning_plan_parse_failure.json', result['annualCommissioningPlanParse'])
        if (dataset or {}).get('id') == '1643901746738761728':
            try:
                from .annual_commissioning_grid_pdf import parse_pdf as parse_annual_commissioning_grid_pdf
                result['annualCommissioningGridParse'] = parse_annual_commissioning_grid_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'annual_commissioning_grid')
            except Exception as exc:
                result['annualCommissioningGridParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                          'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'annual_commissioning_grid_parse_failure.json', result['annualCommissioningGridParse'])
        if (dataset or {}).get('id') == '1643899280639004672':
            try:
                from .monthly_actual_load_pdf import parse_pdf as parse_monthly_actual_load_pdf
                result['monthlyActualLoadParse'] = parse_monthly_actual_load_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_actual_load')
            except Exception as exc:
                result['monthlyActualLoadParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                    'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_actual_load_parse_failure.json', result['monthlyActualLoadParse'])
        if (dataset or {}).get('id') == 'disclosure:4f1ae9fb-4997-4e3d-acdf-4564d01b2751':
            try:
                from .green_settlement_pdf import parse_pdf as parse_green_settlement_pdf
                result['greenSettlementParse'] = parse_green_settlement_pdf(
                    artifact, result['sha256'], dest / 'green_settlement')
            except Exception as exc:
                result['greenSettlementParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                   'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'green_settlement_parse_failure.json', result['greenSettlementParse'])
        if (dataset or {}).get('id') == 'disclosure:8c15a8c7-3888-4e14-b50b-9b141891e56b':
            try:
                from .monthly_wholesale_settlement_pdf import parse_pdf as parse_monthly_wholesale_settlement_pdf
                result['monthlyWholesaleSettlementParse'] = parse_monthly_wholesale_settlement_pdf(
                    artifact, result['sha256'], dest / 'monthly_wholesale_settlement')
            except Exception as exc:
                result['monthlyWholesaleSettlementParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                              'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_wholesale_settlement_parse_failure.json', result['monthlyWholesaleSettlementParse'])
        if (dataset or {}).get('id') == 'disclosure:8c15a8c7-3888-4e14-b50b-9b141891e56b':
            try:
                from .monthly_retail_purchase_price_pdf import parse_pdf as parse_monthly_retail_purchase_price_pdf
                result['monthlyRetailPurchasePriceParse'] = parse_monthly_retail_purchase_price_pdf(
                    artifact, result['sha256'], dest / 'monthly_retail_purchase_price')
            except Exception as exc:
                result['monthlyRetailPurchasePriceParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                             'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_retail_purchase_price_parse_failure.json', result['monthlyRetailPurchasePriceParse'])
        if (dataset or {}).get('id') == 'disclosure:8c15a8c7-3888-4e14-b50b-9b141891e56b':
            try:
                from .monthly_market_price_announcement_pdf import parse_pdf as parse_monthly_market_price_announcement_pdf
                result['monthlyMarketPriceAnnouncementParse'] = parse_monthly_market_price_announcement_pdf(
                    artifact, result['sha256'], dest / 'monthly_market_price_announcement')
            except Exception as exc:
                result['monthlyMarketPriceAnnouncementParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                                  'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_market_price_announcement_parse_failure.json', result['monthlyMarketPriceAnnouncementParse'])
        if (dataset or {}).get('id') == '1643899371462463488':
            try:
                from .monthly_transmission_maintenance_pdf import parse_pdf as parse_transmission_maintenance_pdf
                result['monthlyTransmissionMaintenanceParse'] = parse_transmission_maintenance_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_transmission_maintenance')
            except Exception as exc:
                result['monthlyTransmissionMaintenanceParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                                   'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_transmission_maintenance_parse_failure.json', result['monthlyTransmissionMaintenanceParse'])
        if (dataset or {}).get('id') == '1643898982801477632':
            try:
                from .monthly_generator_execution_pdf import parse_pdf as parse_generator_execution_pdf
                result['monthlyGeneratorExecutionParse'] = parse_generator_execution_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_generator_execution')
            except Exception as exc:
                result['monthlyGeneratorExecutionParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                            'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_generator_execution_parse_failure.json', result['monthlyGeneratorExecutionParse'])
        if (dataset or {}).get('id') == '1643899086627278848':
            try:
                from .monthly_generation_summary import parse_pdf as parse_generation_summary_pdf
                result['monthlyGenerationSummaryParse'] = parse_generation_summary_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_generation_summary')
            except Exception as exc:
                result['monthlyGenerationSummaryParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                           'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_generation_summary_parse_failure.json', result['monthlyGenerationSummaryParse'])
        if (dataset or {}).get('id') == '1643899086627278848':
            try:
                from .monthly_network_commissioning_pdf import parse_pdf as parse_network_commissioning_pdf
                result['monthlyNetworkCommissioningParse'] = parse_network_commissioning_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_network_commissioning')
            except Exception as exc:
                result['monthlyNetworkCommissioningParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                               'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_network_commissioning_parse_failure.json', result['monthlyNetworkCommissioningParse'])
        if (dataset or {}).get('id') == '1643898897313173504':
            try:
                from .monthly_network_maintenance_execution_pdf import parse_pdf as parse_network_maintenance_execution_pdf
                result['monthlyNetworkMaintenanceExecutionParse'] = parse_network_maintenance_execution_pdf(
                    artifact, result['source']['date'], result['sha256'], dest / 'monthly_network_maintenance_execution')
            except Exception as exc:
                result['monthlyNetworkMaintenanceExecutionParse'] = {'status': 'requires_review', 'errorType': type(exc).__name__,
                                                                       'error': str(exc), 'sourceSha256': result['sha256']}
                atomic(dest / 'monthly_network_maintenance_execution_parse_failure.json', result['monthlyNetworkMaintenanceExecutionParse'])
        atomic(dest / 'source_metadata.json', result)
        return
    rows = result.get('rows')
    if not isinstance(rows, list):
        rows = result.get('records')
    if not isinstance(rows, list):
        return
    fields = list(dict.fromkeys([c['prop'] for c in result.get('native_columns', []) if c.get('prop')]
                                + [k for row in rows for k in row]))
    identity_fields = result.get('source_identity_fields')
    date_field = result['source_date_field'] if 'source_date_field' in result else 'date'
    metadata = {key: value for key, value in result.items() if key not in ('rows', 'keys', 'site_decoded_rows', 'records')}
    metadata['source_date_field'] = date_field
    metadata['csv_row_ordinal_is_derived'] = True
    metadata['csv_structured_fields_encoding'] = 'JSON for object/array cells'
    atomic(dest / 'source_metadata.json', metadata)
    if 'site_decoded_rows' in result:
        atomic(dest / 'site_decoded_rows.json', result['site_decoded_rows'])
    if (dataset or {}).get('id') in {'disclosure:94166a8e-f50f-4185-8987-e22cd1834e4d',
                                     'disclosure:3fa94746-44c1-4a86-be30-06ef6483e2ae',
                                     'disclosure:32d5a58d-fdd1-439f-8b97-bd9b2b7c5db0',
                                     'disclosure:f25eac56-e78e-460b-aa23-bce269ec8c2a'}:
        try:
            from .generation_2020_table import export_page as export_generation_2020
            generation_2020 = export_generation_2020(result, dest / 'generation_2020')
        except Exception as exc:
            generation_2020 = {'status': 'requires_review', 'errorType': type(exc).__name__, 'error': str(exc)}
        atomic(dest / 'generation_2020_parse_status.json', generation_2020)
    if (dataset or {}).get('id') == 'disclosure:32d5a58d-fdd1-439f-8b97-bd9b2b7c5db0':
        try:
            from .generation_capacity_table import export_page as export_generation_capacity_table
            capacity_table = export_generation_capacity_table(result, dest / 'generation_capacity_table')
        except Exception as exc:
            capacity_table = {'status': 'requires_review', 'errorType': type(exc).__name__, 'error': str(exc)}
        atomic(dest / 'generation_capacity_table_parse_status.json', capacity_table)
    if (dataset or {}).get('id') == 'disclosure:dc3d886b-4e96-4dfe-8cbb-9e788bba85a1':
        try:
            from .industry_consumption_table import export_page as export_industry_consumption
            consumption = export_industry_consumption(result, dest / 'industry_consumption')
        except Exception as exc:
            consumption = {'status': 'requires_review', 'errorType': type(exc).__name__, 'error': str(exc)}
        atomic(dest / 'industry_consumption_parse_status.json', consumption)
    if (dataset or {}).get('id') == 'disclosure:d9135828-37d6-444c-b70c-77e861dda98b':
        try:
            from .annual_supply_forecast_2026 import export_page as export_annual_supply_forecast
            forecast = export_annual_supply_forecast(result, dest / 'annual_supply_forecast')
        except Exception as exc:
            forecast = {'status': 'requires_review', 'errorType': type(exc).__name__, 'error': str(exc)}
        atomic(dest / 'annual_supply_forecast_parse_status.json', forecast)
    if (dataset or {}).get('id') == 'disclosure:94166a8e-f50f-4185-8987-e22cd1834e4d':
        # Keep the original listing regardless of derivative parsing outcomes.
        try:
            from .generation_capacity_narrative import export_page
            derived = export_page(result, dest / 'capacity_narrative')
        except Exception as exc:
            derived = {'status': 'requires_review', 'errorType': type(exc).__name__, 'error': str(exc)}
        atomic(dest / 'generation_capacity_parse_status.json', derived)
        try:
            from .generation_statistics_narrative import export_page as export_generation_statistics
            statistics = export_generation_statistics(result, dest / 'generation_statistics_narrative')
        except Exception as exc:
            statistics = {'status': 'requires_review', 'errorType': type(exc).__name__, 'error': str(exc)}
        atomic(dest / 'generation_statistics_parse_status.json', statistics)
        try:
            from .net_capacity_narrative import export_page as export_net_capacity
            changes = export_net_capacity(result, dest / 'net_capacity_narrative')
        except Exception as exc:
            changes = {'status': 'requires_review', 'errorType': type(exc).__name__, 'error': str(exc)}
        atomic(dest / 'net_capacity_parse_status.json', changes)
        try:
            from .generation_change_narrative import export_page as export_generation_changes
            comparisons = export_generation_changes(result, dest / 'generation_change_narrative')
        except Exception as exc:
            comparisons = {'status': 'requires_review', 'errorType': type(exc).__name__, 'error': str(exc)}
        atomic(dest / 'generation_change_parse_status.json', comparisons)
    with (dest / 'native_rows.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: json.dumps(v, ensure_ascii=False, separators=(',', ':')) if isinstance(v, (dict, list)) else v for k, v in row.items()} for row in rows)
        f.flush()
        os.fsync(f.fileno())
    # A blank CSV cell alone cannot distinguish missing, null and empty string.
    # Keep all source values unchanged and export their states separately.
    with (dest / 'native_field_states.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['derived_row_ordinal', 'native_prop', 'field_present', 'is_null', 'is_empty'])
        for ordinal, row in enumerate(rows, 1):
            for prop in fields:
                writer.writerow([ordinal, prop, prop in row, row.get(prop) is None, row.get(prop) == ''])
        f.flush()
        os.fsync(f.fileno())
    with (dest / 'native_time_series.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['derived_row_ordinal', 'native_id_field', 'source_id', 'source_identity_json',
                         'source_date_field', 'source_date', 'period_ordinal', 'native_time_label',
                         'native_prop', 'native_label', 'value',
                         'is_null', 'is_empty', 'field_present', 'query_date', 'date_provenance'])
        for ordinal, row in enumerate(rows, 1):
            id_field = 'id' if 'id' in row else 'ID' if 'ID' in row else None
            source_key_fields = identity_fields or ([id_field] if id_field else [])
            key = json.dumps({key: row.get(key) for key in source_key_fields}, ensure_ascii=False)
            times = [column for column in result.get('native_columns', []) if time_label(column)]
            for period, column in enumerate(times, 1):
                if time_label(column):
                    value = row.get(column['prop'])
                    writer.writerow([ordinal, id_field, row.get(id_field), key, date_field, row.get(date_field),
                                     period, time_label(column), column['prop'], column['label'], value, value is None,
                                     value == '', column['prop'] in row,
                                     result.get('native_query', {}).get('dateTime', result.get('native_query', {}).get('date')),
                                     result.get('date_provenance', 'native_source_date_field')])
        f.flush()
        os.fsync(f.fileno())
    # Non-time fields include site-provided unit, node and category dimensions.
    # The ordinal joins to the already exported native_time_series.csv without
    # duplicating those fields once per time point or inventing their meaning.
    from .dimension_export import write_native_dimensions
    dimension_proof=write_native_dimensions(result,dest)
    if dimension_proof:atomic(dest/'native_dimensions_metadata.json',dimension_proof)


if __name__ == '__main__':
    source, destination = map(Path, sys.argv[1:3])
    try:
        request = json.loads(source.read_text())
        result = validate(request)
        export(result, destination.parent, request.get('dataset'))
        atomic(destination, result)
        print(json.dumps({'ok': True, 'path': str(destination)}))
    except Exception as exc:
        atomic(destination.with_name('validation_failure.json'), {'error': str(exc), 'type': type(exc).__name__})
        print(json.dumps({'ok': False, 'error': str(exc)}))
        sys.exit(1)
