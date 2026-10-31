"""Long-run memory for the worker process (ports of Shanxi production's 2026-10-31 fixes).

- ``install_asyncio_wait_fix``: CPython gh-152569 backport. Python 3.14.4
  leaves an await-graph edge on the losing, long-lived Future in
  ``asyncio.wait(FIRST_COMPLETED)``; Playwright's transport-error future then
  owns every completed API task and its stack until the connection closes.
- ``prepare_driver``: the Playwright driver loads ``browser/driver_memory.cjs``
  (bounded Request/Route/Frame dispatchers, private POST body copies) and a
  capped heap, through NODE_OPTIONS set before the binding starts it.
- ``Maintenance``: between ticks, at most once a minute, ``gc.collect`` plus
  glibc ``malloc_trim(0)`` and a count of the connection's objects.
- ``Admission`` (opt-in, ``browser.memory_guard``): business requests pause
  while the worker, browser, host or driver memory reserve is short.
- ``BrowserReclaim``: near the browser cgroup's limit, the page drops console
  entries and runs V8 GC (CDP only, never the site).

Nothing here sends a site request or touches the ledger or credentials.
"""
from __future__ import annotations

import asyncio
import collections
import ctypes
import gc
import hashlib
import inspect
import json
import os
import sys
import time
from pathlib import Path

DRIVER_MODULE = Path(__file__).resolve().parent / "browser" / "driver_memory.cjs"
NODE_FLAGS = ("--max-old-space-size=192", "--max-semi-space-size=8")

# inspect.getsource(asyncio.tasks._wait) of CPython 3.14.4, as reviewed in Shanxi production.
REVIEWED_WAIT_SHA = "f3e03fb80bc46666db3fba976e85794c5ccadfa3e38f20e936ca11777aa659bb"
MARKER = "_pmos_await_graph_cleanup"


def install_asyncio_wait_fix() -> str:
    if sys.version_info < (3, 14):
        return "not_affected"
    original = asyncio.tasks._wait
    if getattr(original, MARKER, False) or getattr(original, "_shanxi_await_graph_cleanup", False):
        return "already_installed"
    source = inspect.getsource(original)
    needle = "            f.remove_done_callback(_on_completion)\n"
    fixed = needle + "            futures.future_discard_from_awaited_by(f, cur_task)\n"
    if fixed in source:
        return "upstream_fixed"
    if hashlib.sha256(source.encode()).hexdigest() != REVIEWED_WAIT_SHA:
        raise RuntimeError("unreviewed_asyncio_wait_version")
    assert source.count(needle) == 1
    namespace: dict = {}
    exec(compile(source.replace(needle, fixed), __file__ + ":backport", "exec"), asyncio.tasks.__dict__, namespace)
    replacement = namespace["_wait"]
    setattr(replacement, MARKER, True)
    asyncio.tasks._wait = replacement
    return "installed_reviewed_upstream_fix"


def business_urls(province) -> list[str]:
    """Every site path a spec sends to (its listing, download, bootstrap), as full URLs."""
    paths = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("path", "template") and isinstance(item, str) and item.startswith("/"):
                    paths.add(item.split("?", 1)[0])
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for spec in province.datasets().values():
        walk(spec.request)
        walk(spec.documents or {})
    return sorted(province.origin + p for p in paths)


def prepare_driver(province, state_dir: Path) -> dict:
    """Before ``async_playwright().start()``: the driver it spawns loads the lifecycle module."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    urls = business_urls(province)
    temp = state_dir / "driver_business_urls.json.tmp"
    temp.write_text(json.dumps({"urls": urls}, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, state_dir / "driver_business_urls.json")
    options = os.environ.get("NODE_OPTIONS", "").split()
    wanted = [*NODE_FLAGS, f"--require={DRIVER_MODULE}"]
    os.environ["NODE_OPTIONS"] = " ".join([o for o in options if o not in wanted] + wanted)
    os.environ["PMOS_DRIVER_STATE"] = str(state_dir.resolve())
    return {"business_urls": len(urls), "node_options": os.environ["NODE_OPTIONS"]}


def rss() -> int | None:
    try:
        line = next(x for x in Path("/proc/self/status").read_text().splitlines() if x.startswith("VmRSS:"))
        return int(line.split()[1]) * 1024
    except (OSError, StopIteration, ValueError):
        return None


def reclaim() -> dict:
    collected = gc.collect()
    trimmed = False
    if sys.platform == "linux":
        try:
            trim = ctypes.CDLL(None).malloc_trim
            trim.argtypes, trim.restype = [ctypes.c_size_t], ctypes.c_int
            trimmed = bool(trim(0))  # only returns already-free glibc pages
        except (AttributeError, OSError):
            pass
    return {"unreachable_python_objects": collected, "allocator_pages_returned": trimmed}


def object_counts(browser) -> dict:
    """Counts only; never request arguments, page values or credentials."""
    connection = getattr(getattr(browser, "_impl_obj", None), "_connection", None)
    if connection is None:
        return {}
    future = connection._transport.on_error_future
    return {"connection_objects": dict(collections.Counter(type(o).__name__ for o in connection._objects.values())),
            "pending_callbacks": len(connection._callbacks),
            "transport_awaited_by_tasks": len(getattr(future, "_asyncio_awaited_by", ()) or ())}


class Maintenance:
    """Called by the engine between ticks; no operation is in progress then."""

    def __init__(self, state, browser=None, *, clock=time.monotonic, every: float = 60):
        self.state, self.browser, self.clock, self.every = state, browser, clock, every
        self.last = float("-inf")

    def __call__(self) -> None:
        now = self.clock()
        if now - self.last < self.every:
            return
        self.last = now
        before = rss()
        try:
            result = {"status": "reclaimed", **reclaim(), "before_rss": before, "after_rss": rss(),
                      **object_counts(self.browser)}
        except Exception as exc:
            # Optional: never changes a request outcome.
            result = {"status": "reclaim_unavailable", "error_type": type(exc).__name__}
        self.state.write("python_memory", {**result, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})


# -- memory admission (port of Shanxi production's memory_guard) ---------------
# Business requests pause while any reserve is short; the login keeps being
# renewed. Hysteresis: resume only above the larger reserves. A missing
# observation fails closed for business only. Opt-in per province:
# ``browser.memory_guard`` in province.json.
MIB = 1024 ** 2
STOP_RESERVES = {"worker": 192 * MIB, "browser": 96 * MIB, "host": 384 * MIB, "driver": 32 * MIB}
RESUME_RESERVES = {"worker": 256 * MIB, "browser": 160 * MIB, "host": 512 * MIB, "driver": 64 * MIB}
# The driver's RSS+swap includes code pages beyond its 192 MiB JS heap cap.
DRIVER_BUDGET = 256 * MIB
CGROUP_ROOT = Path("/sys/fs/cgroup")


def own_cgroup(proc: Path = Path("/proc"), root: Path = CGROUP_ROOT) -> Path:
    line = next(x for x in (proc / "self" / "cgroup").read_text().splitlines() if x.startswith("0::"))
    return root / line[3:].lstrip("/")


def cgroup_memory(group: Path) -> dict:
    current, maximum = (group / "memory.current").read_text().strip(), (group / "memory.max").read_text().strip()
    if not current.isdecimal() or not (maximum.isdecimal() or maximum == "max"):
        raise ValueError("invalid_memory_counter")
    pressure = dict(part.split("=") for part in (group / "memory.pressure").read_text().splitlines()[0].split()[1:])
    # Inactive file cache (Chrome's disk cache, written originals) is reclaimed
    # before any OOM: it is not used memory for the reserves.
    stat = group / "memory.stat"
    cache = 0
    if stat.exists():
        cache = int(dict(line.split() for line in stat.read_text().splitlines()).get("inactive_file", 0))
    value = {"current": int(current), "reclaimable_cache": cache, "pressure_avg10": float(pressure["avg10"])}
    if maximum != "max":
        value.update(maximum=int(maximum), headroom=int(maximum) - (int(current) - cache))
    return value


def host_available(meminfo: Path = Path("/proc/meminfo")) -> int:
    amount, unit = dict(x.split(":", 1) for x in meminfo.read_text().splitlines())["MemAvailable"].split()
    if unit != "kB" or int(amount) < 0:
        raise ValueError("invalid_host_counter")
    return int(amount) * 1024


def driver_usage(group: Path, proc: Path = Path("/proc")) -> dict:
    """The Playwright driver in the worker's cgroup; names and counters only (never argv/environ)."""
    found = []
    for pid in (group / "cgroup.procs").read_text().split():
        if not pid.isdecimal():
            raise ValueError("invalid_worker_pid")
        try:
            status = dict(x.split(":", 1) for x in (proc / pid / "status").read_text().splitlines())
            if status["Name"].strip() not in ("node", "MainThread"):
                continue
            if not str((proc / pid / "exe").resolve()).endswith("/playwright/driver/node"):
                continue
        except OSError:
            continue  # a process that just exited
        found.append({"pid": int(pid), "resident": int(status["VmRSS"].split()[0]) * 1024,
                      "swapped": int(status.get("VmSwap", "0 kB").split()[0]) * 1024})
    if len(found) != 1:
        raise ValueError("single_driver_required")
    driver = found[0]
    return {**driver, "budget": DRIVER_BUDGET, "headroom": DRIVER_BUDGET - driver["resident"] - driver["swapped"]}


def observe(config: dict, *, proc: Path = Path("/proc"), root: Path = CGROUP_ROOT,
            meminfo: Path = Path("/proc/meminfo")) -> dict:
    worker = own_cgroup(proc, root)
    values = {"worker": cgroup_memory(worker), "host": {"headroom": host_available(meminfo)},
              "driver": driver_usage(worker, proc)}
    if config.get("browser_cgroup"):
        values["browser"] = cgroup_memory(root / str(config["browser_cgroup"]).strip("/"))
    return values


def decide(values: dict, was_paused: bool = False) -> dict:
    reserves = RESUME_RESERVES if was_paused else STOP_RESERVES
    reasons = [f"{key}_memory_reserve" for key, minimum in reserves.items()
               if "headroom" in values.get(key, {}) and values[key]["headroom"] < minimum]
    return {"allowed": not reasons, "reasons": reasons}


class Admission:
    """Checked by the engine before each collection request."""

    def __init__(self, state, config: dict, *, observe=observe, clock=time.monotonic):
        self.state, self.config, self.observe, self.clock = state, config, observe, clock
        self.paused, self.written = False, (None, float("-inf"))

    def __call__(self) -> dict:
        try:
            values = self.observe(self.config)
            result = {**decide(values, self.paused), "observations": values}
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            result = {"allowed": False, "reasons": ["memory_observation_unavailable"]}
        self.paused = not result["allowed"]
        # One small latest snapshot: on every change, else once a minute.
        if self.written[0] != result["reasons"] or self.clock() - self.written[1] >= 60:
            self.state.write("memory_admission", {**result, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            self.written = (result["reasons"], self.clock())
        return result


class BrowserReclaim:
    """Between ticks, near the browser's cgroup limit: let the page drop console entries and run V8 GC.

    Port of Shanxi production's idle_browser_reclaim: never navigate,
    reconnect, clear storage or touch the site; at most every 5 minutes; after
    a failure it stays off for this process (no retry).
    """

    def __init__(self, state, page, config: dict, *, observe=cgroup_memory, available=host_available,
                 clock=time.monotonic):
        self.state, self.page, self.config = state, page, config
        self.observe, self.available, self.clock = observe, available, clock
        self.last, self.disabled = float("-inf"), False

    def needed(self, browser: dict) -> bool:
        return ("headroom" in browser and browser["headroom"] < 192 * MIB and self.available() >= 500 * MIB
                and 0 <= browser["pressure_avg10"] < 5)

    async def __call__(self) -> None:
        if self.disabled or not self.config.get("browser_cgroup") or self.clock() - self.last < 300:
            return
        group = CGROUP_ROOT / str(self.config["browser_cgroup"]).strip("/")
        try:
            before = self.observe(group)
            if not self.needed(before):
                return
        except (OSError, ValueError, KeyError):
            return
        self.last = self.clock()
        record = {"status": "completed", "before": before, "website_requests": 0}
        session = None
        try:
            session = await asyncio.wait_for(self.page.context.new_cdp_session(self.page), 5)
            await asyncio.wait_for(session.send("Runtime.discardConsoleEntries"), 5)
            await asyncio.wait_for(session.send("HeapProfiler.collectGarbage"), 30)
            record["after"] = self.observe(group)
        except Exception as exc:
            self.disabled = True
            record.update(status="failed_no_retry", error_type=type(exc).__name__)
        finally:
            if session is not None:
                try:
                    await asyncio.wait_for(session.detach(), 5)
                except Exception:
                    pass
        self.state.write("browser_reclaim", {**record, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})


class PageHeapRefresh:
    """Between ticks: reload the site tab when its JS heap has grown past a limit.

    The site's own app keeps every large answer (1000-row tables) in the main
    tab's heap: Shanxi's tab reached 549 MiB after ten hours and pushed the
    browser cgroup into swap; one in-place reload dropped it to 79 MiB and kept
    the login (2026-11-01 00:31). A reload sends only the page's own load
    requests, never a business call of ours. PMOS's app frames and captured
    templates die with the old document, as at a fresh start. Checked at most
    every ``check_every`` seconds, reloaded at most every ``min_gap``; after a
    failure it stays off for this process (no retry).
    """

    def __init__(self, state, page, transport, *, limit_mib: float = 300, check_every: float = 120,
                 min_gap: float = 600, settle: float = 8, clock=time.monotonic):
        self.state, self.page, self.transport = state, page, transport
        self.limit, self.check_every, self.min_gap, self.settle = limit_mib * MIB, check_every, min_gap, settle
        self.clock = clock
        self.checked = self.reloaded = float("-inf")
        self.disabled = False

    @property
    def current(self):
        return getattr(self.transport, "page", None) or self.page

    async def heap(self) -> int:
        return int(await asyncio.wait_for(self.current.evaluate(
            "() => performance.memory ? performance.memory.usedJSHeapSize : 0"), 10))

    async def __call__(self) -> None:
        now = self.clock()
        if self.disabled or now - self.checked < self.check_every or now - self.reloaded < self.min_gap:
            return
        self.checked = now
        try:
            before = await self.heap()
        except Exception:
            return
        if before < self.limit:
            return
        self.reloaded = now
        record = {"status": "reloaded", "before_mib": before >> 20, "website_business_requests": 0}
        try:
            await self.current.reload(wait_until="load", timeout=90_000)
            await asyncio.sleep(self.settle)
            templates = getattr(self.transport, "templates", None)
            if templates is not None:
                templates.items.clear()
            record["after_mib"] = (await self.heap()) >> 20
        except Exception as exc:
            self.disabled = True
            record.update(status="failed_no_retry", error_type=type(exc).__name__)
        self.state.write("page_heap_refresh", {**record, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        self.state.event("page_heap_refresh", **record)


# -- renderer watch (Anhui 2026-11-01: one renderer at 2.4 GB / 650% CPU) -----
def renderer_processes(profile_dir: str, run=None) -> list[dict]:
    """This browser's renderer processes (pid, rss MiB, cpu %), from the process table only."""
    import subprocess
    run = run or (lambda: subprocess.run(["ps", "axo", "pid=,rss=,%cpu=,command="], capture_output=True, text=True,
                                         timeout=10).stdout)
    marker = f"--user-data-dir={profile_dir}"
    found = []
    for line in run().splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and "--type=renderer" in parts[3] and marker in parts[3]:
            try:
                found.append({"pid": int(parts[0]), "rss_mb": int(parts[1]) // 1024, "cpu": float(parts[2])})
            except ValueError:
                continue
    return found


class RendererWatch:
    """Between ticks: record the heaviest renderer of this province's browser; an event above the limits.

    ``browser.renderer_alert``: {"rss_mb": ..., "cpu_percent": ...}. Same-site
    tabs may share one renderer, so the value is per process, not per page.
    """

    def __init__(self, state, profile_dir: str, config: dict, *, clock=time.monotonic, run=None):
        self.state, self.profile_dir, self.config, self.clock, self.run = state, profile_dir, config, clock, run
        self.last = self.alerted = float("-inf")

    def __call__(self) -> None:
        now = self.clock()
        if now - self.last < 60:
            return
        self.last = now
        try:
            found = renderer_processes(self.profile_dir, self.run)
        except Exception as exc:
            self.state.write("renderer_health", {"status": "unavailable", "error_type": type(exc).__name__})
            return
        top = max(found, key=lambda r: r["rss_mb"], default=None)
        record = {"status": "observed", "renderers": len(found), "max_rss_mb": top and top["rss_mb"],
                  "max_cpu": max((r["cpu"] for r in found), default=None), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.state.write("renderer_health", record)
        heavy = top is not None and (top["rss_mb"] >= self.config.get("rss_mb", 1536)
                                     or record["max_cpu"] >= self.config.get("cpu_percent", 300))
        if heavy and now - self.alerted >= 600:
            self.alerted = now
            self.state.event("renderer_heavy", max_rss_mb=record["max_rss_mb"], max_cpu=record["max_cpu"])
