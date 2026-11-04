"""Verify original responses and port native rows/PDF sidecars into PMOS.

Only the private pure-validator snapshot is imported. Existing workers,
credentials, profiles and browser controllers are never imported here.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from .adapters.shaanxi_native.queue_validate import validate, export
from .contracts import ParsedArtifact, canonical
from .faults import Fault
from .shaanxi_transport import inside, atomic


def replay(input_file,source_root,*,destination=None,expected_validation=None):
    source_root=Path(source_root).resolve();source=inside(source_root,input_file)
    if source.is_symlink() or source.stat().st_size>16*1024*1024:
        raise Fault('native_validation_input_not_bounded','local')
    request=json.loads(source.read_text())
    # Keep source references bounded; all reads below are existing originals.
    for item in request.get('turn',{}).get('responses',[]):
        if item.get('responseFile'):
            inside(source_root,item['responseFile'])
    try:
        result=validate(request)
    except (ValueError,KeyError,TypeError,AttributeError):
        raise Fault('native_replay_contract_failed','site_data') from None
    digest=result.get('sha256') or result.get('response_sha256')
    matches=[x for x in request['turn']['responses'] if x.get('kind')=='data' and
             x.get('sha256')==digest and x.get('responseFile')]
    if len(matches)!=1:
        raise Fault('native_replay_primary_ambiguous','site_data')
    original=inside(source_root,matches[0]['responseFile']);raw=original.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:
        raise Fault('native_replay_primary_hash_changed','site_data')
    if expected_validation is not None:
        expected=json.loads(inside(source_root,expected_validation).read_text())
        if (expected.get('sha256') or expected.get('response_sha256'))!=digest:
            raise Fault('native_validation_primary_mismatch','site_data')
        for field in ('outcome','kind','rows','records','native_query'):
            if field in expected and field in result and canonical(expected[field])!=canonical(result[field]):
                raise Fault('native_validation_replay_result_changed','site_data')
    if destination is not None:
        target=Path(destination).resolve()
        if target==source_root or target.is_relative_to(source_root):
            raise Fault('replay_output_must_be_independent','local')
        target.mkdir(parents=True,exist_ok=True,mode=0o700)
        if result.get('kind')=='pdf_original':
            artifact=target/'original.pdf'
            if not artifact.exists():
                # Same filesystem: retain one immutable PDF inode, not two
                # full byte copies on mz's almost full root partition.
                try:
                    os.link(original,artifact)
                    fd=os.open(target,os.O_RDONLY)
                    try:os.fsync(fd)
                    finally:os.close(fd)
                except OSError:
                    pass  # export has a durable bounded fallback copy
        export(result,target,request.get('dataset'))
        atomic(target/'validated.json',result)
    rows=result.get('rows')
    if not isinstance(rows,list):rows=result.get('records',[])
    if not isinstance(rows,list):raise Fault('native_replay_rows_shape_changed','site_data')
    artifact=ParsedArtifact(rows,result['outcome']=='empty',unit=None,
        semantic_state='native_validated_fields_and_semantic_sidecars' if destination is not None
                       else 'native_validated_fields_replay_only')
    return {'parsed':artifact,'validation':result,'body':raw,'source_path':original,
            'response_status':matches[0]['status'],'source_sha256':digest,
            'parsed_sha256':hashlib.sha256(canonical(rows)).hexdigest(),
            'website_requests':0}
