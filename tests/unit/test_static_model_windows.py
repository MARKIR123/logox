"""静态模型上下文窗口与探针剔除回归测试集。"""

from __future__ import annotations

import tomllib
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from logox.app import Runtime
from logox.config.schema import LogoxConfig, ProviderInstanceConfig
from logox.providers.anthropic import AnthropicProvider
from logox.providers.base import ModelInfo
from logox.providers.openai_compat import OpenAICompatProvider
from logox.providers.registry import MissingKeyProvider, ProviderRegistry, ProviderSpec, build_provider


def test_provider_instance_config_validates_windows():
    """验证 ProviderInstanceConfig 对 context_window 与 model_windows 的解析和非正数拦截。"""
    config = ProviderInstanceConfig(
        kind="openai_compat",
        base_url="http://127.0.0.1:11434/v1",
        context_window=32768,
        model_windows={
            "qwen3.8:27b": 98304,
            "minimind-3": 4096,
        },
    )
    assert config.context_window == 32768
    assert config.model_windows["qwen3.8:27b"] == 98304
    assert config.model_windows["minimind-3"] == 4096

    # 拦截 context_window <= 0
    with pytest.raises(ValidationError, match="greater than 0"):
        ProviderInstanceConfig(context_window=0)

    # 拦截 model_windows 中的非正数
    with pytest.raises(ValidationError, match="必须大于 0"):
        ProviderInstanceConfig(model_windows={"bad": 0})

    with pytest.raises(ValidationError, match="必须大于 0"):
        ProviderInstanceConfig(model_windows={"negative": -100})


def test_provider_spec_window_for_resolution():
    """验证 ProviderSpec.window_for 的三级分层查找：精确匹配 -> Tag 规范化 -> Provider 默认值回退。"""
    spec = ProviderSpec(
        name="ollama",
        base_url="http://127.0.0.1:11434/v1",
        context_window=32768,
        model_windows={
            "qwen3.8:27b": 98304,
            "llama3.1:latest": 131072,
        },
    )

    # 1. 精确匹配
    assert spec.window_for("qwen3.8:27b") == 98304

    # 2. Tag 规范化：查询带 :latest，配置中无 :latest
    assert spec.window_for("qwen3.8:27b:latest") == 98304

    # 3. 逆向 Tag 规范化：查询不带 :latest，配置中带 :latest
    assert spec.window_for("llama3.1") == 131072
    assert spec.window_for("llama3.1:latest") == 131072

    # 4. 回退至 Provider 级 context_window
    assert spec.window_for("other-model") == 32768

    # 5. 若无默认 context_window 则返回 None
    spec_no_default = ProviderSpec(name="test", model_windows={"m1": 4096})
    assert spec_no_default.window_for("m1") == 4096
    assert spec_no_default.window_for("unknown") is None


def test_provider_registry_window_for():
    """验证 ProviderRegistry.window_for 直接通过 spec 进行静态查询。"""
    spec = ProviderSpec(
        name="ollama",
        context_window=32768,
        model_windows={"qwen3.8:27b": 98304},
    )
    registry = ProviderRegistry({"ollama": spec})
    assert registry.window_for("ollama", "qwen3.8:27b") == 98304
    assert registry.window_for("ollama", "other") == 32768
    assert registry.window_for("nonexistent_provider", "qwen3.8:27b") is None


def test_openai_compat_provider_window_for_and_list_models():
    """验证 OpenAICompatProvider 初始化接收 model_windows 并在 list_models() 中反映具体模型的窗口。"""
    provider = OpenAICompatProvider(
        context_window=32768,
        model_windows={"qwen3.8:27b": 98304, "minimind-3": 4096},
        models=["qwen3.8:27b", "minimind-3", "llama3.2"],
    )

    assert provider.window_for("qwen3.8:27b") == 98304
    assert provider.window_for("minimind-3") == 4096
    assert provider.window_for("llama3.2") == 32768

    models = {m.id: m for m in provider.list_models()}
    assert models["qwen3.8:27b"].context_window == 98304
    assert models["minimind-3"].context_window == 4096
    assert models["llama3.2"].context_window == 32768


def test_anthropic_provider_window_for_and_list_models():
    """验证 AnthropicProvider 同样支持 model_windows 静态映射。"""
    provider = AnthropicProvider(
        context_window=200000,
        model_windows={"claude-opus-4-1": 500000},
        models=["claude-sonnet-4-5", "claude-opus-4-1"],
    )
    assert provider.window_for("claude-opus-4-1") == 500000
    assert provider.window_for("claude-sonnet-4-5") == 200000

    models = {m.id: m for m in provider.list_models()}
    assert models["claude-opus-4-1"].context_window == 500000
    assert models["claude-sonnet-4-5"].context_window == 200000


def test_missing_key_provider_window_for():
    """验证占位适配器 MissingKeyProvider 也能正确读取 spec 的模型窗口。"""
    spec = ProviderSpec(
        name="test_missing",
        api_key_env="TEST_API_KEY",
        context_window=16384,
        model_windows={"custom-model": 65536},
        models=("custom-model", "default-model"),
    )
    provider = MissingKeyProvider(spec)
    assert provider.window_for("custom-model") == 65536
    assert provider.window_for("default-model") == 16384

    models = {m.id: m for m in provider.list_models()}
    assert models["custom-model"].context_window == 65536
    assert models["default-model"].context_window == 16384


def test_build_provider_passes_model_windows():
    """验证 build_provider 将 ProviderSpec 的 model_windows 正确透传给适配器。"""
    spec = ProviderSpec(
        name="ollama",
        kind="openai_compat",
        context_window=32768,
        model_windows={"qwen3.8:27b": 98304},
        models=("qwen3.8:27b",),
    )
    provider = build_provider(spec, api_key="unused")
    assert provider.window_for("qwen3.8:27b") == 98304
    assert provider.window_for("fallback") == 32768


def test_runtime_window_for_pure_static_zero_network(tmp_path):
    """验证 Runtime.window_for 在查询模型窗口时只读取注册表静态信息，零网络请求。"""
    spec = ProviderSpec(
        name="ollama",
        context_window=32768,
        model_windows={"qwen3.8:27b": 98304},
    )
    registry = ProviderRegistry({"ollama": spec})
    runtime = Runtime(
        bus=MagicMock(),
        kernel=MagicMock(),
        reducer=MagicMock(),
        theme=MagicMock(),
        config=LogoxConfig(),
        provider_name="ollama",
        model="qwen3.8:27b",
        cwd=tmp_path,
        tools=[],
        registry=registry,
    )

    # 查配置了的模型
    assert runtime.window_for("qwen3.8:27b") == 98304
    # 查走兜底的模型
    assert runtime.window_for("unknown-model") == 32768


def test_toml_deserialization_of_model_windows():
    """验证真实 TOML 文本能反序列化为带有 model_windows 的 ProviderInstanceConfig。"""
    toml_text = """
    kind = "openai_compat"
    base_url = "http://127.0.0.1:11434/v1"
    context_window = 32768

    [model_windows]
    "qwen3.8:27b" = 98304
    "minimind-3" = 4096
    """
    data = tomllib.loads(toml_text)
    cfg = ProviderInstanceConfig.model_validate(data)
    assert cfg.context_window == 32768
    assert cfg.model_windows == {
        "qwen3.8:27b": 98304,
        "minimind-3": 4096,
    }
