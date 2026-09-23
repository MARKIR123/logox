"""事件总线——Logox 内核的唯一主干与全部扩展机制（D7 / D23 / D25 / K1–K4）。

设计要点
--------
* **异常隔离**：任一订阅者抛异常都不影响其他订阅者，更不会中断 Agent 循环。
  ``asyncio.CancelledError`` **必须原样重新抛出**——否则按 Esc 会静默失效，
  这是最恶劣的一类体验缺陷（E-5/E-6）。
* **可见降级**：连续失败达阈值的订阅者被隔离（quarantine），并发布
  ``SubscriberQuarantined`` 事件（K3）。静默停用会让用户只看到「界面不刷新了」。
* **阻塞 / 非阻塞双通道**（K2）：必须在循环继续前完成的走 ``blocking=True``；
  慢且可落后的（日志落盘、重绘）走 ``blocking=False``，进入有界队列，
  **满时丢弃最旧的并计数**——日志绝不能拖慢 Agent 循环。

  ⚠️ **已知边界**：所有非阻塞订阅者**共享同一条队列与同一个消费者协程**，
  因此它们**彼此之间并不隔离**——一个慢订阅者会推迟排在其后的其他慢订阅者。
  这对 Agent 循环无影响（它们都不在主链路上），但若将来出现第二个耗时的非阻塞订阅者，
  就需要改造成"每订阅者一条队列"。现在不做的理由：只有一个慢订阅者时，
  改造成本换不来收益。
* **总线不做超时**（K1）：超时是订阅者自己的责任（钩子用 ``asyncio.wait_for``）。
* **重入保护**：嵌套发布深度上限，兜住互相发布的死循环（E-7/E-8）。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from logox.errors import BusClosedError, SessionMismatchError
from logox.kernel.events import AnyEvent, ErrorOccurred, Event, SubscriberQuarantined

__all__ = [
    "PRIORITY_BUILTIN",
    "PRIORITY_CORE",
    "PRIORITY_HOOK",
    "PRIORITY_PLUGIN",
    "DispatchReport",
    "EventBus",
    "Handler",
    "Subscription",
]

logger = logging.getLogger("logox.kernel.bus")

Handler = Callable[[AnyEvent], Awaitable[None]]

PRIORITY_CORE = 50
"""内核自身（最先执行）。"""
PRIORITY_BUILTIN = 100
"""内置能力：权限、度量归约、持久化。"""
PRIORITY_PLUGIN = 200
"""L2 插件。"""
PRIORITY_HOOK = 900
"""观察型 shell 钩子（永远最后，保证界面与日志先看到事件，D23）。"""

MATCH_ALL = "*"

_VERSION_RE = re.compile(r"^(>=|<=|==|!=|>|<)\s*(\d+)$")


def _is_async_callable(obj: Any) -> bool:
    """判断是否为 async 可调用对象（E-22：同步函数订阅必须在订阅时报错）。

    这里用 ``getattr(obj, "__call__", None)`` 而不是 ``callable(obj)`` 是**刻意的**：
    我们要拿到的不是"能不能调用"，而是**那个 ``__call__`` 本身是不是协程函数**
    ——一个实现了 ``async def __call__`` 的实例对象也必须被认出来。
    """
    if inspect.iscoroutinefunction(obj):
        return True
    call = getattr(obj, "__call__", None)  # noqa: B004 - 见上：需要拿到 __call__ 本体
    return call is not None and inspect.iscoroutinefunction(call)


def _version_satisfies(version: int, spec: str) -> bool:
    """支持 ``">=1,<2"`` 这类逗号分隔的比较式（D7 的插件兼容声明）。"""
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        match = _VERSION_RE.match(part)
        if match is None:
            logger.warning("无法解析版本声明 %r，视为不兼容", part)
            return False
        op, raw = match.group(1), int(match.group(2))
        if op == ">=" and not version >= raw:
            return False
        if op == "<=" and not version <= raw:
            return False
        if op == ">" and not version > raw:
            return False
        if op == "<" and not version < raw:
            return False
        if op == "==" and version != raw:
            return False
        if op == "!=" and version == raw:
            return False
    return True


class Subscription(BaseModel):
    """一条订阅登记（可变：需要累计失败次数与隔离状态）。

    ``handler`` 用 ``Any`` 承载并排除在序列化之外——订阅条目会经
    ``quarantined()`` 暴露给 ``/debug`` 面板，但可调用对象不可序列化，
    也不必对外暴露。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    event_type: str = MATCH_ALL
    priority: int = PRIORITY_BUILTIN
    blocking: bool = True
    requires_api: str | None = None
    failures: int = 0
    quarantined: bool = False
    skipped_reported: bool = False
    handler: Any = Field(default=None, exclude=True, repr=False)


class DispatchReport(BaseModel):
    """一次发布的投递结果（测试断言与 ``/debug`` 展示都用它）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    delivered: int = 0
    skipped: int = 0
    failed: int = 0
    quarantined: int = 0
    dropped_depth: int = 0
    dropped_queue: int = 0


class EventBus:
    """优先级 + 双通道 + 异常隔离的事件总线。"""

    def __init__(
        self,
        *,
        session_id: str,
        max_depth: int = 5,
        quarantine_after: int = 3,
        queue_size: int = 1024,
    ) -> None:
        if max_depth < 1:
            raise ValueError("max_depth 必须 ≥ 1")
        if quarantine_after < 1:
            raise ValueError("quarantine_after 必须 ≥ 1")

        self._session_id = session_id
        self._max_depth = max_depth
        self._quarantine_after = quarantine_after
        self._subscriptions: list[Subscription] = []
        self._depth = 0
        self._closed = False
        self._queue: asyncio.Queue[tuple[Subscription, AnyEvent]] | None = (
            asyncio.Queue(maxsize=queue_size) if queue_size > 0 else None
        )
        self._consumer: asyncio.Task[None] | None = None
        self._dropped_queue = 0
        self._depth_errors: list[str] = []

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def dropped_queue(self) -> int:
        """因队列写满而被丢弃的事件数（``/debug`` 面板显示，K2）。"""
        return self._dropped_queue

    def subscriptions(self) -> list[Subscription]:
        return list(self._subscriptions)

    def quarantined(self) -> list[Subscription]:
        return [s for s in self._subscriptions if s.quarantined]

    # ------------------------------------------------------------------ #
    # 订阅管理
    # ------------------------------------------------------------------ #

    def subscribe(
        self,
        event_type: type[Event] | str,
        handler: Handler,
        *,
        name: str,
        priority: int = PRIORITY_BUILTIN,
        blocking: bool = True,
        requires_api: str | None = None,
    ) -> Subscription:
        """登记一个订阅者。

        :param event_type: 事件类；传基类 :class:`Event` 表示订阅**全部**事件。
        :param blocking: ``True`` 时在 ``publish`` 中顺序 await；
            ``False`` 时投递到内部队列（慢且可落后的订阅者用）。
        :param requires_api: 形如 ``">=1,<2"`` 的版本兼容声明（D7）。
        :raises TypeError: ``handler`` 不是 async 可调用对象（E-22）。
        """
        if self._closed:
            raise BusClosedError(self._session_id)
        if not _is_async_callable(handler):
            raise TypeError(
                f"订阅者 {name!r} 必须是 async 可调用对象（如 async def handler(event)）；"
                f"收到的是 {type(handler).__name__}"
            )

        subscription = Subscription(
            name=name,
            event_type=self._type_name_of(event_type),
            priority=priority,
            blocking=blocking,
            requires_api=requires_api,
            handler=handler,
        )
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """取消订阅（幂等：重复调用不报错，E-14）。"""
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    @staticmethod
    def _type_name_of(event_type: type[Event] | str) -> str:
        if isinstance(event_type, str):
            return event_type
        if event_type is Event:
            return MATCH_ALL
        literal_default = event_type.model_fields["type"].default
        if not isinstance(literal_default, str):  # pragma: no cover - 防御性
            raise TypeError(f"{event_type.__name__} 未定义 type 字段的字面默认值")
        return literal_default

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #

    async def publish(self, event: Event) -> DispatchReport:
        """发布事件并等待全部**阻塞**订阅者处理完成。

        :raises BusClosedError: 总线已关闭（E-11）。
        :raises SessionMismatchError: 事件的 ``session_id`` 与总线不符（E-10）。
        """
        if self._closed:
            raise BusClosedError(self._session_id)
        if event.session_id != self._session_id:
            raise SessionMismatchError(self._session_id, event.session_id)

        report = DispatchReport()
        self._depth += 1
        try:
            if self._depth > self._max_depth:
                message = (
                    f"事件嵌套发布深度超过上限 {self._max_depth}"
                    f"（type={event.type}）——已丢弃，疑似订阅者互相发布形成循环"
                )
                logger.warning(message)
                self._depth_errors.append(message)
                return DispatchReport(dropped_depth=1)
            report = await self._dispatch(event)
        finally:
            self._depth -= 1

        # 深度超限的告警在**栈展开后**再发布，否则新事件会立刻再次超限（E-7）。
        if self._depth == 0 and self._depth_errors:
            pending, self._depth_errors = self._depth_errors, []
            for message in pending:
                try:
                    await self.publish(self._make_error_event(message))
                except Exception:  # pragma: no cover - 告警失败不得影响主流程
                    logger.exception("发布深度超限告警失败")

        return report

    async def _dispatch(self, event: Event) -> DispatchReport:
        delivered = skipped = failed = quarantined = 0

        targets = [s for s in self._subscriptions if s.event_type in (MATCH_ALL, event.type)]
        targets.sort(key=lambda s: s.priority)

        for subscription in targets:
            if subscription.quarantined:
                skipped += 1
                continue
            if subscription.requires_api and not _version_satisfies(event.version, subscription.requires_api):
                skipped += 1
                if not subscription.skipped_reported:
                    subscription.skipped_reported = True
                    logger.warning(
                        "订阅者 %r 声明 requires_api=%r，与 %s v%d 不兼容，已跳过（不再重复提醒）",
                        subscription.name,
                        subscription.requires_api,
                        event.type,
                        event.version,
                    )
                continue

            if subscription.blocking:
                outcome = await self._invoke(subscription, event)
                delivered += int(outcome == "delivered")
                failed += int(outcome == "failed")
                quarantined += int(outcome == "quarantined")
            else:
                self._enqueue(subscription, event)

        return DispatchReport(
            delivered=delivered,
            skipped=skipped,
            failed=failed,
            quarantined=quarantined,
            dropped_queue=self._dropped_queue,
        )

    async def _invoke(self, subscription: Subscription, event: AnyEvent) -> str:
        """调用单个订阅者并处理其异常。返回 ``delivered`` / ``failed`` / ``quarantined``。"""
        try:
            await subscription.handler(event)
        except asyncio.CancelledError:
            # 必须原样抛出：吞掉它会让 Esc 中断静默失效（E-5/E-6）。
            raise
        except Exception as exc:
            subscription.failures += 1
            logger.exception("订阅者 %r 处理 %s 时失败（第 %d 次）", subscription.name, event.type, subscription.failures)
            if subscription.failures >= self._quarantine_after and not subscription.quarantined:
                subscription.quarantined = True
                await self._announce_quarantine(subscription, exc)
                return "quarantined"
            return "failed"
        else:
            subscription.failures = 0  # 一次成功即归零，避免偶发失败累积误杀（E-4）
            return "delivered"

    async def _announce_quarantine(self, subscription: Subscription, exc: BaseException) -> None:
        logger.error(
            "订阅者 %r 连续失败 %d 次，已隔离并停止调用；相关能力（日志/渲染/持久化等）可能不再更新",
            subscription.name,
            subscription.failures,
        )
        try:
            await self.publish(
                SubscriberQuarantined(
                    session_id=self._session_id,
                    turn=0,
                    subscriber=subscription.name,
                    failures=subscription.failures,
                    last_error=f"{type(exc).__name__}: {exc}"[:500],
                )
            )
        except Exception:  # pragma: no cover - 公告失败不得影响主流程
            logger.exception("发布 SubscriberQuarantined 失败")

    def _make_error_event(self, message: str) -> ErrorOccurred:
        return ErrorOccurred(
            session_id=self._session_id,
            turn=0,
            category="bus_recursion",
            message=message,
            retryable=False,
        )

    # ------------------------------------------------------------------ #
    # 非阻塞通道
    # ------------------------------------------------------------------ #

    def _ensure_consumer(self) -> None:
        """确保非阻塞消费者任务存活（E-19：意外死亡后自动重建）。"""
        if self._queue is None:
            return
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.get_running_loop().create_task(self._consume())

    def _enqueue(self, subscription: Subscription, event: AnyEvent) -> None:
        if self._queue is None:
            logger.warning("未启用非阻塞通道，丢弃订阅者 %r 的事件 %s", subscription.name, event.type)
            return
        self._ensure_consumer()
        try:
            self._queue.put_nowait((subscription, event))
        except asyncio.QueueFull:
            # K2：丢弃最旧的并计数——日志与重绘绝不能拖慢 Agent 循环。
            with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover - 竞态兜底
                self._queue.get_nowait()
                self._queue.task_done()
                self._dropped_queue += 1
            with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover - 竞态兜底
                self._queue.put_nowait((subscription, event))

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            subscription, event = await self._queue.get()
            try:
                await self._invoke(subscription, event)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - _invoke 已兜住，这里仅防消费者猝死
                logger.exception("非阻塞消费者处理事件时发生未预期异常")
            finally:
                # 无论成功、失败还是被取消，都必须恰好调用一次 task_done()，
                # 否则 drain() 会永久挂起（E-18）。
                self._queue.task_done()

    async def drain(self) -> None:
        """等待非阻塞队列清空（退出与测试用）。

        已关闭时**立即返回**，不得挂死（E-18）。
        """
        if self._closed or self._queue is None:
            return
        if self._consumer is None or self._consumer.done():
            return
        await self._queue.join()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def aclose(self) -> None:
        """关闭总线：取消消费者任务；此后 ``publish`` 抛 :class:`BusClosedError`。"""
        self._closed = True
        consumer, self._consumer = self._consumer, None
        if consumer is not None and not consumer.done():
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - 关闭期的异常不阻塞退出
                logger.exception("关闭非阻塞消费者时发生异常")

    def reset(self, session_id: str) -> None:
        """复用同一个总线对象开启新会话（``/resume`` 场景）。"""
        self._session_id = session_id
        self._closed = False
        self._depth = 0
        self._depth_errors.clear()
        self._dropped_queue = 0
        for subscription in self._subscriptions:
            subscription.failures = 0
            subscription.quarantined = False
            subscription.skipped_reported = False
