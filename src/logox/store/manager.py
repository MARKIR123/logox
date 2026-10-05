"""会话管理器与项目级多会话分桶索引（D98）。

对齐 Pi Agent 与 Claude Code 的会话管理模型：
1. 按工作区路径 (cwd) 分桶：`~/.logox/sessions/<slug_cwd>/`；
2. 单项目目录下容纳多个会话（每个会话为一个独立 `.jsonl` 文件）；
3. 提供 `list_sessions(cwd)`、`find_most_recent(cwd)`、`create_session(cwd)`；
4. 轻量扫描会话日志，秒级提取「会话标题摘要 · 轮次 · 时间」元数据。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from logox.paths import LogoxPaths
from logox.store.slug import slugify_cwd

logger = logging.getLogger(__name__)

__all__ = ["SessionInfo", "SessionManager"]

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """单个会话的轻量索引元数据（供列表展示、排序与接续）。"""

    session_id: str
    file_path: Path
    cwd: str
    created_at: float
    updated_at: float
    turn_count: int
    title_summary: str
    total_tokens: int = 0

    @property
    def formatted_updated_at(self) -> str:
        """格式化为易读的日期时间字符串（如 '2026-09-16 22:15'）。"""
        dt = datetime.fromtimestamp(self.updated_at)
        return dt.strftime("%Y-%m-%d %H:%M")


class SessionManager:
    """按工作区隔离的会话生命周期管理器。"""

    def __init__(self, base_sessions_dir: Path | None = None) -> None:
        self.base_dir = Path(base_sessions_dir) if base_sessions_dir else LogoxPaths.default().sessions

    def get_project_dir(self, cwd: Path | str) -> Path:
        """获取某个工作区对应的会话存储分桶目录。"""
        slug = slugify_cwd(cwd)
        return self.base_dir / slug

    def list_sessions(self, cwd: Path | str) -> list[SessionInfo]:
        """列出指定工作区下的所有历史会话，按最后修改时间 (mtime) 倒序排列。

        最近活跃的会话排在第 1 项（下标 0）。
        """
        project_dir = self.get_project_dir(cwd)
        if not project_dir.is_dir():
            return []

        results: list[SessionInfo] = []
        try:
            entries = list(project_dir.glob("*.jsonl"))
        except OSError as exc:
            logger.warning("扫描会话目录失败：%s", exc)
            return []

        for path in entries:
            if not path.is_file():
                continue
            info = self.scan_session_metadata(path, cwd=str(cwd))
            if info is not None:
                results.append(info)

        # 按最后修改时间倒序排列（最近活跃的在前）
        results.sort(key=lambda s: s.updated_at, reverse=True)
        return results

    def find_most_recent(self, cwd: Path | str) -> SessionInfo | None:
        """获取当前工作区最近活跃的会话；无会话时返回 None。"""
        project_dir = self.get_project_dir(cwd)
        if not project_dir.is_dir():
            return None
        candidates: list[tuple[float, Path]] = []
        try:
            for path in project_dir.glob("*.jsonl"):
                try:
                    if path.is_file():
                        candidates.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        except OSError as exc:
            logger.warning("扫描会话目录失败：%s", exc)
            return None
        # 稳定排序保持相同 mtime 下原目录枚举顺序；仅解析最新可读文件。
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, path in candidates:
            info = self.scan_session_metadata(path, cwd=str(cwd))
            if info is not None:
                return info
        return None

    def create_session(
        self,
        cwd: Path | str,
        session_id: str | None = None,
        initial_title: str = "",
    ) -> SessionInfo:
        """为当前工作区创建一个全新的会话文件并初始化。"""
        project_dir = self.get_project_dir(cwd)
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "tools").mkdir(parents=True, exist_ok=True)

        now = datetime.now()
        now_ts = now.timestamp()
        if not session_id:
            time_part = now.strftime("%Y-%m-%dT%H-%M-%S")
            short_uuid = uuid.uuid4().hex[:8]
            session_id = f"{time_part}_{short_uuid}"

        file_path = project_dir / f"{session_id}.jsonl"
        summary = self.extract_summary(initial_title) if initial_title else "新会话"

        # 如果文件不存在，写入首行元数据标记
        if not file_path.exists():
            header = {
                "version": 1,
                "session_id": session_id,
                "cwd": str(cwd),
                "created_at": now_ts,
                "timestamp": now_ts,
                "type": "session_init",
                "title": initial_title or "",
            }
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(header, ensure_ascii=False) + "\n")
            except OSError as exc:
                logger.warning("创建会话文件失败：%s", exc)

        return SessionInfo(
            session_id=session_id,
            file_path=file_path,
            cwd=str(cwd),
            created_at=now_ts,
            updated_at=now_ts,
            turn_count=0,
            title_summary=summary,
            total_tokens=0,
        )

    def delete_session(self, session_path: Path | str, *, soft: bool = True) -> Path:
        """删除指定会话文件（D104 会话回收站）。

        :param session_path: 会话 .jsonl 文件路径
        :param soft: True 时软删除移动到同项目 .trash/ 目录；False 时物理销毁
        :return: 最终目标路径
        """
        import shutil

        target = Path(session_path).resolve()
        if not target.exists():
            raise FileNotFoundError(f"会话文件不存在：{target}")

        if not soft:
            target.unlink()
            return target

        trash_dir = target.parent / ".trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        dest = trash_dir / target.name
        shutil.move(str(target), str(dest))
        return dest

    def extract_summary(self, prompt: str, max_len: int | None = None) -> str:
        """从用户指令或轮次摘要中提取干净的单行摘要（去除代码围栏、换行、多余空格，可选限制长度）。"""
        if not prompt or not prompt.strip():
            return "新会话"

        cleaned = prompt.strip()
        # 去掉 Markdown 标题或代码围栏前缀
        cleaned = re.sub(r"^[`#*\->\s]+", "", cleaned)
        cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()

        if not cleaned:
            return "新会话"

        if max_len is not None and len(cleaned) > max_len:
            return cleaned[:max_len].rstrip() + "..."
        return cleaned

    def scan_session_metadata(self, file_path: Path, cwd: str = "") -> SessionInfo | None:
        """轻量扫描单份 .jsonl 会话文件，提取关键统计元数据。"""
        try:
            stat = file_path.stat()
        except OSError:
            return None

        mtime = stat.st_mtime
        ctime = stat.st_ctime
        session_id = file_path.stem
        max_turn = 0
        turn_summaries: list[str] = []
        user_prompts: list[str] = []
        init_title = ""
        total_tokens = 0

        # 从文件名解析创建时间（如果符合 ISO 格式前缀）
        created_at = ctime
        if "_" in session_id:
            ts_str = session_id.split("_")[0]
            with contextlib.suppress(ValueError):
                created_at = datetime.strptime(ts_str, "%Y-%m-%dT%H-%M-%S").timestamp()

        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(record, dict):
                        continue

                    # 解析回合
                    turn = record.get("turn")
                    if isinstance(turn, int) and turn > max_turn:
                        max_turn = turn

                    # 收集轮次摘要 turn_summary
                    ts = record.get("turn_summary")
                    if isinstance(ts, str) and ts.strip():
                        turn_summaries.append(ts.strip())
                    elif record.get("type") == "turn_finished" or record.get("event_type") == "turn_finished":
                        content = record.get("content")
                        if isinstance(content, str) and content.strip():
                            turn_summaries.append(content.strip())

                    # 收集用户 prompt
                    role = record.get("role")
                    event_type = record.get("type") or record.get("event_type")
                    content = record.get("content")
                    if (role == "user" or event_type in ("user_prompt", "UserPromptSubmit")) and (
                        isinstance(content, str) and content.strip()
                    ):
                        user_prompts.append(content.strip())

                    if not init_title and record.get("type") == "session_init" and record.get("title"):
                        init_title = str(record["title"]).strip()

                    # 累加 token
                    meta = record.get("meta")
                    if isinstance(meta, dict):
                        tokens = meta.get("total_tokens") or meta.get("input_tokens")
                        if isinstance(tokens, int):
                            total_tokens += tokens
        except OSError as exc:
            logger.warning("读取会话文件 %s 失败：%s", file_path, exc)
            return None

        # 挑选最佳会话摘要：优先采用模型生成的有效轮次摘要，若第一轮为简单问候且有后续轮次则顺延
        greetings = ("问候", "问好", "打招呼", "你好", "hi", "hello")
        best_candidate = ""
        if turn_summaries:
            candidate = turn_summaries[0]
            if any(g in candidate for g in greetings) and len(turn_summaries) > 1:
                for item in turn_summaries[1:]:
                    if not any(g in item for g in greetings):
                        candidate = item
                        break
            best_candidate = candidate
        elif user_prompts:
            candidate = user_prompts[0]
            if candidate.lower().strip() in greetings and len(user_prompts) > 1:
                for item in user_prompts[1:]:
                    if item.lower().strip() not in greetings:
                        candidate = item
                        break
            best_candidate = candidate
        elif init_title:
            best_candidate = init_title
        else:
            best_candidate = "新会话"

        summary = self.extract_summary(best_candidate)

        return SessionInfo(
            session_id=session_id,
            file_path=file_path,
            cwd=cwd or str(file_path.parent),
            created_at=created_at,
            updated_at=mtime,
            turn_count=max_turn,
            title_summary=summary,
            total_tokens=total_tokens,
        )
