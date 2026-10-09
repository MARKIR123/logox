# 主题与字形

核对日期：2026-10-09。主题属于用户级偏好，同名用户文件覆盖内置，不读取项目级主题目录。

## 切换与创建

`/theme` 打开选择器；`/theme logox-light` 或 `/theme logox-dark`、`/theme logox-contrast` 直接切换。选择保存到 state，下一次启动记住。

在仓库目录复制完整内置主题：

```powershell
$themeDir = Join-Path $env:USERPROFILE '.logox\themes'
New-Item -ItemType Directory -Path $themeDir -Force | Out-Null
Copy-Item -LiteralPath 'src\logox\tui\themes\logox-dark.toml' -Destination (Join-Path $themeDir 'my-theme.toml')
```

修改 name 为 my-theme，label 为显示名，再调整已有 palette 值。用完整内置文件为起点，避免遗漏必需字段；必填范围以 [ThemeFile / Palette](../../src/logox/config/schema.py) 为准，不维护易过时的字段计数副本。

文件名是发现键，建议 name 与文件名一致。UTF-8 TOML、schema_version=1，variant 为 dark/light/high_contrast。syntax 与 glyphs 有默认值。

## 生效与失败

改当前主题文件后，在前台空闲时 /reload 重读；也可切到其它主题再切回。修改 ui.icon_set 等构造期配置需重启。

文件格式、版本或必需字段错误拒绝加载；切换失败保留当前主题，启动失败回退内置并提示。对比度不足给警告，不强制改色；淡色提示与终端背景组合需现场查看。

不是每个 Palette 字段都用于每种组件。要改输入框先调整 input_border/input_text/input_hint；推理用 thinking_text，工具输出用 tool_output_fg。不要以静态字段数量推断所有场景一定使用该色。

## 字形与复制

glyphs 的 running、success、error、denied、cancelled、thinking 等传入卡片和状态栏。ui.icon_set 可选 unicode/ascii/nerd；Nerd 需要终端字体支持，无法显示时用 ascii。

轨道装饰可用 Ctrl+B 隐藏后复制。模型或工具状态还以文字表达，不能只靠颜色或图标。配色/字形变化须使缓存失效，设计约束见 [UI-SPEC](../UI-SPEC.md)，加载机制见 [主题源码](../../src/logox/config/theme.py)。
