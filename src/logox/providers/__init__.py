"""模型适配层（M2）。

**这是整个项目里唯一允许出现厂商专有字段的地方**（D9）。下游拿到的
``DeltaEvent`` / ``ToolCallEvent`` / ``Usage`` 里不得出现 ``reasoning_content``、
``cache_read_input_tokens``、``prompt_tokens_details`` 这类词汇。

本包不依赖 ``EventBus``：它产出的是**自己的事件流**，由内核在循环里翻译成总线事件。
因此适配层可以用纯 ``async for`` 单测，不必启总线、也不必打网络。
"""

from __future__ import annotations

__all__: list[str] = []
