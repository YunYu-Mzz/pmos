"""Explicit, bounded rechecks of healthy retained P0 files by the existing owner.

Never reopen a task, release a consumed identity, or retry a failed/unknown
period. New versions use the observation ledger/archive and keep old files.
"""
from datetime import date
import hashlib
import json
from pathlib import Path

from .archive import Archive
from .faults import Fault
from .publication_watch import handle_observation_fault, stamp

APPROVAL = ('用户2026-11-05：查清并修复网络阻断，确认登录有效后恢复原单worker，'
            '优先补齐P0真正缺失数据；保留原件和失败记录，不重复失败请求，不重启浏览器，不开定时任务')


def identity(item):
    return item['dataset'], item['period'], item.get('scope', '')


def checked_item(engine, item):
    """No side effects: exact period, baseline bytes and settled healthy history."""
    dataset, period, scope = identity(item)
    spec = engine.province.datasets().get(dataset)
    if spec is None or spec.status not in ('sampled', 'verified', 'enabled'):
        raise ValueError('recheck_spec_not_eligible')
    if spec.period['kind'] != 'day' or date.fromisoformat(period).isoformat() != period:
        raise ValueError('recheck_daily_period_required')
    if (spec.path is None or hashlib.sha256(spec.path.read_bytes()).hexdigest()
            != item.get('spec_sha256')):
        raise ValueError('recheck_spec_hash_changed')
    spec.scope_values(scope)
    bad = engine.ledger.db.execute(
        "SELECT 1 FROM attempts WHERE dataset=? AND period=? AND outcome IN ('pending','failed','unknown') LIMIT 1",
        (dataset, period)).fetchone()
    if bad or engine.ledger.unsettled():
        raise ValueError('recheck_failed_or_unknown_preserved')
    old = engine.ledger.db.execute(
        'SELECT identity_json,state FROM legacy_records WHERE consumed=1 AND identity_json LIKE ? AND identity_json LIKE ?',
        ('%'+dataset+'%', '%'+period+'%'))
    matches = []
    for row in old:
        value = json.loads(row[0])
        # Canonical TaskIdentity documents bind dataset and period explicitly.
        if value.get('dataset') == dataset and value.get('period', {}).get('label') == period:
            matches.append(row[1])
    if any(state not in ('saved', 'empty', 'complete') for state in matches):
        raise ValueError('recheck_legacy_failure_or_unknown_preserved')
    task = engine.ledger.task(dataset, period, scope)
    if not matches and (task is None or task['status'] not in ('saved', 'empty')):
        raise ValueError('recheck_healthy_baseline_required')
    baseline = Path(item['baseline_path']).resolve()
    # Explicitly approved roots only. Never inspect a browser profile or secret.
    roots = [engine.instance/'original', Path('/home/ubuntu/sd_crawler/shanxi/data/2026/original')]
    if not any(baseline.is_relative_to((root/dataset/period).resolve()) for root in roots):
        raise ValueError('recheck_baseline_outside_original_archive')
    if hashlib.sha256(baseline.read_bytes()).hexdigest() != item['baseline_sha256']:
        raise ValueError('recheck_baseline_hash_changed')
    return spec


def assert_grant(engine, dataset, period, scope):
    plan = engine.state.read('p0_recheck', {}) or {}
    current = getattr(engine, '_p0_recheck_grant', None)
    if plan.get('approval') != APPROVAL or not plan.get('enabled') or current is None:
        raise Fault('p0_recheck_not_authorized', 'local', unsent=True)
    if identity(current) != (dataset, period, scope) or current not in plan.get('items', []):
        raise Fault('p0_recheck_range_not_authorized', 'local', unsent=True)
    checked_item(engine, current)


async def verify_keeper_once(engine):
    reviewed = engine.state.read('reviewed_keepalive') or {}
    if not reviewed.get('verify_once'):
        return False
    spec = engine._keepalive_spec()  # verifies the accepted evidence and failure history
    lane = spec.source.get('lane')
    if engine.spacing_wait('keepalive', lane) > 0:
        return True
    admitted = engine.admission() if engine.admission else None
    if admitted is not None and not admitted['allowed']:
        return True
    request = engine._sendable(spec.build(reviewed['period']))
    if request is None:
        raise Fault('reviewed_keeper_template_missing', 'local', unsent=True)
    # Consume this explicit verification before dispatch, so a crash cannot replay it.
    engine.state.write('reviewed_keepalive', {**reviewed,'verify_once':False,'verification_started':stamp(engine.clock())})
    result = await engine.request('keepalive',request,spec.response,dataset=spec.id,
                                  period=reviewed['period'],lane=lane)
    proof = Archive(engine.instance/'keepalive_review',engine.province.id).store(
        spec.id,reviewed['period'],result.response.body,result.outcome.extension,
        acquisition='reviewed_keeper_verification_20261105',http_status=result.response.status)
    engine.state.write('reviewed_keeper_verified', {**proof,'finished_at':stamp(engine.clock())})
    return True


async def run_one(engine):
    plan = engine.state.read('p0_recheck', {}) or {}
    if not plan.get('enabled'):
        return False
    if (plan.get('approval') != APPROVAL or not 0 < len(plan.get('items', [])) <= 8
            or len({identity(x) for x in plan['items']}) != len(plan['items'])
            or type(plan.get('after_attempt')) is not int or plan['after_attempt'] < 0):
        raise Fault('p0_recheck_plan_invalid', 'local', unsent=True)
    progress = engine.state.read('p0_recheck_results', {}) or {}
    for item in plan['items']:
        key = '|'.join(identity(item))
        if key in progress:
            continue
        try:
            spec = checked_item(engine, item)
            # A crash after dispatch but before progress never replays the range.
            row = engine.ledger.db.execute(
                'SELECT id,outcome FROM attempts WHERE id>? AND kind=? AND dataset=? AND period=? AND scope=? LIMIT 1',
                (plan['after_attempt'], 'watch', *identity(item))).fetchone()
            if row:
                progress[key] = {'state':'already_attempted_not_retried','attempt':row[0],'outcome':row[1]}
                engine.state.write('p0_recheck_results', progress)
                continue
            request = engine._sendable(spec.build(item['period'], spec.scope_values(item.get('scope',''))))
            if request is None:
                progress[key] = {'state':'template_missing_no_request'}
                engine.state.write('p0_recheck_results', progress)
                continue
            lane = spec.source.get('lane')
            if engine.spacing_wait('watch', lane) > 0:
                return True
            admitted = engine.admission() if engine.admission else None
            if admitted is not None and not admitted['allowed']:
                await engine._renew(lane)
                return True
            renewed = await engine._renew(lane)
            if renewed is None or renewed:
                return True
            engine._p0_recheck_grant = item
            started = engine.clock()
            try:
                result = await engine.request('watch', request, spec.response, dataset=spec.id,
                                              period=item['period'], scope=item.get('scope',''), lane=lane)
                original = Archive(engine.instance/'update_observations', engine.province.id).store(
                    spec.id, item['period'], result.response.body, result.outcome.extension,
                    scope=item.get('scope',''), acquisition='explicit_p0_recheck_20261105',
                    http_status=result.response.status)
                progress[key] = {'state':'captured_needs_96point_acceptance','started_at':stamp(started),
                                 'finished_at':stamp(engine.clock()), **original}
            except Fault as fault:
                progress[key] = {'state':'failed_not_retried','reason':fault.code,'category':fault.category,
                                 'started_at':stamp(started),'finished_at':stamp(engine.clock())}
                await handle_observation_fault(engine, fault, *identity(item))
            finally:
                engine._p0_recheck_grant = None
            engine.state.write('p0_recheck_results', progress)
            return True
        except ValueError as exc:
            progress[key] = {'state':'rejected_no_request','reason':str(exc)}
            engine.state.write('p0_recheck_results', progress)
    engine.state.write('p0_recheck', {**plan, 'enabled':False, 'completed_at':stamp(engine.clock())})
    return False
