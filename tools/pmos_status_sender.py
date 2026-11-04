"""Run the original single sender, with a read-only PMOS owner annotation."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import argparse
import importlib.util
from pmos.shaanxi_reporting import wrap_existing_snapshot


def main():
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--legacy-root',type=Path,required=True);parser.add_argument('--pmos-instance',type=Path,required=True)
    args,remainder=parser.parse_known_args();directory=args.legacy_root.resolve()/'reporting'
    # Only the original sender reads its registered private configuration.
    # Nothing is copied or printed by this wrapper.
    sys.path.insert(0,str(directory))
    import snapshot as source_snapshot
    if (args.pmos_instance/'direct_config/report_baseline.json').is_file():
        from pmos.shaanxi_direct_reporting import snapshot as direct_snapshot
        source_snapshot.snapshot=lambda root:direct_snapshot(args.pmos_instance,source_snapshot.status_mail)
    else:source_snapshot.snapshot=wrap_existing_snapshot(source_snapshot,args.pmos_instance)
    spec=importlib.util.spec_from_file_location('existing_shaanxi_status_sender',directory/'sender.py')
    sender=importlib.util.module_from_spec(spec);spec.loader.exec_module(sender)
    from pmos.shaanxi_sender_policy import connection_failure_before_submission
    original_classifier=sender.connection_failed_before_submission
    sender.connection_failed_before_submission=lambda receipt,error:connection_failure_before_submission(original_classifier,receipt,error)
    sys.argv=[str(directory/'sender.py'),*remainder];sender.main()

if __name__=='__main__':main()
