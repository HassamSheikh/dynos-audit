"""dynos-work commands may only run when the user types the namespaced form.

The plugin publishes skills whose bare names — `resume`, `status`, `init`,
`memory` — match Claude Code built-ins. Under a plugin install the host
already namespaces them, so `/dynos-work:resume` is the only *typed* route
in. The leak is the other invocation path: a skill `description` is a
trigger signal, so a description that reads "Use after session restart" or
"Show current task state" fires the skill when the user restarts a session
or asks what the status is. To the user that is indistinguishable from a
command collision — they never typed anything and a dynos command ran.

Two rules, enforced here:

1. Every skill description states that the command runs only on explicit
   invocation, and names its own `/dynos-work:<name>` form.
2. No description contains trigger bait — phrasing that invites the model
   to fire the skill from context rather than from a typed command.

The SessionStart hook is checked for the same reason: it injects text into
every session, so an instruction there to "use dynos-work start for new
tasks" is a standing auto-invocation order.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "skills"
TEMPLATES_DIR = ROOT / "cli" / "assets" / "templates" / "base"
SESSION_START = ROOT / "hooks" / "session-start"

NAMESPACE = "dynos-work"

# Phrasings that invite context-driven invocation. Each one reads to the
# model as "fire me when you see X" rather than "this is what the command
# does". The explicit-invocation clause is allowed to say "never
# auto-triggered ... by <X>", so matches inside that clause are excluded by
# _strip_guard_clause below.
TRIGGER_BAIT = [
    re.compile(r"\buse (?:this )?(?:after|when|if)\b", re.I),
    re.compile(r"\bruns automatically\b", re.I),
    re.compile(r"\bperiodically\b", re.I),
    re.compile(r"\bproactively\b", re.I),
    re.compile(r"\bwhenever\b", re.I),
]


def _frontmatter_field(path: Path, field: str) -> str | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    m = re.search(rf"^{field}:\s*(.+?)\s*$", text[3:end], flags=re.M)
    return m.group(1).strip().strip('"').strip("'") if m else None


def _skill_dirs() -> list[Path]:
    return sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def _guard_clause(name: str) -> re.Pattern[str]:
    """The clause naming the typed command as an invocation route.

    Most skills say "runs only when the user explicitly types ...". A few
    (`memory`, `execution`) are also invoked by the pipeline itself and say
    "... or when the user explicitly types ...". Both are acceptable; what
    is not acceptable is a description with no explicit-invocation clause at
    all, which is checked together with the never-auto clause below.
    """
    return re.compile(
        rf"when the user explicitly types /{re.escape(NAMESPACE)}:{re.escape(name)}\b",
        re.I,
    )


NEVER_AUTO = re.compile(r"never auto-triggered", re.I)


def _strip_guard_clause(desc: str) -> str:
    """Drop the explicit-invocation sentence before scanning for bait.

    That sentence legitimately contains words like "by session restart" as
    part of stating what must *not* trigger the skill.
    """
    idx = desc.lower().find("runs only when the user explicitly types")
    if idx == -1:
        idx = desc.lower().find("or when the user explicitly types")
    return desc[:idx] if idx != -1 else desc


def test_there_are_skills_to_check() -> None:
    """Guard against this module passing vacuously."""
    assert len(_skill_dirs()) >= 20


@pytest.mark.parametrize("skill_dir", _skill_dirs(), ids=lambda p: p.name)
def test_description_scopes_invocation_to_the_typed_command(skill_dir: Path) -> None:
    name = skill_dir.name
    desc = _frontmatter_field(skill_dir / "SKILL.md", "description")
    assert desc, f"{name}: no description"
    assert _guard_clause(name).search(desc), (
        f"skill {name!r} does not scope itself to explicit invocation. Its "
        f"description must state that it runs when the user explicitly "
        f"types /{NAMESPACE}:{name}. Got: {desc!r}"
    )
    assert NEVER_AUTO.search(desc), (
        f"skill {name!r} names its typed command but never says it is not "
        f"auto-triggered, which is the half that actually suppresses "
        f"context-driven invocation. Got: {desc!r}"
    )


@pytest.mark.parametrize("skill_dir", _skill_dirs(), ids=lambda p: p.name)
def test_description_has_no_auto_trigger_bait(skill_dir: Path) -> None:
    name = skill_dir.name
    desc = _frontmatter_field(skill_dir / "SKILL.md", "description") or ""
    body = _strip_guard_clause(desc)
    for rx in TRIGGER_BAIT:
        m = rx.search(body)
        assert not m, (
            f"skill {name!r} description contains auto-trigger phrasing "
            f"{m.group(0)!r}, which invites invocation from context instead of "
            f"from a typed /{NAMESPACE}:{name}. Describe what the command does, "
            f"not when the model should reach for it."
        )


@pytest.mark.parametrize("skill_dir", _skill_dirs(), ids=lambda p: p.name)
def test_template_mirror_description_matches(skill_dir: Path) -> None:
    """Templates mirror skills — a stale mirror re-opens the trigger."""
    template = TEMPLATES_DIR / f"{skill_dir.name}.md"
    if not template.is_file():
        pytest.skip(f"no template mirror for {skill_dir.name}")
    assert _frontmatter_field(template, "description") == (
        _frontmatter_field(skill_dir / "SKILL.md", "description")
    ), f"template {template.name} description has drifted from the skill"


def test_skill_names_stay_bare_so_the_command_is_singly_namespaced() -> None:
    """The plugin prefix supplies the namespace; the name must not repeat it.

    A skill named `dynos-resume` under plugin `dynos-work` would register as
    `/dynos-work:dynos-resume`.
    """
    for skill_dir in _skill_dirs():
        name = _frontmatter_field(skill_dir / "SKILL.md", "name")
        assert name == skill_dir.name, (
            f"frontmatter name {name!r} != directory {skill_dir.name!r}"
        )
        assert not name.startswith("dynos-"), (
            f"skill {name!r} repeats the namespace already supplied by the "
            f"plugin prefix, yielding /{NAMESPACE}:{name}"
        )


AGENTS_DIR = ROOT / "agents"

# Agents are not slash commands — the pipeline spawns them. The equivalent
# leak is the model spawning one spontaneously because its description reads
# like a standing offer ("Implements API routes, services, business logic").
# A stray executor writes code; a stray auditor burns a frontier spawn.
AGENT_GUARD = re.compile(
    r"Spawned only by the dynos-work pipeline during an explicitly invoked "
    r"/dynos-work:[\w-]+; never spawn this agent directly",
    re.I,
)


def _agent_files() -> list[Path]:
    """Agent prompt files that declare frontmatter (excludes shared schemas)."""
    return sorted(
        p for p in AGENTS_DIR.rglob("*.md")
        if _frontmatter_field(p, "description") is not None
    )


def _command_templates() -> list[Path]:
    return sorted(TEMPLATES_DIR.glob("*.md"))


def test_there_are_agents_and_templates_to_check() -> None:
    assert len(_agent_files()) >= 35
    assert len(_command_templates()) >= 20


@pytest.mark.parametrize("template", _command_templates(), ids=lambda p: p.name)
def test_every_command_template_is_guarded(template: Path) -> None:
    """Covers templates with no skills/ counterpart, e.g. founder.

    A template that mirrors a skill is checked for drift elsewhere; this
    catches the ones that would otherwise be checked by nothing.
    """
    desc = _frontmatter_field(template, "description")
    assert desc, f"{template.name}: no description"
    name = _frontmatter_field(template, "name") or template.stem
    assert _guard_clause(name).search(desc), (
        f"command template {template.name!r} does not scope itself to explicit "
        f"invocation of /{NAMESPACE}:{name}. Got: {desc!r}"
    )
    assert NEVER_AUTO.search(desc), (
        f"command template {template.name!r} does not declare itself "
        f"never auto-triggered. Got: {desc!r}"
    )


@pytest.mark.parametrize("agent", _agent_files(), ids=lambda p: p.name)
def test_every_agent_is_scoped_to_pipeline_spawning(agent: Path) -> None:
    desc = _frontmatter_field(agent, "description")
    assert desc, f"{agent.name}: no description"
    assert AGENT_GUARD.search(desc), (
        f"agent {agent.name!r} does not scope itself to pipeline spawning. Its "
        f"description must name the /{NAMESPACE}:<command> that spawns it and "
        f"forbid direct spawning, or the model may spawn it spontaneously "
        f"outside any dynos-work task. Got: {desc!r}"
    )


@pytest.mark.parametrize("agent", _agent_files(), ids=lambda p: p.name)
def test_agent_description_has_no_auto_trigger_bait(agent: Path) -> None:
    desc = _frontmatter_field(agent, "description") or ""
    body = desc[: desc.find("Spawned only by")] if "Spawned only by" in desc else desc
    for rx in TRIGGER_BAIT:
        m = rx.search(body)
        assert not m, (
            f"agent {agent.name!r} description contains auto-trigger phrasing "
            f"{m.group(0)!r}. Describe what the agent does, not when to reach for it."
        )


class TestSessionStartHook:
    def test_hook_forbids_self_initiated_invocation(self) -> None:
        text = SESSION_START.read_text(encoding="utf-8")
        assert "user-invoked only" in text, (
            "session-start must tell the model that dynos-work commands are "
            "user-invoked only"
        )
        assert "on your own initiative" in text, (
            "session-start must forbid self-initiated pipeline entry"
        )

    def test_hook_does_not_instruct_the_model_to_start_tasks(self) -> None:
        """The original text ended 'Use dynos-work start for new tasks.'

        Injected into every session, that is a standing order to invoke the
        pipeline whenever a request looks task-shaped.
        """
        text = SESSION_START.read_text(encoding="utf-8")
        bait = re.compile(r"use dynos-work start\b", re.I)
        assert not bait.search(text), (
            "session-start still instructs the model to start dynos-work tasks "
            "on its own"
        )
