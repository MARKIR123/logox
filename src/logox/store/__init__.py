"""会话持久化、多会话管理与回放模块 (M8 / D98)。"""

from __future__ import annotations

from logox.store.blob import BlobStore
from logox.store.checkpoint import (
    CheckpointTracker,
    ConflictInfo,
    FileSnapshot,
    RewindResult,
    TurnCheckpoint,
)
from logox.store.manager import SessionInfo, SessionManager
from logox.store.persistence import SessionPersistenceSubscriber
from logox.store.replay import (
    filter_rewound_records,
    load_session_records,
    reconstruct_messages,
    replay_into_timeline,
    replay_session,
)
from logox.store.rewind import check_conflicts, execute_rewind
from logox.store.slug import slugify_cwd, unslug_cwd_hint

__all__ = [
    "BlobStore",
    "CheckpointTracker",
    "ConflictInfo",
    "FileSnapshot",
    "RewindResult",
    "SessionInfo",
    "SessionManager",
    "SessionPersistenceSubscriber",
    "TurnCheckpoint",
    "check_conflicts",
    "execute_rewind",
    "filter_rewound_records",
    "load_session_records",
    "reconstruct_messages",
    "replay_into_timeline",
    "replay_session",
    "slugify_cwd",
    "unslug_cwd_hint",
]

