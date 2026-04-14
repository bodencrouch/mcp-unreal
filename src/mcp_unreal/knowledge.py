from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable
from urllib.parse import unquote, urlparse


_REPO_ROOT = Path(__file__).resolve().parents[2]
KB_DIR = _REPO_ROOT / "knowledge_base"
BOOK_DIR = KB_DIR / "book_extracts"
RESOURCE_SCHEME = "knowledge"
RESOURCE_HOST = "ue5"


@dataclass(frozen=True)
class KnowledgeResource:
    name: str
    title: str
    path: Path
    uri: str
    description: str
    mime_type: str = "text/markdown"


TOPIC_MAP: dict[str, list[str]] = {
    "overview": ["00_AGENT_KNOWLEDGE_BASE.md", "INDEX.md"],
    "blueprints": [
        "01_BLUEPRINT_FUNDAMENTALS.md",
        "book_extracts/BOOK_BLUEPRINTS_FUNDAMENTALS.md",
    ],
    "communication": [
        "02_BLUEPRINT_COMMUNICATION.md",
        "book_extracts/BOOK_BLUEPRINT_COMMUNICATION.md",
    ],
    "gameplay": [
        "03_GAMEPLAY_FRAMEWORK.md",
        "book_extracts/BOOK_GAMEPLAY_FRAMEWORK.md",
    ],
    "ai": ["04_AI_SYSTEMS.md", "book_extracts/BOOK_AI_BEHAVIOR_TREES.md"],
    "animation": ["05_ANIMATION_SYSTEM.md", "book_extracts/BOOK_ANIMATION.md"],
    "ui": ["06_UI_UMG_SYSTEMS.md", "book_extracts/BOOK_UI_UMG.md"],
    "data": ["07_DATA_STRUCTURES.md", "book_extracts/BOOK_DATA_STRUCTURES.md"],
    "materials": [
        "08_MATERIALS_AND_RENDERING.md",
        "book_extracts/BOOK_MATERIALS_VFX.md",
    ],
    "niagara": ["09_NIAGARA_VFX.md", "book_extracts/BOOK_MATERIALS_VFX.md"],
    "world": ["10_WORLD_BUILDING.md"],
    "components": [
        "11_BLUEPRINT_LIBRARIES_AND_COMPONENTS.md",
        "book_extracts/BOOK_TECHNICAL_ART.md",
    ],
    "python": ["12_UE_PYTHON_RECIPES.md", "13_PYTHON_API_ROADMAP.md"],
    "input": ["15_INPUT_SYSTEM_AND_UMG.md", "book_extracts/BOOK_INPUT.md"],
    "animation_deep": ["16_ANIMATION_DEEP_DIVE.md", "book_extracts/BOOK_ANIMATION.md"],
    "cookbook": ["17_GAME_SYSTEMS_COOKBOOK.md"],
    "packaging": [
        "18_PACKAGING_AND_OPTIMIZATION.md",
        "book_extracts/BOOK_TECHNICAL_ART.md",
    ],
}


ALIASES: dict[str, str] = {
    "behavior tree": "ai",
    "behavior_tree": "ai",
    "bt": "ai",
    "enemy": "ai",
    "npc": "ai",
    "widget": "ui",
    "hud": "ui",
    "umg": "ui",
    "menu": "ui",
    "blueprint": "blueprints",
    "bp": "blueprints",
    "nodes": "blueprints",
    "material": "materials",
    "shader": "materials",
    "rendering": "materials",
    "particle": "niagara",
    "vfx": "niagara",
    "effects": "niagara",
    "struct": "data",
    "enum": "data",
    "datatable": "data",
    "save": "data",
    "gamemode": "gameplay",
    "game mode": "gameplay",
    "character": "gameplay",
    "playercontroller": "gameplay",
    "player controller": "gameplay",
    "interface": "communication",
    "dispatcher": "communication",
    "cast": "communication",
    "anim": "animation",
    "montage": "animation",
    "skeleton": "animation",
    "enhanced input": "input",
    "binding": "input",
    "key": "input",
    "performance": "packaging",
    "optimization": "packaging",
    "component": "components",
    "actor component": "components",
    "python api": "python",
    "recipes": "python",
}


GHOST_TOOL_MODULE_COUNTS: dict[str, int] = {
    "advanced_node_tools.py": 57,
    "ai_tools.py": 22,
    "animation_tools.py": 16,
    "blueprint_tools.py": 8,
    "communication_tools.py": 11,
    "data_tools.py": 28,
    "editor_tools.py": 12,
    "gameplay_tools.py": 11,
    "knowledge_tools.py": 3,
    "library_tools.py": 9,
    "material_tools.py": 12,
    "node_tools.py": 38,
    "physics_tools.py": 38,
    "procedural_tools.py": 13,
    "project_tools.py": 4,
    "savegame_tools.py": 13,
    "umg_tools.py": 19,
    "variant_tools.py": 7,
    "vr_tools.py": 10,
    "graph_layout.py": 0,
}


def list_knowledge_resources() -> list[KnowledgeResource]:
    if not KB_DIR.exists():
        return []

    resources: list[KnowledgeResource] = []
    for path in sorted(KB_DIR.glob("**/*.md")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(KB_DIR).as_posix()
        stem = Path(rel_path).stem
        title = stem.replace("_", " ")
        uri = f"{RESOURCE_SCHEME}://{RESOURCE_HOST}/{rel_path}"
        description = f"UE5 knowledge base document: {rel_path}"
        resources.append(
            KnowledgeResource(
                name=rel_path,
                title=title,
                path=path,
                uri=uri,
                description=description,
            )
        )
    return resources


def get_resource_by_uri(uri: str) -> KnowledgeResource | None:
    parsed = urlparse(str(uri))
    if parsed.scheme != RESOURCE_SCHEME:
        return None
    if parsed.netloc not in {"", RESOURCE_HOST}:
        return None

    rel_path = parsed.path.lstrip("/")
    if not rel_path and parsed.netloc == RESOURCE_HOST:
        rel_path = parsed.path.lstrip("/")
    rel_path = unquote(rel_path)

    for resource in list_knowledge_resources():
        if resource.name == rel_path:
            return resource
    return None


def resolve_topic(topic: str) -> str | None:
    normalized = topic.lower().strip()
    if normalized in TOPIC_MAP:
        return normalized
    if normalized in ALIASES:
        return ALIASES[normalized]
    for key in TOPIC_MAP:
        if normalized in key or key in normalized:
            return key
    return None


def read_markdown(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def read_relative_markdown(relative_path: str) -> str | None:
    path = KB_DIR / relative_path
    return read_markdown(path)


def all_knowledge_documents() -> Iterable[tuple[str, str]]:
    for resource in list_knowledge_resources():
        content = read_markdown(resource.path)
        if content is not None:
            yield resource.name, content


def list_knowledge_base_topics_text() -> str:
    lines = [
        "# Knowledge Base Topics",
        "",
        "Call get_knowledge_base(topic) with any topic below.",
        "Each topic maps to existing UE5 docs in this repository.",
        "",
        "## Core Topics",
        "  overview         - agent rules, index, quick lookup",
        "  blueprints       - Blueprint fundamentals and node patterns",
        "  communication    - casting, interfaces, event dispatchers",
        "  gameplay         - GameMode, Controller, Character, framework",
        "  ai               - Behavior Trees, Blackboard, NavMesh, sensing",
        "  animation        - AnimBP, state machines, montages, notifies",
        "  ui               - UMG widgets, HUD, menus",
        "  data             - arrays, maps, sets, structs, enums",
        "  materials        - materials, instances, rendering",
        "  niagara          - VFX and particle systems",
        "  world            - level and world building",
        "  components       - reusable components and libraries",
        "  python           - execute-script recipes and API roadmap",
        "  input            - Enhanced Input and UI interaction",
        "  animation_deep   - deeper animation techniques",
        "  cookbook         - step-by-step gameplay recipes",
        "  packaging        - packaging, profiling, optimization",
        "",
        "## Examples",
        "  get_knowledge_base('ai')",
        "  get_knowledge_base('python')",
        "  search_knowledge_base('behavior tree task')",
    ]
    return "\n".join(lines)


def get_knowledge_base_text(topic: str) -> str:
    canonical = resolve_topic(topic)
    if canonical is None:
        available = ", ".join(sorted(TOPIC_MAP))
        return (
            f"Topic '{topic}' not found.\n\n"
            f"Available topics: {available}\n\n"
            f"Try search_knowledge_base('{topic}') for a keyword search across all files."
        )

    sections: list[str] = []
    for relative_path in TOPIC_MAP[canonical]:
        content = read_relative_markdown(relative_path)
        if content is None:
            continue
        label = "Book Extract" if relative_path.startswith("book_extracts/") else "Reference Doc"
        sections.append(f"<!-- {label}: {relative_path} -->\n\n{content}")

    if not sections:
        return f"No content found for topic '{topic}'."

    header = (
        f"# Knowledge Base: {canonical.upper()}\n\n"
        f"> Sources: {', '.join(TOPIC_MAP[canonical])}\n"
        f"> Read this before implementing UE5 editor automation for this area.\n\n"
        "---\n\n"
    )
    return header + "\n\n---\n\n".join(sections)


def search_knowledge_base_text(query: str) -> str:
    normalized = query.lower().strip()
    if len(normalized) < 2:
        return "Please provide a search query of at least 2 characters."

    terms = normalized.split()
    results: list[tuple[int, str, list[str]]] = []

    for name, content in all_knowledge_documents():
        lowered = content.lower()
        score = sum(lowered.count(term) for term in terms)
        if score == 0:
            continue

        snippets: list[str] = []
        for paragraph in re.split(r"\n\n+", content):
            paragraph_lower = paragraph.lower()
            if any(term in paragraph_lower for term in terms) and len(paragraph.strip()) > 40:
                snippets.append(paragraph.strip()[:800])
            if len(snippets) == 3:
                break

        if snippets:
            results.append((score, name, snippets))

    if not results:
        return (
            f"No results found for '{query}'.\n\n"
            "Try broader terms or use list_knowledge_base_topics() first."
        )

    results.sort(key=lambda item: item[0], reverse=True)
    lines = [
        f"# Search Results: '{query}'",
        "",
        f"Found matches in {len(results)} files. Showing top {min(6, len(results))}.",
        "",
    ]
    for score, name, snippets in results[:6]:
        lines.append(f"## {name} (score: {score})")
        lines.append("")
        for snippet in snippets:
            lines.append(snippet)
            lines.append("")
    return "\n".join(lines)


def build_info_prompt(arguments: dict[str, str] | None = None) -> str:
    task = (arguments or {}).get("task", "")
    task_line = f"- Current task: {task}" if task else "- Current task: not provided"
    module_lines = [
        f"- {name}: {count} tools"
        for name, count in sorted(GHOST_TOOL_MODULE_COUNTS.items())
        if count > 0
    ]

    parts = [
        "# mcp-unreal Info",
        "",
        "This server keeps the original execute-script architecture and adds Ghost-inspired discovery surfaces in Python only.",
        "",
        "## Available MCP Tools",
        "- execute-script(code, exec_mode, unattended)",
        "- list_knowledge_base_topics()",
        "- get_knowledge_base(topic)",
        "- search_knowledge_base(query)",
        "",
        "## Available MCP Resources",
        f"- {RESOURCE_SCHEME}://{RESOURCE_HOST}/INDEX.md",
        f"- {RESOURCE_SCHEME}://{RESOURCE_HOST}/00_AGENT_KNOWLEDGE_BASE.md",
        f"- {RESOURCE_SCHEME}://{RESOURCE_HOST}/12_UE_PYTHON_RECIPES.md",
        f"- {RESOURCE_SCHEME}://{RESOURCE_HOST}/book_extracts/BOOK_AI_BEHAVIOR_TREES.md",
        "",
        "## Working Model",
        "- Use execute-script for all Unreal editor actions.",
        "- Use knowledge tools and resources before writing Unreal Python.",
        "- Prefer Unreal Python API calls over plugin-specific abstractions.",
        task_line,
        "",
        "## Ghost Python Inventory Reference",
        "- Ghost defines 1 Python prompt, 0 Python resources, and 331 Python tool registrations.",
        *module_lines,
    ]
    return "\n".join(parts)