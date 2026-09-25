"""离岛免税电子标签监管服务的领域模型与状态枚举。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MaterialKind(str, Enum):
    """随批资料种类。"""

    INGREDIENT = "INGREDIENT"  # 成分声明
    ORIGIN = "ORIGIN"  # 原产地证明
    MANUAL = "MANUAL"  # 中文说明
    CUSTOMS = "CUSTOMS"  # 通关单证
    FORMULA = "FORMULA"  # 配方附件（企业保密，永不公开）


#: 允许进入标签草案、可向消费者公开的资料种类；配方附件不在其列。
PUBLIC_MATERIAL_KINDS = (
    MaterialKind.INGREDIENT,
    MaterialKind.ORIGIN,
    MaterialKind.MANUAL,
    MaterialKind.CUSTOMS,
)


class MaterialStatus(str, Enum):
    SUBMITTED = "SUBMITTED"  # 已提交，待资料审核
    CONFIRMED = "CONFIRMED"  # 已确认，可被签发引用
    REJECTED = "REJECTED"


class DraftStatus(str, Enum):
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    VOID = "VOID"


class VersionStatus(str, Enum):
    EFFECTIVE = "EFFECTIVE"  # 当前正式版本
    SUPERSEDED = "SUPERSEDED"  # 已被新版本取代


class ItemStatus(str, Enum):
    IN_ZONE = "IN_ZONE"  # 在区
    RELEASED = "RELEASED"  # 已出区
    SOLD = "SOLD"  # 已售出
    RETURNED = "RETURNED"  # 已退运
    DESTROYED = "DESTROYED"  # 已销毁


class CarrierStatus(str, Enum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"  # 破损补发后立即失效


class BatchStatus(str, Enum):
    NORMAL = "NORMAL"
    FROZEN = "FROZEN"  # 抽检异常、成分更正或召回导致的冻结


class ReleaseStatus(str, Enum):
    PENDING = "PENDING"  # 已申请、库存已预留，待海关核放
    CLEARED = "CLEARED"  # 已核放出区
    MANUAL_REVIEW = "MANUAL_REVIEW"  # 单证矛盾，停在人工处理
    CANCELLED = "CANCELLED"  # 申报方撤销，预留库存已释放


class RecallStatus(str, Enum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"  # 全部应通知对象均已通知


class MovementKind(str, Enum):
    INBOUND = "INBOUND"  # 入区
    REPACK = "REPACK"  # 分装
    TRANSFER = "TRANSFER"  # 换仓
    RELEASE = "RELEASE"  # 出区
    RETURN = "RETURN"  # 退运
    DESTROY = "DESTROY"  # 销毁


@dataclass
class ProductModel:
    """商品型号。"""

    id: str
    name: str
    registered_by: str
    registered_at: str


@dataclass
class Batch:
    """进口批次；分装产生的子批次通过 parent_batch_id 接续来源。"""

    id: str
    model_id: str
    quantity: int  # 当前挂靠本批次的逐件数量（件数守恒）
    location: str
    status: str = BatchStatus.NORMAL
    parent_batch_id: str | None = None
    freeze: dict[str, Any] | None = None


@dataclass
class Material:
    """随批资料：成分声明、原产地证明、中文说明、通关单证、配方附件。"""

    id: str
    batch_id: str
    kind: str
    content: dict[str, Any]
    submitted_by: str
    submitted_at: str
    status: str = MaterialStatus.SUBMITTED
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None


@dataclass
class LabelDraft:
    """标签草案：引用同一批次、已经确认的资料。"""

    id: str
    batch_id: str
    refs: dict[str, str]  # MaterialKind -> Material.id
    created_by: str
    created_at: str
    status: str = DraftStatus.DRAFT


@dataclass
class LabelVersion:
    """正式标签版本：签发时从已确认资料快照生成，内容不再回改。"""

    id: str
    batch_id: str
    number: int
    draft_id: str
    issuer: str
    issued_at: str
    public: dict[str, Any]  # 消费者可见内容快照，绝不含配方附件
    material_ids: list[str]  # 审批依据
    status: str = VersionStatus.EFFECTIVE


@dataclass
class Item:
    """逐件商品：序列唯一，实物数量与标签身份一起移动。"""

    serial: str
    model_id: str
    batch_id: str
    location: str
    status: str = ItemStatus.IN_ZONE
    reserved_by: str | None = None  # 预留中的出区申请
    sale: dict[str, Any] | None = None


@dataclass
class Carrier:
    """标签载体（码值）：补发时旧码立即失效，新码接续历史。"""

    code: str
    serial: str
    issued_at: str
    reason: str
    status: str = CarrierStatus.ACTIVE
    blocked_at: str | None = None
    replaced_by: str | None = None


@dataclass
class Movement:
    """库存去向流水：每一次实物移动都记录逐件序列。"""

    id: str
    kind: str
    serials: list[str]
    at: str
    operator: str
    from_location: str | None = None
    to_location: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Release:
    """出区申请与海关核放记录；key 为幂等键，重复提交不重复扣减。"""

    id: str
    key: str
    batch_id: str
    quantity: int
    doc_ids: list[str]
    requested_by: str
    requested_at: str
    status: str = ReleaseStatus.PENDING
    serials: list[str] = field(default_factory=list)
    locked_version_id: str | None = None  # 海关实际核验并锁定的标签版本
    cleared_by: str | None = None
    cleared_at: str | None = None
    conflict: str | None = None


@dataclass
class RecallItem:
    """召回涉及的逐件状态：销售状态与通知进度。"""

    serial: str
    location_state: str
    sale_status: str  # SOLD / UNSOLD / NOT_APPLICABLE
    notification: str  # PENDING / NOTIFIED / NOT_REQUIRED
    notified_at: str | None = None


@dataclass
class Recall:
    """批次召回：在区库存冻结，已出区商品逐件跟踪销售与通知。"""

    id: str
    batch_ids: list[str]
    reason: str
    created_by: str
    created_at: str
    items: dict[str, RecallItem] = field(default_factory=dict)
    status: str = RecallStatus.OPEN
