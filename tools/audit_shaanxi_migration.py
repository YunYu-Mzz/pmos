"""Read-only identity audit and one native-response replay per shape."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import argparse
from collections import Counter
import hashlib,json,datetime
from pmos.migration.shaanxi import audit
from pmos.shaanxi_replay import replay


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source-root',type=Path,required=True);ap.add_argument('--report',type=Path,required=True)
    args=ap.parse_args();source=args.source_root.resolve();report=args.report.resolve()
    if not report.is_relative_to(ROOT) or report.is_relative_to(source):raise ValueError('independent_report_required')
    raw=(source/'state/collection_queue/journal.json').read_bytes();journal=json.loads(raw);registry=json.loads((source/'config/spot_registry.json').read_text());datasets={d['id']:d for d in registry['datasets']}
    value=audit(journal,'shaanxi-production');value.update(at=datetime.datetime.now(datetime.timezone.utc).isoformat(),snapshot_sha256=hashlib.sha256(raw).hexdigest(),rolling_view=True)
    samples={};errors={}
    for job in sorted(journal['jobs'].values(),key=lambda j:str(j.get('finishedAt','')),reverse=True):
        if job.get('status') not in {'saved','empty'}:continue
        family=datasets.get(job['datasetId'],{}).get('family');kind=job['kind'];group='pdf_download' if kind=='download' else str(family)+'/'+kind
        if group in samples or group in errors:continue
        f=source/'state/collection_queue/tasks'/job['key']/'validation_input.json'
        if not f.is_file():continue
        try:
            validation=job.get('validation')
            if not validation:raise ValueError('original_validation_reference_missing')
            result=replay(f,source,expected_validation=validation)
            samples[group]={'job_key':job['key'],'input_ref':str(f),'source_sha256':result['source_sha256'],
                'parsed_sha256':result['parsed_sha256'],'qualification':'single_original_replay_only',**result['parsed'].summary()}
        except Exception as exc:
            errors[group]={'job_key':job['key'],'error_type':type(exc).__name__,'error_code':getattr(exc,'code','replay_failed')}
    value['native_replays']=samples;value['native_replay_errors']=errors
    value['unmapped_by_kind_state']=dict(Counter(j['kind']+'/'+j['status'] for k,j in journal['jobs'].items() if k in set(value['mapping_errors'])))
    report.parent.mkdir(parents=True,exist_ok=True);report.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'records':value['records'],'mapped':value['mapped'],'unmapped':value['unmapped'],
        'original_replay_groups':len(samples),'replay_errors':errors,'website_requests':0},ensure_ascii=False))

if __name__=='__main__':main()
