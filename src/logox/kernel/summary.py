"""回合摘要的**位置契约**：摘要 = 最终答复的最后一行（D135 第二步）。

为什么不再用标签
================
D114 起用 ``<turn_summary>`` 标签承载摘要，D116 又加了"四重防线"。实测那条路**易碎且危险**：
模型在正文里"提到"这个标签就会被误判 —— 扫 9 个会话 / 55 轮，**4 轮（7.3%）**把正文
当成了摘要（最长吞掉 **2873 字**）；而症状看起来像"模型输出被截断"，
**把排查方向带偏了好几轮**（连当时的 agent 自己都解释错了）。

新机制是**位置**：摘要 = 最终答复里最后一个"有实质内容"的行。
不需要模型声明任何标记 ⇒ **无标记、无变体、无带内歧义**，也不需要跨 chunk 状态机。

什么时候判定"这是最终答复"
==========================
**不是模型说了算**：内核现有的判据就够了 ——
``无工具调用`` && ``有正文`` && ``stop_reason != "max_tokens"``（`loop.py` 的"场景 2"）。
⚠️ 注意 ``turn_finished`` **不能**用来判断"模型有没有输出摘要"：它是**我们自己**的事件，
其 ``turn_summary`` 就是我们已经抽好的摘要（循环论证）。

三层兜底（用户裁定 Q-D）
======================
1. **末尾行**（:func:`extract_trailing_summary`）—— 主路径，零成本；
2. **模型补写**（`loop.py` 的 ``_summarize_turn_via_model``）—— 第 1 层不合规时再问一次模型；
3. **本地生成**（:func:`deterministic_summary`）—— 最后一道，绝不再调模型。

不变量
======
**任何一层都不得修改正文。** 本模块只"读 + 判定"，返回摘要与拒绝原因；
正文永远原样交给界面与落盘（"用摘要替换正文"只发生在**发给模型的压缩历史**里）。
"""

from __future__ import annotations

import re

from logox.kernel.messages import Message, TextBlock

__all__ = [
    "INTERRUPT_SUMMARY_QUESTION_CHARS",
    "SUMMARY_MIN_CHARS",
    "SUMMARY_MODEL_FALLBACK_MIN_CHARS",
    "SUMMARY_SYSTEM_PROMPT",
    "SummaryVerdict",
    "deterministic_summary",
    "extract_trailing_summary",
    "interrupted_summary",
    "is_substantive_line",
    "normalize_model_summary",
    "render_turn_transcript",
]

#: 值得为它**再调一次模型**补写摘要的最小正文长度（可见字符）。
#:
#: 这是一个**成本闸门**，不是正确性门：两字回答（"ok" / "完成"）走本地兜底就够了，
#: 为它再发一次请求纯属浪费；而"真干了活"的轮次（调过工具、或正文很长）才值得。
#: 触发条件：``正文长度 ≥ 本值`` **或** ``本轮调过工具``。
SUMMARY_MODEL_FALLBACK_MIN_CHARS = 200

#: 摘要的**下限**：比这还短的多半不是摘要（"好了"、"完成"、"无"）。
SUMMARY_MIN_CHARS = 8

#: 摘要的**上限**：★ **CHANGE-052 已删除**（用户裁定：摘要多少**完全靠契约与
#: 模型的自觉**）。下方沿革**保留** —— 它记录了同一件事曾有过**四个数**，
#: 以及"为什么每一档都该消失"。
#:
#: （旧语义）超过它就不是"摘要"而是"正文段落"，
#: 用来识别"模型没写摘要、而整段回答恰好只有一行"那种情形。
#:
#: ⚠️ **它不再是"长度限制"。** 从前这里有两档（`SUMMARY_MAX_CHARS = 100` 收下并截断 +
#: `SUMMARY_HARD_MAX_CHARS = 300` 拒绝），提示词里另写着"不超过 60 字"——**同一件事四个数**。
#: 实测（37 条真实摘要）：
#:
#: * **49%（18 条）超过 60 字** —— 提示词里的"60 字"契约近一半没被遵守；
#: * **27%（10 条）以「…」结尾** —— 被截断过；
#: * 而**截断砍掉的永远是尾部** —— 可契约恰恰要求把"踩坑与修法""待办"放在尾部。
#:
#: ⟹ **截断系统性地删掉最有价值的那部分。** 实例：本项目某一轮的摘要原文以
#: "…并发现 `keep_recent_turns` 配置仍无人读" 结尾（一个待办 + 一组实测数字），
#: 落盘时被砍到 100 字，**恰好把这段砍掉了**，只留下前面"做了什么"。
#: 也就是说：**读者能自己大致推断的东西留下了，只有摘要能记住的东西丢了。**
#:
#: 因此现在的口径（用户原话："每轮做的事就是有多有少，我们应该限制模型在
#: 尽可能少的文本下把话说清楚"）：
#:
#: ★ **CHANGE-052 起：不再有上限这一档。**
#: 末尾行 **≥ `SUMMARY_MIN_CHARS` 即收下**，长度本身不再触发拒绝。
#: 判断"是不是摘要"只剩三道**结构**守卫（代码块内 / 结构行开头 / 过短）——
#: 它们防的是"正文段落"，而**长度从来不是判断这件事的好指标**。
#:
#: 为什么删（用户裁定）：
#:
#: * **它防的东西结构守卫已经防住**：正文段落的结尾极少恰好是"一句像摘要的单行"；
#: * **它误伤的东西很实在**：实测 `summary_reason` 里 **4 轮**
#:   `too_long → l2_rejected → l3` —— 模型写了摘要、被字数否决，
#:   于是换成"抓正文第一行"。**用户想要的摘要被换掉了**；
#: * 折叠结构改为**逐轮 user 全文 + assistant 摘要**后，摘要长短只影响
#:   token 预算，**不再是"收不收"的问题**。
#:
#: ⚠️ **代价（登记）**：不再有 `too_long` 之后，"模型开始不守契约写超长摘要"
#: 会**静默接受** ⇒ 必须**保留长度度量**（只记不拒），否则此事不可观测。
#:
#: ⚠️ **没有例外**（CHANGE-052 裁定）：连 `deterministic_summary` 的兜底
#: 也不再截断 —— "简洁"整体转由**契约软约束**承担。
#: 登记风险：兜底抓的是**正文里的一行**、不受契约约束，遇到无换行的超长段落
#: 会整段返回（该轮几乎没被压缩）。接受，因为它是罕见降级路径。
#: ==================== ==================================================
#: 末尾行长度             处理
#: ==================== ==================================================
#: 8 ～ 300             **原样收下，一个字都不改**（``ok``）
#: > 300               拒绝（``too_long``）—— 那是正文段落，不是摘要
#: ==================== ==================================================
#:
#: **短由契约管，长由类型判。** 要"短"就改提示词（那是可以被遵守的），
#: 而不是靠事后截断（那只能丢信息，永远不能让摘要更清楚）。
#: ★ CHANGE-052：`SUMMARY_MAX_CHARS` **已删除**（见上）。
#: 上方表格保留为历史（"< 300 收下 / > 300 拒绝"那两档不再存在）。

#: 这些开头是**结构行**，不是摘要：表格 / 列表 / 标题 / 引用 / 代码围栏。
_STRUCTURAL_PREFIX = re.compile(r"^\s*(?:[|>#*+\-]|```|~~~|\d+[.)、]\s|[-*+]\s)")

#: "有实质内容"：至少含一个字母 / 数字 / CJK。纯符号行（`---`、`===`、`|`）不算。
_SUBSTANTIVE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


class SummaryVerdict:
    """一次"末尾行判定"的结果（**带原因**，便于度量与调试）。"""

    __slots__ = ("reason", "summary")

    def __init__(self, summary: str | None, reason: str) -> None:
        self.summary = summary
        #: 取值：``ok`` / ``no_text`` / ``no_substantive_line`` / ``structural`` /
        #: ``too_short`` / ``inside_code_block``
        #: ⚠️ 已退役的取值（读到当作历史信号即可，不再产生）：
        #: * ``truncated`` —— D160 起不再截断；
        #: * ``too_long`` —— **CHANGE-052 起连上限都删除了**。
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"SummaryVerdict(summary={self.summary!r}, reason={self.reason!r})"


def is_substantive_line(line: str) -> bool:
    """这一行"有实质内容"吗？（空行、纯符号行都不算 —— 用户裁定 Q-B）

    ``---`` / ``===`` / ``***`` / ``|`` 这类**分隔线与表格残行**在技术性长回答里非常常见
    （实测 25% 的末尾是结构行），它们绝不能当摘要。
    """
    stripped = line.strip()
    if not stripped:
        return False
    return _SUBSTANTIVE.search(stripped) is not None


def _code_block_mask(lines: list[str]) -> list[bool]:
    """标出每一行是否**处在代码块内部**（含围栏行本身）。

    为什么要它：模型忘了写摘要、而回答以代码块结尾时，"最后一个有实质内容的行"
    很可能是**代码里的某一行**（它又短又不以结构符号开头，前缀判据挡不住）。
    围栏计数是**精确**的，比关键词猜测可靠。
    """
    mask: list[bool] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        is_fence = stripped.startswith("```") or stripped.startswith("~~~")
        if is_fence:
            mask.append(True)  # 围栏行本身也不能当摘要
            inside = not inside
            continue
        mask.append(inside)
    return mask


def extract_trailing_summary(text: str) -> SummaryVerdict:
    """从**最终答复正文**里取末尾摘要（位置契约的主路径）。只读，不改正文。

    判定顺序（每一步都留下原因，便于度量）：

    1. 从后往前找**最后一个"有实质内容"的行**（空行 / 纯符号行跳过 —— Q-B）；
    2. 它在**代码块内部** ⇒ 拒绝（``inside_code_block``）；
    3. 它以结构符号开头（``|`` / ``-`` / ``#`` / 围栏 / 编号列表）⇒ 拒绝（``structural``）；
    4. 短于 ``SUMMARY_MIN_CHARS`` ⇒ 拒绝（``too_short``）；
    5. 否则**原样收下**（``ok``）——★ CHANGE-052：**长度不再触发拒绝**
       （旧行为是超过 300 字 ⇒ ``too_long``）。
    """
    if not text or not text.strip():
        return SummaryVerdict(None, "no_text")

    lines = text.splitlines()
    code_mask = _code_block_mask(lines)

    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if not is_substantive_line(line):
            continue  # 空行 / 纯符号行：**跳过继续往前找**（Q-B）
        if code_mask[index]:
            return SummaryVerdict(None, "inside_code_block")
        stripped = line.strip()
        if _STRUCTURAL_PREFIX.match(stripped):
            return SummaryVerdict(None, "structural")
        if len(stripped) < SUMMARY_MIN_CHARS:
            return SummaryVerdict(None, "too_short")
        # ★ CHANGE-052：**长度上限已删除** —— 不再有 `too_long` 这一档。
        #   理由见 `SUMMARY_MIN_CHARS` 上方那段沿革：上限防的"正文段落"已由
        #   上面两道结构守卫拦住，而它误伤了 4 轮真实摘要（模型写了却被否决）。
        #   要"短"就改提示词 —— 那是可以被遵守的；事后拒绝只会把摘要换成兜底。
        #
        # ★ D160：**原样收下，一个字都不改**（从前这里会截断到 100 字）。
        #   截断只能丢信息，永远不能让摘要更清楚 —— 实测它系统性砍掉尾部，
        #   而契约要求把"踩坑与修法""待办"放在尾部。
        return SummaryVerdict(stripped, "ok")

    return SummaryVerdict(None, "no_substantive_line")


def normalize_model_summary(text: str) -> str | None:
    """把**第 2 层（模型补写）**的返回归一成一句摘要；不合规返回 ``None``。

    为什么补写也要校验：模型对"写一句摘要"的执行力并不比它对"写在最后一行"更好 ——
    实测它会带解释（"好的，这是摘要：…"）、会写多行、会写成标题。
    不校验就会把这种噪声当摘要存进列表与压缩索引（那就等于把坑从一层搬到另一层）。
    """
    # ★ 不要把所有行 join 成一行再撞长度上限（D135-4）：模型补写时**常带半句解释**，
    #   join 之后整条超长 ⇒ 一律被拒 ⇒ 静默降级（等于白花了一次请求）。
    #   正确做法是**挑最像摘要的那一行**：优先不是结构行/标题的那些。
    # ★ CHANGE-052：上限删除 ⇒ 不再需要"优先挑能放下的那条"，直接在合格行里挑最长。
    candidates = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and is_substantive_line(line) and not _STRUCTURAL_PREFIX.match(line.strip())
    ]
    if not candidates:
        return None
    cleaned = max(candidates, key=len)
    if not cleaned:
        return None
    # 常见的“客套开头”直接剥掉，而不是整句丢弃
    for prefix in ("摘要：", "摘要:", "本轮摘要：", "本轮摘要:", "总结：", "总结:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    if not is_substantive_line(cleaned) or _STRUCTURAL_PREFIX.match(cleaned):
        return None
    if len(cleaned) < SUMMARY_MIN_CHARS:
        return None
    # ★ CHANGE-052：**不再有上限判定**（旧行为：超 300 字 ⇒ 拒收 ⇒ 降级到 L3）。
    #   长度不再触发拒绝 —— 详细理由见 `SUMMARY_MIN_CHARS` 上方那段沿革。
    return cleaned


def deterministic_summary(message: Message, *, tool_call_count: int = 0, turn_index: int = 0) -> str:
    """**第 3 层兜底**：不调模型，从正文里确定性地拼一句（永远不会失败）。

    取"正文里第一个**像一句摘要**的行"（剥掉 markdown 前缀）。
    ⚠️ 这里**没有任何上限**（CHANGE-052）：D160 不再按 60 字截断、
    CHANGE-052 连"安全上限 `[:SUMMARY_MAX_CHARS]`"也删了 —— 用户裁定
    「不要有上限，但是一定要在契约强调要输出简洁的摘要」。
    正文里一句可用的话都没有时，退到"执行了 N 次工具操作"/"完成第 N 轮交互"。

    ⚠️ **必须过滤过短的行**（D135-4）：本兜底曾经直接取"第一个有实质内容的行"，
    而真机实测里那个行是 **"一句话"**（3 字，模型在回答里写的小标题）——
    结果摘要字段里就躺着 "一句话"，等于没有信息。
    """
    for block in message.blocks:
        if not isinstance(block, TextBlock):
            continue
        for line in block.text.splitlines():
            if not is_substantive_line(line):
                continue
            first = line.strip().lstrip("#*->| ").strip()
            if len(first) < SUMMARY_MIN_CHARS:
                continue  # ★ 太短：小标题/口头语（"一句话"、"结论"），不是摘要
            # ★ CHANGE-052：**不再做上限截断**（旧行为：`first[:300]`）。
            #   用户裁定："不要有上限，但一定要在契约强调要输出简洁的摘要" ——
            #   即"简洁"从此由**契约软约束**承担，不再有系统硬截断。
            #   ⚠️ 登记风险：本兜底抓的是**正文里的一行**，不受契约约束 ——
            #   遇到无换行的超长段落，它会整段返回，该轮几乎没被压缩。
            #   接受该风险：这一档只在"模型未按契约给出摘要"时才走到（罕见的降级路径）。
            return first
    if tool_call_count > 0:
        return f"执行了 {tool_call_count} 次工具操作并完成"
    return f"完成第 {turn_index} 轮交互"


#: 异常终止轮次的摘要里，用户提问最多带多少字（CHANGE-052）。
#:
#: ⚠️ 这是**我们自己代写的标签**，不是模型写的摘要 —— 所以它**必须自带上限**：
#: 用户的提问可以很长（真机实测有 690 字的大段粘贴），而摘要契约管不到我们。
#: 这与"摘要不设上限"（裁定）**不矛盾**：那条说的是**模型写的**摘要 ——
#: 它有契约约束自觉写短；兜底标签没有任何东西约束它。
INTERRUPT_SUMMARY_QUESTION_CHARS = 200


def interrupted_summary(user_text: str, reason: str) -> str:
    """异常终止轮次的摘要：**用户问题 + 异常说明**（CHANGE-052，用户裁定）。

    用户原话：「如果有被中断的轮次，摘要应该是**用户问题 + 该轮次被异常中断**」。

    为什么原先那种写法（只有 `在第 N 轮被中断`）不够：
    折叠之后该轮的**原文整段消失**，摘要是模型唯一能看到的东西。而
    "第 7 轮被中断"**没有说这一轮想干什么** —— 模型接着干活时不知道
    "用户当时要的那个东西"还需不需要做。

    ⚠️ 两处形态约束（都会影响下游）：

    * **必须压成单行**：用户在编辑框里可以贴多段文本，摘要却是"一行标签"，
      嵌了换行会污染折叠结构与落盘记录；
    * **提问截到 `INTERRUPT_SUMMARY_QUESTION_CHARS`**：见该常量上方说明。
    """
    question = " ".join((user_text or "").split())
    if len(question) > INTERRUPT_SUMMARY_QUESTION_CHARS:
        question = question[: INTERRUPT_SUMMARY_QUESTION_CHARS - 1] + "…"
    return f"提问：{question}；{reason}" if question else reason


# --------------------------------------------------------------------------- #
# 第 2 层兜底：让模型补写一句摘要（上下文只用**本轮**对话，且不写回历史）
# --------------------------------------------------------------------------- #

SUMMARY_SYSTEM_PROMPT = """你是对话摘要器。用户会给你一段刚刚结束的对话记录。

请用**一行**总结这一轮：做了什么、有什么具体产物
（文件名 / 函数名 / 命令 / 报错关键字）、踩了什么坑又是怎么修的、留下了什么待办。

**用尽可能少的字把话说清楚**（每轮做的事有多有少，所以没有固定字数）——
但宁可多写几个字，也不要省掉上面任何一桶。自检：删掉某个短语之后，
读的人还明白这一轮发生了什么吗？还明白 → 它本来就多余；不明白 → 它必须留着。

只输出这一行摘要本身：不加前缀、不加标签、不加引号、不要换行、不要解释你在做什么。"""


def render_turn_transcript(
    messages: list[Message], *, per_message_chars: int = 1500, total_chars: int = 12_000
) -> str:
    """把**本轮**消息渲染成给摘要器的纯文本（有界长度，避免为摘要付大 token）。

    为什么要有界：这一层是"本来没拿到摘要"时的补救，成本不该失控 ——
    所以每条消息截断，整体再截断一次（保留**头部**，因为"这一轮要做什么"通常在开头）。
    """
    chunks: list[str] = []
    for message in messages:
        body = message.text.strip()
        if not body:
            continue
        if len(body) > per_message_chars:
            body = body[:per_message_chars] + "…（截断）"
        chunks.append(f"[{message.role}] {body}")
    transcript = "\n\n".join(chunks)
    if len(transcript) > total_chars:
        transcript = transcript[:total_chars] + "\n…（后续截断）"
    return transcript
