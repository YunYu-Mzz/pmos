"""Shaanxi offline bridge to PMOS ParsedArtifact; no transport or file reads.

Retain the existing pure validators as a private snapshot. A supplied contract
is NOT authenticated evidence: it only makes fixture/replay boundaries explicit.
This adapter grants no website access, historical coverage or migration rights.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from ..contracts import ParsedArtifact, canonical, reject_secrets, unit_from_response
from ..faults import Fault
from ..validate import Response, evaluate
from .shaanxi_legacy.spot_tables import ValidationError, native_table
from .shaanxi_legacy.curve_validation import native_curve
from .shaanxi_legacy.catalogue import validate_directory
from .shaanxi_legacy.disclosure_list_validation import native_listing
from .shaanxi_legacy.file_list_validation import file_listing
from .shaanxi_legacy.weighted_price_validation import weighted_price

FAMILIES = frozenset({'grid', 'preview', 'weekly', 'curve', 'directory',
                      'disclosure_list', 'file_list', 'weighted_price'})
MAX_BYTES = 16 * 1024 * 1024


def _fail(code):
    raise Fault(code, 'site_data')


def _page(result, params):
    page, size, total = params.get('page'), params.get('size'), result['server_total_rows']
    if any(type(x) is not int for x in (page, size, total)) or min(page, size) < 1 or total < 0:
        _fail('shaanxi_explicit_page_bounds_required')
    pages = max(1, (total + size - 1) // size)
    if page > pages or len(result['rows']) != min(size, max(0, total - (page-1)*size)):
        _fail('shaanxi_page_incomplete')
    return {'page': page, 'page_size': size, 'reported_total': total,
            'expected_pages': pages, 'selected_page_complete': True,
            'all_pages_verified': pages == 1}


def _decode(raw: bytes, contract: dict, *, http_status: int = 200) -> ParsedArtifact:
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        _fail('shaanxi_bounded_bytes_required')
    # Shared PMOS error taxonomy precedes legacy shape validation.
    evaluate({'kind': 'json'}, Response(http_status, 'application/json', raw))
    try:
        body = json.loads(raw, parse_constant=lambda _: _fail('shaanxi_nonfinite_json'))
    except (ValueError, UnicodeError):
        _fail('shaanxi_json_required')
    reject_secrets(body)
    reject_secrets(contract)
    if contract.get('response_sha256') != hashlib.sha256(raw).hexdigest():
        _fail('shaanxi_response_hash_mismatch')
    dataset, params, turn = (contract.get(k) for k in ('dataset', 'params', 'turn'))
    if not all(isinstance(x, dict) for x in (dataset, params, turn)):
        _fail('shaanxi_explicit_native_contract_required')
    family = dataset.get('family')
    if family not in FAMILIES or not isinstance(dataset.get('id'), str) or not dataset['id']:
        _fail('shaanxi_family_not_reviewed')
    status_field, success = ('code', 200) if family in {'grid','weekly','directory','weighted_price'} else ('status', 0)
    if not isinstance(body, dict) or type(body.get(status_field)) is not int or body[status_field] != success or body.get('success') is False:
        _fail('shaanxi_strict_business_envelope_required')
    # A recorded terminal result is mandatory, even for an empty source.
    if (turn.get('pending') != [] or turn.get('failures') != [] or
        turn.get('phase') not in {'captured_requires_adapter_validation',
                                  'discovered_requires_adapter_validation'}):
        _fail('shaanxi_original_turn_not_terminal')
    binding = contract.get('query_binding', {})
    field = binding.get('invoke_field', 'nativeQuery')
    if field not in {None, 'nativeQuery', 'form'}:
        _fail('shaanxi_query_binding_field_not_reviewed')
    invoked = turn.get('invokeResult') or {}
    actual = invoked if field is None else invoked.get(field)
    if (not isinstance(binding.get('expected'), dict) or not binding['expected']
            or actual is None or canonical(actual) != canonical(binding['expected'])):
        _fail('shaanxi_original_query_binding_required')
    # Every response carrying its own date must agree with the selected date;
    # source filters are additionally checked below rather than assumed complete.
    if family in {'grid', 'preview', 'weekly', 'weighted_price'}:
        rule = dataset.get('validation', {})
        if not rule.get('native_columns'):
            _fail('shaanxi_native_field_mapping_required')
        date_field = rule.get('source_date_field') or 'date'
        data = body.get('data') if isinstance(body, dict) else None
        rows = data if family == 'weighted_price' else data.get('list') if isinstance(data, dict) else None
        if not isinstance(rows, list):
            _fail('shaanxi_native_rows_required')
        if any(not isinstance(row, dict) or any(c['prop'] not in row
                   for c in rule['native_columns']) for row in rows):
            _fail('shaanxi_native_column_field_missing')
        if params.get('date') and any(date_field not in row for row in rows if isinstance(row, dict)):
            _fail('shaanxi_response_date_field_missing')
        result = native_table(raw, rule.get('native_columns') or [], http_status=http_status,
            expected_date=params.get('date'), require_96=rule.get('points_per_row') == 96,
            identity_fields=rule.get('source_identity_fields'), date_field=date_field,
            protocol='status0' if family == 'preview' else 'code200',
            date_encoding=rule.get('source_date_encoding', 'iso_date'),
            period_minutes=rule.get('period_minutes'),
            shape='array' if family == 'weighted_price' else 'paged_table')
        for field, expected in contract.get('response_scope', {}).items():
            if any(row.get(field) != expected or field not in row for row in rows):
                _fail('shaanxi_response_scope_mismatch')
        if family == 'weekly':
            from datetime import date
            begin, end = params.get('startDate'), params.get('endDate')
            if not begin:
                _fail('shaanxi_weekly_native_start_required')
            begin = date.fromisoformat(begin)
            end = date.fromisoformat(end) if end is not None else None
            if end is not None and end < begin:
                _fail('shaanxi_weekly_invalid_bounds')
            if any(date.fromisoformat(row[date_field]) < begin or
                   end is not None and date.fromisoformat(row[date_field]) > end for row in rows):
                _fail('shaanxi_weekly_source_outside_native_bounds')
        pagination = None if family == 'weighted_price' else _page(result, params)
        if family == 'weighted_price':
            weighted_price(raw, dataset, turn, params, {'status': http_status, 'path': '/offline',
                'responseFile': None, 'sha256': None})
        semantic = 'native_source_fields_only_selected_scope'
    elif family == 'curve':
        result = native_curve(body, dataset, turn, params, http_status)
        pagination, semantic = None, 'native_curve_query_date_only_no_response_date'
    elif family == 'directory':
        result = validate_directory(raw, http_status=http_status, file_name=dataset['fileName'])
        result['rows'] = result['records']
        pagination, semantic = None, 'attachment_directory_only_zero_downloads'
    elif family == 'disclosure_list':
        result = native_listing(body, dataset, turn, params, http_status)
        pagination, semantic = _page(result, params), 'publication_listing_only_zero_downloads'
    else:
        result = file_listing(raw, dataset, turn, params, {'status': http_status, 'path': '/offline',
            'responseFile': None, 'sha256': None})
        pagination, semantic = _page(result, params), 'file_listing_only_zero_downloads'
    return ParsedArtifact(deepcopy(result['rows']), result['outcome'] == 'empty',
                          pagination=pagination, unit=unit_from_response(raw, contract),
                          semantic_state=semantic)


def decode(raw: bytes, contract: dict, *, http_status: int = 200) -> ParsedArtifact:
    try:
        return _decode(raw, contract, http_status=http_status)
    except Fault:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        # Legacy validators used ValueError; the common Engine expects Fault.
        # Never put raw field values or key names from the response in logs.
        raise Fault('shaanxi_contract_or_shape_invalid', 'site_data') from None
