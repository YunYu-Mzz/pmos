#!/usr/bin/env python3
"""One check of a province's collection (run by a timer): see pmos/monitor.py. Sends mail; never touches the website.

    PMOS_DATA=/home/ecs-user/pmos-data python tools/pmos_monitor.py liaoning --unit pmos-liaoning --label 辽宁 \
        --mail-config /home/ecs-user/sd_crawler/liaoning/reporting-private/config.json

--dry-run prints the conditions and sends nothing. The mail account is the province's status-mail configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import stat
import subprocess
import sys
import time
from pathlib import Path

RELEASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RELEASE))

from pmos import monitor  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("province")
    parser.add_argument("--unit", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--mail-config", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-mail", action="store_true", help="send one mail saying so, to prove the channel; changes no state")
    args = parser.parse_args()
    instance = Path(os.environ["PMOS_DATA"]) / args.province
    unit = subprocess.run(["systemctl", "is-active", args.unit], capture_output=True, text=True, timeout=10).stdout.strip()
    now = time.time()
    found = monitor.conditions(instance, unit, now)
    if args.dry_run:
        print(json.dumps({"unit": unit, "conditions": found}, ensure_ascii=False, indent=2))
        return
    path = instance / "monitor.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state, mails = monitor.reconcile(state, found, now)
    if args.test_mail:
        state, mails = {"active": {"test": {"sent": None}}, "cleared": []}, [
            {"kind": "alert", "code": "test", "text": "这是一封测试邮件，告警通道正常，没有发生故障。", "since": now}]
    if mails:
        if args.mail_config.is_symlink() or stat.S_IMODE(args.mail_config.stat().st_mode) & 0o077:
            raise SystemExit("mail config must be a private regular file")
        mail = json.loads(args.mail_config.read_text(encoding="utf-8"))["mail"]
        try:
            with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=25, context=ssl.create_default_context()) as smtp:
                smtp.login(mail["address"], mail["authorization_code"])
                for item in mails:
                    smtp.send_message(monitor.message(item, args.label, mail["address"], mail["recipient"]))
                    monitor.mark_sent(state, item, now)
        except (OSError, smtplib.SMTPException) as exc:
            print(json.dumps({"mail_error": type(exc).__name__, "unsent": len(mails)}), file=sys.stderr)
    if args.test_mail:
        print(json.dumps({"test_mail_sent": state["active"]["test"]["sent"] is not None}))
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    print(json.dumps({"unit": unit, "conditions": sorted(found), "mails": len(mails)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
