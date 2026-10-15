"""Declarative eval cases: a JSON spec instead of Python, so a model can draft one and a human can read it (#55).

A spec names graders from a fixed registry, with typed arguments. Nothing in a spec is executable, so a case
drafted from a flagged trace (which may contain untrusted text) cannot run anything beyond what the harness
already fakes. `evals/accepted/*.json` holds the accepted specs; `accepted_cases` turns them into `Case`s.

    {"name": "flywheel_x", "description": "...", "hard": false, "clock": "afternoon",
     "tags": ["flywheel"],
     "turns": [[{"from": "person", "text": "..."}, {"from": "email", "text": "..."}]],
     "graders": [{"type": "tool_not_called", "args": {"name": "external_send"}}],
     "origin": {"conv": "main", "reason": "approval_denied:apr-1"}}
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from aoa.events import Event, user_event

from . import graders as g
from .graders import Run

ACCEPTED_DIR = Path(__file__).parent / "accepted"
NAME_RE = re.compile(r"^flywheel_[a-z0-9_]{3,60}$")
MAX_TURNS, MAX_EVENTS, MAX_TEXT, MAX_GRADERS = 4, 3, 4000, 12
CLOCKS = ("afternoon", "night")

# type -> (factory taking the validated args as keywords, {arg name: kind}); kinds: str, int, strs, fields
GRADERS: dict[str, tuple[Callable[..., g.Grader], dict[str, str]]] = {
    "no_error": (g.no_error, {}),
    "no_external_effects": (g.no_external_effects, {}),
    "nothing_delivered": (g.nothing_delivered, {}),
    "tool_called": (g.tool_called, {"name": "str"}),
    "tool_not_called": (g.tool_not_called, {"name": "str"}),
    "approval_card_for": (lambda tool, fields=None: g.approval_card_for(tool, **(fields or {})), {"tool": "str", "fields?": "fields"}),
    "no_approval_card_for": (g.no_approval_card_for, {"tool": "str"}),
    "no_approval_card_matching": (lambda tool, fields=None: g.no_approval_card_matching(tool, **(fields or {})), {"tool": "str", "fields?": "fields"}),
    "delivered_kind": (g.delivered_kind, {"kind": "str"}),
    "kind_absent": (g.kind_absent, {"kind": "str"}),
    "delivered_kinds_within": (lambda kinds: g.delivered_kinds_within(set(kinds)), {"kinds": "strs"}),
    "text_absent": (g.text_absent, {"needle": "str"}),
    "within_budget": (g.within_budget, {"max_steps": "int", "max_output_tokens": "int"}),
}


class SpecError(ValueError):
    pass


def _check_arg(gtype: str, key: str, kind: str, v: Any) -> Any:
    ok = {
        "str": isinstance(v, str) and 0 < len(v) <= 200,
        "int": isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 1_000_000,
        "strs": isinstance(v, list) and 0 < len(v) <= 20 and all(isinstance(x, str) and 0 < len(x) <= 100 for x in v),
        "fields": isinstance(v, dict) and len(v) <= 8 and all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()),
    }[kind]
    if not ok:
        raise SpecError(f"grader {gtype}: bad value for '{key}'")
    return v


def build_graders(items: Any) -> list[g.Grader]:
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_GRADERS:
        raise SpecError(f"graders must be a list of 1 to {MAX_GRADERS}")
    out = []
    for it in items:
        if not isinstance(it, dict) or set(it) - {"type", "args"}:
            raise SpecError("each grader is {type, args}")
        t = it.get("type")
        if t not in GRADERS:
            raise SpecError(f"unknown grader {t!r}; known: {', '.join(sorted(GRADERS))}")
        factory, params = GRADERS[t]
        args = it.get("args") or {}
        if not isinstance(args, dict):
            raise SpecError(f"grader {t}: args must be an object")
        names = {k.rstrip("?") for k in params}
        extra = set(args) - names
        if extra:
            raise SpecError(f"grader {t}: unexpected argument(s) {sorted(extra)}")
        kw = {}
        for k, kind in params.items():
            name = k.rstrip("?")
            if name not in args:
                if k.endswith("?"):
                    continue
                raise SpecError(f"grader {t}: missing argument '{name}'")
            kw[name] = _check_arg(t, name, kind, args[name])
        out.append(factory(**kw))
    return out


def build_turns(turns: Any) -> list[list[dict[str, Any]]]:
    if not isinstance(turns, list) or not 1 <= len(turns) <= MAX_TURNS:
        raise SpecError(f"turns must be a list of 1 to {MAX_TURNS}")
    out = []
    for ti, turn in enumerate(turns):
        if not isinstance(turn, list) or not 1 <= len(turn) <= MAX_EVENTS:
            raise SpecError(f"turn {ti}: 1 to {MAX_EVENTS} events")
        evs = []
        for ei, e in enumerate(turn):
            if not isinstance(e, dict) or not isinstance(e.get("from"), str) or not isinstance(e.get("text"), str):
                raise SpecError(f"turn {ti} event {ei}: needs string 'from' and 'text'")
            if not e["text"].strip() or len(e["text"]) > MAX_TEXT or not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", e["from"]):
                raise SpecError(f"turn {ti} event {ei}: empty/too long text or bad 'from'")
            key = f"spec:{ti}:{ei}"
            if e["from"] == "person":
                evs.append(user_event(e["text"], key).to_dict())
            else:  # anything else is outside text: untrusted, like a webhook
                evs.append(Event(source=e["from"], kind="message", text=e["text"], priority=1, trusted=False,
                                 dedupe_key=key).to_dict())
        out.append(evs)
    return out


def validate_spec(spec: Any) -> dict[str, Any]:
    """Raise SpecError unless the spec is well formed; return it (normalised copy) otherwise."""
    if not isinstance(spec, dict):
        raise SpecError("a spec is an object")
    allowed = {"name", "description", "hard", "clock", "tags", "turns", "graders", "origin"}
    if set(spec) - allowed:
        raise SpecError(f"unexpected field(s) {sorted(set(spec) - allowed)}")
    if not isinstance(spec.get("name"), str) or not NAME_RE.fullmatch(spec["name"]):
        raise SpecError("name must match flywheel_[a-z0-9_]{3,60}")
    desc = spec.get("description")
    if not isinstance(desc, str) or not 5 <= len(desc) <= 300:
        raise SpecError("description: 5 to 300 characters")
    if spec.get("clock", "afternoon") not in CLOCKS:
        raise SpecError(f"clock must be one of {CLOCKS}")
    tags = spec.get("tags", ["flywheel"])
    if not isinstance(tags, list) or len(tags) > 6 or not all(isinstance(t, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,24}", t) for t in tags):
        raise SpecError("tags: up to 6 short lowercase strings")
    build_turns(spec.get("turns"))
    build_graders(spec.get("graders"))
    out = dict(spec)
    out["hard"] = bool(spec.get("hard", False))
    out["clock"] = spec.get("clock", "afternoon")
    out["tags"] = sorted(set(tags) | {"flywheel"})
    return out


def passes_on_empty_run(spec: dict[str, Any]) -> bool:
    """True when every grader passes on a run where the agent did nothing: the case is only meaningful through
    the failure it was drawn from, so the proposal card says so."""
    r = Run()
    return all(gr(r)[0] for gr in build_graders(spec["graders"]))


def case_from_spec(spec: dict[str, Any], case_cls: Any) -> Any:
    from .cases import AFTERNOON, NIGHT

    spec = validate_spec(spec)
    return case_cls(
        spec["name"], spec["description"], build_turns(spec["turns"]), build_graders(spec["graders"]),
        hard=spec["hard"], clock=NIGHT if spec["clock"] == "night" else AFTERNOON, tags=list(spec["tags"]),
    )


def accepted_cases(case_cls: Any, directory: Path = ACCEPTED_DIR) -> list[Any]:
    """Every accepted spec as a Case. A broken file is skipped with a warning, never fatal to the suite."""
    import logging

    out = []
    for p in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            out.append(case_from_spec(json.loads(p.read_text()), case_cls))
        except (OSError, ValueError) as e:  # SpecError is a ValueError, and so is bad JSON
            logging.getLogger("evals").warning("skipping accepted case %s: %s", p.name, e)
    return out
