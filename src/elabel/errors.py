"""监管服务领域异常。"""

from __future__ import annotations


class LabelServiceError(Exception):
    """监管服务领域错误的基类。"""


class NotFoundError(LabelServiceError):
    """引用的档案、资料、码值或单据不存在。"""


class ValidationError(LabelServiceError):
    """提交内容不满足业务约束。"""


class StateError(LabelServiceError):
    """当前状态不允许执行该操作。"""


class SeparationOfDutiesError(StateError):
    """资料审核与签发两个职责由同一人承担。"""


class FrozenError(StateError):
    """批次已冻结，停止一切流转。"""
