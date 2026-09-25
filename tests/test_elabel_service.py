"""离岛免税电子标签监管服务的业务约束测试。"""

import tempfile
import unittest
from pathlib import Path

from src.elabel import (
    FrozenError,
    LabelService,
    MaterialKind,
    NotFoundError,
    SeparationOfDutiesError,
    StateError,
    ValidationError,
)

T0 = "2026-09-01T08:00:00+08:00"
T1 = "2026-09-02T08:00:00+08:00"
T2 = "2026-09-03T08:00:00+08:00"
T3 = "2026-09-04T08:00:00+08:00"
T4 = "2026-09-05T08:00:00+08:00"
T5 = "2026-09-06T08:00:00+08:00"

INGREDIENT_HASH = "ing-hash-001"


def make_service() -> LabelService:
    service = LabelService()
    service.register_model("MODEL-1", "舒润保湿喷雾 30ml", actor="企业A", at=T0)
    return service


def add_batch(service: LabelService, batch_id: str = "B-001", quantity: int = 100) -> str:
    service.register_batch(batch_id, "MODEL-1", quantity, "W1-01", operator="仓库员甲", at=T0)
    return batch_id


def submit_and_confirm(
    service: LabelService,
    batch_id: str,
    *,
    ingredient_hash: str = INGREDIENT_HASH,
    origin: str = "法国",
    customs_quantity: int = 100,
    doc_no: str = "CUS-001",
) -> dict[str, str]:
    ids = {
        MaterialKind.INGREDIENT: f"{batch_id}-ING",
        MaterialKind.ORIGIN: f"{batch_id}-ORG",
        MaterialKind.MANUAL: f"{batch_id}-MAN",
        MaterialKind.CUSTOMS: f"{batch_id}-CUS",
    }
    service.submit_material(
        ids[MaterialKind.INGREDIENT],
        batch_id,
        MaterialKind.INGREDIENT,
        {"ingredients": "水、甘油、烟酰胺", "hash": ingredient_hash},
        actor="企业A",
        at=T0,
    )
    service.submit_material(
        ids[MaterialKind.ORIGIN],
        batch_id,
        MaterialKind.ORIGIN,
        {"origin": origin},
        actor="企业A",
        at=T0,
    )
    service.submit_material(
        ids[MaterialKind.MANUAL],
        batch_id,
        MaterialKind.MANUAL,
        {"text": "使用前请摇匀，避开眼周。"},
        actor="企业A",
        at=T0,
    )
    service.submit_material(
        ids[MaterialKind.CUSTOMS],
        batch_id,
        MaterialKind.CUSTOMS,
        {
            "doc_no": doc_no,
            "origin": origin,
            "ingredient_hash": ingredient_hash,
            "quantity": customs_quantity,
        },
        actor="企业A",
        at=T0,
    )
    for material_id in ids.values():
        service.review_material(material_id, reviewer="药监员乙", approved=True, at=T1)
    return {kind.value: mid for kind, mid in ids.items()}


def issue_label(
    service: LabelService,
    batch_id: str,
    refs: dict[str, str],
    *,
    version_id: str | None = None,
    draft_id: str | None = None,
    at: str = T2,
) -> str:
    version_id = version_id or f"{batch_id}-V1"
    draft_id = draft_id or f"{batch_id}-D1"
    service.create_draft(
        draft_id,
        batch_id,
        {MaterialKind(k): v for k, v in refs.items()},
        actor="企业A",
        at=T1,
    )
    service.issue_version(version_id, draft_id, issuer="签发员丙", at=at)
    return version_id


def label_batch(
    service: LabelService, batch_id: str = "B-001", quantity: int = 100
) -> dict[str, str]:
    add_batch(service, batch_id, quantity)
    refs = submit_and_confirm(service, batch_id)
    issue_label(service, batch_id, refs)
    return refs


class MaterialReviewTest(unittest.TestCase):
    """资料审核与签发职责分离。"""

    def test_submitter_cannot_review_own_material(self) -> None:
        service = make_service()
        add_batch(service)
        service.submit_material(
            "M1", "B-001", MaterialKind.ORIGIN, {"origin": "法国"}, actor="企业A", at=T0
        )
        with self.assertRaises(SeparationOfDutiesError):
            service.review_material("M1", reviewer="企业A", approved=True, at=T1)

    def test_draft_requires_confirmed_materials(self) -> None:
        service = make_service()
        add_batch(service)
        service.submit_material(
            "M-ING", "B-001", MaterialKind.INGREDIENT,
            {"ingredients": "水", "hash": "h"}, actor="企业A", at=T0,
        )
        service.submit_material(
            "M-ORG", "B-001", MaterialKind.ORIGIN, {"origin": "法国"}, actor="企业A", at=T0
        )
        service.submit_material(
            "M-MAN", "B-001", MaterialKind.MANUAL, {"text": "说明"}, actor="企业A", at=T0
        )
        refs = {
            MaterialKind.INGREDIENT: "M-ING",
            MaterialKind.ORIGIN: "M-ORG",
            MaterialKind.MANUAL: "M-MAN",
        }
        with self.assertRaises(StateError):
            service.create_draft("D-1", "B-001", refs, actor="企业A", at=T1)

    def test_issuer_must_differ_from_reviewer_and_drafter(self) -> None:
        service = make_service()
        add_batch(service)
        refs = submit_and_confirm(service, batch_id="B-001")
        service.create_draft(
            "D-1",
            "B-001",
            {MaterialKind(k): v for k, v in refs.items()},
            actor="企业A",
            at=T1,
        )
        with self.assertRaises(SeparationOfDutiesError):
            service.issue_version("V-1", "D-1", issuer="药监员乙", at=T2)
        with self.assertRaises(SeparationOfDutiesError):
            service.issue_version("V-1", "D-1", issuer="企业A", at=T2)
        service.issue_version("V-1", "D-1", issuer="签发员丙", at=T2)

    def test_formula_attachment_never_enters_label(self) -> None:
        service = make_service()
        add_batch(service)
        refs = submit_and_confirm(service, batch_id="B-001")
        service.submit_material(
            "B-001-FORMULA",
            "B-001",
            MaterialKind.FORMULA,
            {"secret": "配方比例附件"},
            actor="企业A",
            at=T0,
        )
        service.review_material("B-001-FORMULA", reviewer="药监员乙", approved=True, at=T1)
        with self.assertRaises(ValidationError):
            service.create_draft(
                "D-9",
                "B-001",
                {
                    MaterialKind.INGREDIENT: refs["INGREDIENT"],
                    MaterialKind.ORIGIN: refs["ORIGIN"],
                    MaterialKind.MANUAL: refs["MANUAL"],
                    MaterialKind.FORMULA: "B-001-FORMULA",
                },
                actor="企业A",
                at=T1,
            )


class VersionChainTest(unittest.TestCase):
    """二维码始终指向一个正式版本，消费者只看到公开内容。"""

    def test_scan_returns_only_public_content(self) -> None:
        service = make_service()
        label_batch(service)
        service.issue_carrier("QR-1", "B-001-00001", operator="仓库员甲", at=T2)
        view = service.scan("QR-1")
        self.assertEqual(view["version"], 1)
        self.assertEqual(view["content"]["ingredients"], "水、甘油、烟酰胺")
        self.assertEqual(view["content"]["origin"], "法国")
        self.assertIn("customs", view["content"])
        self.assertNotIn("secret", str(view["content"]))
        self.assertNotIn("配方", str(view["content"]))

    def test_new_version_supersedes_and_scan_follows(self) -> None:
        service = make_service()
        add_batch(service)
        refs = submit_and_confirm(service, "B-001")
        issue_label(service, "B-001", refs, version_id="V-1", draft_id="D-1", at=T2)
        # 入区后成分更正：新资料、新草案、新版本
        service.submit_material(
            "B-001-ING2",
            "B-001",
            MaterialKind.INGREDIENT,
            {"ingredients": "水、甘油、烟酰胺、泛醇", "hash": "ing-hash-002"},
            actor="企业A",
            at=T3,
        )
        service.review_material("B-001-ING2", reviewer="药监员乙", approved=True, at=T3)
        refs2 = dict(refs)
        refs2["INGREDIENT"] = "B-001-ING2"
        issue_label(service, "B-001", refs2, version_id="V-2", draft_id="D-2", at=T4)

        service.issue_carrier("QR-1", "B-001-00001", operator="仓库员甲", at=T2)
        current = service.scan("QR-1")
        self.assertEqual(current["version"], 2)
        self.assertIn("泛醇", current["content"]["ingredients"])
        # 监管人员可还原任一时点消费者能读到的中文信息
        earlier = service.scan("QR-1", at=T3)
        self.assertEqual(earlier["version"], 1)
        self.assertNotIn("泛醇", earlier["content"]["ingredients"])
        self.assertEqual(
            service.effective_version_at("B-001", T3).id,
            service.store.versions["V-1"].id,
        )


class CarrierLifecycleTest(unittest.TestCase):
    """破损补发：新码接续旧码历史，旧载体立即失效。"""

    def test_replacement_blocks_old_and_keeps_history(self) -> None:
        service = make_service()
        label_batch(service)
        service.issue_carrier("QR-OLD", "B-001-00001", operator="仓库员甲", at=T2)
        service.replace_carrier("QR-OLD", "QR-NEW", reason="标签破损", operator="仓库员甲", at=T3)
        with self.assertRaises(StateError):
            service.scan("QR-OLD")
        view = service.scan("QR-NEW")
        self.assertEqual(view["serial_hint"], "B-001-00001")
        lineage = service.trace_serial("B-001-00001")["carrier_lineage"]
        self.assertEqual([c["code"] for c in lineage], ["QR-OLD", "QR-NEW"])
        self.assertEqual(lineage[0]["status"], "BLOCKED")
        self.assertEqual(lineage[0]["replaced_by"], "QR-NEW")
        with self.assertRaises(StateError):
            service.replace_carrier("QR-OLD", "QR-OTHER", reason="重复补发", operator="仓库员甲", at=T4)


class WarehouseMovementTest(unittest.TestCase):
    """分装、部分放行、换仓、退运、销毁时实物与标签身份一起移动。"""

    def test_repack_moves_items_and_conserves_count(self) -> None:
        service = make_service()
        label_batch(service, quantity=10)
        service.repack(
            "B-001",
            "B-001-A",
            ["B-001-00001", "B-001-00002", "B-001-00003"],
            "W2-01",
            operator="仓库员甲",
            at=T3,
        )
        self.assertEqual(service.store.batches["B-001"].quantity, 7)
        self.assertEqual(service.store.batches["B-001-A"].quantity, 3)
        moved = service.store.items["B-001-00001"]
        self.assertEqual(moved.batch_id, "B-001-A")
        self.assertEqual(moved.location, "W2-01")
        trace = service.trace_serial("B-001-00001")
        self.assertEqual(trace["batch"]["parent_batch_id"], "B-001")
        self.assertIn("REPACK", [m["kind"] for m in trace["inventory_movements"]])
        # 子批次逐件沿用父批正式标签版本，扫码正常
        service.issue_carrier("QR-R1", "B-001-00001", operator="仓库员甲", at=T3)
        self.assertEqual(service.scan("QR-R1")["version"], 1)
        self.assertEqual(trace["approval_basis"][0]["materials"][0]["kind"], "INGREDIENT")

    def test_transfer_return_destroy_update_status_and_quantity(self) -> None:
        service = make_service()
        label_batch(service, quantity=5)
        service.transfer(["B-001-00001", "B-001-00002"], "W3-02", operator="仓库员甲", at=T3)
        self.assertEqual(service.store.items["B-001-00001"].location, "W3-02")
        service.return_goods(["B-001-00003"], operator="仓库员甲", at=T3, reason="外商召回退运")
        service.destroy(["B-001-00004"], operator="仓库员甲", at=T3, reason="监督销毁")
        self.assertEqual(service.store.items["B-001-00003"].status, "RETURNED")
        self.assertEqual(service.store.items["B-001-00004"].status, "DESTROYED")
        self.assertEqual(service.store.batches["B-001"].quantity, 3)
        with self.assertRaises(StateError):
            service.transfer(["B-001-00003"], "W1-01", operator="仓库员甲", at=T4)

    def test_partial_release_reserves_then_clears_exact_items(self) -> None:
        service = make_service()
        refs = label_batch(service, quantity=10)
        release = service.request_release(
            "R-1",
            key="KEY-1",
            batch_id="B-001",
            quantity=4,
            doc_ids=[refs["CUSTOMS"]],
            requested_by="企业A",
            at=T3,
        )
        self.assertEqual(len(release.serials), 4)
        # 已预留的件不能参与其他流转
        with self.assertRaises(StateError):
            service.transfer([release.serials[0]], "W9-09", operator="仓库员甲", at=T3)
        service.customs_clear("R-1", officer="海关关员丁", at=T4)
        self.assertEqual(service.store.batches["B-001"].quantity, 6)
        for serial in release.serials:
            self.assertEqual(service.store.items[serial].status, "RELEASED")


class FreezeIsolationTest(unittest.TestCase):
    """抽检异常或成分更正只冻结关联批次。"""

    def test_freeze_blocks_flow_but_not_other_batches(self) -> None:
        service = make_service()
        label_batch(service, "B-001", quantity=5)
        refs_b = label_batch(service, "B-002", quantity=5)
        service.freeze_batch("B-001", "抽检异常", authority="海关", officer="海关关员丁", at=T3)
        with self.assertRaises(FrozenError):
            service.transfer(["B-001-00001"], "W2-01", operator="仓库员甲", at=T3)
        with self.assertRaises(FrozenError):
            service.repack("B-001", "B-001-A", ["B-001-00001"], "W2-01", operator="仓库员甲", at=T3)
        with self.assertRaises(FrozenError):
            service.request_release(
                "R-1", key="K-1", batch_id="B-001", quantity=1,
                doc_ids=["B-001-CUS"], requested_by="企业A", at=T3,
            )
        # 冻结批次仍允许退运与销毁等处置
        service.destroy(["B-001-00001"], operator="仓库员甲", at=T3, reason="监督销毁")
        # 其他批次不受牵连
        service.transfer(["B-002-00001"], "W2-01", operator="仓库员甲", at=T3)
        release = service.request_release(
            "R-2", key="K-2", batch_id="B-002", quantity=1,
            doc_ids=[refs_b["CUSTOMS"]], requested_by="企业A", at=T3,
        )
        cleared = service.customs_clear(release.id, officer="海关关员丁", at=T4)
        self.assertEqual(cleared.status, "CLEARED")

    def test_unfreeze_after_issue_resolved(self) -> None:
        service = make_service()
        label_batch(service, quantity=5)
        service.freeze_batch("B-001", "成分更正核查", authority="药监", officer="药监员乙", at=T3)
        service.unfreeze_batch("B-001", officer="药监员乙", at=T4)
        service.transfer(["B-001-00001"], "W2-01", operator="仓库员甲", at=T4)


class CustomsClearanceTest(unittest.TestCase):
    """海关核放：锁定核验版本、幂等、矛盾单证转人工。"""

    def test_clearance_locks_verified_version(self) -> None:
        service = make_service()
        refs = label_batch(service, quantity=5)
        release = service.request_release(
            "R-1", key="K-1", batch_id="B-001", quantity=2,
            doc_ids=[refs["CUSTOMS"]], requested_by="企业A", at=T3,
        )
        cleared = service.customs_clear(release.id, officer="海关关员丁", at=T4)
        self.assertEqual(cleared.locked_version_id, "B-001-V1")
        trace = service.trace_serial(cleared.serials[0])
        self.assertEqual(trace["releases"][0]["locked_version"], "B-001-V1")

    def test_duplicate_submission_and_double_clearance_are_idempotent(self) -> None:
        service = make_service()
        refs = label_batch(service, quantity=5)
        first = service.request_release(
            "R-1", key="K-1", batch_id="B-001", quantity=2,
            doc_ids=[refs["CUSTOMS"]], requested_by="企业A", at=T3,
        )
        again = service.request_release(
            "R-1B", key="K-1", batch_id="B-001", quantity=2,
            doc_ids=[refs["CUSTOMS"]], requested_by="企业A", at=T3,
        )
        self.assertIs(first, again)
        service.customs_clear("R-1", officer="海关关员丁", at=T4)
        quantity_after_clear = service.store.batches["B-001"].quantity
        service.customs_clear("R-1", officer="海关关员丁", at=T5)
        self.assertEqual(service.store.batches["B-001"].quantity, quantity_after_clear)
        self.assertEqual(quantity_after_clear, 3)

    def test_conflicting_documents_go_to_manual_review(self) -> None:
        service = make_service()
        add_batch(service, quantity=5)
        refs = submit_and_confirm(service, "B-001", origin="法国")
        # 第二张互相矛盾的通关单证（原产地不一致）
        service.submit_material(
            "B-001-CUS2",
            "B-001",
            MaterialKind.CUSTOMS,
            {"doc_no": "CUS-002", "origin": "日本", "ingredient_hash": INGREDIENT_HASH, "quantity": 5},
            actor="企业A",
            at=T0,
        )
        service.review_material("B-001-CUS2", reviewer="药监员乙", approved=True, at=T1)
        issue_label(service, "B-001", refs)
        release = service.request_release(
            "R-1", key="K-1", batch_id="B-001", quantity=5,
            doc_ids=[refs["CUSTOMS"], "B-001-CUS2"], requested_by="企业A", at=T3,
        )
        result = service.customs_clear(release.id, officer="海关关员丁", at=T4)
        self.assertEqual(result.status, "MANUAL_REVIEW")
        self.assertIn("矛盾", result.conflict)
        # 矛盾单证不减少库存，货物仍在区
        self.assertEqual(service.store.batches["B-001"].quantity, 5)
        self.assertEqual(service.store.items[release.serials[0]].status, "IN_ZONE")

    def test_manual_review_resumes_after_correction(self) -> None:
        service = make_service()
        add_batch(service, quantity=5)
        refs = submit_and_confirm(service, "B-001")
        # 先申报后补标签版本：核放时无版本可核验，转人工
        release = service.request_release(
            "R-1", key="K-1", batch_id="B-001", quantity=5,
            doc_ids=[refs["CUSTOMS"]], requested_by="企业A", at=T3,
        )
        result = service.customs_clear(release.id, officer="海关关员丁", at=T4)
        self.assertEqual(result.status, "MANUAL_REVIEW")
        issue_label(service, "B-001", refs, at=T4)
        resumed = service.resolve_manual_review("R-1", officer="海关关员丁", at=T5)
        self.assertEqual(resumed.status, "PENDING")
        cleared = service.customs_clear("R-1", officer="海关关员丁", at=T5)
        self.assertEqual(cleared.status, "CLEARED")
        self.assertEqual(cleared.locked_version_id, "B-001-V1")

    def test_ingredient_mismatch_between_doc_and_label_is_conflict(self) -> None:
        service = make_service()
        add_batch(service, quantity=5)
        refs = submit_and_confirm(service, "B-001", ingredient_hash="hash-A")
        issue_label(service, "B-001", refs)
        service.submit_material(
            "B-001-CUS2",
            "B-001",
            MaterialKind.CUSTOMS,
            {"doc_no": "CUS-002", "origin": "法国", "ingredient_hash": "hash-B", "quantity": 5},
            actor="企业A",
            at=T0,
        )
        service.review_material("B-001-CUS2", reviewer="药监员乙", approved=True, at=T1)
        release = service.request_release(
            "R-1", key="K-1", batch_id="B-001", quantity=5,
            doc_ids=["B-001-CUS2"], requested_by="企业A", at=T3,
        )
        result = service.customs_clear(release.id, officer="海关关员丁", at=T4)
        self.assertEqual(result.status, "MANUAL_REVIEW")


class RecallTest(unittest.TestCase):
    """召回：冻结在区库存，定位已出区商品的销售状态与通知进度。"""

    def _service_with_out_of_zone_goods(self) -> tuple[LabelService, list[str]]:
        service = make_service()
        refs = label_batch(service, quantity=5)
        release = service.request_release(
            "R-1", key="K-1", batch_id="B-001", quantity=3,
            doc_ids=[refs["CUSTOMS"]], requested_by="企业A", at=T3,
        )
        service.customs_clear("R-1", officer="海关关员丁", at=T3)
        service.record_sale(release.serials[0], at=T4, order_ref="ORDER-1")
        return service, release.serials

    def test_recall_freezes_stock_and_tracks_out_of_zone_items(self) -> None:
        service, out_serials = self._service_with_out_of_zone_goods()
        recall = service.open_recall("RC-1", ["B-001"], "成分标注更正召回", officer="药监员乙", at=T5)
        self.assertEqual(service.store.batches["B-001"].status, "FROZEN")
        with self.assertRaises(FrozenError):
            service.transfer(["B-001-00004"], "W2-01", operator="仓库员甲", at=T5)
        sold = recall.items[out_serials[0]]
        unsold = recall.items[out_serials[1]]
        in_zone = recall.items["B-001-00004"]
        self.assertEqual(sold.sale_status, "SOLD")
        self.assertEqual(sold.notification, "PENDING")
        self.assertEqual(unsold.sale_status, "UNSOLD")
        self.assertEqual(unsold.notification, "PENDING")
        self.assertEqual(in_zone.notification, "NOT_REQUIRED")

    def test_notification_progress_until_completed(self) -> None:
        service, out_serials = self._service_with_out_of_zone_goods()
        service.open_recall("RC-1", ["B-001"], "召回", officer="药监员乙", at=T5)
        progress = service.recall_progress("RC-1")
        self.assertEqual(progress["status"], "OPEN")
        self.assertEqual(len(progress["pending"]), 3)
        for serial in out_serials:
            service.notify_recall_item("RC-1", serial, at=T5)
        progress = service.recall_progress("RC-1")
        self.assertEqual(progress["status"], "COMPLETED")
        self.assertEqual(progress["pending"], [])

    def test_trace_shows_sale_state_and_notification(self) -> None:
        service, out_serials = self._service_with_out_of_zone_goods()
        service.open_recall("RC-1", ["B-001"], "召回", officer="药监员乙", at=T5)
        service.notify_recall_item("RC-1", out_serials[0], at=T5)
        trace = service.trace_serial(out_serials[0])
        self.assertEqual(trace["sale"]["order_ref"], "ORDER-1")
        self.assertEqual(trace["recalls"][0]["sale_status"], "SOLD")
        self.assertEqual(trace["recalls"][0]["notification"], "NOTIFIED")


class RecoveryTest(unittest.TestCase):
    """应用重启后优先恢复未完成出区和召回通知。"""

    def test_restart_recovers_pending_release_and_recall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.json"
            service = make_service()
            refs = label_batch(service, "B-001", quantity=5)
            refs2 = label_batch(service, "B-002", quantity=5)
            # B-002 有未完成出区；B-001 有已出区已售商品并进入召回
            service.request_release(
                "R-1", key="K-1", batch_id="B-002", quantity=2,
                doc_ids=[refs2["CUSTOMS"]], requested_by="企业A", at=T3,
            )
            release2 = service.request_release(
                "R-2", key="K-2", batch_id="B-001", quantity=1,
                doc_ids=[refs["CUSTOMS"]], requested_by="企业A", at=T3,
            )
            service.customs_clear("R-2", officer="海关关员丁", at=T3)
            service.record_sale(release2.serials[0], at=T4, order_ref="ORDER-9")
            service.open_recall("RC-1", ["B-001"], "召回", officer="药监员乙", at=T4)
            service.save(path)

            restored = LabelService.restore(path)
            queue = restored.recovery_queue()
            self.assertEqual(queue["pending_releases"], ["R-1"])
            self.assertEqual(queue["pending_recall_notifications"], [release2.serials[0]])
            # 恢复后可以继续完成核放与通知
            cleared = restored.customs_clear("R-1", officer="海关关员丁", at=T5)
            self.assertEqual(cleared.status, "CLEARED")
            restored.notify_recall_item("RC-1", release2.serials[0], at=T5)
            self.assertEqual(restored.recovery_queue()["pending_recall_notifications"], [])


class TraceTest(unittest.TestCase):
    """监管人员按商品序列查询的完整视图。"""

    def test_trace_combines_approval_lineage_movements_and_consumer_view(self) -> None:
        service = make_service()
        refs = label_batch(service, quantity=5)
        service.issue_carrier("QR-1", "B-001-00001", operator="仓库员甲", at=T2)
        service.replace_carrier("QR-1", "QR-2", reason="破损", operator="仓库员甲", at=T3)
        service.transfer(["B-001-00001"], "W2-01", operator="仓库员甲", at=T3)
        trace = service.trace_serial("B-001-00001", at=T4)

        basis = trace["approval_basis"][0]
        self.assertEqual(basis["issuer"], "签发员丙")
        self.assertEqual(basis["draft_created_by"], "企业A")
        reviewers = {m["reviewed_by"] for m in basis["materials"]}
        self.assertEqual(reviewers, {"药监员乙"})
        self.assertEqual(
            {m["kind"] for m in basis["materials"]},
            {"INGREDIENT", "ORIGIN", "MANUAL", "CUSTOMS"},
        )

        self.assertEqual(
            [c["code"] for c in trace["carrier_lineage"]], ["QR-1", "QR-2"]
        )
        kinds = [m["kind"] for m in trace["inventory_movements"]]
        self.assertEqual(kinds, ["INBOUND", "TRANSFER"])
        self.assertEqual(trace["consumer_view"]["ingredients"], "水、甘油、烟酰胺")
        self.assertEqual(trace["consumer_version_then"], 1)
        self.assertEqual(trace["consumer_view_at"], T4)


class GuardTest(unittest.TestCase):
    def test_unknown_references_are_rejected(self) -> None:
        service = make_service()
        with self.assertRaises(NotFoundError):
            service.scan("NOPE")
        with self.assertRaises(NotFoundError):
            service.trace_serial("NOPE")
        with self.assertRaises(NotFoundError):
            service.register_batch("B-9", "NOPE", 1, "W1", operator="x", at=T0)

    def test_release_requires_confirmed_customs_doc_of_same_batch(self) -> None:
        service = make_service()
        label_batch(service, "B-001", quantity=5)
        label_batch(service, "B-002", quantity=5)
        with self.assertRaises(ValidationError):
            service.request_release(
                "R-1", key="K-1", batch_id="B-001", quantity=1,
                doc_ids=["B-002-CUS"], requested_by="企业A", at=T3,
            )
        with self.assertRaises(ValidationError):
            service.request_release(
                "R-2", key="K-2", batch_id="B-001", quantity=99,
                doc_ids=["B-001-CUS"], requested_by="企业A", at=T3,
            )


if __name__ == "__main__":
    unittest.main()
