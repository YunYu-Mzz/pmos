"""status_mail: the one status mail every province sends (user 2026-11-01, Anhui's template)."""
import unittest
from pathlib import Path

from pmos import status_mail as sm

CANONICAL = Path(sm.__file__)
CHECKOUT = CANONICAL.parents[2]
PROVINCES = ("anhui", "jibei", "liaoning", "shaanxi", "shanxi")


def legacy_metrics(**values):
    metrics = dict.fromkeys(sm.FIELDS)
    metrics.update(ordinary_originals=100, ordinary_bytes=1000, last_hour_files_mtime=0, missing_originals=1,
                   business_last_success_at="2026-11-01T01:00:00+00:00", counts_note="旧说明")
    metrics.update(values)
    return metrics


def pmos_status(**values):
    status = {"province": "anhui", "originals": 20, "bytes": 200, "last_hour_files_mtime": 5,
              "missing_originals": 2, "requests_last_hour": 7, "worker_pid": 4321, "phase": "collecting",
              "current_dataset": "load", "blocked": False, "block_reason": None, "held_datasets": ["a"],
              "tasks": {"saved": 20, "empty": 3, "pending": 9, "failed": 1, "deferred": 4},
              "business_last_success_at": "2026-11-01T05:00:00+00:00", "keeper_last_success_at": None,
              "since": "2026-11-01 12:31", "headline": "正常", "stalled_minutes": 0}
    status.update(values)
    return status


class Note(unittest.TestCase):
    def test_a_pmos_province_says_what_anhuis_mail_said(self):
        metrics = legacy_metrics()
        sm.apply_pmos(metrics, pmos_status(), label="安徽", running=True)
        self.assertEqual(metrics["counts_note"],
            "2026-11-01 12:31起安徽由PMOS采集（正常）。原件合计120份（旧系统冻结100份、PMOS20份；近1小时5份）；"
            "近1小时请求7次；任务完成20、空3、待采9、等待发布4；暂停栏目1个；阻断：无；停滞0分钟。"
            "按文件路径去重；近1小时按文件mtime估算。逐指标表仍为旧系统冻结统计。")
        self.assertEqual((metrics["ordinary_originals"], metrics["ordinary_bytes"], metrics["last_hour_files_mtime"],
                          metrics["missing_originals"]), (120, 1200, 5, 3))
        self.assertEqual((metrics["collector_state"], metrics["collector_pid"], metrics["collection_paused"],
                          metrics["blocked"], metrics["keeper_enabled"]), ("active", 4321, False, False, True))
        self.assertEqual((metrics["current_dataset"], metrics["phase"]), ("load", "collecting"))
        self.assertEqual(metrics["business_last_success_at"], "2026-11-01T05:00:00+00:00")

    def test_the_feishu_runtime_line_can_say_running(self):
        """report_layout.runtime_line shows 运行 only for exactly "active": "active(PMOS)" showed 待核验."""
        metrics = legacy_metrics()
        sm.apply_pmos(metrics, pmos_status(), label="安徽", running=True)
        self.assertEqual(metrics["collector_state"], "active")

    def test_a_stopped_or_blocked_pmos_is_not_called_normal(self):
        metrics = legacy_metrics()
        sm.apply_pmos(metrics, pmos_status(), label="安徽", running=False)
        self.assertEqual((metrics["collector_state"], metrics["collector_pid"], metrics["collection_paused"],
                          metrics["keeper_enabled"]), ("inactive", None, True, False))
        self.assertIn("安徽由PMOS采集（已停止）", metrics["counts_note"])
        metrics = legacy_metrics()
        sm.apply_pmos(metrics, pmos_status(blocked=True, block_reason="auth_expired", headline="已暂停：auth_expired"),
                      label="安徽", running=True)
        self.assertEqual((metrics["blocked"], metrics["keeper_enabled"]), (True, False))
        self.assertIn("（已暂停：auth_expired）", metrics["counts_note"])
        self.assertIn("阻断：auth_expired；", metrics["counts_note"])
        metrics = legacy_metrics()
        sm.apply_pmos(metrics, pmos_status(phase="paused"), label="安徽", running=True)
        self.assertTrue(metrics["collection_paused"])

    def test_an_older_pmos_success_does_not_replace_a_newer_legacy_one(self):
        metrics = legacy_metrics(business_last_success_at="2026-11-01T06:00:00+00:00")
        sm.apply_pmos(metrics, pmos_status(), label="安徽", running=True)
        self.assertEqual(metrics["business_last_success_at"], "2026-11-01T06:00:00+00:00")

    def test_an_unreadable_pmos_claims_nothing_about_the_collector(self):
        metrics = legacy_metrics(collector_state="stopped", collector_pid=1, blocked=False)
        sm.pmos_unreadable(metrics, label="山西")
        self.assertTrue(metrics["counts_note"].startswith("PMOS状态读取失败。山西由PMOS采集（状态未知）。原件合计未知份（旧系统冻结100份"))
        self.assertEqual((metrics["collector_state"], metrics["collector_pid"], metrics["blocked"]), (None, None, None))

    def test_a_legacy_province_says_the_same_parts_with_unknowns(self):
        metrics = legacy_metrics(collector_state="active", collection_paused=False, blocked=False, last_hour_files_mtime=3)
        self.assertEqual(sm.legacy_note(metrics, label="陕西", pending=12, dedupe="原件SHA"),
            "陕西由旧系统采集（正常）。原件合计100份（旧系统100份、PMOS0份；近1小时3份）；近1小时请求未知次；"
            "任务完成未知、空未知、待采12、等待发布未知；暂停栏目未知个；阻断：无；停滞未知分钟。"
            "按原件SHA去重；近1小时按文件mtime估算。")

    def test_a_legacy_headline_follows_the_metrics(self):
        self.assertEqual(sm.headline(legacy_metrics(blocked=True, collector_state="active")), "已暂停：阻断")
        self.assertEqual(sm.headline(legacy_metrics(collection_paused=True)), "已暂停")
        self.assertEqual(sm.headline(legacy_metrics(collector_state="inactive")), "已停止")
        self.assertEqual(sm.headline(legacy_metrics()), "状态未知")
        self.assertIn("阻断：原因未知", sm.legacy_note(legacy_metrics(blocked=True), label="冀北"))

    def test_the_note_fits_the_protocol(self):
        metrics = legacy_metrics()
        sm.apply_pmos(metrics, pmos_status(block_reason="x\n" * 400, blocked=True), label="安徽", running=True)
        self.assertLessEqual(len(metrics["counts_note"]), 500)
        self.assertFalse(any(ord(c) < 32 for c in metrics["counts_note"]))

    def test_only_the_twenty_fields_are_sent(self):
        metrics = legacy_metrics(all_originals=5, native_originals=2)
        self.assertEqual(tuple(sm.standard(metrics)), sm.FIELDS)
        self.assertEqual(len(sm.FIELDS), 20)

    def test_a_status_answer_with_a_bad_count_is_not_merged(self):
        self.assertTrue(sm.valid_pmos(pmos_status()))
        self.assertFalse(sm.valid_pmos(pmos_status(originals=-1)))
        self.assertFalse(sm.valid_pmos(pmos_status(requests_last_hour=None)))
        self.assertFalse(sm.valid_pmos([]))


class Body(unittest.TestCase):
    def test_the_mail_body_is_anhuis(self):
        report = {"generated_at": "2026-11-01T05:00:03+00:00",
                  "metrics": legacy_metrics(counts_note="说明")}
        self.assertEqual(sm.render(report, "安徽"),
            "安徽采集进度\n核验时间：2026-11-01T05:00:03+00:00\n原始载体/源回执：100份；1000字节\n说明\n"
            "逐指标统计通过签名schema2协议交由唯一汇总端写入原飞书表格。")
        report["metrics"].update(ordinary_originals=None, ordinary_bytes=None)
        self.assertIn("原始载体/源回执：未知份；未知字节", sm.render(report, "安徽"))


class Copies(unittest.TestCase):
    def test_every_province_carries_the_canonical_file_unchanged(self):
        canonical = CANONICAL.read_bytes()
        for province in PROVINCES:
            copy = CHECKOUT / province / "reporting" / "status_mail.py"
            if copy.exists():
                with self.subTest(province=province):
                    self.assertEqual(copy.read_bytes(), canonical, f"{copy}: copy pmos/pmos/status_mail.py verbatim")


if __name__ == "__main__":
    unittest.main()
