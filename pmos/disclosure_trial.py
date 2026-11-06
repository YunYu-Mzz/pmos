"""Bounded Liaoning pilot, called ONLY by the existing serial owner.

Disabled without a separately approved immutable configuration. No CLI,
network worker, timer, retries, historical queue settlement, OCR or browser
lifecycle action is implemented here. This artifact has NOT been deployed.
"""
from __future__ import annotations
import hashlib,json,re,math
from dataclasses import replace
from datetime import date
from .faults import Fault,decide


def config_hash(config):
    payload={k:v for k,v in config.items() if k!='authorization'}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def validate(config,engine):
    auth=config.get('authorization',{})
    if (not isinstance(auth,dict) or engine.province.id!='liaoning' or auth.get('approved_by_human') is not True
            or not auth.get('message_id') or auth.get('config_sha256')!=config_hash(config)):
        raise ValueError('disclosure_configuration_unapproved')
    if (not re.fullmatch('[a-z0-9_]{1,40}',config.get('trial_id','')) or config.get('rounds')!=2
            or config.get('max_business_requests')!=4 or config.get('window_seconds')!=900
            or config.get('completion_reserve_seconds')!=65 or len(config.get('jobs',[]))!=2
            or config['expires_at']<=engine.clock() or config['expires_at']-config['activated_at']>2700
            or config['activated_at']>engine.clock()):
        raise ValueError('disclosure_configuration_budget_or_time_invalid')
    specs=engine.province.datasets()
    for job in config['jobs']:
        # Two fixed business dates of the same healthy, proven JSON report.
        # No missing menu, denied source, attachment or pagination probe.
        spec=specs[job['dataset']]
        if (job['dataset'] not in ('system_load','realtime_node_price')
                or spec.status not in ('sampled','verified','enabled') or spec.documents
                or spec.request.get('transport')!='component' or 'pagination' in spec.request
                or spec.response.get('kind')!='json'
                or hashlib.sha256(spec.path.read_bytes()).hexdigest()!=job['spec_sha256']):
            raise ValueError('disclosure_dataset_contract_changed')
        spec.scope_values(job.get('scope',''))
        date.fromisoformat(job['period'])
    if len({j['dataset'] for j in config['jobs']})!=1:
        raise ValueError('disclosure_single_existing_report_only')
    if len({(j['dataset'],j['period'],j.get('scope','')) for j in config['jobs']})!=2:
        raise ValueError('disclosure_duplicate_range')
    return specs


def due_at(config,records,index,round_no):
    if round_no==0:return config['activated_at']
    previous=next(r for r in records if r['job']==index and r['round']==round_no-1)
    # Completion deadline comes from previous REQUEST START, not from the
    # finish of the last round. Reserve includes current timeout plus margin.
    return previous['started_at']+config['window_seconds']-config['completion_reserve_seconds']


async def tick(engine):
    try:
        config=engine.state.read('disclosure_trial_config',{}) or {}
        state=engine.state.read('disclosure_trial',{}) or {}
        if not isinstance(config,dict) or not isinstance(state,dict):raise ValueError('invalid_state_shape')
    except (ValueError,TypeError):
        if not getattr(engine,'_disclosure_state_invalid',False):
            engine.state.event('disclosure_state_invalid',purpose='disclosure_observation')
        engine._disclosure_state_invalid=True
        return None
    engine._disclosure_state_invalid=False
    if not config.get('enabled'):return None
    if state.get('phase') in ('complete','requires_review','expired'):return None
    try:specs=validate(config,engine)
    except (ValueError,KeyError,TypeError):
        engine.state.write('disclosure_trial',{**state,'phase':'requires_review','reason':'configuration_invalid'})
        return None
    if state and (state.get('trial_id')!=config['trial_id'] or state.get('config_sha256')!=config_hash(config)):
        engine.state.write('disclosure_trial',{**state,'phase':'requires_review','reason':'configuration_changed'})
        return None
    if state.get('pending'):
        # A crash after a known response is reviewable; never replay an
        # observation just because the reporting receipt was not finished.
        engine.state.write('disclosure_trial',{**state,'phase':'requires_review','reason':'pending_observation_no_replay'})
        return None
    records=state.get('records',[])
    try:
        if not isinstance(records,list) or len(records)>4:raise ValueError('invalid_records')
        pairs=[]
        for row in records:
            if not isinstance(row,dict) or row['job'] not in (0,1) or row['round'] not in (0,1):raise ValueError('invalid_record')
            for key in ('started_at','finished_at'):
                if type(row[key]) not in (int,float) or not math.isfinite(row[key]):raise ValueError('invalid_time')
            if row['finished_at']<row['started_at'] or row['outcome'] not in ('success','empty'):raise ValueError('invalid_result')
            expected=config['jobs'][row['job']]
            if any(row.get(k)!=expected.get(k,'') for k in ('dataset','period','scope')):raise ValueError('range_changed')
            pairs.append((row['job'],row['round']))
        if len(set(pairs))!=len(pairs):raise ValueError('duplicate_round')
    except (ValueError,KeyError,TypeError):
        engine.state.write('disclosure_trial',{**state,'phase':'requires_review','reason':'malformed_observation_records'})
        return None
    state={**state,'trial_id':config['trial_id'],'config_sha256':config_hash(config),'records':records}
    completed={(r['job'],r['round']) for r in records}
    candidates=[(due_at(config,records,i,r),r,i,j) for r in range(2) for i,j in enumerate(config['jobs'])
                if (i,r) not in completed and (r==0 or (i,r-1) in completed)]
    if not candidates:
        engine.state.write('disclosure_trial',{**state,'phase':'complete','pending':None})
        return None
    due,round_no,index,job=min(candidates,key=lambda x:(x[0],x[1],x[2]))
    if engine.clock()<due:return None
    spec=specs[job['dataset']]; scope=job.get('scope','');period=job['period']
    if spec.id in engine.dataset_holds():
        engine.state.write('disclosure_trial',{**state,'phase':'requires_review','reason':'existing_dataset_hold'})
        return None
    old_bad=engine.ledger.db.execute("SELECT 1 FROM attempts WHERE dataset=? AND period=? AND scope=? "
        "AND kind IN ('collect','sample') AND outcome IN ('failed','unknown','pending') LIMIT 1",(spec.id,period,scope)).fetchone()
    if old_bad:
        engine.state.write('disclosure_trial',{**state,'phase':'requires_review','reason':'historical_exception_no_retry'})
        return None
    # The owning tick has checked global guards and stop-all. Renewals win
    # before spacing/resource admission; never reset or suppress their due time.
    renewed=await engine._renew(spec.source.get('lane'))
    if renewed is None or renewed:return .2
    if engine.spacing_wait('observe',spec.source.get('lane'))>0:return None
    if engine._disk_low():return None
    admission=engine.admission() if engine.admission is not None else None
    if admission is not None and not admission['allowed']:return None
    request=engine._sendable(spec.build(period,spec.scope_values(scope)))
    if request is None:
        engine.state.write('disclosure_trial',{**state,'phase':'requires_review','reason':'request_template_missing'})
        return None
    request={**request,'existing_frame_only':True}
    operation=f"disclosure_observation:{config['trial_id']}:{index}:{round_no}"
    identity=replace(engine.task_identity(spec,period,scope),operation=operation)
    pending={'job':index,'round':round_no,'dataset':spec.id,'period':period,'scope':scope,
             'identity_key':identity.key,'purpose':'disclosure_observation'}
    state={**state,'pending':pending,'phase':'requesting'}
    engine.state.write('disclosure_trial',state)
    try:
        result=await engine.request('observe',request,spec.response,dataset=spec.id,period=period,scope=scope,
                                    identity=identity,lane=spec.source.get('lane'))
    except Fault as fault:
        engine.state.write('disclosure_trial',{**state,'phase':'requires_review','reason':fault.code})
        # Existing authentication/rate-limit/network/unknown policy remains
        # authoritative. Local pilot failures do not settle historical tasks.
        if (fault.category in ('auth','rate_limit','network') or
                fault.category in ('permission','unknown') and fault.category not in engine.policy.isolate_items):
            engine.block(fault,decide(fault,'collect'),spec.id)
        return .2
    except Exception:
        engine.state.write('disclosure_trial',{**state,'phase':'requires_review','reason':'observation_exception'})
        return .2
    attempt=engine.ledger.last_attempt('observe')
    row={**pending,'attempt_id':attempt['id'],'started_at':attempt['started_at'],'finished_at':attempt['finished_at'],
         'outcome':attempt['outcome'],'raw_receipt':result.record,'field_parse_complete':False,
         'semantic_state':'parse_pending','over_900':round_no>0 and attempt['finished_at']-
             next(r['started_at'] for r in records if r['job']==index and r['round']==round_no-1)>900}
    records.append(row)
    engine.state.write('disclosure_trial',{**state,'pending':None,'records':records,
        'phase':'complete' if len(records)==4 else 'waiting_next_completion_window'})
    return .2
