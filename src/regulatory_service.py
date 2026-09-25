"""离岛免税电子标签监管服务。

围绕“逐件可核对”组织关系：

    商品型号 ─ 进口批次 ─ 成分/原产地/中文说明/通关单证（资料）
             └ 标签草案 ─ 资料审核 ─ 签发（不可变正式版本）
                        └ 二维码载体（始终指向一个正式版本）
                                 └ 实物件（序列号）─ 仓位/移动/核放/召回

全部状态变化以追加事件落盘，重启回放后优先恢复未完成出区核放与召回通知。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .event_store import EventStore

# ---------------------------------------------------------------------------
# 角色与异常
# ---------------------------------------------------------------------------

ROLE_ENTERPRISE = "免税经营企业"
ROLE_REVIEWER = "资料审核"
ROLE_ISSUER = "标签签发"
ROLE_WAREHOUSE = "仓库人员"
ROLE_CUSTOMS = "海关监管人员"
ROLE_DRUG = "药品监管人员"

DOC_COMPOSITION = "成分声明"
DOC_ORIGIN = "原产地证明"
DOC_INSTRUCTIONS = "中文说明"
DOC_CUSTOMS = "通关单证"
DOC_TYPES = (DOC_COMPOSITION, DOC_ORIGIN, DOC_INSTRUCTIONS, DOC_CUSTOMS)

# 消费者公开视图允许出现的字段（白名单），配方附件等绝不进入。
_PUBLIC_DOC_FIELDS = {
    DOC_COMPOSITION: ("ingredients",),
    DOC_ORIGIN: ("origin", "certificate_no"),
    DOC_INSTRUCTIONS: ("chinese_name", "instructions"),
    DOC_CUSTOMS: ("declaration_no", "summary"),
}


class Violation(ValueError):
    """业务规则被违反；调用方应据此拒绝操作。"""


@dataclass(frozen=True)
class Actor:
    role: str
    officer_id: str

    def __str__(self) -> str:
        return f"{self.role}:{self.officer_id}"


# ---------------------------------------------------------------------------
# 内存投影
# ---------------------------------------------------------------------------


@dataclass
class Unit:
    serial: str
    product_model: str
    batch_id: str
    code: str
    version_id: str
    location: str
    # 在区 / 已出区 / 退运 / 销毁
    status: str = "在区"
    sales_status: str | None = None  # 待售 / 已售
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Projection:
    seq: int = 0
    products: dict[str, dict[str, Any]] = field(default_factory=dict)
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    drafts: dict[str, dict[str, Any]] = field(default_factory=dict)
    versions: dict[str, dict[str, Any]] = field(default_factory=dict)
    product_versions: dict[str, list[str]] = field(default_factory=dict)
    carriers: dict[str, dict[str, Any]] = field(default_factory=dict)
    batches: dict[str, dict[str, Any]] = field(default_factory=dict)
    units: dict[str, Unit] = field(default_factory=dict)
    releases: dict[str, dict[str, Any]] = field(default_factory=dict)
    result_refs: dict[str, str] = field(default_factory=dict)
    recalls: dict[str, dict[str, Any]] = field(default_factory=dict)

    def current_version(self, product_model: str) -> dict[str, Any] | None:
        ids = self.product_versions.get(product_model)
        return self.versions[ids[-1]] if ids else None


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = str(sorted(payload.items()))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


class _Projector:
    """把事件流更新到内存投影；回放与实时追加走同一份代码。"""

    def __init__(self, p: Projection) -> None:
        self.p = p

    def apply(self, e: dict[str, Any]) -> None:
        kind = e["type"]
        p = self.p
        p.seq = e["seq"]

        if kind == "ProductRegistered":
            p.products[e["model"]] = {
                "model": e["model"],
                "chinese_name": e["chinese_name"],
                "brand": e["brand"],
            }
        elif kind == "DocumentSubmitted":
            p.documents[e["doc_id"]] = {
                "doc_id": e["doc_id"],
                "product_model": e["product_model"],
                "doc_type": e["doc_type"],
                "title": e["title"],
                "public": e["public"],
                "confidential_attachment": e.get("confidential_attachment"),
                "replaces": e.get("replaces"),
                "status": "待确认",
                "submitted_by": e["actor"],
            }
        elif kind == "DocumentConfirmed":
            doc = p.documents[e["doc_id"]]
            doc["status"] = "已确认"
            doc["confirmed_by"] = e["actor"]
            if doc["replaces"]:
                old = p.documents.get(doc["replaces"])
                if old:
                    old["status"] = "已被替代"
        elif kind == "LabelDraftCreated":
            p.drafts[e["draft_id"]] = {
                "draft_id": e["draft_id"],
                "product_model": e["product_model"],
                "bindings": dict(e["bindings"]),
                "status": "待审核",
                "created_by": e["actor"],
                "reviewer": None,
                "review_note": None,
            }
        elif kind == "LabelDraftReviewed":
            draft = p.drafts[e["draft_id"]]
            draft["status"] = "审核通过" if e["decision"] == "通过" else "审核驳回"
            draft["reviewer"] = e["actor"]
            draft["review_note"] = e.get("note")
        elif kind == "LabelVersionIssued":
            p.versions[e["version_id"]] = dict(e["snapshot"])
            p.product_versions.setdefault(e["product_model"], []).append(e["version_id"])
            draft = p.drafts[e["draft_id"]]
            draft["status"] = "已签发"
            draft["issued_version"] = e["version_id"]
        elif kind == "CarrierRegistered":
            p.carriers[e["code"]] = {
                "code": e["code"],
                "version_id": e["version_id"],
                "status": "有效",
                "replaced_by": None,
            }
        elif kind == "ImportReceived":
            batch = {
                "batch_id": e["batch_id"],
                "product_model": e["product_model"],
                "customs_doc_id": e["customs_doc_id"],
                "location": e["location"],
                "frozen": False,
                "freeze_reason": None,
                "parent": None,
            }
            p.batches[e["batch_id"]] = batch
            for item in e["items"]:
                unit = Unit(
                    serial=item["serial"],
                    product_model=e["product_model"],
                    batch_id=e["batch_id"],
                    code=item["code"],
                    version_id=item["version_id"],
                    location=e["location"],
                )
                unit.history.append(
                    {
                        "seq": e["seq"],
                        "action": "入区",
                        "detail": {
                            "batch": e["batch_id"],
                            "location": e["location"],
                            "version": item["version_id"],
                        },
                    }
                )
                p.units[item["serial"]] = unit
        elif kind == "CarrierReplaced":
            old = p.carriers[e["old_code"]]
            old["status"] = "已停用"
            old["replaced_by"] = e["new_code"]
            p.carriers[e["new_code"]] = {
                "code": e["new_code"],
                "version_id": e["version_id"],
                "status": "有效",
                "replaced_by": None,
            }
            unit = p.units[e["serial"]]
            unit.code = e["new_code"]
            unit.version_id = e["version_id"]
            unit.history.append(
                {
                    "seq": e["seq"],
                    "action": "破损补发",
                    "detail": {
                        "old_code": e["old_code"],
                        "new_code": e["new_code"],
                        "old_version": e["old_version_id"],
                        "version": e["version_id"],
                        "reason": e["reason"],
                    },
                }
            )
        elif kind == "UnitsRepacked":
            for new_batch_id, codes in e["splits"]:
                parent = p.batches[e["batch_id"]]
                p.batches[new_batch_id] = {
                    "batch_id": new_batch_id,
                    "product_model": parent["product_model"],
                    "customs_doc_id": parent["customs_doc_id"],
                    "location": e["location"],
                    "frozen": False,
                    "freeze_reason": None,
                    "parent": e["batch_id"],
                }
                for code in codes:
                    unit = p.units[self._serial_by_code(code)]
                    unit.batch_id = new_batch_id
                    unit.location = e["location"]
                    unit.history.append(
                        {"seq": e["seq"], "action": "分装", "detail": {"from": e["batch_id"], "to": new_batch_id}}
                    )
        elif kind == "UnitsTransferred":
            for serial in e["serials"]:
                unit = p.units[serial]
                unit.location = e["to_location"]
                unit.history.append(
                    {"seq": e["seq"], "action": "换仓", "detail": {"to": e["to_location"]}}
                )
        elif kind == "UnitsReturned":
            for serial in e["serials"]:
                unit = p.units[serial]
                unit.status = "退运"
                unit.history.append({"seq": e["seq"], "action": "退运", "detail": {"reason": e["reason"]}})
        elif kind == "UnitsDestroyed":
            for serial in e["serials"]:
                unit = p.units[serial]
                unit.status = "销毁"
                unit.history.append({"seq": e["seq"], "action": "销毁", "detail": {"reason": e["reason"]}})
        elif kind == "BatchFrozen":
            batch = p.batches[e["batch_id"]]
            batch["frozen"] = True
            batch["freeze_reason"] = e["reason"]
        elif kind == "BatchUnfrozen":
            batch = p.batches[e["batch_id"]]
            batch["frozen"] = False
            batch["freeze_reason"] = None
        elif kind == "ReleaseDeclared":
            p.releases[e["release_id"]] = {
                "release_id": e["release_id"],
                "status": "待核放",
                "serials": list(e["serials"]),
                "customs_doc_id": e["customs_doc_id"],
                "result_ref": e["result_ref"],
                "locked_versions": dict(e["locked_versions"]),
                "declared_by": e["actor"],
                "block_reason": None,
            }
            p.result_refs[e["result_ref"]] = e["release_id"]
        elif kind == "ReleaseBlocked":
            p.releases[e["release_id"]]["status"] = "人工处理"
            p.releases[e["release_id"]]["block_reason"] = e["reason"]
        elif kind == "ReleaseCleared":
            rel = p.releases[e["release_id"]]
            rel["status"] = "已核放"
            rel["cleared_by"] = e["actor"]
            for serial in rel["serials"]:
                unit = p.units[serial]
                unit.status = "已出区"
                unit.sales_status = "待售"
                unit.history.append(
                    {
                        "seq": e["seq"],
                        "action": "海关核放出区",
                        "detail": {"release": e["release_id"], "version": rel["locked_versions"][serial]},
                    }
                )
        elif kind == "UnitSold":
            unit = p.units[e["serial"]]
            unit.sales_status = "已售"
            unit.history.append({"seq": e["seq"], "action": "销售", "detail": {}})
        elif kind == "RecallStarted":
            p.recalls[e["recall_id"]] = {
                "recall_id": e["recall_id"],
                "scope": dict(e["scope"]),
                "reason": e["reason"],
                "serials": list(e["serials"]),
                "notified": [],
                "started_by": e["actor"],
            }
        elif kind == "ConsumerNotified":
            p.recalls[e["recall_id"]]["notified"].append(e["serial"])
        else:  # pragma: no cover - 未知事件说明流已损坏
            raise Violation(f"未知事件类型: {kind}")

    def _serial_by_code(self, code: str) -> str:
        for unit in self.p.units.values():
            if unit.code == code:
                return unit.serial
        raise Violation(f"载体未绑定实物: {code}")


# ---------------------------------------------------------------------------
# 监管服务
# ---------------------------------------------------------------------------


class RegulatoryService:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.p = Projection()
        self._projector = _Projector(self.p)
        self.store.subscribe(self._projector.apply)
        self.store.replay()

    # -- 基础 ----------------------------------------------------------------

    def _emit(self, type_: str, **data: Any) -> dict[str, Any]:
        self.p.seq += 1
        event = {"seq": self.p.seq, "type": type_, **data}
        self.store.append(event)
        return event

    @staticmethod
    def _require(actor: Actor, role: str) -> None:
        if actor.role != role:
            raise Violation(f"该操作仅允许{role}执行，当前为{actor.role}")

    def _product(self, model: str) -> dict[str, Any]:
        if model not in self.p.products:
            raise Violation(f"商品型号未登记: {model}")
        return self.p.products[model]

    def _doc_confirmed(self, doc_id: str, product_model: str, doc_type: str) -> dict[str, Any]:
        doc = self.p.documents.get(doc_id)
        if not doc:
            raise Violation(f"资料不存在: {doc_id}")
        if doc["product_model"] != product_model:
            raise Violation(f"资料{doc_id}不属于商品{product_model}")
        if doc["doc_type"] != doc_type:
            raise Violation(f"资料{doc_id}类型应为{doc_type}")
        if doc["status"] != "已确认":
            raise Violation(f"资料{doc_id}尚未经资料审核确认，签发人不得引用")
        return doc

    def _unit(self, serial: str) -> Unit:
        unit = self.p.units.get(serial)
        if not unit:
            raise Violation(f"实物件不存在: {serial}")
        return unit

    # -- 商品与资料 -----------------------------------------------------------

    def register_product(self, model: str, chinese_name: str, brand: str, *, actor: Actor) -> None:
        self._require(actor, ROLE_ENTERPRISE)
        if model in self.p.products:
            raise Violation(f"商品型号已登记: {model}")
        self._emit(
            "ProductRegistered", model=model, chinese_name=chinese_name, brand=brand, actor=str(actor)
        )

    def submit_document(
        self,
        doc_id: str,
        product_model: str,
        doc_type: str,
        title: str,
        public: dict[str, Any],
        *,
        confidential_attachment: str | None = None,
        replaces: str | None = None,
        actor: Actor,
    ) -> None:
        self._require(actor, ROLE_ENTERPRISE)
        self._product(product_model)
        if doc_type not in DOC_TYPES:
            raise Violation(f"不支持的资料类型: {doc_type}")
        if doc_id in self.p.documents:
            raise Violation(f"资料编号已存在: {doc_id}")
        unknown = set(public) - set(_PUBLIC_DOC_FIELDS[doc_type])
        if unknown:
            raise Violation(f"{doc_type}公开字段超出白名单: {sorted(unknown)}")
        if replaces and replaces not in self.p.documents:
            raise Violation(f"被替代资料不存在: {replaces}")
        self._emit(
            "DocumentSubmitted",
            doc_id=doc_id,
            product_model=product_model,
            doc_type=doc_type,
            title=title,
            public=dict(public),
            confidential_attachment=confidential_attachment,
            replaces=replaces,
            actor=str(actor),
        )

    def confirm_document(self, doc_id: str, *, actor: Actor) -> None:
        self._require(actor, ROLE_REVIEWER)
        doc = self.p.documents.get(doc_id)
        if not doc:
            raise Violation(f"资料不存在: {doc_id}")
        if doc["status"] != "待确认":
            raise Violation(f"资料{doc_id}当前状态为{doc['status']}，不能确认")
        self._emit("DocumentConfirmed", doc_id=doc_id, actor=str(actor))

    # -- 标签草案、审核与签发 --------------------------------------------------

    def create_draft(
        self, draft_id: str, product_model: str, bindings: dict[str, str], *, actor: Actor
    ) -> None:
        self._require(actor, ROLE_ENTERPRISE)
        self._product(product_model)
        if draft_id in self.p.drafts:
            raise Violation(f"草案编号已存在: {draft_id}")
        if set(bindings) != set(DOC_TYPES):
            raise Violation("标签草案必须绑定成分声明、原产地证明、中文说明、通关单证四类资料")
        for doc_type, doc_id in bindings.items():
            # 草案建得上，但引用关系必须真实；是否“已确认”在签发时再次校验。
            doc = self.p.documents.get(doc_id)
            if not doc or doc["product_model"] != product_model or doc["doc_type"] != doc_type:
                raise Violation(f"草案绑定的{doc_type}资料无效: {doc_id}")
        self._emit(
            "LabelDraftCreated",
            draft_id=draft_id,
            product_model=product_model,
            bindings={k: bindings[k] for k in DOC_TYPES},
            actor=str(actor),
        )

    def review_draft(self, draft_id: str, decision: str, note: str = "", *, actor: Actor) -> None:
        self._require(actor, ROLE_REVIEWER)
        draft = self.p.drafts.get(draft_id)
        if not draft:
            raise Violation(f"标签草案不存在: {draft_id}")
        if decision not in ("通过", "驳回"):
            raise Violation("审核结论只能是通过或驳回")
        if draft["status"] != "待审核":
            raise Violation(f"草案{draft_id}已处理，不能重复审核")
        if decision == "通过":
            for doc_type, doc_id in draft["bindings"].items():
                if self.p.documents[doc_id]["status"] != "已确认":
                    raise Violation(f"{doc_type}资料{doc_id}未经确认，审核不得通过")
        self._emit(
            "LabelDraftReviewed",
            draft_id=draft_id,
            decision=decision,
            note=note,
            actor=str(actor),
        )

    def issue_label(self, draft_id: str, *, version_id: str, actor: Actor) -> str:
        """签发不可变正式版本；返回版本号。签发只引用已确认资料。"""
        self._require(actor, ROLE_ISSUER)
        draft = self.p.drafts.get(draft_id)
        if not draft:
            raise Violation(f"标签草案不存在: {draft_id}")
        if draft["status"] != "审核通过":
            raise Violation(f"草案{draft_id}未经资料审核通过，签发人不能签发")
        if draft["reviewer"] is not None and draft["reviewer"].split(":", 1)[1] == actor.officer_id:
            raise Violation("资料审核与标签签发必须由不同人员承担")
        if version_id in self.p.versions:
            raise Violation(f"正式版本号已存在: {version_id}")

        product_model = draft["product_model"]
        docs: dict[str, dict[str, Any]] = {}
        for doc_type, doc_id in draft["bindings"].items():
            docs[doc_type] = self._doc_confirmed(doc_id, product_model, doc_type)

        snapshot = {
            "version_id": version_id,
            "product_model": product_model,
            "issued_by": str(actor),
            "reviewed_by": draft["reviewer"],
            "chinese_name": docs[DOC_INSTRUCTIONS]["public"].get("chinese_name"),
            "ingredients": list(docs[DOC_COMPOSITION]["public"]["ingredients"]),
            "origin": docs[DOC_ORIGIN]["public"]["origin"],
            "instructions": docs[DOC_INSTRUCTIONS]["public"].get("instructions", ""),
            "clearance": {
                "declaration_no": docs[DOC_CUSTOMS]["public"].get("declaration_no", ""),
                "summary": docs[DOC_CUSTOMS]["public"].get("summary", ""),
            },
            "doc_refs": {t: d["doc_id"] for t, d in docs.items()},
        }
        snapshot["fingerprint"] = _fingerprint({k: str(v) for k, v in snapshot.items()})

        # 成分更正：新版本替代了旧成分声明时，只冻结仍在区且挂旧版本的批次。
        old_version = self.p.current_version(product_model)
        self._emit(
            "LabelVersionIssued",
            version_id=version_id,
            product_model=product_model,
            draft_id=draft_id,
            snapshot=snapshot,
            actor=str(actor),
        )

        if old_version is not None:
            old_comp = old_version["doc_refs"][DOC_COMPOSITION]
            new_comp = snapshot["doc_refs"][DOC_COMPOSITION]
            if old_comp != new_comp and self.p.documents[new_comp].get("replaces") == old_comp:
                for batch_id, batch in self.p.batches.items():
                    if batch["product_model"] != product_model or batch["frozen"]:
                        continue
                    affected = any(
                        u.status == "在区" and u.version_id == old_version["version_id"]
                        for u in self._batch_units(batch_id)
                    )
                    if affected:
                        self.freeze_batch(batch_id, f"成分更正：{old_comp}→{new_comp}", actor=Actor(ROLE_DRUG, "system"))
        return version_id

    # -- 二维码载体与入区 ------------------------------------------------------

    def register_carrier(self, code: str, product_model: str, *, actor: Actor) -> str:
        """二维码载体注册到商品当前正式版本；一个码只能指向一个正式版本。"""
        self._require(actor, ROLE_ENTERPRISE)
        if code in self.p.carriers:
            raise Violation(f"码值已注册: {code}")
        version = self.p.current_version(product_model)
        if not version:
            raise Violation(f"商品{product_model}尚无正式标签版本，不能投放二维码")
        self._emit("CarrierRegistered", code=code, version_id=version["version_id"], actor=str(actor))
        return version["version_id"]

    def receive_import(
        self,
        batch_id: str,
        product_model: str,
        customs_doc_id: str,
        location: str,
        codes: list[str],
        *,
        actor: Actor,
    ) -> None:
        self._require(actor, ROLE_WAREHOUSE)
        self._product(product_model)
        if batch_id in self.p.batches:
            raise Violation(f"进口批次已存在: {batch_id}")
        customs = self._doc_confirmed(customs_doc_id, product_model, DOC_CUSTOMS)
        if not codes:
            raise Violation("入区数量不能为空")
        items = []
        seen = set()
        for code in codes:
            carrier = self.p.carriers.get(code)
            if not carrier:
                raise Violation(f"码值未注册: {code}")
            if carrier["status"] != "有效":
                raise Violation(f"码值{code}已失效，不能用于入区")
            if code in seen:
                raise Violation(f"码值重复: {code}")
            seen.add(code)
            version = self.p.versions[carrier["version_id"]]
            if version["product_model"] != product_model:
                raise Violation(f"码值{code}指向的商品型号与批次不符")
            if any(u.code == code for u in self.p.units.values()):
                raise Violation(f"码值{code}已绑定实物")
            # 序列号在入区时确定且终身不变；码值可因破损补发而替换。
            serial = f"{batch_id}#{len(items) + 1:04d}"
            items.append({"serial": serial, "code": code, "version_id": carrier["version_id"]})
        self._emit(
            "ImportReceived",
            batch_id=batch_id,
            product_model=product_model,
            customs_doc_id=customs["doc_id"],
            location=location,
            items=items,
            actor=str(actor),
        )

    # -- 件数守恒的库存移动 ----------------------------------------------------

    def _live_units(self, serials: list[str], *, must_in_zone: bool = True) -> list[Unit]:
        units = [self._unit(s) for s in serials]
        for u in units:
            if must_in_zone and u.status != "在区":
                raise Violation(f"实物{u.serial}状态为{u.status}，不能在库内移动或出区")
            batch = self.p.batches[u.batch_id]
            if batch["frozen"]:
                raise Violation(f"批次{u.batch_id}已冻结（{batch['freeze_reason']}），实物{u.serial}不得移动")
        return units

    def _batch_units(self, batch_id: str) -> list[Unit]:
        return [u for u in self.p.units.values() if u.batch_id == batch_id]

    def repack(self, batch_id: str, splits: list[tuple[str, list[str]]], *, location: str, actor: Actor) -> None:
        """分装：实物与标签身份一起进入子批次，件数保持守恒。"""
        self._require(actor, ROLE_WAREHOUSE)
        if batch_id not in self.p.batches:
            raise Violation(f"批次不存在: {batch_id}")
        all_codes: list[str] = []
        for new_batch_id, codes in splits:
            if new_batch_id in self.p.batches:
                raise Violation(f"目标批次已存在: {new_batch_id}")
            if not codes:
                raise Violation("分装目标批次不能为空")
            all_codes.extend(codes)
        if len(all_codes) != len(set(all_codes)):
            raise Violation("分装清单存在重复实物")
        current = {u.code for u in self._batch_units(batch_id) if u.status == "在区"}
        if set(all_codes) - current:
            raise Violation("分装实物不属于该批次或已不在区")
        self._emit(
            "UnitsRepacked", batch_id=batch_id, splits=[(b, c) for b, c in splits],
            location=location, actor=str(actor),
        )

    def transfer(self, serials: list[str], to_location: str, *, actor: Actor) -> None:
        self._require(actor, ROLE_WAREHOUSE)
        units = self._live_units(serials)
        if not serials or len(serials) != len(set(serials)):
            raise Violation("换仓清单为空或重复")
        if all(u.location == to_location for u in units):
            raise Violation("实物已在目标仓位")
        self._emit("UnitsTransferred", serials=list(serials), to_location=to_location, actor=str(actor))

    def return_units(self, serials: list[str], reason: str, *, actor: Actor) -> None:
        self._require(actor, ROLE_WAREHOUSE)
        self._live_units(serials)
        self._emit("UnitsReturned", serials=list(serials), reason=reason, actor=str(actor))

    def destroy_units(self, serials: list[str], reason: str, *, actor: Actor) -> None:
        self._require(actor, ROLE_WAREHOUSE)
        self._live_units(serials)
        self._emit("UnitsDestroyed", serials=list(serials), reason=reason, actor=str(actor))

    def relabel_unit(self, serial: str, new_code: str, *, actor: Actor) -> None:
        """破损补发：新码接续旧码全部历史，旧载体立即停用。"""
        self._require(actor, ROLE_WAREHOUSE)
        unit = self._unit(serial)
        if new_code in self.p.carriers:
            raise Violation(f"新码值已被使用: {new_code}")
        version = self.p.current_version(unit.product_model)
        if not version:
            raise Violation("商品无正式版本，无法补发")
        old_code = unit.code
        old_version_id = unit.version_id
        self._emit("CarrierRegistered", code=new_code, version_id=version["version_id"], actor=str(actor))
        self._emit(
            "CarrierReplaced",
            serial=serial,
            old_code=old_code,
            new_code=new_code,
            version_id=version["version_id"],
            old_version_id=old_version_id,
            reason="破损补发",
            actor=str(actor),
        )

    # -- 冻结 ----------------------------------------------------------------

    def freeze_batch(self, batch_id: str, reason: str, *, actor: Actor) -> None:
        # 抽检异常与成分更正联动均以药品监管人员身份冻结。
        self._require(actor, ROLE_DRUG)
        if batch_id not in self.p.batches:
            raise Violation(f"批次不存在: {batch_id}")
        if self.p.batches[batch_id]["frozen"]:
            raise Violation(f"批次{batch_id}已处于冻结状态")
        self._emit("BatchFrozen", batch_id=batch_id, reason=reason, actor=str(actor))

    def unfreeze_batch(self, batch_id: str, *, actor: Actor) -> None:
        self._require(actor, ROLE_DRUG)
        if not self.p.batches[batch_id]["frozen"]:
            raise Violation(f"批次{batch_id}未冻结")
        self._emit("BatchUnfrozen", batch_id=batch_id, actor=str(actor))

    # -- 海关核放 -------------------------------------------------------------

    def declare_release(
        self,
        release_id: str,
        codes: list[str],
        customs_doc_id: str,
        result_ref: str,
        *,
        actor: Actor,
    ) -> dict[str, Any]:
        """申报出区核放，锁定每件实物当前码实际指向的标签版本。

        重复提交同一核验结果编号是幂等的：返回既有核放单，不再扣减库存。
        """
        self._require(actor, ROLE_CUSTOMS)
        if result_ref in self.p.result_refs:
            existing = self.p.releases[self.p.result_refs[result_ref]]
            submitted = {self._serial_by_code(c) for c in codes}
            if set(existing["serials"]) != submitted:
                raise Violation("同一核验结果编号对应的实物范围不一致，拒绝重复申报")
            return existing

        if release_id in self.p.releases:
            raise Violation(f"核放单编号已存在: {release_id}")
        if not codes or len(codes) != len(set(codes)):
            raise Violation("核放实物清单为空或重复")

        serials: list[str] = []
        locked: dict[str, str] = {}
        for code in codes:
            carrier = self.p.carriers.get(code)
            if not carrier or carrier["status"] != "有效":
                raise Violation(f"码值{code}无效，不能核放")
            serial = self._serial_by_code(code)
            unit = self.p.units[serial]
            if unit.code != code or unit.status != "在区":
                raise Violation(f"码值{code}未对应在区实物")
            serials.append(unit.serial)
            locked[unit.serial] = carrier["version_id"]
        self._emit(
            "ReleaseDeclared",
            release_id=release_id,
            serials=serials,
            customs_doc_id=customs_doc_id,
            result_ref=result_ref,
            locked_versions=locked,
            actor=str(actor),
        )
        return self.p.releases[release_id]

    def _serial_by_code(self, code: str) -> str:
        unit = self.p.units.get(code)
        if unit:
            return unit.serial
        for candidate in self.p.units.values():
            if candidate.code == code:
                return candidate.serial
        raise Violation(f"码值未绑定实物: {code}")

    def customs_clear(self, release_id: str, *, actor: Actor) -> dict[str, Any]:
        """海关实际核放：版本必须仍是锁定版本，单证矛盾则停人工处理。"""
        self._require(actor, ROLE_CUSTOMS)
        rel = self.p.releases.get(release_id)
        if not rel:
            raise Violation(f"核放单不存在: {release_id}")
        if rel["status"] == "已核放":
            return rel  # 幂等：重复核放不重复扣减

        def block(reason: str) -> dict[str, Any]:
            self._emit("ReleaseBlocked", release_id=release_id, reason=reason, actor=str(actor))
            return self.p.releases[release_id]

        for serial in rel["serials"]:
            unit = self.p.units[serial]
            if unit.status != "在区":
                return block(f"实物{serial}已不在区，件数状态矛盾")
            batch = self.p.batches[unit.batch_id]
            if batch["frozen"]:
                return block(f"批次{unit.batch_id}被冻结：{batch['freeze_reason']}")
            # 锁定版本必须与海关实际核验时码指向的版本一致。
            actual = self.p.carriers[unit.code]["version_id"]
            if actual != rel["locked_versions"][serial]:
                return block(
                    f"实物{serial}标签版本已变化：核验{rel['locked_versions'][serial]} / 当前{actual}"
                )
            # 矛盾单证：核放引用单证与入区通关单证不一致。
            if rel["customs_doc_id"] != batch["customs_doc_id"]:
                return block(
                    f"实物{serial}通关单证矛盾：核放{rel['customs_doc_id']} / 入区{batch['customs_doc_id']}"
                )
            if self.p.documents[rel["customs_doc_id"]]["status"] != "已确认":
                return block(f"通关单证{rel['customs_doc_id']}未经确认")

        self._emit("ReleaseCleared", release_id=release_id, actor=str(actor))
        return self.p.releases[release_id]

    # -- 销售与召回 ------------------------------------------------------------

    def sell_unit(self, serial: str, *, actor: Actor) -> None:
        self._require(actor, ROLE_ENTERPRISE)
        unit = self._unit(serial)
        if unit.status != "已出区":
            raise Violation("只有已出区商品可以销售")
        if unit.sales_status == "已售":
            raise Violation("实物已销售，不能重复销售")
        self._emit("UnitSold", serial=serial, actor=str(actor))

    def start_recall(self, recall_id: str, scope: dict[str, str], reason: str, *, actor: Actor) -> None:
        """启动召回，定位已出区实物的销售状态；通知进度按件跟踪。"""
        self._require(actor, ROLE_DRUG)
        if recall_id in self.p.recalls:
            raise Violation(f"召回编号已存在: {recall_id}")
        if "batch_id" in scope:
            if scope["batch_id"] not in self.p.batches:
                raise Violation("召回批次不存在")
            hit = lambda u: u.batch_id == scope["batch_id"]  # noqa: E731
        elif "product_model" in scope:
            self._product(scope["product_model"])
            hit = lambda u: u.product_model == scope["product_model"]  # noqa: E731
        else:
            raise Violation("召回范围必须指定批次或商品型号")
        serials = sorted(s for s, u in self.p.units.items() if u.status == "已出区" and hit(u))
        self._emit(
            "RecallStarted",
            recall_id=recall_id,
            scope=dict(scope),
            reason=reason,
            serials=serials,
            actor=str(actor),
        )

    def notify_consumer(self, recall_id: str, serial: str, *, actor: Actor) -> None:
        self._require(actor, ROLE_DRUG)
        recall = self.p.recalls.get(recall_id)
        if not recall:
            raise Violation(f"召回不存在: {recall_id}")
        if serial not in recall["serials"]:
            raise Violation(f"实物{serial}不在召回范围")
        if serial in recall["notified"]:
            raise Violation(f"实物{serial}已通知，不能重复通知")
        self._emit("ConsumerNotified", recall_id=recall_id, serial=serial, actor=str(actor))

    # -- 消费者扫码（公开视图）--------------------------------------------------

    def consumer_view(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """从正式版本快照中按白名单摘取知情所需内容，配方附件绝不出现。"""
        return {
            "商品名称": snapshot["chinese_name"],
            "成分": list(snapshot["ingredients"]),
            "原产地": snapshot["origin"],
            "中文说明": snapshot["instructions"],
            "通关信息": dict(snapshot["clearance"]),
            "标签版本": snapshot["version_id"],
        }

    def consumer_scan(self, code: str) -> dict[str, Any]:
        carrier = self.p.carriers.get(code)
        if not carrier:
            return {"status": "无效码", "提示": "未查询到该标签信息"}
        if carrier["status"] != "有效":
            return {
                "status": "标签载体已停用",
                "提示": "该标签已因破损补发停用，请勿据此购买，可联系销售方核验",
            }
        snapshot = self.p.versions[carrier["version_id"]]
        view = self.consumer_view(snapshot)
        for recall in self.p.recalls.values():
            if code in recall["serials"] or self._serial_by_code_safe(code) in recall["serials"]:
                view["召回提示"] = recall["reason"]
        return view

    def _serial_by_code_safe(self, code: str) -> str | None:
        for unit in self.p.units.values():
            if code in (unit.code, unit.serial):
                return unit.serial
        return None

    # -- 监管按序列查询与恢复 ---------------------------------------------------

    def stock(self, batch_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for unit in self._batch_units(batch_id):
            counts[unit.status] = counts.get(unit.status, 0) + 1
        return counts

    def serial_query(self, serial: str) -> dict[str, Any]:
        """按商品序列汇总：审批依据、码值替换、库存去向、各时点消费者可见信息。"""
        unit = self._unit(serial)
        code_chain: list[dict[str, Any]] = []
        code = unit.code
        # 从当前码沿替换关系回溯。
        for h in unit.history:
            if h["action"] == "破损补发":
                code_chain.append(
                    {"code": h["detail"]["old_code"], "停用时点": h["seq"], "去向": f"由{h['detail']['new_code']}接续"}
                )
        code_chain.append({"code": unit.code, "停用时点": None, "去向": "当前有效载体"})

        approval = []
        seen_versions: set[str] = set()
        for h in unit.history:
            detail = h["detail"]
            if detail.get("version"):
                seen_versions.add(detail["version"])
            if detail.get("old_version"):
                seen_versions.add(detail["old_version"])
        for ver_id in sorted(seen_versions):
            snap = self.p.versions[ver_id]
            approval.append(
                {
                    "version_id": ver_id,
                    "审核人": snap["reviewed_by"],
                    "签发人": snap["issued_by"],
                    "依据资料": [
                        {"类型": t, "编号": d, "确认人": self.p.documents[d]["confirmed_by"]}
                        for t, d in snap["doc_refs"].items()
                    ],
                    "内容指纹": snap["fingerprint"],
                }
            )

        # 逐移动时点给出消费者当时扫码可见内容。
        views = []
        for h in unit.history:
            ver_id = self._version_at(unit.serial, h["seq"])
            views.append(
                {
                    "时点": h["seq"],
                    "事件": h["action"],
                    "消费者可见": self.consumer_view(self.p.versions[ver_id]),
                }
            )

        recall_info = None
        for recall in self.p.recalls.values():
            if serial in recall["serials"]:
                recall_info = {
                    "recall_id": recall["recall_id"],
                    "销售状态": unit.sales_status,
                    "通知状态": "已通知" if serial in recall["notified"] else "待通知",
                }

        return {
            "serial": serial,
            "商品型号": unit.product_model,
            "当前批次": unit.batch_id,
            "当前仓位": unit.location,
            "实物状态": unit.status,
            "销售状态": unit.sales_status,
            "批次冻结": self.p.batches[unit.batch_id]["frozen"],
            "审批依据": approval,
            "码值替换": code_chain,
            "库存去向": list(unit.history),
            "各时点消费者可见信息": views,
            "召回": recall_info,
        }

    def _version_at(self, serial: str, seq: int) -> str:
        """重放到 seq 时刻，返回该实物码当时指向的正式版本。"""
        p = Projection()
        projector = _Projector(p)
        for event in self.store.all_events():
            if event["seq"] > seq:
                break
            projector.apply(event)
        unit = p.units.get(serial)
        if not unit:
            # 入区事件本身：入区时码指向的版本即载体注册版本。
            raise Violation("内部错误：时点早于实物入区")
        return p.carriers[unit.code]["version_id"]

    def recall_progress(self, recall_id: str) -> dict[str, Any]:
        recall = self.p.recalls[recall_id]
        rows = []
        for serial in recall["serials"]:
            u = self.p.units[serial]
            rows.append({"serial": serial, "销售状态": u.sales_status, "通知": "已通知" if serial in recall["notified"] else "待通知"})
        return {
            "recall_id": recall_id,
            "总数": len(recall["serials"]),
            "已通知": len(recall["notified"]),
            "待通知": len(recall["serials"]) - len(recall["notified"]),
            "明细": rows,
        }

    def pending_recovery(self) -> dict[str, list[str]]:
        """重启后优先恢复：未完成出区核放，以及召回中尚未通知的实物。"""
        pending_releases = sorted(
            rid for rid, r in self.p.releases.items() if r["status"] in ("待核放", "人工处理")
        )
        pending_notify: list[str] = []
        for recall in self.p.recalls.values():
            for serial in recall["serials"]:
                if serial not in recall["notified"]:
                    pending_notify.append(f"{recall['recall_id']}:{serial}")
        return {"未完成出区核放": pending_releases, "待召回通知": sorted(pending_notify)}
