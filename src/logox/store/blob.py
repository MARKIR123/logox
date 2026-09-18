"""内容寻址存储（CAS Blob Store，D102 / L4）。

实现基于 SHA-256 哈希的内容寻址对象池（Content-Addressable Storage）。
特性：
1. **天然去重**：同一文件内容无论被保存多少次、跨会话或跨项目，磁盘上仅保留唯一一份；
2. **两级分片**：采用 ``blobs/<hash[:2]>/<hash[2:]>`` 结构，避免单个目录下堆积过多文件；
3. **只读不可变与幂等写入**：如果目标哈希文件已存在，直接返回，零冗余写 I/O；
4. **原子还原（Atomic Restore）**：还原文件时采用同目录临时文件 + ``os.replace`` 事务级替换。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import uuid
from pathlib import Path

from logox.paths import LogoxPaths

logger = logging.getLogger(__name__)

__all__ = ["BlobStore"]


class BlobStore:
    """全局内容寻址对象存储。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base_dir = Path(base_dir).resolve() if base_dir is not None else LogoxPaths.default().blobs
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _blob_path(self, sha256_hash: str) -> Path:
        """根据 64 位十六进制哈希生成两级分片路径。"""
        clean_hash = sha256_hash.strip().lower()
        if len(clean_hash) != 64:
            raise ValueError(f"不合法的 SHA-256 哈希长度：{sha256_hash!r}")
        return self.base_dir / clean_hash[:2] / clean_hash[2:]

    def put_bytes(self, data: bytes) -> str:
        """存储字节切片，返回 64 位 SHA-256 十六进制哈希。天然幂等。"""
        sha256_hash = hashlib.sha256(data).hexdigest()
        target = self._blob_path(sha256_hash)
        if target.is_file():
            return sha256_hash

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = target.parent / f".tmp_{uuid.uuid4().hex}"
        try:
            tmp_file.write_bytes(data)
            os.replace(tmp_file, target)
        finally:
            if tmp_file.exists():
                with contextlib.suppress(Exception):
                    tmp_file.unlink(missing_ok=True)
        return sha256_hash

    def put_file(self, file_path: Path | str) -> str:
        """读取指定文件并将内容存入 CAS 对象池，返回 SHA-256 哈希。"""
        p = Path(file_path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"文件不存在，无法存储快照：{p}")
        return self.put_bytes(p.read_bytes())

    def get_bytes(self, sha256_hash: str) -> bytes | None:
        """根据哈希读取快照字节；若不存在返回 None。"""
        target = self._blob_path(sha256_hash)
        if not target.is_file():
            return None
        return target.read_bytes()

    def restore_to_file(self, sha256_hash: str, target_path: Path | str) -> bool:
        """将指定哈希的内容原子写入到目标文件。返回是否成功。"""
        data = self.get_bytes(sha256_hash)
        if data is None:
            logger.warning("CAS 存储中不存在哈希为 %s 的快照对象", sha256_hash)
            return False

        p = Path(target_path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = p.parent / f".{p.name}.tmp_{uuid.uuid4().hex[:8]}"
        try:
            tmp_file.write_bytes(data)
            os.replace(tmp_file, p)
            return True
        finally:
            if tmp_file.exists():
                with contextlib.suppress(Exception):
                    tmp_file.unlink(missing_ok=True)

    def has(self, sha256_hash: str) -> bool:
        """检查指定哈希对象是否存在。"""
        try:
            return self._blob_path(sha256_hash).is_file()
        except ValueError:
            return False
