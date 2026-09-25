"""监管服务的持久化：整体 JSON 快照，支持应用重启后恢复。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import (
    Batch,
    Carrier,
    Item,
    LabelDraft,
    LabelVersion,
    Material,
    Movement,
    ProductModel,
    Recall,
    RecallItem,
    Release,
)

SCHEMA_VERSION = 1


class Store:
    """全部领域对象的内存容器，可整体序列化为 JSON 快照。"""

    def __init__(self) -> None:
        self.models: dict[str, ProductModel] = {}
        self.batches: dict[str, Batch] = {}
        self.materials: dict[str, Material] = {}
        self.drafts: dict[str, LabelDraft] = {}
        self.versions: dict[str, LabelVersion] = {}
        self.items: dict[str, Item] = {}
        self.carriers: dict[str, Carrier] = {}  # 以码值为键
        self.movements: list[Movement] = []
        self.releases: dict[str, Release] = {}
        self.release_keys: dict[str, str] = {}  # 幂等键 -> 出区申请编号
        self.recalls: dict[str, Recall] = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "models": {k: asdict(v) for k, v in self.models.items()},
            "batches": {k: asdict(v) for k, v in self.batches.items()},
            "materials": {k: asdict(v) for k, v in self.materials.items()},
            "drafts": {k: asdict(v) for k, v in self.drafts.items()},
            "versions": {k: asdict(v) for k, v in self.versions.items()},
            "items": {k: asdict(v) for k, v in self.items.items()},
            "carriers": {k: asdict(v) for k, v in self.carriers.items()},
            "movements": [asdict(m) for m in self.movements],
            "releases": {k: asdict(v) for k, v in self.releases.items()},
            "release_keys": dict(self.release_keys),
            "recalls": {k: asdict(v) for k, v in self.recalls.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Store":
        if data.get("schema") != SCHEMA_VERSION:
            raise ValueError("存储快照版本不受支持")
        store = cls()
        store.models = {k: ProductModel(**v) for k, v in data["models"].items()}
        store.batches = {k: Batch(**v) for k, v in data["batches"].items()}
        store.materials = {k: Material(**v) for k, v in data["materials"].items()}
        store.drafts = {k: LabelDraft(**v) for k, v in data["drafts"].items()}
        store.versions = {k: LabelVersion(**v) for k, v in data["versions"].items()}
        store.items = {k: Item(**v) for k, v in data["items"].items()}
        store.carriers = {k: Carrier(**v) for k, v in data["carriers"].items()}
        store.movements = [Movement(**m) for m in data["movements"]]
        store.releases = {k: Release(**v) for k, v in data["releases"].items()}
        store.release_keys = dict(data["release_keys"])
        store.recalls = {
            k: Recall(
                **{
                    **v,
                    "items": {s: RecallItem(**entry) for s, entry in v["items"].items()},
                }
            )
            for k, v in data["recalls"].items()
        }
        return store

    def save(self, path: Path) -> None:
        """原子写入快照，避免崩溃留下半个文件。"""
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=1)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)

    @classmethod
    def load(cls, path: Path) -> "Store":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)
