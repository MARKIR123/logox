"""代码回滚与外部漂移防卫引擎（D102 / L4）。

核心流程：
1. **外部漂移检测（External Drift Guard）**：回滚前检查磁盘物理文件是否被外部编辑器修改；
2. **时空穿梭原子还原（Time-Travel Restore）**：
   - 提取目标区间（turn >= to_turn）中每个文件的最早修改前快照（earliest_before_hash）；
   - 若最早修改前为 None（新创建的文件），安全删除物理文件；
   - 若存在快照哈希，从 CAS BlobStore 原子写还原文件。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from logox.store.blob import BlobStore
from logox.store.checkpoint import ConflictInfo, RewindResult

logger = logging.getLogger(__name__)

__all__ = ["check_conflicts", "execute_rewind"]


def _active_checkpoint_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤出当前活跃的 checkpoint 记录，自动剪裁已被历史 session_rewind 截断的记录。"""
    active: list[dict[str, Any]] = []
    for r in records:
        event_type = r.get("type")
        if event_type == "session_rewind":
            rewind_to = int(r.get("to_turn", 0))
            active = [item for item in active if int(item.get("turn", 0)) < rewind_to]
        elif event_type == "checkpoint":
            active.append(r)
    return active


def _validated_targets(records, to_turn, cwd):
    root = cwd.resolve()
    targets = [r for r in _active_checkpoint_records(records) if int(r.get("turn", 0)) >= to_turn]
    for record in targets:
        raw = str(record.get("path", "")).strip()
        if not raw:
            raise ValueError("检查点路径为空")
        path = (root / raw).resolve()
        path.relative_to(root)
        if path == root:
            raise ValueError("检查点不能指向工作区根目录")
        for key in ("before_hash", "after_hash"):
            value = record.get(key)
            if value is None and key == "before_hash":
                continue
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
                raise ValueError(f"检查点 {raw} 的 {key} 不合法")
    return targets


def check_conflicts(
    records: list[dict[str, Any]],
    to_turn: int,
    cwd: Path,
) -> list[ConflictInfo]:
    """比对磁盘上物理文件的当前哈希与快照记录中的预期 after_hash，检测外部漂移。"""
    target_records = _validated_targets(records, to_turn, cwd)

    # 取每个文件在该区间内【最后一次修改后】的 after_hash 作为物理文件应该处于的基准哈希
    latest_after_hashes: dict[str, str] = {}
    for r in target_records:
        path = str(r.get("path", "")).replace("\\", "/")
        latest_after_hashes[path] = str(r.get("after_hash", ""))

    earliest_before = {}
    for r in target_records:
        earliest_before.setdefault(str(r["path"]).replace("\\", "/"), r.get("before_hash"))
    conflicts: list[ConflictInfo] = []
    for rel_path, expected_after in latest_after_hashes.items():
        disk_file = cwd / rel_path
        if not disk_file.exists():
            if earliest_before[rel_path] is None:
                continue
            conflicts.append(
                ConflictInfo(
                    path=rel_path,
                    disk_hash="<deleted>",
                    expected_hash=expected_after,
                )
            )
        else:
            try:
                current_hash = hashlib.sha256(disk_file.read_bytes()).hexdigest()
                if current_hash not in {expected_after.lower(), str(earliest_before[rel_path]).lower()}:
                    conflicts.append(
                        ConflictInfo(
                            path=rel_path,
                            disk_hash=current_hash,
                            expected_hash=expected_after,
                        )
                    )
            except OSError as exc:
                conflicts.append(
                    ConflictInfo(
                        path=rel_path,
                        disk_hash=f"<error: {exc}>",
                        expected_hash=expected_after,
                    )
                )
    return conflicts


def execute_rewind(
    records: list[dict[str, Any]],
    to_turn: int,
    cwd: Path,
    blob_store: BlobStore,
    *,
    force: bool = False,
) -> RewindResult:
    """执行文件回滚：还原修改前文件并删除新建文件。"""
    try:
        target_records = _validated_targets(records, to_turn, cwd)
        conflicts = check_conflicts(records, to_turn, cwd)
    except (ValueError, OSError, TypeError) as exc:
        return RewindResult(success=False, to_turn=to_turn, message=f"回滚预检查失败，未修改文件：{exc}")
    if conflicts and not force:
        return RewindResult(
            success=False,
            to_turn=to_turn,
            conflicts=conflicts,
            message=f"检测到 {len(conflicts)} 处外部文件修改冲突，已终止回滚。",
        )

    if not target_records:
        return RewindResult(
            success=True,
            to_turn=to_turn,
            conflicts=conflicts,
            message="目标轮次区间没有代码修改记录，无需还原文件。",
        )

    # 计算每个文件在该区间内的【最早一次修改前】状态（earliest_before_hash）
    # target_records 必须按记录顺序遍历
    earliest_before_hashes: dict[str, str | None] = {}
    for r in target_records:
        path = str(r.get("path", "")).replace("\\", "/")
        if path not in earliest_before_hashes:
            before_hash = r.get("before_hash")
            earliest_before_hashes[path] = str(before_hash) if before_hash is not None else None

    invalid_snapshots = []
    for path, before_hash in earliest_before_hashes.items():
        if before_hash is not None:
            try:
                if blob_store.get_bytes(before_hash) is None:
                    invalid_snapshots.append(path)
            except (ValueError, OSError):
                invalid_snapshots.append(path)
    if invalid_snapshots:
        return RewindResult(success=False, to_turn=to_turn, conflicts=conflicts,
            message=f"缺少或损坏回滚快照，未执行文件恢复：{', '.join(invalid_snapshots)}")

    restored_files: list[str] = []
    deleted_files: list[str] = []
    failed_files: list[str] = []

    for rel_path, before_hash in earliest_before_hashes.items():
        disk_file = cwd / rel_path
        if before_hash is None:
            # 该文件是在回滚区间内新创建的，安全删除物理文件
            if disk_file.exists():
                try:
                    disk_file.unlink(missing_ok=True)
                    deleted_files.append(rel_path)
                except OSError as exc:
                    logger.warning("删除新建文件失败 %s: %s", disk_file, exc)
                    failed_files.append(rel_path)
            else:
                deleted_files.append(rel_path)
        else:
            # 该文件在回滚区间前已存在，从 BlobStore 取出修改前内容原子覆盖
            try:
                ok = blob_store.restore_to_file(before_hash, disk_file)
            except (OSError, ValueError) as exc:
                logger.warning("恢复文件失败 %s: %s", disk_file, exc)
                ok = False
            if ok:
                restored_files.append(rel_path)
            else:
                logger.error("从 CAS 存储还原快照失败：%s (hash: %s)", rel_path, before_hash)
                failed_files.append(rel_path)

    if failed_files:
        return RewindResult(
            success=False,
            to_turn=to_turn,
            restored_files=restored_files,
            deleted_files=deleted_files,
            conflicts=conflicts,
            message=(f"回滚未完成，失败文件：{', '.join(failed_files)}。"
                     f"已还原 {len(restored_files)} 个文件、删除 {len(deleted_files)} 个文件；"
                     "可能已部分执行，对话历史未提交回滚。"),
        )

    msg = f"成功回滚至轮次 {to_turn}（还原 {len(restored_files)} 个文件，删除 {len(deleted_files)} 个新建文件）。"
    return RewindResult(
        success=True,
        to_turn=to_turn,
        restored_files=restored_files,
        deleted_files=deleted_files,
        conflicts=conflicts,
        message=msg,
    )
