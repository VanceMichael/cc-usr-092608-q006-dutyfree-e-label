"""离岛免税电子标签监管服务。

一条主线：商品型号 → 进口批次 → 逐件实物 → 标签载体（码值），
资料（成分声明、原产地证明、中文说明、通关单证、配方附件）挂在批次上，
标签草案只能引用已确认资料，经与审核分离的签发职责生成不可回改的正式版本；
二维码始终解析到批次当前正式版本，消费者视图只含知情所需内容。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .errors import (
    FrozenError,
    NotFoundError,
    SeparationOfDutiesError,
    StateError,
    ValidationError,
)
from .models import (
    PUBLIC_MATERIAL_KINDS,
    Batch,
    BatchStatus,
    Carrier,
    CarrierStatus,
    DraftStatus,
    Item,
    ItemStatus,
    LabelDraft,
    LabelVersion,
    Material,
    MaterialKind,
    MaterialStatus,
    Movement,
    MovementKind,
    ProductModel,
    Recall,
    RecallItem,
    RecallStatus,
    Release,
    ReleaseStatus,
    VersionStatus,
)
from .store import Store

#: 标签草案必须引用的公开资料种类（通关单证可在出区前以新版本补齐）。
DRAFT_REQUIRED_KINDS = (
    MaterialKind.INGREDIENT,
    MaterialKind.ORIGIN,
    MaterialKind.MANUAL,
)


def _fingerprint(content: dict[str, Any]) -> str:
    import json

    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class LabelService:
    """监管应用服务；所有状态变更结束后均可整体持久化并在重启时恢复。"""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # ------------------------------------------------------------------ 档案

    def register_model(
        self, model_id: str, name: str, actor: str, at: str
    ) -> ProductModel:
        if model_id in self.store.models:
            raise ValidationError("商品型号已存在")
        model = ProductModel(id=model_id, name=name, registered_by=actor, registered_at=at)
        self.store.models[model_id] = model
        return model

    def register_batch(
        self,
        batch_id: str,
        model_id: str,
        quantity: int,
        location: str,
        operator: str,
        at: str,
    ) -> Batch:
        """入区登记：生成批次与逐件实物，记录入区流水。"""
        if model_id not in self.store.models:
            raise NotFoundError("商品型号不存在")
        if batch_id in self.store.batches:
            raise ValidationError("批次已存在")
        if quantity <= 0:
            raise ValidationError("入区数量必须为正")
        batch = Batch(id=batch_id, model_id=model_id, quantity=quantity, location=location)
        self.store.batches[batch_id] = batch
        serials: list[str] = []
        for index in range(1, quantity + 1):
            serial = f"{batch_id}-{index:05d}"
            self.store.items[serial] = Item(
                serial=serial,
                model_id=model_id,
                batch_id=batch_id,
                location=location,
            )
            serials.append(serial)
        self.store.movements.append(
            Movement(
                id=f"MV-{len(self.store.movements) + 1:06d}",
                kind=MovementKind.INBOUND,
                serials=serials,
                at=at,
                operator=operator,
                to_location=location,
                detail={"quantity": quantity},
            )
        )
        return batch

    # ------------------------------------------------------------------ 资料

    def submit_material(
        self,
        material_id: str,
        batch_id: str,
        kind: MaterialKind,
        content: dict[str, Any],
        actor: str,
        at: str,
    ) -> Material:
        if batch_id not in self.store.batches:
            raise NotFoundError("批次不存在")
        if material_id in self.store.materials:
            raise ValidationError("资料编号已存在")
        if not isinstance(kind, MaterialKind):
            raise ValidationError("资料种类无效")
        material = Material(
            id=material_id,
            batch_id=batch_id,
            kind=kind.value,
            content=dict(content),
            submitted_by=actor,
            submitted_at=at,
        )
        self.store.materials[material_id] = material
        return material

    def review_material(
        self,
        material_id: str,
        reviewer: str,
        approved: bool,
        at: str,
        note: str | None = None,
    ) -> Material:
        """资料审核职责：确认或驳回；审核人不得是提交人。"""
        material = self._material(material_id)
        if material.status != MaterialStatus.SUBMITTED:
            raise StateError("资料已审核，不能重复审核")
        if reviewer == material.submitted_by:
            raise SeparationOfDutiesError("资料审核人与提交人不能为同一人")
        material.status = (
            MaterialStatus.CONFIRMED if approved else MaterialStatus.REJECTED
        ).value
        material.reviewed_by = reviewer
        material.reviewed_at = at
        material.review_note = note
        return material

    # ------------------------------------------------------------ 草案与签发

    def create_draft(
        self,
        draft_id: str,
        batch_id: str,
        refs: dict[MaterialKind, str],
        actor: str,
        at: str,
    ) -> LabelDraft:
        """建立标签草案；引用的资料必须全部属于本批次且已经确认。"""
        if batch_id not in self.store.batches:
            raise NotFoundError("批次不存在")
        if draft_id in self.store.drafts:
            raise ValidationError("草案编号已存在")
        kinds = {kind for kind in refs}
        missing = {k.value for k in DRAFT_REQUIRED_KINDS if k not in kinds}
        if missing:
            raise ValidationError(f"草案缺少必要资料：{sorted(missing)}")
        normalized: dict[str, str] = {}
        for kind, material_id in refs.items():
            if not isinstance(kind, MaterialKind):
                raise ValidationError("资料种类无效")
            material = self._material(material_id)
            if kind is MaterialKind.FORMULA:
                raise ValidationError("配方附件为企业保密资料，不得进入标签")
            if material.kind != kind.value:
                raise ValidationError("资料种类与引用不一致")
            if material.batch_id != batch_id:
                raise ValidationError("资料与草案批次不一致")
            if material.status != MaterialStatus.CONFIRMED:
                raise StateError("签发人只能引用已确认资料")
            normalized[kind.value] = material_id
        draft = LabelDraft(
            id=draft_id,
            batch_id=batch_id,
            refs=normalized,
            created_by=actor,
            created_at=at,
        )
        self.store.drafts[draft_id] = draft
        return draft

    def issue_version(
        self, version_id: str, draft_id: str, issuer: str, at: str
    ) -> LabelVersion:
        """签发职责：从已确认资料生成正式版本；签发人与建草案人、各资料审核人分离。"""
        draft = self._draft(draft_id)
        if draft.status != DraftStatus.DRAFT:
            raise StateError("草案已签发或已作废")
        if issuer == draft.created_by:
            raise SeparationOfDutiesError("签发人不能同时是草案建单人")
        referenced = [self._material(mid) for mid in draft.refs.values()]
        reviewers = {m.reviewed_by for m in referenced}
        if issuer in reviewers:
            raise SeparationOfDutiesError("签发人与资料审核人不能为同一人")

        batch = self._batch(draft.batch_id)
        previous = self.effective_version(batch.id)
        number = (
            max(
                (v.number for v in self.store.versions.values() if v.batch_id == batch.id),
                default=0,
            )
            + 1
        )
        public = self._build_public_view(batch.model_id, referenced)
        checksums = {m.id: _fingerprint(m.content) for m in referenced}
        version = LabelVersion(
            id=version_id,
            batch_id=batch.id,
            number=number,
            draft_id=draft.id,
            issuer=issuer,
            issued_at=at,
            public=public,
            material_ids=[m.id for m in referenced],
        )
        self.store.versions[version_id] = version
        draft.status = DraftStatus.ISSUED.value
        if previous is not None:
            previous.status = VersionStatus.SUPERSEDED.value
        return version

    def _build_public_view(
        self, model_id: str, materials: list[Material]
    ) -> dict[str, Any]:
        """只组装消费者知情所需内容；配方附件永远不会出现在这里。"""
        by_kind = {m.kind: m for m in materials}
        view: dict[str, Any] = {
            "model": self.store.models[model_id].name,
            "ingredients": by_kind[MaterialKind.INGREDIENT.value].content["ingredients"],
            "origin": by_kind[MaterialKind.ORIGIN.value].content["origin"],
            "manual_zh": by_kind[MaterialKind.MANUAL.value].content["text"],
        }
        customs = by_kind.get(MaterialKind.CUSTOMS.value)
        if customs is not None:
            view["customs"] = {
                "doc_no": customs.content["doc_no"],
                "origin": customs.content.get("origin"),
            }
        return view

    def _batch_chain(self, batch_id: str) -> list[str]:
        """批次及其分装来源链（子→父）。"""
        chain: list[str] = []
        current: str | None = batch_id
        while current is not None:
            chain.append(current)
            current = self._batch(current).parent_batch_id
        return chain

    def effective_version(self, batch_id: str) -> LabelVersion | None:
        for bid in self._batch_chain(batch_id):
            version = next(
                (
                    v
                    for v in self.store.versions.values()
                    if v.batch_id == bid and v.status == VersionStatus.EFFECTIVE
                ),
                None,
            )
            if version is not None:
                return version
        return None

    def effective_version_at(self, batch_id: str, at: str) -> LabelVersion | None:
        """某一时点消费者扫码能够读到的版本（沿分装谱系、按版本号与签发时间推导）。"""
        candidates = [
            v
            for bid in self._batch_chain(batch_id)
            for v in self.store.versions.values()
            if v.batch_id == bid and v.issued_at <= at
        ]
        return max(candidates, key=lambda v: (v.issued_at, v.number), default=None)

    # ------------------------------------------------------------------ 码值

    def issue_carrier(
        self, code: str, serial: str, operator: str, at: str, reason: str = "初次贴标"
    ) -> Carrier:
        item = self._item(serial)
        existing = self._active_carrier(serial)
        if existing is not None:
            raise StateError("该件已有有效载体，补发应使用换码流程")
        if code in self.store.carriers:
            raise ValidationError("码值已被使用")
        carrier = Carrier(code=code, serial=serial, issued_at=at, reason=reason)
        self.store.carriers[code] = carrier
        return carrier

    def replace_carrier(
        self, old_code: str, new_code: str, reason: str, operator: str, at: str
    ) -> Carrier:
        """破损补发：旧载体立即失效，新码接续同一实物的全部历史。"""
        old = self.store.carriers.get(old_code)
        if old is None:
            raise NotFoundError("旧码值不存在")
        if old.status != CarrierStatus.ACTIVE:
            raise StateError("旧码值已失效，不能再次补发")
        if new_code in self.store.carriers:
            raise ValidationError("新码值已被使用")
        old.status = CarrierStatus.BLOCKED.value
        old.blocked_at = at
        old.replaced_by = new_code
        new = Carrier(
            code=new_code,
            serial=old.serial,
            issued_at=at,
            reason=f"补发换码：{reason}",
        )
        self.store.carriers[new_code] = new
        return new

    def carrier_history(self, serial: str) -> list[Carrier]:
        return sorted(
            (c for c in self.store.carriers.values() if c.serial == serial),
            key=lambda c: c.issued_at,
        )

    def scan(self, code: str, at: str | None = None) -> dict[str, Any]:
        """消费者扫码：始终解析到正式版本，只返回公开视图。"""
        carrier = self.store.carriers.get(code)
        if carrier is None:
            raise NotFoundError("码值不存在")
        if carrier.status != CarrierStatus.ACTIVE:
            raise StateError("该标签载体已失效，请以补发新码为准")
        item = self._item(carrier.serial)
        if at is None:
            version = self.effective_version(item.batch_id)
        else:
            version = self.effective_version_at(item.batch_id, at)
        if version is None:
            raise StateError("该批次标签尚未签发正式版本")
        return {
            "code": code,
            "serial_hint": carrier.serial,
            "version": version.number,
            "issued_at": version.issued_at,
            "content": version.public,
        }

    # ------------------------------------------------------------ 仓库流转

    def repack(
        self,
        from_batch_id: str,
        to_batch_id: str,
        serials: list[str],
        to_location: str,
        operator: str,
        at: str,
    ) -> Batch:
        """分装：逐件实物与标签身份一起迁入子批次，件数同步移动。"""
        parent = self._batch(from_batch_id)
        self._require_not_frozen(parent)
        if to_batch_id in self.store.batches:
            raise ValidationError("目标批次已存在")
        items = self._collect_serials(serials)
        child = Batch(
            id=to_batch_id,
            model_id=parent.model_id,
            quantity=len(items),
            location=to_location,
            parent_batch_id=parent.id,
        )
        self.store.batches[to_batch_id] = child
        for item in items:
            item.batch_id = child.id
            item.location = to_location
        parent.quantity = self._in_zone_count(parent.id)
        self.store.movements.append(
            Movement(
                id=f"MV-{len(self.store.movements) + 1:06d}",
                kind=MovementKind.REPACK,
                serials=serials,
                at=at,
                operator=operator,
                from_location=parent.location,
                to_location=to_location,
                detail={"from_batch": parent.id, "to_batch": child.id},
            )
        )
        return child

    def transfer(
        self,
        serials: list[str],
        to_location: str,
        operator: str,
        at: str,
    ) -> Movement:
        """换仓：实物移动，标签身份与批次归属不变。"""
        items = self._collect_serials(serials)
        origins = {item.location for item in items}
        for item in items:
            item.location = to_location
        self.store.movements.append(
            Movement(
                id=f"MV-{len(self.store.movements) + 1:06d}",
                kind=MovementKind.TRANSFER,
                serials=serials,
                at=at,
                operator=operator,
                from_location=sorted(origins)[0] if len(origins) == 1 else None,
                to_location=to_location,
            )
        )
        return self.store.movements[-1]

    def return_goods(
        self, serials: list[str], operator: str, at: str, reason: str
    ) -> Movement:
        """退运：在区实物退出，逐件状态与标签身份一并登记；冻结批次也允许处置。"""
        items = self._collect_serials(serials, allow_frozen=True)
        affected_batches = {item.batch_id for item in items}
        for item in items:
            item.status = ItemStatus.RETURNED.value
            item.location = "退运出区"
            item.reserved_by = None
        for batch_id in affected_batches:
            self._batch(batch_id).quantity = self._in_zone_count(batch_id)
        movement = Movement(
            id=f"MV-{len(self.store.movements) + 1:06d}",
            kind=MovementKind.RETURN,
            serials=serials,
            at=at,
            operator=operator,
            detail={"reason": reason},
        )
        self.store.movements.append(movement)
        return movement

    def destroy(
        self, serials: list[str], operator: str, at: str, reason: str
    ) -> Movement:
        """销毁：冻结批次下的监督处置同样允许，件数逐件核销。"""
        items = self._collect_serials(serials, allow_frozen=True)
        affected_batches = {item.batch_id for item in items}
        for item in items:
            item.status = ItemStatus.DESTROYED.value
            item.location = "销毁"
            item.reserved_by = None
        for batch_id in affected_batches:
            self._batch(batch_id).quantity = self._in_zone_count(batch_id)
        movement = Movement(
            id=f"MV-{len(self.store.movements) + 1:06d}",
            kind=MovementKind.DESTROY,
            serials=serials,
            at=at,
            operator=operator,
            detail={"reason": reason},
        )
        self.store.movements.append(movement)
        return movement

    # ------------------------------------------------------------ 出区核放

    def request_release(
        self,
        release_id: str,
        key: str,
        batch_id: str,
        quantity: int,
        doc_ids: list[str],
        requested_by: str,
        at: str,
    ) -> Release:
        """仓库申报出区并预留库存；同一幂等键重复提交不会再次占用或扣减。"""
        if key in self.store.release_keys:
            return self.store.releases[self.store.release_keys[key]]
        batch = self._batch(batch_id)
        self._require_not_frozen(batch)
        if quantity <= 0:
            raise ValidationError("出区数量必须为正")
        if not doc_ids:
            raise ValidationError("出区申报必须随附通关单证")
        for doc_id in doc_ids:
            doc = self._material(doc_id)
            if doc.batch_id != batch_id or doc.kind != MaterialKind.CUSTOMS.value:
                raise ValidationError("通关单证与申报批次不匹配")
            if doc.status != MaterialStatus.CONFIRMED:
                raise StateError("通关单证未经确认")
        available = [
            item
            for item in self.store.items.values()
            if item.batch_id == batch_id
            and item.status == ItemStatus.IN_ZONE
            and item.reserved_by is None
        ]
        if len(available) < quantity:
            raise ValidationError("可出区库存不足")
        chosen = sorted(available, key=lambda i: i.serial)[:quantity]
        release = Release(
            id=release_id,
            key=key,
            batch_id=batch_id,
            quantity=quantity,
            doc_ids=list(doc_ids),
            requested_by=requested_by,
            requested_at=at,
            serials=[item.serial for item in chosen],
        )
        for item in chosen:
            item.reserved_by = release_id
        self.store.releases[release_id] = release
        self.store.release_keys[key] = release_id
        return release

    def customs_clear(
        self, release_id: str, officer: str, at: str
    ) -> Release:
        """海关核放：锁定实际核验的标签版本；矛盾单证停在人工处理。

        重复核放同一单据直接返回既有结果，库存不会二次减少。
        """
        release = self._release(release_id)
        if release.status == ReleaseStatus.CLEARED:
            return release
        if release.status == ReleaseStatus.CANCELLED:
            raise StateError("出区申报已撤销，不能核放")
        conflict = self._detect_conflict(release)
        if conflict is not None:
            release.status = ReleaseStatus.MANUAL_REVIEW.value
            release.conflict = conflict
            return release
        batch = self._batch(release.batch_id)
        self._require_not_frozen(batch)
        version = self.effective_version(batch.id)
        if version is None:
            raise StateError("批次尚无正式标签版本，无法核放")
        release.status = ReleaseStatus.CLEARED.value
        release.locked_version_id = version.id
        release.cleared_by = officer
        release.cleared_at = at
        for serial in release.serials:
            item = self._item(serial)
            item.status = ItemStatus.RELEASED.value
            item.reserved_by = None
        batch.quantity = self._in_zone_count(batch.id)
        self.store.movements.append(
            Movement(
                id=f"MV-{len(self.store.movements) + 1:06d}",
                kind=MovementKind.RELEASE,
                serials=list(release.serials),
                at=at,
                operator=officer,
                from_location=batch.location,
                detail={
                    "release_id": release.id,
                    "locked_version": version.id,
                    "docs": list(release.doc_ids),
                },
            )
        )
        return release

    def resolve_manual_review(
        self, release_id: str, officer: str, at: str
    ) -> Release:
        """人工更正单证后重新校验；矛盾消除则回到待核放，否则维持人工处理。"""
        release = self._release(release_id)
        if release.status != ReleaseStatus.MANUAL_REVIEW:
            raise StateError("该单据不在人工处理状态")
        conflict = self._detect_conflict(release)
        if conflict is None:
            release.status = ReleaseStatus.PENDING.value
            release.conflict = None
        return release

    def cancel_release(self, release_id: str, officer: str, at: str) -> Release:
        """撤销出区申报（含人工处理挂起），释放预留库存。"""
        release = self._release(release_id)
        if release.status == ReleaseStatus.CLEARED:
            raise StateError("已核放单据不能撤销")
        for serial in release.serials:
            self._item(serial).reserved_by = None
        release.status = ReleaseStatus.CANCELLED.value
        release.conflict = f"{at} 由 {officer} 撤销申报"
        return release

    def _detect_conflict(self, release: Release) -> str | None:
        docs = [self._material(doc_id) for doc_id in release.doc_ids]
        origins = {doc.content.get("origin") for doc in docs}
        ingredient_hashes = {doc.content.get("ingredient_hash") for doc in docs}
        if len(origins) > 1:
            return "通关单证之间原产地申报互相矛盾"
        if len(ingredient_hashes) > 1:
            return "通关单证之间成分摘要互相矛盾"
        declared_quantities = {doc.content.get("quantity") for doc in docs if doc.content.get("quantity") is not None}
        # 允许部分放行：同一批累计核放数量不得超过单证申报数量
        if declared_quantities:
            already = sum(
                other.quantity
                for other in self.store.releases.values()
                if other.id != release.id
                and other.batch_id == release.batch_id
                and other.status == ReleaseStatus.CLEARED
            )
            if release.quantity + already > max(declared_quantities):
                return "通关单证申报数量与实际核放数量不符"
        version = self.effective_version(release.batch_id)
        if version is None:
            return "批次尚无正式标签版本可供核验"
        referenced = {mid: self._material(mid) for mid in version.material_ids}
        ingredient = next(
            (m for m in referenced.values() if m.kind == MaterialKind.INGREDIENT.value),
            None,
        )
        origin_material = next(
            (m for m in referenced.values() if m.kind == MaterialKind.ORIGIN.value),
            None,
        )
        if ingredient is not None and ingredient.content.get("hash") not in ingredient_hashes:
            return "通关单证成分摘要与标签版本成分声明不一致"
        if origin_material is not None and origin_material.content.get("origin") not in origins:
            return "通关单证原产地与标签版本原产地证明不一致"
        return None

    # ------------------------------------------------------------ 冻结与召回

    def freeze_batch(
        self, batch_id: str, reason: str, authority: str, officer: str, at: str
    ) -> Batch:
        """抽检异常或成分更正：只冻结关联批次，其他批次流转不受影响。"""
        batch = self._batch(batch_id)
        batch.status = BatchStatus.FROZEN.value
        batch.freeze = {"reason": reason, "authority": authority, "by": officer, "at": at}
        return batch

    def unfreeze_batch(self, batch_id: str, officer: str, at: str) -> Batch:
        batch = self._batch(batch_id)
        if batch.status != BatchStatus.FROZEN:
            raise StateError("批次未处于冻结状态")
        if self._batch_under_open_recall(batch_id):
            raise StateError("批次仍在召回期内，不能解除冻结")
        batch.status = BatchStatus.NORMAL.value
        batch.freeze = None
        return batch

    def open_recall(
        self,
        recall_id: str,
        batch_ids: list[str],
        reason: str,
        officer: str,
        at: str,
    ) -> Recall:
        """发起批次召回：在区库存冻结；已出区商品逐件定位销售状态与通知进度。"""
        if recall_id in self.store.recalls:
            raise ValidationError("召回编号已存在")
        for batch_id in batch_ids:
            self.freeze_batch(batch_id, f"召回：{reason}", "药品监管", officer, at)
        items: dict[str, RecallItem] = {}
        for batch_id in batch_ids:
            for item in self.store.items.values():
                if item.batch_id != batch_id:
                    continue
                if item.status == ItemStatus.SOLD:
                    entry = RecallItem(
                        serial=item.serial,
                        location_state=item.status,
                        sale_status="SOLD",
                        notification="PENDING",
                    )
                elif item.status == ItemStatus.RELEASED:
                    entry = RecallItem(
                        serial=item.serial,
                        location_state=item.status,
                        sale_status="UNSOLD",
                        notification="PENDING",
                    )
                else:
                    entry = RecallItem(
                        serial=item.serial,
                        location_state=item.status,
                        sale_status="NOT_APPLICABLE",
                        notification="NOT_REQUIRED",
                    )
                items[item.serial] = entry
        recall = Recall(
            id=recall_id,
            batch_ids=list(batch_ids),
            reason=reason,
            created_by=officer,
            created_at=at,
            items=items,
        )
        self.store.recalls[recall_id] = recall
        return recall

    def record_sale(self, serial: str, at: str, order_ref: str) -> Item:
        """登记离岛销售：出区商品售出后可被召回通知定位。"""
        item = self._item(serial)
        if item.status != ItemStatus.RELEASED:
            raise StateError("只有已出区商品可以登记销售")
        item.status = ItemStatus.SOLD.value
        item.sale = {"order_ref": order_ref, "sold_at": at}
        for recall in self.store.recalls.values():
            entry = recall.items.get(serial)
            if entry is not None and recall.status == RecallStatus.OPEN:
                entry.location_state = ItemStatus.SOLD.value
                entry.sale_status = "SOLD"
                if entry.notification == "NOT_REQUIRED":
                    entry.notification = "PENDING"
        return item

    def notify_recall_item(
        self, recall_id: str, serial: str, at: str
    ) -> RecallItem:
        recall = self._recall(recall_id)
        entry = recall.items.get(serial)
        if entry is None:
            raise NotFoundError("该件不在召回范围")
        if entry.notification != "PENDING":
            raise StateError("该件无需通知或已通知")
        entry.notification = "NOTIFIED"
        entry.notified_at = at
        if all(item.notification != "PENDING" for item in recall.items.values()):
            recall.status = RecallStatus.COMPLETED.value
        return entry

    def recall_progress(self, recall_id: str) -> dict[str, Any]:
        recall = self._recall(recall_id)
        pending = [s for s, i in recall.items.items() if i.notification == "PENDING"]
        notified = [s for s, i in recall.items.items() if i.notification == "NOTIFIED"]
        return {
            "recall_id": recall.id,
            "status": recall.status,
            "total": len(recall.items),
            "pending": pending,
            "notified": notified,
        }

    # ------------------------------------------------------------ 重启恢复

    def save(self, path: str | Path) -> None:
        self.store.save(Path(path))

    @classmethod
    def restore(cls, path: str | Path) -> "LabelService":
        """重启恢复：载入快照后优先整理未完成出区与召回通知。"""
        service = cls(Store.load(Path(path)))
        return service

    def recovery_queue(self) -> dict[str, list[str]]:
        """应用重启后应优先处理的事项：未完成出区在前，召回通知在后。"""
        pending_releases = [
            release.id
            for release in self.store.releases.values()
            if release.status in (ReleaseStatus.PENDING, ReleaseStatus.MANUAL_REVIEW)
        ]
        pending_notifications = [
            serial
            for recall in self.store.recalls.values()
            if recall.status == RecallStatus.OPEN
            for serial, item in recall.items.items()
            if item.notification == "PENDING"
        ]
        return {
            "pending_releases": sorted(pending_releases),
            "pending_recall_notifications": sorted(pending_notifications),
        }

    # ------------------------------------------------------------ 监管溯源

    def trace_serial(self, serial: str, at: str | None = None) -> dict[str, Any]:
        """监管人员按商品序列查询：审批依据、码值替换、库存去向、时点消费者视图。"""
        item = self._item(serial)
        batch = self._batch(item.batch_id)
        model = self.store.models[item.model_id]

        approval_basis: list[dict[str, Any]] = []
        chain_ids = self._batch_chain(batch.id)
        for version in sorted(
            (v for v in self.store.versions.values() if v.batch_id in chain_ids),
            key=lambda v: (v.issued_at, v.number),
        ):
            draft = self.store.drafts[version.draft_id]
            materials = []
            for material_id in version.material_ids:
                material = self._material(material_id)
                materials.append(
                    {
                        "id": material.id,
                        "kind": material.kind,
                        "submitted_by": material.submitted_by,
                        "reviewed_by": material.reviewed_by,
                        "reviewed_at": material.reviewed_at,
                        "status": material.status,
                        # 配方附件只登记审批留痕，内容不在任何视图公开
                        "content": None
                        if material.kind == MaterialKind.FORMULA.value
                        else material.content,
                    }
                )
            approval_basis.append(
                {
                    "version_id": version.id,
                    "number": version.number,
                    "status": version.status,
                    "issued_at": version.issued_at,
                    "issuer": version.issuer,
                    "draft_id": draft.id,
                    "draft_created_by": draft.created_by,
                    "materials": materials,
                }
            )

        releases = [
            {
                "release_id": release.id,
                "status": release.status,
                "locked_version": release.locked_version_id,
                "cleared_at": release.cleared_at,
                "cleared_by": release.cleared_by,
                "conflict": release.conflict,
            }
            for release in self.store.releases.values()
            if serial in release.serials
        ]

        recalls = [
            {
                "recall_id": recall.id,
                "reason": recall.reason,
                "sale_status": recall.items[serial].sale_status,
                "notification": recall.items[serial].notification,
                "notified_at": recall.items[serial].notified_at,
            }
            for recall in self.store.recalls.values()
            if serial in recall.items
        ]

        view_at = at
        version_then = (
            self.effective_version_at(batch.id, view_at) if view_at else self.effective_version(batch.id)
        )
        return {
            "serial": serial,
            "model": {"id": model.id, "name": model.name},
            "batch": {
                "id": batch.id,
                "parent_batch_id": batch.parent_batch_id,
                "status": batch.status,
                "location": item.location,
                "item_status": item.status,
                "in_zone_quantity": batch.quantity,
            },
            "approval_basis": approval_basis,
            "carrier_lineage": [
                {
                    "code": c.code,
                    "status": c.status,
                    "issued_at": c.issued_at,
                    "reason": c.reason,
                    "blocked_at": c.blocked_at,
                    "replaced_by": c.replaced_by,
                }
                for c in self.carrier_history(serial)
            ],
            "inventory_movements": [
                {
                    "id": m.id,
                    "kind": m.kind,
                    "at": m.at,
                    "operator": m.operator,
                    "from_location": m.from_location,
                    "to_location": m.to_location,
                    "detail": m.detail,
                }
                for m in self.store.movements
                if serial in m.serials
            ],
            "releases": releases,
            "sale": item.sale,
            "recalls": recalls,
            "consumer_view_at": view_at,
            "consumer_view": None if version_then is None else version_then.public,
            "consumer_version_then": None if version_then is None else version_then.number,
        }

    # ------------------------------------------------------------------ 辅助

    def _batch(self, batch_id: str) -> Batch:
        batch = self.store.batches.get(batch_id)
        if batch is None:
            raise NotFoundError("批次不存在")
        return batch

    def _material(self, material_id: str) -> Material:
        material = self.store.materials.get(material_id)
        if material is None:
            raise NotFoundError("资料不存在")
        return material

    def _draft(self, draft_id: str) -> LabelDraft:
        draft = self.store.drafts.get(draft_id)
        if draft is None:
            raise NotFoundError("标签草案不存在")
        return draft

    def _release(self, release_id: str) -> Release:
        release = self.store.releases.get(release_id)
        if release is None:
            raise NotFoundError("出区申请不存在")
        return release

    def _recall(self, recall_id: str) -> Recall:
        recall = self.store.recalls.get(recall_id)
        if recall is None:
            raise NotFoundError("召回不存在")
        return recall

    def _item(self, serial: str) -> Item:
        item = self.store.items.get(serial)
        if item is None:
            raise NotFoundError("逐件商品不存在")
        return item

    def _active_carrier(self, serial: str) -> Carrier | None:
        return next(
            (
                c
                for c in self.store.carriers.values()
                if c.serial == serial and c.status == CarrierStatus.ACTIVE
            ),
            None,
        )

    def _collect_serials(self, serials: list[str], allow_frozen: bool = False) -> list[Item]:
        if not serials:
            raise ValidationError("逐件清单不能为空")
        if len(set(serials)) != len(serials):
            raise ValidationError("逐件清单重复")
        items = [self._item(serial) for serial in serials]
        for item in items:
            if item.status != ItemStatus.IN_ZONE:
                raise StateError(f"{item.serial} 已不在区，不能参与该流转")
            if item.reserved_by is not None:
                raise StateError(f"{item.serial} 已被出区申请预留")
            if not allow_frozen:
                self._require_not_frozen(self._batch(item.batch_id))
        return items

    def _in_zone_count(self, batch_id: str) -> int:
        return sum(
            1
            for item in self.store.items.values()
            if item.batch_id == batch_id and item.status == ItemStatus.IN_ZONE
        )

    def _batch_under_open_recall(self, batch_id: str) -> bool:
        return any(
            batch_id in recall.batch_ids and recall.status == RecallStatus.OPEN
            for recall in self.store.recalls.values()
        )

    def _require_not_frozen(self, batch: Batch) -> None:
        if batch.status == BatchStatus.FROZEN:
            raise FrozenError(f"批次 {batch.id} 已冻结：{batch.freeze}")
