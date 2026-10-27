"""Province profile (``provinces/<id>/province.json``) and its runtime instance.

Code is shared; everything that differs per province is data:

* ``province.json`` – site origin, browser attachment, pacing policy, session
  keeping and any extra notice texts.
* ``datasets/*.json`` – one spec per report (see spec.py).

Runtime files of one province live in its *instance* directory (default
``<repo>/var/<id>``, override with ``PMOS_DATA``): ``state/``, ``ledger.sqlite3``,
``original/``, ``samples/``, ``captures/``. Instances never share state, so one
process per province is the rule and provinces cannot disturb each other.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .pacing import Policy
from .spec import DatasetSpec, SpecError, load_all

REPO = Path(__file__).resolve().parents[1]
PROVINCES = REPO / "provinces"
ID = re.compile(r"[a-z][a-z0-9_]{1,31}")


class ProvinceError(ValueError):
    pass


@dataclass
class Province:
    id: str
    name: str
    origin: str
    policy: Policy
    browser: dict
    session: dict
    notices: dict = field(default_factory=dict)
    directory: Path = PROVINCES

    @classmethod
    def load(cls, province_id: str, root: Path = PROVINCES) -> "Province":
        if not ID.fullmatch(province_id):
            raise ProvinceError("province_id_invalid")
        directory = root / province_id
        path = directory / "province.json"
        if not path.exists():
            raise ProvinceError(f"unknown province {province_id}: {path} missing")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("id") != province_id:
            raise ProvinceError("province.json id must equal its directory")
        origin = value.get("origin", "")
        if not re.fullmatch(r"https://[a-z0-9.-]+", origin):
            raise ProvinceError("origin must be https://host without a path")
        return cls(id=province_id, name=value["name"], origin=origin,
                   policy=Policy.from_dict(value.get("policy", {})),
                   browser=value.get("browser", {}), session=value.get("session", {}),
                   notices=value.get("notices", {}), directory=directory)

    # -- datasets ---------------------------------------------------------
    @property
    def datasets_dir(self) -> Path:
        return self.directory / "datasets"

    def datasets(self) -> dict[str, DatasetSpec]:
        specs = load_all(self.datasets_dir) if self.datasets_dir.exists() else {}
        available = set(self.scripts())
        for spec in specs.values():
            missing = spec.scripts() - available
            if missing:
                raise SpecError(f"{spec.id}: page scripts missing in {self.directory / 'scripts'}: {sorted(missing)}")
        return specs

    def scripts(self) -> dict[str, str]:
        """Province page scripts: ``scripts/<name>.js``, one async function each.

        The escape hatch for page glue that declarative steps cannot express
        (e.g. opening a menu node of an encrypted menu tree). Reviewed code,
        kept with the province, referenced by name from specs.
        """
        directory = self.directory / "scripts"
        if not directory.exists():
            return {}
        return {path.stem: path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.js"))
                if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", path.stem)}

    def enabled(self) -> list[DatasetSpec]:
        """Collection order: the order given in province.json, then by id."""
        specs = [s for s in self.datasets().values() if s.status == "enabled"]
        order = {name: index for index, name in enumerate(self.session.get("collection_order", []))}
        return sorted(specs, key=lambda s: (order.get(s.id, len(order)), s.id))

    def keepalive_spec(self) -> DatasetSpec | None:
        name = self.session.get("keepalive_dataset")
        if not name:
            return None
        spec = self.datasets().get(name)
        if spec is None or not spec.keepalive:
            raise SpecError(f"keepalive_dataset {name} must exist, be enabled and set keepalive=true")
        return spec

    # -- runtime instance -------------------------------------------------
    def instance(self) -> Path:
        base = Path(os.environ.get("PMOS_DATA", REPO / "var"))
        return base / self.id

    def browser_config(self) -> dict:
        """province.json ``browser`` defaults, overridden per machine by
        ``<instance>/browser.json`` (profile path, CDP URL of that host)."""
        override = self.instance() / "browser.json"
        local = json.loads(override.read_text(encoding="utf-8")) if override.exists() else {}
        return {**self.browser, **local}

    def url(self, path: str) -> str:
        return self.origin + path


def province_ids(root: Path = PROVINCES) -> list[str]:
    return sorted(p.name for p in root.iterdir() if (p / "province.json").exists()) if root.exists() else []
