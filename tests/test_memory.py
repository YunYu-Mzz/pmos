"""Long-run memory: asyncio backport, driver lifecycle module wiring, idle reclaim."""
import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pmos import memory
from pmos.province import Province
from pmos.state import StateStore

from helpers import Clock, TempDir

REPO = Path(__file__).resolve().parents[1]


def driver_dir():
    try:
        import playwright
    except ImportError:
        return None
    return Path(playwright.__file__).parent / "driver"


class AsyncioWaitFix(unittest.IsolatedAsyncioTestCase):
    async def test_completed_waits_leave_no_await_edges_on_a_long_lived_future(self):
        result = memory.install_asyncio_wait_fix()
        if sys.version_info < (3, 14):
            self.assertEqual(result, "not_affected")
            return
        self.assertIn(result, ("installed_reviewed_upstream_fix", "already_installed", "upstream_fixed"))
        long_lived = asyncio.get_running_loop().create_future()   # like Playwright's on_error_future

        async def call():
            done = asyncio.ensure_future(asyncio.sleep(0))
            await asyncio.wait([done, long_lived], return_when=asyncio.FIRST_COMPLETED)
        for _ in range(200):
            await asyncio.ensure_future(call())
        self.assertLess(len(getattr(long_lived, "_asyncio_awaited_by", ()) or ()), 5)
        self.assertIn(memory.install_asyncio_wait_fix(), ("already_installed", "upstream_fixed"))


class DriverWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        environ = patch.dict(os.environ, {})
        environ.start()
        self.addCleanup(environ.stop)

    def test_shanxi_business_urls_cover_listings_downloads_and_the_keepalive_export(self):
        province = Province.load("shanxi")
        urls = memory.business_urls(province)
        origin = province.origin
        self.assertIn(origin + "/px-settlement-infpubmeex/fileService/download", urls)
        self.assertIn(origin + "/px-settlement-infpubquery/ipDisclosureApply/getData", urls)
        keepalive = province.datasets()["queryMarketClearingRT"]
        self.assertIn(origin + keepalive.request["path"], urls)
        self.assertTrue(all(u.startswith(origin + "/") and "?" not in u for u in urls))

    def test_node_options_load_the_module_once_with_a_capped_heap(self):
        province = Province.load("shanxi")
        os.environ["NODE_OPTIONS"] = "--trace-warnings"
        memory.prepare_driver(province, self.tmp.path / "state")
        memory.prepare_driver(province, self.tmp.path / "state")
        options = os.environ["NODE_OPTIONS"].split()
        self.assertEqual(options.count(f"--require={memory.DRIVER_MODULE}"), 1)
        self.assertEqual(options[0], "--trace-warnings")
        self.assertIn("--max-old-space-size=192", options)
        self.assertTrue(memory.DRIVER_MODULE.is_file())
        self.assertEqual(os.environ["PMOS_DRIVER_STATE"], str((self.tmp.path / "state").resolve()))
        urls = json.loads((self.tmp.path / "state" / "driver_business_urls.json").read_text())["urls"]
        self.assertEqual(urls, memory.business_urls(province))

    @unittest.skipIf(driver_dir() is None, "playwright not installed")
    def test_the_real_driver_installs_the_module_at_start(self):
        from playwright.sync_api import sync_playwright
        state = self.tmp.path / "state"
        memory.prepare_driver(Province.load("shanxi"), state)
        with sync_playwright():
            report = json.loads((state / "driver_memory.json").read_text())
        self.assertEqual((report["version"], report["maintenanceErrors"]), (1, 0))
        self.assertGreater(report["businessUrls"], 10)

    @unittest.skipIf(driver_dir() is None, "playwright not installed")
    def test_driver_lifecycle_node_tests(self):
        node = driver_dir() / "node"
        result = subprocess.run([str(node), "--expose-gc", "--test", str(REPO / "tests" / "test_driver_memory.cjs")],
                                env={**os.environ, "TEST_PLAYWRIGHT_BUNDLE": str(driver_dir() / "package/lib/coreBundle.js"),
                                     "NODE_OPTIONS": ""}, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])


class Maintenance(unittest.TestCase):
    def test_reclaims_at_most_once_a_minute_and_records_counts_only(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        state, clock = StateStore(tmp.path / "state"), Clock()
        upkeep = memory.Maintenance(state, None, clock=clock)
        upkeep()
        first = state.read("python_memory")
        self.assertEqual(first["status"], "reclaimed")
        state.remove("python_memory")
        clock.advance(30)
        upkeep()
        self.assertIsNone(state.read("python_memory"))
        clock.advance(31)
        upkeep()
        self.assertIsNotNone(state.read("python_memory"))


class UpkeepAtAFastPace(unittest.IsolatedAsyncioTestCase):
    async def test_upkeep_still_runs_when_no_wait_between_requests_reaches_a_second(self):
        """1 s pace (user, 2026-10-31): every tick waits < 1 s, upkeep runs every UPKEEP_EVERY_SECONDS anyway."""
        from pmos.engine import UPKEEP_EVERY_SECONDS, Engine
        from helpers import FakeTransport, dataset, make_province
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        province = make_province(tmp.path, [dataset("d")], policy={"response_interval_seconds": 1})
        engine = Engine(province, FakeTransport(clock), instance=tmp.path / "var", clock=clock)
        ticks, calls = [], []

        async def tick():
            ticks.append(clock())
            clock.advance(1.5)            # one request and its 1 s spacing per tick
            return 0.001                  # never a whole second to wait
        engine.tick = tick
        await engine.run(asyncio.Event(), stop_when=lambda: len(ticks) >= 61, between=[lambda: calls.append(clock())])
        self.assertEqual(len(calls), 3, "the first tick, then every 30 s of ticks")
        self.assertTrue(all(b - a >= UPKEEP_EVERY_SECONDS for a, b in zip(calls, calls[1:])))


class RendererWatchTests(unittest.TestCase):
    def test_the_heaviest_renderer_of_this_browser_only_and_an_event_above_the_limits(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        state, clock = StateStore(tmp.path / "state"), Clock()
        table = "\n".join([
            " 101 2516582 650.0 /Edge Helper (Renderer) --type=renderer --user-data-dir=/p/anhui --foo",
            " 102  409600   3.0 /Edge Helper (Renderer) --type=renderer --user-data-dir=/p/anhui",
            " 103 9999999 900.0 /Chrome --type=renderer --user-data-dir=/p/other",
            " 104 1000000  10.0 /Edge --type=gpu-process --user-data-dir=/p/anhui"])
        watch = memory.RendererWatch(state, "/p/anhui", {"rss_mb": 1536, "cpu_percent": 300}, clock=clock, run=lambda: table)
        watch()
        self.assertEqual({k: state.read("renderer_health")[k] for k in ("renderers", "max_rss_mb", "max_cpu")},
                         {"renderers": 2, "max_rss_mb": 2457, "max_cpu": 650.0})
        events = [json.loads(line)["phase"] for line in (state.root / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events.count("renderer_heavy"), 1)
        clock.advance(120)
        watch()
        events = [json.loads(line)["phase"] for line in (state.root / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events.count("renderer_heavy"), 1, "at most one alert per 10 minutes")


MIB = 1024 ** 2


def fake_machine(root: Path, *, worker=(300, 640), browser=(800, 1536), available=2048, driver=(150, 0),
                 pressure=0.0):
    """A /proc + /sys/fs/cgroup tree: worker unit with one Playwright driver, a browser unit."""
    proc, cgroup = root / "proc", root / "cgroup"
    unit = cgroup / "user.slice" / "pmos-x.service"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text("0::/user.slice/pmos-x.service\n")
    for group, (current, maximum) in ((unit, worker), (cgroup / "system.slice" / "x-browser.service", browser)):
        group.mkdir(parents=True)
        (group / "memory.current").write_text(str(current * MIB))
        (group / "memory.max").write_text(str(maximum * MIB) if maximum else "max")
        (group / "memory.pressure").write_text(f"some avg10={pressure:.2f} avg60=0.00 avg300=0.00 total=1\n"
                                               "full avg10=0.00 avg60=0.00 avg300=0.00 total=1\n")
    (unit / "cgroup.procs").write_text("100\n200\n")
    exe = root / "venv" / "playwright" / "driver" / "node"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    for pid, name, target in (("100", "python3", root / "venv" / "python"), ("200", "MainThread", exe)):
        (proc / pid).mkdir()
        rss, swap = driver if pid == "200" else (90, 0)
        (proc / pid / "status").write_text(f"Name:\t{name}\nVmRSS:\t{rss * 1024} kB\nVmSwap:\t{swap * 1024} kB\n")
        (proc / pid / "exe").symlink_to(target)
    meminfo = root / "meminfo"
    meminfo.write_text(f"MemTotal: 4000000 kB\nMemAvailable: {available * 1024} kB\n")
    return {"proc": proc, "root": cgroup, "meminfo": meminfo}


class MemoryAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"browser_cgroup": "system.slice/x-browser.service"}

    def observe(self, **machine):
        paths = fake_machine(self.tmp.path / str(len(list(self.tmp.path.iterdir()))), **machine)
        return memory.observe(self.config, **paths)

    def test_reads_worker_browser_host_and_the_single_driver(self):
        values = self.observe()
        self.assertEqual(values["worker"]["headroom"], 340 * MIB)
        self.assertEqual(values["browser"]["headroom"], 736 * MIB)
        self.assertEqual(values["host"]["headroom"], 2048 * MIB)
        self.assertEqual((values["driver"]["pid"], values["driver"]["headroom"]), (200, 106 * MIB))
        self.assertEqual(memory.decide(values), {"allowed": True, "reasons": []})

    def test_each_short_reserve_pauses_business_with_hysteresis(self):
        cases = {"worker_memory_reserve": {"worker": (500, 640)}, "browser_memory_reserve": {"browser": (1480, 1536)},
                 "host_memory_reserve": {"available": 300}, "driver_memory_reserve": {"driver": (200, 40)}}
        for reason, machine in cases.items():
            self.assertEqual(memory.decide(self.observe(**machine))["reasons"], [reason], reason)
        # 120 MiB browser headroom: enough to keep going, not enough to resume a pause.
        between = self.observe(browser=(1416, 1536))
        self.assertTrue(memory.decide(between)["allowed"])
        self.assertFalse(memory.decide(between, was_paused=True)["allowed"])

    def test_inactive_file_cache_is_not_counted_against_a_reserve(self):
        root = self.tmp.path / "cache"
        paths = fake_machine(root, browser=(1480, 1536))
        (root / "cgroup" / "system.slice" / "x-browser.service" / "memory.stat").write_text(
            f"anon {900 * MIB}\ninactive_file {400 * MIB}\nactive_file {180 * MIB}\n")
        values = memory.observe(self.config, **paths)
        self.assertEqual(values["browser"]["headroom"], 456 * MIB, "56 MiB free plus 400 MiB reclaimable cache")
        self.assertTrue(memory.decide(values)["allowed"])

    def test_unlimited_cgroups_count_only_host_and_driver(self):
        values = self.observe(worker=(300, 0), browser=(800, 0))
        self.assertNotIn("headroom", values["worker"])
        self.assertTrue(memory.decide(values)["allowed"])

    def test_a_short_worker_reserve_first_asks_the_kernel_to_drop_cache(self):
        state = StateStore(self.tmp.path / "state")
        machines = [self.observe(worker=(500, 640)), self.observe(worker=(300, 640))]
        asked = []
        admission = memory.Admission(state, self.config, observe=lambda config: machines.pop(0),
                                     clock=Clock(), reclaim=lambda: asked.append(1) or True)
        result = admission()
        self.assertEqual((result["allowed"], asked), (True, [1]), "re-observed after the reclaim")
        self.assertFalse(state.read("memory_admission")["reasons"])

    def test_reclaim_is_rate_limited_and_only_for_the_worker_reserve(self):
        state = StateStore(self.tmp.path / "state")
        clock, asked = Clock(), []
        short = lambda config: self.observe(worker=(500, 640))
        admission = memory.Admission(state, self.config, observe=short, clock=clock,
                                     reclaim=lambda: asked.append(1) or False)
        self.assertFalse(admission()["allowed"])
        self.assertFalse(admission()["allowed"])
        self.assertEqual(len(asked), 1, "a failed reclaim is not retried within 5 minutes")
        clock.advance(memory.RECLAIM_EVERY + 1)
        admission()
        self.assertEqual(len(asked), 2)
        other = memory.Admission(state, self.config, observe=lambda config: self.observe(available=300),
                                 clock=Clock(), reclaim=lambda: asked.append(1) or True)
        self.assertEqual(other()["reasons"], ["host_memory_reserve"])
        self.assertEqual(len(asked), 2, "host shortage is not the worker cache")

    def test_a_missing_observation_pauses_business_and_is_recorded(self):
        state = StateStore(self.tmp.path / "state")
        admission = memory.Admission(state, self.config, observe=lambda config: memory.observe(
            config, proc=self.tmp.path / "missing", root=self.tmp.path, meminfo=self.tmp.path / "x"))
        self.assertEqual(admission(), {"allowed": False, "reasons": ["memory_observation_unavailable"]})
        self.assertEqual(state.read("memory_admission")["reasons"], ["memory_observation_unavailable"])


class FakeSession:
    def __init__(self, sent, fail=False):
        self.sent, self.fail = sent, fail

    async def send(self, method):
        if self.fail:
            raise RuntimeError("cdp")
        self.sent.append(method)

    async def detach(self):
        self.sent.append("detach")


class FakePage:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail
        page = self

        class Context:
            async def new_cdp_session(self, target):
                assert target is page
                return FakeSession(page.sent, page.fail)
        self.context = Context()


class BrowserReclaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_near_the_limit_the_page_drops_console_entries_and_collects_garbage_at_most_every_5_min(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        state, clock, page = StateStore(tmp.path / "state"), Clock(), FakePage()
        browser = {"current": 1400 * MIB, "maximum": 1536 * MIB, "headroom": 136 * MIB, "pressure_avg10": 0.0}
        reclaim = memory.BrowserReclaim(state, page, {"browser_cgroup": "system.slice/x.service"},
                                        observe=lambda group: browser, available=lambda: 1024 * MIB, clock=clock)
        await reclaim()
        self.assertEqual(page.sent, ["Runtime.discardConsoleEntries", "HeapProfiler.collectGarbage", "detach"])
        self.assertEqual(state.read("browser_reclaim")["website_requests"], 0)
        clock.advance(200)
        await reclaim()
        self.assertEqual(len(page.sent), 3, "not again within 5 minutes")
        browser.update(headroom=600 * MIB)
        clock.advance(200)
        await reclaim()
        self.assertEqual(len(page.sent), 3, "only near the limit")

    async def test_a_failure_turns_it_off_without_retry(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        state, clock, page = StateStore(tmp.path / "state"), Clock(), FakePage(fail=True)
        browser = {"headroom": 100 * MIB, "pressure_avg10": 0.0}
        reclaim = memory.BrowserReclaim(state, page, {"browser_cgroup": "x"}, observe=lambda group: browser,
                                        available=lambda: 1024 * MIB, clock=clock)
        await reclaim()
        self.assertEqual(state.read("browser_reclaim")["status"], "failed_no_retry")
        self.assertEqual(page.sent, ["detach"])
        clock.advance(400)
        page.fail = False
        await reclaim()
        self.assertEqual(page.sent, ["detach"], "no second session")


if __name__ == "__main__":
    unittest.main()
