"""paths 模块单元测试（D119 工作区自动初始化与路径发现）。"""

from __future__ import annotations

from pathlib import Path

from logox.paths import (
    CONFIG_DIRNAME,
    discover_project_chain,
    ensure_project_initialized,
    nearest_project,
)


def test_ensure_project_initialized_creates_scaffold(tmp_path: Path):
    """测试空目标目录自动初始化生成 .logox 骨架。"""
    workspace = tmp_path / "my_new_project"
    workspace.mkdir()

    # 首次调用：创建
    proj, created = ensure_project_initialized(workspace)
    assert created is True
    assert proj.root == workspace

    logox_dir = workspace / CONFIG_DIRNAME
    assert logox_dir.is_dir()

    gitignore = logox_dir / ".gitignore"
    assert gitignore.is_file()
    content = gitignore.read_text(encoding="utf-8")
    assert ".env" in content
    assert "state.toml" in content

    state_file = logox_dir / "state.toml"
    assert state_file.is_file()
    assert "schema_version" in state_file.read_text(encoding="utf-8")

    # 二次调用：幂等，不重复创建
    proj2, created2 = ensure_project_initialized(workspace)
    assert created2 is False
    assert proj2.root == workspace


def test_nearest_project_resolves_to_initialized_workspace(tmp_path: Path):
    """测试自动初始化后，nearest_project 直接锚定当前工作区，防止向上漂移。"""
    parent_workspace = tmp_path / "parent_repo"
    parent_workspace.mkdir()
    ensure_project_initialized(parent_workspace)

    sub_project = parent_workspace / "sub_service"
    sub_project.mkdir()
    ensure_project_initialized(sub_project)

    # 在子服务中调用 nearest_project，必须命中 sub_project 自身，而不是父级
    nearest = nearest_project(sub_project)
    assert nearest is not None
    assert nearest.root == sub_project
    assert nearest.state == sub_project / CONFIG_DIRNAME / "state.toml"
