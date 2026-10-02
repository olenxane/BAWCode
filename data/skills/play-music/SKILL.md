---
name: play-music
description: （占位）play-music 技能尚未编写，当前内容为示例模板，请替换为实际的音乐播放操作指南
---

# Example Skill

这是一个技能示例。技能 = `SKILL.md`（YAML frontmatter + 正文）+ 可选 `scripts/`、`references/`。

## frontmatter 字段

- `name`：技能名（缺省取目录名）
- `description`：一句话描述（必填，进 [可用技能] 清单，供模型判断何时加载）

## 使用方式

1. 模型在 [可用技能] 清单里看到本技能；
2. 任务匹配时调用 `load_skill(skill="example")` 读取本文档；
3. 正文即为操作指南，模型遵循其流程执行；脚本经 run_command 执行，资料用 read 查看。

## 编写建议

- 正文控制在数千 token 内，长资料放 `references/`；
- 步骤写成可执行的检查单，而不是概念描述；
- 描述写清"什么时候用我"，这是模型选择技能的唯一依据。
