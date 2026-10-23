"""vigia.toml: which engine builds, which lab reviews, and the caps, per damage tier.

The file is read with the standard library (`tomllib`) and written by `render()`, so vigia has no
runtime dependencies. Validation is strict on purpose: a config that would let a model review its
own lab's work, or that names a model by a moving alias, is refused before anything runs.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "vigia.toml"
TIERS = ("T0", "T1", "T2")

# Engine -> the lab that trains its models. The gate must always come from the other lab.
FAMILIES = {"claude": "anthropic", "codex": "openai"}

# Aliases move when a lab ships a new model; vigia records exact ids so every verdict is reproducible.
MODEL_ALIASES = {"opus", "sonnet", "haiku", "fable", "default", "best", "latest"}

EFFORTS = {
    "claude": {"low", "medium", "high", "xhigh", "max"},
    "codex": {"low", "medium", "high", "xhigh"},
}


class ConfigError(ValueError):
    """vigia.toml is missing, malformed or breaks one of the rules."""


@dataclass(frozen=True)
class Seat:
    """One engine at one model and effort, in one role (builder, gate or reviewer)."""

    engine: str
    model: str
    effort: str | None = None

    @property
    def family(self) -> str:
        return FAMILIES[self.engine]

    def label(self) -> str:
        effort = f" · effort {self.effort}" if self.effort else ""
        return f"{self.engine} · {self.model}{effort}"


@dataclass(frozen=True)
class TierPolicy:
    """T0: builder + cross-lab gate. T1: + a stronger reviewer. T2: + a human label on the exact head."""

    builder: Seat
    gate: Seat
    reviewer: Seat | None = None
    human: bool = False


@dataclass(frozen=True)
class Caps:
    gate_rounds: int = 2  # distinct heads the gate may review per PR
    fix_rounds: int = 2  # fix sessions per PR (gate findings, reviewer findings or red CI)
    review_rounds: int = 2  # stronger-reviewer passes per PR (T1/T2)
    attempts: int = 3  # failed engine runs on one head before the PR is parked
    session_minutes: int = 30  # wall-clock limit for one engine session


@dataclass(frozen=True)
class Labels:
    ready: str = "vigia:ready"
    building: str = "vigia:building"
    review: str = "vigia:review"
    parked: str = "vigia:parked"
    human_ok: str = "vigia:human-ok"
    tier_prefix: str = "tier:"

    def tier(self, tier: str) -> str:
        return f"{self.tier_prefix}{tier}"


@dataclass(frozen=True)
class Config:
    repo: str  # owner/name on GitHub
    base: str
    tiers: dict[str, TierPolicy]
    caps: Caps = field(default_factory=Caps)
    labels: Labels = field(default_factory=Labels)
    require_ci: bool = True
    test_command: str | None = None
    humans: tuple[
        str, ...
    ] = ()  # GitHub logins allowed to add the human label; empty = anyone with write access
    build_in_watch: bool = True  # `vigia watch` also builds ready issues, one at a time
    interval_seconds: int = 120
    engine_args: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=lambda: default_engine_args())
    path: Path | None = None

    def policy(self, tier: str) -> TierPolicy:
        return self.tiers[tier]


# --- defaults ---------------------------------------------------------------------------------

DEFAULT_MODELS = {
    "claude": {"fast": "claude-sonnet-5-5", "strong": "claude-opus-5-5", "strongest": "claude-fable-5-1"},
    "codex": {"fast": "gpt-6.1-sol", "strong": "gpt-6.1-sol", "strongest": "gpt-6-astra"},
}


def other(engine: str) -> str:
    """The engine from the other lab."""
    return "codex" if engine == "claude" else "claude"


def default_tiers(builder: str = "claude") -> dict[str, TierPolicy]:
    """Builders from one lab, the gate from the other, a stronger reviewer from the builder's lab on T1/T2."""
    gate = other(builder)
    b, g = DEFAULT_MODELS[builder], DEFAULT_MODELS[gate]
    return {
        "T0": TierPolicy(builder=Seat(builder, b["fast"], "medium"), gate=Seat(gate, g["fast"], "medium")),
        "T1": TierPolicy(
            builder=Seat(builder, b["fast"], "high"),
            gate=Seat(gate, g["fast"], "medium"),
            reviewer=Seat(builder, b["strong"], "high"),
        ),
        "T2": TierPolicy(
            builder=Seat(builder, b["strong"], "high"),
            gate=Seat(gate, g["strong"], "high"),
            reviewer=Seat(builder, b["strongest"], "high"),
            human=True,
        ),
    }


def default_engine_args() -> dict[str, dict[str, tuple[str, ...]]]:
    return {engine: dict(roles) for engine, roles in DEFAULT_ENGINE_ARGS.items()}


DEFAULT_ENGINE_ARGS: dict[str, dict[str, tuple[str, ...]]] = {
    # Builders edit files and run commands inside their own worktree; reviewers only read.
    "claude": {
        "builder": (
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Bash",
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
        ),
        "reviewer": (
            "--permission-mode",
            "default",
            "--allowedTools",
            "Read",
            "Glob",
            "Grep",
            "Bash(git diff:*)",
            "Bash(git log:*)",
            "Bash(git show:*)",
            "--disallowedTools",
            "Edit",
            "Write",
            "NotebookEdit",
        ),
    },
    "codex": {
        "builder": ("--sandbox", "workspace-write"),
        "reviewer": ("--sandbox", "read-only"),
    },
}


# --- parsing ----------------------------------------------------------------------------------


def _seat(raw: object, where: str, required: bool = True) -> Seat | None:
    if raw is None:
        if required:
            raise ConfigError(f"{where}: missing")
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f'{where}: expected a table like {{ engine = "claude", model = "…" }}')
    unknown = set(raw) - {"engine", "model", "effort"}
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    engine, model, effort = raw.get("engine"), raw.get("model"), raw.get("effort")
    if engine not in FAMILIES:
        raise ConfigError(f"{where}.engine: {engine!r} is not one of {sorted(FAMILIES)}")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(f"{where}.model: an explicit model id is required")
    if model.strip().lower() in MODEL_ALIASES:
        raise ConfigError(
            f"{where}.model: {model!r} is an alias; write the exact model id (e.g. claude-opus-5-5)"
        )
    if effort is not None and effort not in EFFORTS[engine]:
        raise ConfigError(f"{where}.effort: {effort!r} is not valid for {engine} ({sorted(EFFORTS[engine])})")
    return Seat(engine=engine, model=model.strip(), effort=effort)


def _int(raw: dict, key: str, default: int, minimum: int = 1) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"caps.{key}: expected an integer >= {minimum}, got {value!r}")
    return value


def parse(data: dict, path: Path | None = None) -> Config:
    """Build and validate a Config from the parsed TOML document."""
    repo_t = data.get("repo") or {}
    repo = repo_t.get("name")
    if not isinstance(repo, str) or repo.count("/") != 1 or not all(repo.split("/")):
        raise ConfigError("repo.name: expected 'owner/name'")
    base = repo_t.get("base", "main")
    if not isinstance(base, str) or not base:
        raise ConfigError("repo.base: expected a branch name")

    tiers_t = data.get("tiers") or {}
    missing = [t for t in TIERS if t not in tiers_t]
    if missing:
        raise ConfigError(f"tiers: missing {', '.join(missing)}")
    tiers: dict[str, TierPolicy] = {}
    for tier in TIERS:
        t = tiers_t[tier]
        if not isinstance(t, dict):
            raise ConfigError(f"tiers.{tier}: expected a table")
        unknown = set(t) - {"builder", "gate", "reviewer", "human"}
        if unknown:
            raise ConfigError(f"tiers.{tier}: unknown keys {sorted(unknown)}")
        builder = _seat(t.get("builder"), f"tiers.{tier}.builder")
        gate = _seat(t.get("gate"), f"tiers.{tier}.gate")
        reviewer = _seat(t.get("reviewer"), f"tiers.{tier}.reviewer", required=tier != "T0")
        assert builder and gate
        if gate.family == builder.family:
            raise ConfigError(
                f"tiers.{tier}: the gate ({gate.engine}) is from the same lab as the builder ({builder.engine}); "
                "the gate must come from the other lab"
            )
        human = t.get("human", tier == "T2")
        if not isinstance(human, bool):
            raise ConfigError(f"tiers.{tier}.human: expected true or false")
        if tier == "T2" and not human:
            raise ConfigError("tiers.T2.human: T2 always needs a human label on the exact head")
        tiers[tier] = TierPolicy(builder=builder, gate=gate, reviewer=reviewer, human=human)

    caps_t = data.get("caps") or {}
    unknown = set(caps_t) - set(Caps.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"caps: unknown keys {sorted(unknown)}")
    caps = Caps(**{k: _int(caps_t, k, getattr(Caps, k)) for k in Caps.__dataclass_fields__})

    labels_t = data.get("labels") or {}
    unknown = set(labels_t) - set(Labels.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"labels: unknown keys {sorted(unknown)}")
    labels = Labels(**{k: str(v) for k, v in labels_t.items()})
    names = [labels.ready, labels.building, labels.review, labels.parked, labels.human_ok]
    if len(set(names)) != len(names):
        raise ConfigError("labels: every label must be different")

    watch_t = data.get("watch") or {}
    checks_t = data.get("checks") or {}
    humans = checks_t.get("humans", [])
    if not isinstance(humans, list) or not all(isinstance(h, str) and h for h in humans):
        raise ConfigError("checks.humans: expected a list of GitHub logins")
    test_command = checks_t.get("test_command")
    if test_command is not None and (not isinstance(test_command, str) or not test_command.strip()):
        raise ConfigError("checks.test_command: expected a shell command")
    require_ci = checks_t.get("require_ci", True)
    if not isinstance(require_ci, bool):
        raise ConfigError("checks.require_ci: expected true or false")
    interval = watch_t.get("interval_seconds", 120)
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 10:
        raise ConfigError("watch.interval_seconds: expected an integer >= 10")
    build_in_watch = watch_t.get("build", True)
    if not isinstance(build_in_watch, bool):
        raise ConfigError("watch.build: expected true or false")

    engine_args = default_engine_args()
    for engine, roles in (data.get("engines") or {}).items():
        if engine not in FAMILIES:
            raise ConfigError(f"engines.{engine}: unknown engine")
        for role, args in (roles or {}).items():
            if role not in ("builder", "reviewer"):
                raise ConfigError(f"engines.{engine}.{role}: expected 'builder' or 'reviewer'")
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ConfigError(f"engines.{engine}.{role}: expected a list of CLI arguments")
            engine_args[engine][role] = tuple(args)

    return Config(
        repo=repo,
        base=base,
        tiers=tiers,
        caps=caps,
        labels=labels,
        require_ci=require_ci,
        test_command=test_command.strip() if test_command else None,
        humans=tuple(humans),
        build_in_watch=build_in_watch,
        interval_seconds=interval,
        engine_args=engine_args,
        path=path,
    )


def load(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: not found — run `vigia init` first") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse(data, path)


def find(start: Path) -> Path:
    """The nearest vigia.toml at or above `start`."""
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise ConfigError(f"no {CONFIG_NAME} found at or above {start} — run `vigia init` first")


# --- rendering --------------------------------------------------------------------------------


_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def _q(value: str) -> str:
    """A TOML basic string; control characters are escaped so the value stays on one line."""
    return (
        '"'
        + "".join(
            _ESCAPES.get(c) or (f"\\u{ord(c):04x}" if ord(c) < 0x20 or c == "\x7f" else c) for c in value
        )
        + '"'
    )


def _seat_toml(seat: Seat) -> str:
    effort = f", effort = {_q(seat.effort)}" if seat.effort else ""
    return f"{{ engine = {_q(seat.engine)}, model = {_q(seat.model)}{effort} }}"


def render(cfg: Config) -> str:
    """vigia.toml text for `cfg`. `parse(tomllib.loads(render(cfg)))` gives `cfg` back."""
    out = [
        "# vigia — AI agents write the code. A different lab reviews it. A script with no AI merges it.",
        "# Docs: https://github.com/hector14mv/vigia/blob/main/docs/operating-model.md",
        "",
        "[repo]",
        f"name = {_q(cfg.repo)}",
        f"base = {_q(cfg.base)}",
        "",
        "# One table per damage tier. The gate must come from the other lab than the builder.",
        "# T0: cosmetic, docs, tests, internal tooling — builder + gate.",
        "# T1: product logic with no money and no other people's data — + a stronger reviewer.",
        "# T2: money, auth, data, migrations, messages to customers, credentials — + a human label.",
    ]
    for tier in TIERS:
        p = cfg.tiers[tier]
        out += ["", f"[tiers.{tier}]", f"builder = {_seat_toml(p.builder)}", f"gate = {_seat_toml(p.gate)}"]
        if p.reviewer:
            out.append(f"reviewer = {_seat_toml(p.reviewer)}")
        if p.human:
            out.append("human = true")
    c = cfg.caps
    out += [
        "",
        "[caps]",
        f"gate_rounds = {c.gate_rounds}",
        f"fix_rounds = {c.fix_rounds}",
        f"review_rounds = {c.review_rounds}",
        f"attempts = {c.attempts}",
        f"session_minutes = {c.session_minutes}",
        "",
        "[checks]",
        f"require_ci = {'true' if cfg.require_ci else 'false'}",
    ]
    if cfg.test_command:
        out.append(f"test_command = {_q(cfg.test_command)}")
    out.append("humans = [" + ", ".join(_q(h) for h in cfg.humans) + "]")
    out += [
        "",
        "[watch]",
        f"build = {'true' if cfg.build_in_watch else 'false'}",
        f"interval_seconds = {cfg.interval_seconds}",
    ]
    defaults = Labels()
    changed = {
        k: getattr(cfg.labels, k)
        for k in Labels.__dataclass_fields__
        if getattr(cfg.labels, k) != getattr(defaults, k)
    }
    if changed:
        out += ["", "[labels]"] + [f"{k} = {_q(v)}" for k, v in changed.items()]
    for engine, roles in cfg.engine_args.items():
        changed_roles = {r: a for r, a in roles.items() if DEFAULT_ENGINE_ARGS.get(engine, {}).get(r) != a}
        if changed_roles:
            out += ["", f"[engines.{engine}]"]
            out += [
                f"{role} = [" + ", ".join(_q(a) for a in args) + "]" for role, args in changed_roles.items()
            ]
    return "\n".join(out) + "\n"
