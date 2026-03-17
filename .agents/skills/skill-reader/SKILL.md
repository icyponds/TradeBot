---
name: skill-reader
description: Helps you discover, list, and read other skills in the workspace or global directories. Use this when you are asked to explore, learn about, or review available skills.
---

# Skill Reader

This skill provides instructions on how to discover and read other skills.

## Where to find skills

Skills are located in two primary directories:
1. **Workspace skills**: `<workspace-root>/.agent/skills/` (e.g. `.agent/skills/` relative to the current project root)
2. **Global skills**: `~/.gemini/antigravity/skills/`

## How to explore and read skills

When you need to discover or read skills, follow these steps:

1. Use the `list_dir` tool on the skill directories to see what skills are available.
2. Look for folders; each skill is a folder that contains a `SKILL.md` file. You can use the `find_by_name` tool with pattern `SKILL.md` in these directories to get an exact list.
3. Use the `view_file` tool to read the `SKILL.md` of any skill you want to learn about. 
   - The YAML frontmatter contains the `name` and `description`.
   - The markdown body contains the detailed instructions for that skill.
4. If a skill directory contains a `scripts/` or `examples/` folder, list the contents of those folders if you need to understand the scripts or examples provided by the skill.
5. If the user asks you to read a specific skill, identify its folder and read its `SKILL.md` directly.
