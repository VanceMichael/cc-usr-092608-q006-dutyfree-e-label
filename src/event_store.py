"""事件存储：JSONL 追加写 + 启动回放。

监管服务以追加事件记录全部状态变化，资料版本链、码值替换链、库存移动
均落在同一追加日志中，重启时回放即可恢复未完成的出区核放与召回通知。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable


class EventStore:
    """单行 JSON 即一个事件的追加日志。

    写入采用“写临时文件并 flush/fsync 后原子替换”不适用于追加场景，
    因此这里直接以追加模式打开并逐行 fsync，保证事件落盘后才对外返回。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 行缓冲二进制追加，同一进程内串行写入即可。
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def append(self, event: dict[str, Any]) -> None:
        """追加并持久化一个事件，随后立即通知内存投影。"""
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        self._handle.write(line + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        for listener in self._listeners:
            listener(event)

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> None:
        # 注册回放与后续事件的统一消费者。
        self._listeners.append(listener)

    def replay(self) -> list[dict[str, Any]]:
        """启动时读取全部历史事件并依次投递给订阅者。"""
        events: list[dict[str, Any]] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        for event in events:
            for listener in self._listeners:
                listener(event)
        return events

    def all_events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
