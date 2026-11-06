"""Deadline-driven observations by the existing owner; raw files before analysis.

This changes neither eligibility nor website request pacing. It reports finite
scope and deadline misses explicitly; it never claims full field coverage.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
import time

from .archive import Archive
from .faults import Fault, decide
from .periods import CHINA
from .publication_watch import candidates, eligibility, key_for, stamp, handle_observation_fault


def epoch(value, default=0.0):
    return datetime.fromisoformat(value).timestamp() if value else default


def cost_for(config, dataset, last):
    # A conservative measured estimate, never a guarantee of server latency.
    measured = max(0.0, last.get('finished_epoch', 0) - epoch(last.get('started_at')))
    prior = config.get('estimated_cost_seconds', {}).get(dataset, 3.0)
    return max(1.0, min(120.0, prior), min(120.0, measured))


def decision(jobs, now, last_response_end, spacing, guard=30):
    """Bound previous-check-start to next-response-end, not response+900s."""
    if not jobs:
        return None, 10.0
    if last_response_end is not None and now < last_response_end + spacing:
        return None, max(.01, last_response_end + spacing - now)
    ready = [job for job in jobs if now >= job['deadline'] - job['cost'] - guard]
    if not ready:
        return None, max(.01, min(j['deadline'] - j['cost'] - guard for j in jobs) - now)
    # Earlier deadlines always win. Peak priority may only break equal deadlines.
    return min(ready, key=lambda j: (j['deadline'], -j.get('priority', 0), j['key'])), 0.0


def append_capture(instance, record):
    directory = instance / 'update_watch_v2'
    directory.mkdir(exist_ok=True)
    with (directory / 'captures.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


async def run_one(engine, enabled):
    config = engine.state.read('publication_watch', {})
    if not config.get('enabled') or config.get('mode') != 'deadline_spool_v2':
        return False
    if config.get('interval_seconds') != 900 or config.get('deadline_guard_seconds', 30) < 15:
        engine.state.write('publication_watch_health', {'status': 'configuration_refused'})
        return False
    count = engine.state.read('publication_watch_v2_count', 0)
    limit = config.get('max_v2_captures')
    time_limit = config.get('trial_until_epoch')
    if (limit is not None and count >= limit) or (time_limit is not None and engine.clock() >= time_limit):
        engine.state.write('publication_watch_health', {'status': 'trial_limit_reached', 'count': count,
                                                      'keepalive_continues': True})
        engine._publication_watch_next_wait = 5.0
        return False
    # Seed only scheduling times from the old index. Do not rewrite old history.
    index = engine.state.read('publication_watch_capture_index', None)
    if index is None:
        index = engine.state.read('publication_watch_index', {})
    now = engine.clock()
    day = datetime.fromtimestamp(now, CHINA).date()
    missing, jobs = {}, []
    for spec in enabled:
        if config.get('datasets') and spec.id not in config['datasets']:
            continue
        if spec.status not in ('sampled', 'verified', 'enabled'):
            missing[spec.id] = 'spec_not_eligible'
            continue
        ranges = candidates(spec, day)[:config.get('periods_per_dataset', 2)]
        if not ranges:
            missing[spec.id] = 'dedicated_period_contract_required'
        for period, scope, values in ranges:
            key = key_for(spec.id, period, scope)
            previous = index.get(key, {})
            reason = eligibility(engine, spec, period, scope)
            if reason or previous.get('failed'):
                missing[key] = reason or 'watch_failure_not_retried'
                continue
            request = engine._sendable(spec.build(period, values))
            if request is None:
                missing[key] = 'request_template_missing'
                continue
            if 'pagination' in spec.request:
                from .paging import apply, first_page
                request = apply(request, spec.request['pagination'], first_page(spec.request['pagination']))
            started = epoch(previous.get('started_at'))
            jobs.append({'key': key, 'spec': spec, 'period': period, 'scope': scope, 'values': values,
                         'request': request, 'previous': previous, 'deadline': started + 900,
                         'cost': cost_for(config, spec.id, previous), 'priority': 0})
    # Respect original per-lane/budget wait. Store the exact earliest wake-up;
    # Engine must not replace a subsecond pacing wait by its old 10s idle sleep.
    available = []
    waits = []
    for job in jobs:
        wait = engine.spacing_wait('watch', job['spec'].source.get('lane'))
        if wait > 0:
            waits.append(wait)
        else:
            available.append(job)
    chosen, wake = decision(available, now, None, 0, config.get('deadline_guard_seconds', 30))
    if waits:
        wake = min(wake, min(waits))
    engine._publication_watch_next_wait = max(.01, min(wake, 5.0))
    health = {'at': stamp(now), 'status': 'active', 'eligible_ranges': len(jobs), 'not_observed': missing,
              'coverage': 'first_known_scope_first_page_only; full_field_dimension_coverage_unproven',
              'overdue_ranges': sum(j['deadline'] < now for j in jobs), 'capture_count': count,
              'next_wake_seconds': engine._publication_watch_next_wait}
    engine.state.write('publication_watch_health', health)
    if chosen is None:
        return False
    admitted = engine.admission() if engine.admission else None
    if admitted is not None and not admitted['allowed']:
        return False
    spec, period, scope, values = (chosen[k] for k in ('spec', 'period', 'scope', 'values'))
    lane = spec.source.get('lane')
    renewed = await engine._renew(lane)
    if renewed is None:
        return False
    if renewed:
        return True
    started = engine.clock()
    previous = chosen['previous']
    record = {'version': 2, 'dataset': spec.id, 'name': spec.name, 'period': period, 'scope': scope,
              'scope_values': values, 'started_at': stamp(started), 'started_epoch': started,
              'previous_started_at': previous.get('started_at'), 'previous_finished_at': previous.get('finished_at'),
              'previous_original_path': previous.get('original_path'), 'previous_sha256': previous.get('sha256'),
              'deadline_epoch': chosen['deadline'] if previous else None,
              'platform_published_at': None, 'publication_time_reason': 'no_verified_platform_publication_field',
              'coverage': 'first_known_scope_first_page; not_complete_dimensions',
              'source': 'explicit_repeated_observation', 'analysis_state': 'queued'}
    try:
        engine._publication_watch_grant = (spec.id, period, scope)
        result = await engine.request('watch', chosen['request'], spec.response, dataset=spec.id,
                                      period=period, scope=scope, lane=lane)
        # The raw archive is committed before any workbook/XML/OCR field analysis.
        original = Archive(engine.instance / 'update_observations', engine.province.id).store(
            spec.id, period, result.response.body, result.outcome.extension, scope=scope,
            acquisition='publication_watch_v2', http_status=result.response.status)
        record.update(sha256=original['sha256'], original_path=original['path'],
                      size=original['size'], http_status=result.response.status, state='captured_pending_analysis')
    except Fault as fault:
        record.update(failed=True, reason=fault.code, category=fault.category, analysis_state='not_applicable')
        await handle_observation_fault(engine, fault, spec.id, period, scope)
    except Exception as exc:
        record.update(failed=True, reason=type(exc).__name__, analysis_state='not_applicable')
        if engine.ledger.unsettled():
            raise
    finally:
        engine._publication_watch_grant = None
    record.update(finished_at=stamp(engine.clock()), finished_epoch=engine.clock())
    record['window_seconds'] = (record['finished_epoch'] - epoch(previous['started_at'])) if previous.get('started_at') else None
    record['within_900_seconds'] = record['window_seconds'] <= 900 if record['window_seconds'] is not None else None
    append_capture(engine.instance, record)
    index[chosen['key']] = record
    engine.state.write('publication_watch_capture_index', index)
    engine.state.write('publication_watch_v2_count', count + 1)
    engine._status('publication_watching_v2', last_dataset=spec.id, last_period=period,
                   window_seconds=record['window_seconds'], analysis_state=record['analysis_state'])
    return True
