---
name: skill-reader
description: Helps you discover, list, and read other skills in this project. Use this when you are asked to explore, learn about, or review available skills.
---

# Skill Reader

This skill provides instructions on how to discover and read other skills.

## Where to find skills

Project skills live in `<workspace-root>/.claude/skills/` (one folder per skill).
User-level skills may also exist in `~/.claude/skills/`.

## How to explore and read skills

When you need to discover or read skills, follow these steps:

1. List the skill directories to see what skills are available.
2. Each skill is a folder containing a `SKILL.md` file (search for `SKILL.md` to get an exact list).
3. Read the `SKILL.md` of any skill you want to learn about.
   - The YAML frontmatter contains the `name` and `description`.
   - The markdown body contains the detailed instructions for that skill.
4. If a skill directory contains a `scripts/`, `references/`, or `examples/` folder, list those contents when you need the scripts or examples the skill provides.
5. If the user asks you to read a specific skill, identify its folder and read its `SKILL.md` directly.
