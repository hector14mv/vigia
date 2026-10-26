import tomllib

import pytest

from vigia.config import (
    Config,
    ConfigError,
    Labels,
    Seat,
    TierPolicy,
    default_tiers,
    find,
    lab_of,
    load,
    other,
    parse,
    render,
)


def base_doc() -> dict:
    return tomllib.loads(render(Config(repo="acme/app", base="main", tiers=default_tiers("claude"))))


def test_render_then_parse_round_trips():
    for builder in ("claude", "codex"):
        cfg = Config(
            repo="acme/app",
            base="trunk",
            tiers=default_tiers(builder),
            test_command="make test",
            humans=("alice",),
        )
        again = parse(tomllib.loads(render(cfg)))
        assert again.tiers == cfg.tiers
        assert (again.repo, again.base, again.test_command, again.humans) == (
            "acme/app",
            "trunk",
            "make test",
            ("alice",),
        )


def test_defaults_put_the_gate_in_the_other_lab():
    for builder, gate_family in (("claude", "openai"), ("codex", "anthropic")):
        for policy in default_tiers(builder).values():
            assert policy.gate.family == gate_family != policy.builder.family


def test_same_lab_gate_is_refused():
    doc = base_doc()
    doc["tiers"]["T1"]["gate"] = {"engine": "claude", "model": "claude-opus-5-5"}
    with pytest.raises(ConfigError, match="same lab"):
        parse(doc)


@pytest.mark.parametrize("alias", ["opus", "sonnet", "default", "Fable"])
def test_model_aliases_are_refused(alias):
    doc = base_doc()
    doc["tiers"]["T0"]["builder"]["model"] = alias
    with pytest.raises(ConfigError, match="alias"):
        parse(doc)


def test_t2_cannot_drop_the_human():
    doc = base_doc()
    doc["tiers"]["T2"]["human"] = False
    with pytest.raises(ConfigError, match="human"):
        parse(doc)


def test_t1_needs_a_reviewer():
    doc = base_doc()
    del doc["tiers"]["T1"]["reviewer"]
    with pytest.raises(ConfigError, match="T1.reviewer"):
        parse(doc)


def test_missing_tier_and_bad_repo():
    doc = base_doc()
    del doc["tiers"]["T2"]
    with pytest.raises(ConfigError, match="missing T2"):
        parse(doc)
    doc = base_doc()
    doc["repo"]["name"] = "no-slash"
    with pytest.raises(ConfigError, match="owner/name"):
        parse(doc)


def test_bad_effort_and_unknown_keys():
    doc = base_doc()
    doc["tiers"]["T0"]["gate"]["effort"] = "max"  # valid for claude, not for codex
    with pytest.raises(ConfigError, match="effort"):
        parse(doc)
    doc = base_doc()
    doc["tiers"]["T0"]["builder"]["temperature"] = 1
    with pytest.raises(ConfigError, match="unknown keys"):
        parse(doc)


@pytest.mark.parametrize("value", [0, -1, "2", True])
def test_caps_must_be_positive_integers(value):
    doc = base_doc()
    doc["caps"]["gate_rounds"] = value
    with pytest.raises(ConfigError, match="gate_rounds"):
        parse(doc)


def test_labels_must_differ():
    doc = base_doc()
    doc["labels"] = {"ready": "vigia:review"}
    with pytest.raises(ConfigError, match="different"):
        parse(doc)


@pytest.mark.parametrize("name", ["vigia:proposed", "VIGIA:HUMAN-TASK", "tier:T1"])
def test_labels_must_not_be_a_planner_or_tier_label(name):
    doc = base_doc()
    doc["labels"] = {"ready": name}
    with pytest.raises(ConfigError, match="reserved"):
        parse(doc)


def test_engine_args_override_round_trips():
    doc = base_doc()
    doc["engines"] = {"codex": {"builder": ["--sandbox", "danger-full-access"]}}
    cfg = parse(doc)
    assert cfg.engine_args["codex"]["builder"] == ("--sandbox", "danger-full-access")
    assert parse(tomllib.loads(render(cfg))).engine_args == cfg.engine_args


def test_custom_labels_round_trip():
    cfg = Config(repo="a/b", base="main", tiers=default_tiers(), labels=Labels(ready="todo:agent"))
    assert parse(tomllib.loads(render(cfg))).labels.ready == "todo:agent"


def test_load_and_find(tmp_path):
    (tmp_path / "vigia.toml").write_text(render(Config(repo="a/b", base="main", tiers=default_tiers())))
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    path = find(nested)
    assert path == tmp_path / "vigia.toml"
    assert load(path).repo == "a/b"
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "missing.toml")


def test_seat_label_and_family():
    seat = Seat("codex", "gpt-6.1-sol", "medium")
    assert seat.family == "openai"
    assert seat.label() == "codex · gpt-6.1-sol · effort medium"
    assert TierPolicy(builder=seat, gate=Seat("claude", "claude-opus-5-5")).reviewer is None


def test_multiline_and_control_characters_round_trip():
    command = 'ruff check .\npytest -q\r\n\techo "done" \\ \x01'
    doc = base_doc()
    doc["checks"] = {"test_command": command}
    cfg = parse(doc)
    assert cfg.test_command == command.strip()
    assert parse(tomllib.loads(render(cfg))).test_command == cfg.test_command


def test_the_follow_up_label_must_differ_from_the_ready_label():
    doc = base_doc()
    doc["labels"] = {"follow_up": "vigia:ready"}
    with pytest.raises(ConfigError, match="different"):
        parse(doc)


@pytest.mark.parametrize("key", ["follow_up", "ready"])
def test_labels_that_differ_only_in_case_are_the_same_label(key):
    doc = base_doc()
    doc["labels"] = {"follow_up": "VIGIA:READY"} if key == "follow_up" else {"ready": "Vigia:Follow-Up"}
    with pytest.raises(ConfigError, match="different"):
        parse(doc)


def test_render_comments_read_carefully():
    text = render(Config(repo="acme/app", base="main", tiers=default_tiers("claude")))
    assert "The gate must come from the other lab, not the builder's." in text
    assert "T2: money, auth, personal data, migrations" in text


# --- labs -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "engine, model, lab",
    [
        ("claude", "claude-opus-5-5", "anthropic"),
        ("claude", "gpt-6.1-sol", "anthropic"),  # a fixed engine's lab never depends on the model
        ("claude", "", "anthropic"),
        ("codex", "gpt-6.1-sol", "openai"),
        ("codex", "claude-opus-5-5", "openai"),
        ("codex", "", "openai"),
        ("opencode", "anthropic/claude-opus-5-5", "anthropic"),
        ("opencode", "github-copilot/claude-opus-5.5", "anthropic"),
        ("opencode", "Anthropic/Claude-X", "anthropic"),
        ("opencode", "openai/gpt-6.1-sol", "openai"),
        ("opencode", "github-copilot/gpt-6-sol", "openai"),
        ("opencode", "openai/o3-mini", "openai"),
        ("opencode", "openai/O4", "openai"),
        ("opencode", "openai/codex-mini", "openai"),
        ("opencode", "github-copilot/gemini-x", "google"),
        ("opencode", "xai/grok-5", "xai"),
        ("opencode", "opencode-go/kimi-k3", "moonshotai"),
        ("opencode", "opencode-go/glm-x", "zhipuai"),
        ("opencode", "opencode-go/deepseek-v4", "deepseek"),
        ("opencode", "opencode-go/qwen-4", "alibaba"),
        ("opencode", "anthropic/gpt-6-sol", "openai"),  # the provider never decides the lab
    ],
)
def test_lab_table(engine, model, lab):
    assert lab_of(engine, model) == lab


@pytest.mark.parametrize(
    "engine, model",
    [
        ("opencode", "kimi-k3"),  # no provider
        ("opencode", "/kimi-k3"),
        ("opencode", "opencode-go/"),
        ("opencode", "somelab/mystery-1"),
        ("opencode", ""),
        ("opencode", "anthropic/claude"),  # a family needs its full prefix
        ("opencode", "anthropic/openai/claude-x"),  # one provider, one model
        ("opencode", "openai/ogpt-1"),  # `o` only counts before a digit
        ("opencode", "openai/o\u0663"),  # an ASCII digit, not any Unicode one
        ("opencode", "moonshot/\u212aimi-k3"),  # KELVIN SIGN is not a `k`
        ("opencode", "anthropic/ claude-x"),
        ("mystery", "anthropic/claude-x"),  # an unknown engine has no lab
    ],
)
def test_lab_of_never_guesses(engine, model):
    assert lab_of(engine, model) is None


def test_a_seat_with_no_lab_has_no_family():
    assert Seat("opencode", "opencode-go/kimi-k3").family == "moonshotai"
    with pytest.raises(ValueError, match="no known lab"):
        _ = Seat("opencode", "somelab/mystery-1").family


def test_other_pairs_only_claude_and_codex():
    assert (other("claude"), other("codex")) == ("codex", "claude")
    for engine in ("opencode", "mystery", ""):
        with pytest.raises(ValueError):
            other(engine)


def test_default_tiers_are_unchanged():
    for builder, gate in (("claude", "codex"), ("codex", "claude")):
        tiers = default_tiers(builder)
        assert {
            t: (p.builder.engine, p.gate.engine, p.reviewer and p.reviewer.engine) for t, p in tiers.items()
        } == {
            "T0": (builder, gate, None),
            "T1": (builder, gate, builder),
            "T2": (builder, gate, builder),
        }
    assert default_tiers("claude")["T2"] == TierPolicy(
        builder=Seat("claude", "claude-opus-5-5", "high"),
        gate=Seat("codex", "gpt-6.1-sol", "high"),
        reviewer=Seat("claude", "claude-fable-5-1", "high"),
        human=True,
    )


@pytest.mark.parametrize("role", ["builder", "gate", "reviewer"])
def test_an_opencode_seat_is_refused_at_load(role):
    doc = base_doc()
    doc["tiers"]["T1"][role] = {"engine": "opencode", "model": "opencode-go/kimi-k3"}
    with pytest.raises(
        ConfigError, match=rf"^tiers\.T1\.{role}\.engine: OpenCode is not available yet \(#107\)$"
    ):
        parse(doc)
