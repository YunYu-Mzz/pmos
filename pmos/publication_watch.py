"""Explicit, opt-in repeated observations by the existing owner; never a second worker.

Observation time is not platform publication time. First-scope and first-page
observations cannot prove every node, unit or page is complete.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

from .archive import Archive
from .faults import Fault, decide
from .periods import CHINA


def stamp(epoch):
    return datetime.fromtimestamp(epoch, CHINA).isoformat(timespec="seconds")


def fingerprint(payload: bytes, extension: str):
    """Ignore XLSX ZIP timestamps/styles; retain cell coordinates, values and sheet order."""
    if extension == '.xlsx':
        with zipfile.ZipFile(io.BytesIO(payload)) as book:
            if book.testzip() is not None:
                raise ValueError('watch_workbook_crc_failed')
            ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            shared = []
            if 'xl/sharedStrings.xml' in book.namelist():
                shared = [''.join(node.itertext()) for node in ET.fromstring(book.read('xl/sharedStrings.xml'))]
            tables = []
            count = 0
            for name in sorted(n for n in book.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')):
                rows = []
                for _, node in ET.iterparse(io.BytesIO(book.read(name)), events=('end',)):
                    if node.tag != '{' + ns['m'] + '}row':
                        continue
                    cells = []
                    for cell in node.findall('m:c', ns):
                        value = cell.findtext('m:v', default='', namespaces=ns)
                        if cell.get('t') == 's' and value:
                            value = shared[int(value)]
                        elif cell.get('t') == 'inlineStr':
                            value = ''.join(cell.find('m:is', ns).itertext())
                        if value != '':
                            cells.append([cell.get('r'), cell.get('t', ''), value])
                    if cells:
                        rows.append(cells)
                    node.clear()
                count += len(rows)
                tables.append([name, rows])
        shape = 'header_only_candidate' if count <= 1 else 'table_rows_observed'
        normalized = tables
    elif extension == '.json':
        normalized = json.loads(payload)
        count = None
        shape = 'json_payload_observed'
    else:
        return hashlib.sha256(payload).hexdigest(), 'binary_observed_semantics_unverified', None
    digest = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return digest, shape, count


def key_for(dataset, period, scope):
    return hashlib.sha256(json.dumps([dataset, period, scope], ensure_ascii=False).encode()).hexdigest()[:24]


def eligibility(engine, spec, period, scope):
    """Only a healthy, already settled base item may be observed again."""
    task = engine.ledger.task(spec.id, period, scope)
    if task is None or task['status'] not in ('saved', 'empty'):
        return 'base_item_not_healthy_or_not_collected'
    bad = engine.ledger.db.execute(
        "SELECT 1 FROM attempts WHERE dataset=? AND period=? AND outcome IN ('failed','unknown','pending') LIMIT 1",
        (spec.id, period)).fetchone()
    if bad:
        return 'failed_or_unknown_period_preserved'
    return None


def assert_grant(engine, dataset, period, scope):
    if getattr(engine, '_p0_recheck_grant', None) is not None:
        from .retained_recheck import assert_grant as assert_recheck_grant
        return assert_recheck_grant(engine, dataset, period, scope)
    if getattr(engine, '_publication_watch_grant', None) != (dataset, period, scope):
        raise Fault('publication_watch_not_authorized', 'local', unsent=True)
    config = engine.state.read('publication_watch', {})
    if not config.get('enabled') or config.get('approval') != '用户2026-11-03授权每日各指标更新监测，每15分钟一轮':
        raise Fault('publication_watch_not_authorized', 'local', unsent=True)
    if config.get('datasets') and dataset not in config['datasets']:
        raise Fault('publication_watch_not_authorized', 'local', unsent=True)
    spec = engine.province.datasets().get(dataset)
    if spec is None or eligibility(engine, spec, period, scope):
        raise Fault('publication_watch_base_not_eligible', 'local', unsent=True)


def candidates(spec, day):
    """Use the spec's own due period, plus its previous day. No invented form parameters."""
    periods = spec.periods_to_collect(on=day)
    if not periods:
        return []
    if spec.period['kind'] not in ('day', 'month'):
        return []
    labels = periods[:2]
    items = spec.scope_items()
    return [(period, items[0][0], items[0][1]) for period in labels]


async def handle_observation_fault(engine, fault, dataset, period, scope):
    """Honor item-isolation policy without changing the healthy base task.

    The failed observation is still committed by the caller and eligibility
    excludes its failed/unknown period forever. A visible logout or repeated
    transport failures retain the existing province-wide protection.
    """
    if fault.category == 'network' and 'network' in engine.policy.isolate_items:
        if engine.check_session is None:
            engine.block(fault, decide(fault, 'collect'), dataset)
            return
        try:
            session_fault = await engine.check_session()
        except Exception:
            # Cannot establish even the existing visible-login guard.
            engine.block(fault, decide(fault, 'collect'), dataset)
            return
        if session_fault is not None:
            engine.block(session_fault, decide(session_fault, 'collect'), dataset)
            return
        from .engine import NETWORK_STREAK
        engine._network_streak += 1
        if not engine.ledger.unsettled() and engine._network_streak < NETWORK_STREAK:
            engine.state.event('observation_isolated', dataset=dataset, period=period,
                               scope=scope, reason=fault.code, category=fault.category,
                               network_streak=engine._network_streak,
                               base_task_preserved=True, failed_request_not_retried=True)
            return
    if fault.category in ('auth', 'rate_limit', 'network') or engine.ledger.unsettled():
        engine.block(fault, decide(fault, 'collect'), dataset)


async def run_one(engine, enabled):
    config = engine.state.read('publication_watch', {})
    if config.get('mode') == 'deadline_spool_v2':
        from .publication_deadlines import run_one as run_deadline
        return await run_deadline(engine, enabled)
    if not config.get('enabled'):
        return False
    count = engine.state.read('publication_watch_count', 0)
    if config.get('max_observations_total') is not None and count >= config['max_observations_total']:
        engine.state.write('publication_watch_health', {'status': 'acceptance_limit_reached', 'at': stamp(engine.clock()), 'count': count})
        return False
    if config.get('interval_seconds') != 900:
        engine.state.write('publication_watch_health', {'status': 'configuration_refused', 'at': stamp(engine.clock())})
        return False
    index = engine.state.read('publication_watch_index', {})
    now = engine.clock()
    todo, missing = [], {}
    day = datetime.fromtimestamp(now, CHINA).date()
    for spec in enabled:
        if config.get('datasets') and spec.id not in config['datasets']:
            continue
        if spec.status not in ('sampled', 'verified', 'enabled'):
            missing[spec.id] = 'spec_not_eligible'
            continue
        for period, scope, values in candidates(spec, day)[:config.get('periods_per_dataset', 2)]:
            key = key_for(spec.id, period, scope)
            last = index.get(key, {})
            reason = eligibility(engine, spec, period, scope)
            if reason:
                missing[key] = reason
                continue
            if last.get('failed'):
                missing[key] = 'watch_failure_not_retried'
                continue
            if now - last.get('finished_epoch', 0) < 900:
                continue
            todo.append((last.get('finished_epoch', 0), spec.id, period, scope, values, key, spec))
    engine.state.write('publication_watch_health', {'at': stamp(now), 'due': len(todo), 'not_observed': missing,
                                                  'status': 'active', 'coverage': 'one_known_scope_and_first_page_only'})
    for _, _, period, scope, values, key, spec in sorted(todo, key=lambda item: item[:4]):
        request = engine._sendable(spec.build(period, values))
        if request is None:
            missing[key] = 'request_template_missing'
            continue
        if 'pagination' in spec.request:
            from .paging import apply, first_page
            request = apply(request, spec.request['pagination'], first_page(spec.request['pagination']))
        lane = spec.source.get('lane')
        if engine.spacing_wait('watch', lane) > 0:
            continue
        admitted = engine.admission() if engine.admission else None
        if admitted is not None and not admitted['allowed']:
            return False
        # Session renewal gets its existing priority before observation work.
        renewed = await engine._renew(lane)
        if renewed is None:
            return False
        if renewed:
            return True
        started = engine.clock()
        record = {'dataset': spec.id, 'name': spec.name, 'period': period, 'scope': scope,
                  'scope_values': values, 'started_at': stamp(started),
                  'platform_published_at': None, 'publication_time_reason': 'no_verified_platform_publication_field',
                  'coverage': 'first_known_scope_first_page; not_complete_dimensions',
                  'source': 'explicit_repeated_observation'}
        last = index.get(key, {})
        try:
            engine._publication_watch_grant = (spec.id, period, scope)
            result = await engine.request('watch', request, spec.response, dataset=spec.id, period=period, scope=scope, lane=lane)
            digest, shape, rows = fingerprint(result.response.body, result.outcome.extension)
            original = Archive(engine.instance / 'update_observations', engine.province.id).store(
                spec.id, period, result.response.body, result.outcome.extension, scope=scope,
                acquisition='publication_watch', http_status=result.response.status)
            record.update(sha256=original['sha256'], original_path=original['path'], content_sha256=digest,
                          shape=shape, observed_nonempty_rows=rows,
                          state='first_observation' if not last else 'unchanged' if last.get('content_sha256') == digest else 'changed',
                          update_interval_start=last.get('started_at') if last and last.get('content_sha256') != digest else None)
            if not last:
                record['left_censored'] = True
            record['finished_at'] = stamp(engine.clock())
            record['finished_epoch'] = engine.clock()
            if record['state'] == 'changed':
                record['update_interval_end'] = record['finished_at']
        except Fault as fault:
            record.update(failed=True, reason=fault.code, category=fault.category,
                          finished_epoch=engine.clock(), finished_at=stamp(engine.clock()))
            await handle_observation_fault(engine, fault, spec.id, period, scope)
        except Exception as exc:
            record.update(failed=True, reason=type(exc).__name__, finished_epoch=engine.clock(), finished_at=stamp(engine.clock()))
            if engine.ledger.unsettled():
                raise
        finally:
            engine._publication_watch_grant = None
        directory = engine.instance / 'update_watch'
        directory.mkdir(exist_ok=True)
        with (directory / 'observations.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        index[key] = record
        engine.state.write('publication_watch_index', index)
        engine.state.write('publication_watch_count', count + 1)
        engine._status('publication_watching', last_dataset=spec.id, last_period=period)
        return True
    if missing:
        engine.state.write('publication_watch_health', {'at': stamp(now), 'due': len(todo), 'not_observed': missing,
                                                      'status': 'partially_waiting', 'coverage': 'one_known_scope_and_first_page_only'})
    return False
