"""
================================================================================
 FILE: skills/loader.py  —  load + validate declarative SKILL.md files
================================================================================

WHAT A SKILL IS (and why this is the scalability story)
-------------------------------------------------------
A skill = a folder with a SKILL.md. The file has TWO parts:
  1. YAML front matter (between --- ---): machine-readable metadata the CODE uses
     (which model group, which tools, output schema, thresholds).
  2. Markdown body: the natural-language INSTRUCTION the LLM uses.

Adding a new agentic use case = drop a new folder. No code change. The loader
discovers skills at startup and VALIDATES (at load time, not runtime) that every
declared tool actually exists in the tool registry.

WHY YAML FRONT MATTER IS A GOOD PRACTICE (interview point)
---------------------------------------------------------
It separates STRUCTURED metadata (the machine reads: model group, tools) from
NATURAL-LANGUAGE instructions (the LLM reads). This is the same pattern as
Anthropic's Agent Skills. The alternative — mixing everything in prose — makes
load-time validation impossible and the file unparseable.

INTERVIEW ANCHOR
----------------
Q: "How do you add a new use case?"  ->  "Drop a SKILL.md. The loader validates its
   tools exist, then the factory turns it into an agent. Reliability (tracing,
   guardrails, routing) lives in the runtime, so the skill author writes only an
   instruction + metadata."
Q: "Why validate tools at load time?"  ->  "So a typo in a skill fails at startup,
   not in production mid-pipeline."
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings, get_settings
from ..tools.registry import ToolRegistry, default_registry


@dataclass
class Skill:
    """One parsed skill. Attributes:
      name          - unique id (also the agent/node name)
      display_name  - human label
      description   - one line, also used as the ADK agent description
      instruction   - the Markdown body = the LLM system instruction
      model         - logical model GROUP ("fast" | "deep") for the LiteLLM router
      tools         - list of tool names this agent may use (validated at load)
      output_schema - name of the expected JSON output shape (for guardrails)
      thresholds    - numeric config the instruction references
    """
    name: str
    display_name: str
    description: str
    instruction: str
    model: str
    tools: list[str]
    output_schema: str
    thresholds: dict
    path: Path


@dataclass
class SkillLoadError(Exception):
    skill_path: str
    reason: str
    def __str__(self) -> str:
        return f"{self.skill_path}: {self.reason}"


def _split_front_matter(text: str) -> tuple[str, str]:
    """Split '---\\n<yaml>\\n---\\n<body>' into (yaml, body)."""
    body = text.lstrip()         #Sans lstrip(), si le fichier commence par des lignes vides :  body.startswith("---") return False
    if not body.startswith("---"):
        raise ValueError("missing '---' front matter")
    parts = body.split("---", 2)   #"abc---def---ghi".split("---", 2) return ["abc", "def", "ghi"] len(parts) == 3
    if len(parts) < 3:
        raise ValueError("malformed front matter (need opening and closing '---')")
    return parts[1], parts[2]


def _coerce(v: str):
    """Turn a YAML scalar string into a real Python value (int/float/bool/str)."""
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return ast.literal_eval(v)   # numbers, quoted strings, lists
    except Exception:
        return v


def _parse_front_matter(fm: str) -> dict:
    """Minimal YAML subset: scalars, inline/block lists, one level of nested map
    (thresholds). Avoids a PyYAML dependency for the core. Sufficient for our format."""
    data: dict = {}
    current_key: str | None = None
    for raw in fm.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):        #la ligne est vide.
            continue    
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent >= 2 and current_key:                 # nested under current_key
            if line.startswith("- "):
                if not isinstance(data.get(current_key), list):
                    data[current_key] = []
                data[current_key].append(_coerce(line[2:].strip()))
            elif ":" in line:
                k, v = line.split(":", 1)
                if not isinstance(data.get(current_key), dict):
                    data[current_key] = {}
                data[current_key][k.strip()] = _coerce(v.strip())
            continue
        if ":" in line:                                  # top-level key: value
            key, val = (p.strip() for p in line.split(":", 1))
            current_key = key
            if val == "":
                data[key] = None
            elif val in ("[]", "{}"):
                data[key] = [] if val == "[]" else {}
            elif val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                data[key] = [_coerce(x.strip()) for x in inner.split(",")] if inner else []
            else:
                data[key] = _coerce(val)
    return data


def load_skill(folder: Path, registry: ToolRegistry) -> Skill:
    """Parse one SKILL.md and validate its tools exist. Raises SkillLoadError."""
    md = folder / "SKILL.md"
    if not md.exists():
        raise SkillLoadError(str(folder), "no SKILL.md")
    try:
        fm_text, body = _split_front_matter(md.read_text())
        meta = _parse_front_matter(fm_text)
    except Exception as exc:
        raise SkillLoadError(str(md), f"front matter parse error: {exc}") from exc

    tools = meta.get("tools") or []
    missing = [t for t in tools if not registry.has(t)]
    if missing:                                          # LOAD-TIME validation
        raise SkillLoadError(str(md), f"declares unknown tools: {missing}")

    model = meta.get("model", "fast")
    if model not in ("fast", "deep"):
        raise SkillLoadError(str(md), f"model must be 'fast' or 'deep', got {model!r}")

    try:
        return Skill(
            name=meta["name"],
            display_name=meta.get("display_name", meta["name"]),
            description=meta.get("description", ""),
            instruction=body.strip(),
            model=model,
            tools=list(tools),
            output_schema=meta.get("output_schema", "object"),
            thresholds=meta.get("thresholds") or {},
            path=folder,
        )
    except KeyError as exc:
        raise SkillLoadError(str(md), f"missing required field: {exc}") from exc


def load_all_skills(settings: Settings | None = None,
                    registry: ToolRegistry | None = None) -> dict[str, Skill]:
    """Discover and load every skill folder under settings.skills_dir."""
    s = settings or get_settings()
    registry = registry or default_registry(s)
    root = Path(s.skills_dir)
    skills: dict[str, Skill] = {}
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        skill = load_skill(folder, registry)
        skills[skill.name] = skill
    return skills


# =============================================================================
# PROGRESSIVE DISCLOSURE (Anthropic-style) — load ONLY name + description first
# =============================================================================
from dataclasses import dataclass as _dataclass


@_dataclass
class SkillManifest:
    """The cheap, first-level view of a skill: just name + description (~30-50
    tokens). This is what Anthropic pre-loads for EVERY skill at startup; the full
    SKILL.md body is only read when a task matches the description."""
    name: str
    description: str
    path: Path


def load_skill_manifests(settings: Settings | None = None) -> list[SkillManifest]:
    """Read ONLY the front-matter name+description of every skill — no body, no tool
    validation. Cheap enough to keep all skills 'on hand' without context cost."""
    s = settings or get_settings()
    root = Path(s.skills_dir)
    out: list[SkillManifest] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        md = folder / "SKILL.md"
        if not md.exists():
            continue
        fm_text, _ = _split_front_matter(md.read_text())
        meta = _parse_front_matter(fm_text)
        out.append(SkillManifest(name=meta["name"],
                                 description=meta.get("description", ""),
                                 path=folder))
    return out
