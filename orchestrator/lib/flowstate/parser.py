from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pydot
import yaml


# Node shape vocabulary. Matches Graphviz's shape attribute for the DOT files
# the parser ingests. `box` = agent / orchestrator / script node (the work-
# performing ones), `Msquare` = terminal (run-completed) marker,
# `Mdiamond` = start marker. See Issue #46.
NodeShape = Literal["box", "Msquare", "Mdiamond"]

# Node runner vocabulary. `agent` = spawn an external worker (e.g. claude),
# `orchestrator` = handle inline in the driving orchestrator agent,
# `script` = run a local script and emit outputs deterministically. Only
# `agent` and `orchestrator` have a prompt_template + render-prompt path.
# Multi-branch runners: `fork` = fan-out to parallel branches, `join` =
# synchronise parallel branches, `dynamic_fanout` = runtime-determined fan-out
# over an iterable variable, `subflow` = delegate to a child flow definition.
NodeRunner = Literal[
    "agent", "orchestrator", "script", "fork", "join", "dynamic_fanout", "subflow",
]


@dataclass
class Node:
    name: str
    shape: NodeShape = "box"
    runner: NodeRunner = "agent"
    prompt_template: str | None = None
    script: str | None = None
    working_dir: str | None = None
    output_schema: str | None = None
    model: str | None = None
    # agentctl harness this node's worker runs on ("claude-code",
    # "opencode", ...). Passed through the node-config payload to
    # `agentctl spawn --harness`. None means "caller decides" — in practice
    # the orchestrator skill's default.
    #
    # Added 2026-08-07: `model` was honoured here but `harness` was not a
    # field at all, so a DOT that set harness="opencode" was silently
    # ignored while its model= was obeyed — spawning `claude --model
    # opencode/...`, which fails with an access error. A silently-dropped
    # attribute that looks meaningful is worse than an unsupported one.
    harness: str | None = None
    # Autonomy level in agentctl's vocabulary ("full" = no permission
    # prompts); passed through the node-config payload to `agentctl spawn
    # --autonomy`, where the selected harness translates it to its native
    # flag. Replaces the old claude-specific `permission_mode` attribute.
    autonomy: str | None = None
    pauses_at_min: str | None = None
    worktree: str | None = None
    bypass_at: list[str] = field(default_factory=list)
    description: str | None = None
    category: str | None = None
    # Multi-branch node attributes. All None/empty unless the runner uses them.
    reducer_script: str | None = None     # script run per branch arrival at a join
    summary_var: str | None = None        # parent-scope var the reducer maintains
    source_var: str | None = None         # variable holding the iterable (dynamic_fanout)
    template_node: str | None = None      # template subflow node name (dynamic_fanout)
    max_branches: int | None = None       # optional fan-out cap (dynamic_fanout)
    max_concurrent: int | None = None     # optional rolling-window concurrency cap (dynamic_fanout)
    subflow_flow: str | None = None       # child flow definition name (subflow)
    subflow_inputs: dict[str, str] = field(default_factory=dict)   # {child_var: parent_expr}
    subflow_outputs: dict[str, str] = field(default_factory=dict)  # {parent_var: child_var}


@dataclass
class Edge:
    source: str
    target: str
    gates: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    condition: str | None = None
    label: str | None = None
    description: str | None = None


@dataclass
class FlowGraph:
    nodes: list[Node]
    edges: list[Edge]
    label: str | None = None
    description: str | None = None

    def node(self, name: str) -> Node:
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(f"no node named {name!r}")

    def start_node(self) -> Node:
        starts = [n for n in self.nodes if n.shape == "Mdiamond"]
        if not starts:
            raise ValueError("no start node (Mdiamond) in graph")
        if len(starts) > 1:
            raise ValueError(
                f"multiple start nodes (Mdiamond) in graph: "
                f"{[n.name for n in starts]} — flow execution would be ambiguous"
            )
        return starts[0]

    def end_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.shape == "Msquare"]

    def edges_from(self, source: str) -> list[Edge]:
        return [e for e in self.edges if e.source == source]


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _split_csv(s: str | None) -> list[str]:
    if not s:
        return []
    return [item.strip() for item in _strip_quotes(s).split(",") if item.strip()]


def _opt(attrs: dict[str, str], key: str) -> str | None:
    return _strip_quotes(attrs[key]) if key in attrs else None


def _unescape_dot_str(s: str) -> str:
    """Unescape backslash-escaped quotes that pydot preserves inside quoted
    string values (e.g. ``\\"`` → ``"``). Called after outer quotes are
    stripped so only inner escape sequences remain."""
    return s.replace('\\"', '"').replace("\\'", "'")


def _parse_kv_attr(raw: str | None) -> dict[str, str]:
    """Parse a DOT attribute of the form `{ key: "value", key2: "value2" }`
    into a dict. Returns {} when absent. Tolerates unquoted keys and
    single/double-quoted values.

    pydot preserves backslash-escaped inner quotes (``\\"``); ``_unescape_dot_str``
    normalises them after stripping outer delimiters.

    Raises ``ValueError`` on a duplicate key — silent last-wins would mask
    flow-authoring errors (e.g. an `inputs="a: 1, a: 2"` attribute) so we
    reject at parse time. See M8."""
    if not raw:
        return {}
    s = _strip_quotes(raw).strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    out: dict[str, str] = {}
    seen: set[str] = set()
    for pair in s.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        k, v = pair.split(":", 1)
        key = _strip_quotes(k.strip())
        if key in seen:
            raise ValueError(
                f"duplicate key {key!r} in kv attribute: {raw!r}"
            )
        seen.add(key)
        out[key] = _strip_quotes(_unescape_dot_str(_strip_quotes(v.strip())))
    return out


def _parse_max_concurrent(attrs: dict[str, str]) -> int | None:
    """Parse the `max_concurrent` attribute on a dynamic_fanout node.

    Returns None when absent. Raises ValueError for non-integer or negative
    values. `max_concurrent=0` is allowed (means unbounded, per spec §4.1).
    """
    raw = _opt(attrs, "max_concurrent")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"max_concurrent attribute on dynamic_fanout must be an integer; "
            f"got {raw!r}"
        )
    if value < 0:
        raise ValueError(
            f"max_concurrent attribute on dynamic_fanout must be a non-negative "
            f"integer; got {value}"
        )
    return value


def _parse_node(pn: pydot.Node) -> Node:
    name = _strip_quotes(pn.get_name())
    attrs = pn.get_attributes() or {}
    if attrs.get("join_mode") is not None:
        raise ValueError(
            f"node {name!r}: join_mode attribute is no longer supported "
            "(per 2026-06-03 design — joins are always all-mode for downstream; "
            "use reducer_script for per-arrival behavior)"
        )
    return Node(
        name=name,
        shape=_strip_quotes(attrs.get("shape", "box")),
        runner=_strip_quotes(attrs.get("runner", "agent")),
        prompt_template=_opt(attrs, "prompt_template"),
        script=_opt(attrs, "script"),
        working_dir=_opt(attrs, "working_dir"),
        output_schema=_opt(attrs, "output_schema"),
        model=_opt(attrs, "model"),
        harness=_opt(attrs, "harness"),
        autonomy=_opt(attrs, "autonomy"),
        pauses_at_min=_opt(attrs, "pauses_at_min"),
        worktree=_opt(attrs, "worktree"),
        bypass_at=_split_csv(attrs.get("bypass_at")),
        description=_opt(attrs, "description"),
        category=_opt(attrs, "category"),
        reducer_script=_opt(attrs, "reducer_script"),
        summary_var=_opt(attrs, "summary_var"),
        source_var=_opt(attrs, "source"),
        template_node=_opt(attrs, "template"),
        max_branches=(int(_opt(attrs, "max_branches")) if _opt(attrs, "max_branches") else None),
        max_concurrent=_parse_max_concurrent(attrs),
        subflow_flow=_opt(attrs, "flow"),
        subflow_inputs=_parse_kv_attr(attrs.get("inputs")),
        subflow_outputs=_parse_kv_attr(attrs.get("outputs")),
    )


def _parse_edge(pe: pydot.Edge) -> Edge:
    attrs = pe.get_attributes() or {}
    return Edge(
        source=_strip_quotes(pe.get_source()),
        target=_strip_quotes(pe.get_destination()),
        gates=_split_csv(attrs.get("gates")),
        scripts=_split_csv(attrs.get("scripts")),
        condition=_opt(attrs, "condition"),
        label=_opt(attrs, "label"),
        description=_opt(attrs, "description"),
    )


def _read_graph_default_attr(g: pydot.Dot, key: str) -> str | None:
    """Read a graph-level default attribute (e.g., `graph [label="..."]`).

    pydot exposes these via a synthetic `graph` Node. The label has a direct
    accessor too; arbitrary attributes are in get_attributes().
    """
    for n in g.get_nodes():
        if _strip_quotes(n.get_name()) == "graph":
            val = n.get_attributes().get(key)
            if val is not None:
                return _strip_quotes(val)
    return None


def parse_dot(dot_text: str) -> FlowGraph:
    graphs = pydot.graph_from_dot_data(dot_text)
    if not graphs:
        raise ValueError("could not parse DOT text")
    g = graphs[0]
    nodes = [
        _parse_node(n)
        for n in g.get_nodes()
        if _strip_quotes(n.get_name()) not in ("node", "edge", "graph")
    ]
    edges = [_parse_edge(e) for e in g.get_edges()]
    # Cross-validate edge endpoints against declared nodes. Without this, a
    # typo in `a -> b` (where `b` isn't declared) parses cleanly and surfaces
    # later as a KeyError deep inside `graph.node(...)`, often mid-state
    # mutation. The .flow.yml `nodes:` block already does this; the DOT side
    # should too. See Issue #42.
    declared = {n.name for n in nodes}
    for edge in edges:
        for endpoint, role in ((edge.source, "source"), (edge.target, "target")):
            if endpoint not in declared:
                raise ValueError(
                    f"edge {edge.source!r} -> {edge.target!r} references "
                    f"undeclared node {endpoint!r} ({role}); declared nodes "
                    f"are {sorted(declared)!r}"
                )
    # Conditions are only evaluated on multi-edge fan-outs (`traversal.advance`
    # gates the condition block on `len(edges) > 1`). A condition on a node
    # with a single outgoing edge is silently ignored — likely an authoring
    # mistake. Catch it at parse time so the intent is clear. See Issue #44h.
    out_counts: dict[str, int] = {}
    for edge in edges:
        out_counts[edge.source] = out_counts.get(edge.source, 0) + 1
    for edge in edges:
        if edge.condition and out_counts[edge.source] == 1:
            raise ValueError(
                f"edge {edge.source!r} -> {edge.target!r} has condition "
                f"{edge.condition!r} but is the only outgoing edge from "
                f"{edge.source!r}; single-edge conditions are silently "
                f"ignored at runtime — either add a sibling edge or drop "
                f"the condition"
            )
    label = _read_graph_default_attr(g, "label")
    description = _read_graph_default_attr(g, "description")
    graph = FlowGraph(nodes=nodes, edges=edges, label=label, description=description)
    graph.start_node()  # raises if missing
    return graph


# ----- .flow.yml schema -----


@dataclass
class SchemaFile:
    name: str
    path: str
    # None => existence-only output: flowstate checks the file exists and is
    # non-empty, but does not parse it, schema-validate it, or require an
    # in-file _session_id. Used for markdown artefacts (research briefs)
    # whose shape is owned by the producing skill. Spec 2026-07-07 §3.2.
    definition: str | None = None


@dataclass
class OutputSchema:
    name: str
    files: list[SchemaFile] = field(default_factory=list)
    sets_variables: dict[str, str] = field(default_factory=dict)
    # Variables the orchestrator MUST set before calling `flowstate complete`
    # on a `runner=orchestrator` node using this schema. Listed by name, not
    # mapped to file IDs (orchestrator nodes have no output files). See
    # Issue #35.
    required_variables: list[str] = field(default_factory=list)


@dataclass
class VarSpec:
    name: str
    type: str  # "string" | "path" | "enum" | "dict" | "list"
    values: list[str] | None = None
    default: Any = None
    # For type=list: declares the item subtype (e.g. "dict", "string"). This is
    # documentation only — flowstate does not structurally validate list items
    # in v1; list-typed variables are treated as opaque JSON values at runtime.
    items: str | None = None

    def __post_init__(self) -> None:
        if self.type not in ("string", "path", "enum", "dict", "list"):
            raise ValueError(f"unknown variable type {self.type!r}")


@dataclass
class Flow:
    graph: FlowGraph
    schemas: dict[str, OutputSchema]
    variables: dict[str, VarSpec]
    definitions: dict[str, dict]  # name -> parsed JSON Schema dict
    flow_dir: Path
    dot_path: Path
    supervision_instructions: dict[str, str] = field(default_factory=dict)
    bypass_variables: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Per-variable merge strategy for all-mode joins. Maps variable name to one
    # of the strategy identifiers honoured by flowstate.merge._apply. Variables
    # not listed fall back to the default `append_in_completion_order`. See C1.
    merge_strategies: dict[str, str] = field(default_factory=dict)


KNOWN_MERGE_STRATEGIES = frozenset({
    "append_in_completion_order",
    "concat",
    "list_append",
    "last_wins",
    "error_on_conflict",
})


def _parse_merge_strategies(raw: Any) -> dict[str, str]:
    """Parse the top-level `merge_strategies:` block in .flow.yml.

    Returns an empty dict if the block is absent. Validates that each value is
    one of the known strategy identifiers handled by `flowstate.merge._apply`.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"merge_strategies must be a dict, got {type(raw).__name__}"
        )
    out: dict[str, str] = {}
    for var_name, strategy in raw.items():
        if strategy not in KNOWN_MERGE_STRATEGIES:
            raise ValueError(
                f"unknown merge strategy {strategy!r} for var {var_name!r}; "
                f"known: {sorted(KNOWN_MERGE_STRATEGIES)}"
            )
        out[str(var_name)] = str(strategy)
    return out


def _parse_schema_file(raw: dict[str, Any]) -> SchemaFile:
    for required in ("name", "path"):
        if required not in raw:
            raise ValueError(
                f"output file entry missing required field {required!r}: {raw!r}"
            )
    definition = raw.get("definition")
    return SchemaFile(
        name=str(raw["name"]),
        path=str(raw["path"]),
        definition=str(definition) if definition is not None else None,
    )


def _parse_schema(name: str, raw: dict[str, Any]) -> OutputSchema:
    files = [_parse_schema_file(f) for f in raw.get("files", [])]
    return OutputSchema(
        name=name,
        files=files,
        sets_variables=dict(raw.get("sets_variables", {})),
        required_variables=list(raw.get("required_variables", [])),
    )


def _parse_variables(raw: dict[str, Any]) -> dict[str, VarSpec]:
    out: dict[str, VarSpec] = {}
    for name, spec in (raw or {}).items():
        if name.startswith("_"):
            raise ValueError(
                f"variable name {name!r} is invalid: "
                f"underscore-prefixed names are reserved for system variables"
            )
        if not isinstance(spec, dict) or "type" not in spec:
            raise ValueError(
                f"variable {name!r} missing required 'type' field; "
                f"declare type: string | path | enum | dict | list"
            )
        vtype = spec["type"]
        # I-4: `dict` is the declared type for variables that hold structured
        # values (the reducer's JSON-encoded summary auto-decodes through
        # parse_reducer_stdout into a Python dict — see smoke-branch's
        # branch_summary). `list` is the declared type for iterable
        # collections (e.g. batch-factory-pipeline's `tickets`, with
        # `items: dict` as an authoring hint). Like the other types, both are
        # descriptive only; there is no runtime write-time enforcement.
        if vtype not in ("string", "path", "enum", "dict", "list"):
            raise ValueError(
                f"variable {name!r} has unsupported type {vtype!r}; "
                f"supported types: string, path, enum, dict, list"
            )
        values: list[str] | None = None
        if vtype == "enum":
            if "values" not in spec or not isinstance(spec["values"], list) or not spec["values"]:
                raise ValueError(
                    f"enum variable {name!r} requires non-empty 'values' list"
                )
            values = [str(v) for v in spec["values"]]
        items = spec.get("items")
        if items is not None and not isinstance(items, str):
            raise ValueError(
                f"variable {name!r}: 'items' must be a string, "
                f"got {type(items).__name__}"
            )
        out[name] = VarSpec(
            name=name,
            type=vtype,
            values=values,
            default=spec.get("default"),
            items=items,
        )
    return out


def _load_definition(flow_dir: Path, name: str) -> dict:
    path = flow_dir / "definitions" / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"definition {name!r} not found at {path!s}; "
            f"every output file's `definition` must correspond to a file in "
            f"{flow_dir / 'definitions'}"
        )
    import json
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"definition {name!r} at {path!s} is not valid JSON: {exc}"
        )


VALID_SUPERVISION_LEVELS = ("afk", "low", "medium", "high")


def _parse_supervision_instructions(raw: dict[str, Any]) -> dict[str, str]:
    """Parse the top-level `supervision_instructions` block.

    Validates that keys are exactly within the allowed level set.
    Returns an empty dict if the block is absent.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"supervision_instructions must be a mapping; got {type(raw).__name__}"
        )
    unknown = set(raw) - set(VALID_SUPERVISION_LEVELS)
    if unknown:
        raise ValueError(
            f"supervision_instructions has unknown level(s) {sorted(unknown)!r}; "
            f"valid levels are {list(VALID_SUPERVISION_LEVELS)!r}"
        )
    return {level: str(text) for level, text in raw.items()}


def _parse_nodes_block(
    raw: dict[str, Any],
    declared_node_names: set[str],
    declared_variable_names: set[str],
) -> dict[str, dict[str, Any]]:
    """Parse the top-level `nodes:` block in .flow.yml.

    Currently supports only the `bypass_variables` sub-key per node. Returns a
    mapping node_name -> bypass_variables_dict. Nodes whose entry has no
    bypass_variables (or has an empty one) are omitted from the return value.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"`nodes` must be a mapping; got {type(raw).__name__}"
        )
    bypass_variables: dict[str, dict[str, Any]] = {}
    for node_name, node_cfg in raw.items():
        if node_name not in declared_node_names:
            raise ValueError(
                f"`nodes.{node_name}` references unknown node; "
                f"declared nodes are {sorted(declared_node_names)!r}"
            )
        if not isinstance(node_cfg, dict):
            raise ValueError(
                f"`nodes.{node_name}` must be a mapping; got {type(node_cfg).__name__}"
            )
        bv_raw = node_cfg.get("bypass_variables")
        if bv_raw is None:
            bv_raw = {}
        if not isinstance(bv_raw, dict):
            raise ValueError(
                f"`nodes.{node_name}.bypass_variables` must be a mapping; "
                f"got {type(bv_raw).__name__}"
            )
        for var_name in bv_raw:
            if var_name not in declared_variable_names:
                raise ValueError(
                    f"`nodes.{node_name}.bypass_variables` references "
                    f"variable {var_name!r} which is not declared in `variables`"
                )
        if bv_raw:
            bypass_variables[node_name] = dict(bv_raw)
    return bypass_variables


def load_flow(dot_path: Path) -> Flow:
    dot_path = Path(dot_path)
    graph = parse_dot(dot_path.read_text())
    flow_dir = dot_path.parent
    flow_yml = dot_path.with_suffix(".flow.yml")
    schemas: dict[str, OutputSchema] = {}
    variables: dict[str, VarSpec] = {}
    supervision_instructions: dict[str, str] = {}
    bypass_variables: dict[str, dict[str, Any]] = {}
    merge_strategies: dict[str, str] = {}
    if flow_yml.exists():
        raw = yaml.safe_load(flow_yml.read_text()) or {}
        for name, sraw in (raw.get("output_schemas") or {}).items():
            schemas[name] = _parse_schema(name, sraw or {})
        variables = _parse_variables(raw.get("variables") or {})
        supervision_instructions = _parse_supervision_instructions(
            raw.get("supervision_instructions") or {}
        )
        bypass_variables = _parse_nodes_block(
            raw.get("nodes") or {},
            declared_node_names={n.name for n in graph.nodes},
            declared_variable_names=set(variables.keys()),
        )
        merge_strategies = _parse_merge_strategies(raw.get("merge_strategies"))

    # Cross-validation: every node references an existing output_schema; every
    # sets_variables entry references an existing file logical-id; every file
    # has a loadable definition.
    for node in graph.nodes:
        if node.shape != "box":
            continue
        if node.runner == "agent":
            for attr in ("prompt_template", "working_dir", "output_schema"):
                if not getattr(node, attr):
                    raise ValueError(
                        f"agent node {node.name!r} missing required attribute "
                        f"{attr!r}"
                    )
        elif node.runner == "script":
            for attr in ("script", "working_dir", "output_schema"):
                if not getattr(node, attr):
                    raise ValueError(
                        f"script node {node.name!r} missing required attribute "
                        f"{attr!r}"
                    )
        elif node.runner == "orchestrator":
            # Orchestrator-run nodes are handled inline by the orchestrator
            # session — no worker spawn, no output files. They still need a
            # prompt_template (the orchestrator reads it to know what to do)
            # and output_schema (which declares the variables the orchestrator
            # must set before calling `flowstate complete`). See Issue #35.
            for attr in ("prompt_template", "output_schema"):
                if not getattr(node, attr):
                    raise ValueError(
                        f"orchestrator node {node.name!r} missing required attribute "
                        f"{attr!r}"
                    )
        elif node.runner in ("fork", "join", "dynamic_fanout", "subflow"):
            # Multi-branch control-flow nodes — attribute requirements are
            # validated at graph execution time, not parse time. No
            # prompt_template / output_schema required.
            pass
        else:
            raise ValueError(
                f"node {node.name!r} has unsupported runner {node.runner!r}"
            )
        if node.worktree and node.working_dir:
            raise ValueError(
                f"node {node.name!r} declares both worktree= and working_dir=; "
                f"these are mutually exclusive"
            )
        # Structural control-flow nodes produce no outputs and legitimately
        # have output_schema=None — they are routing nodes, not work nodes.
        if node.runner in ("fork", "join", "dynamic_fanout", "subflow"):
            continue
        if node.output_schema not in schemas:
            raise ValueError(
                f"node {node.name!r} references unknown output_schema "
                f"{node.output_schema!r}"
            )

    definitions: dict[str, dict] = {}
    for schema in schemas.values():
        for sf in schema.files:
            # definition=None => existence-only output; nothing to load.
            if sf.definition is None:
                continue
            if sf.definition not in definitions:
                definitions[sf.definition] = _load_definition(flow_dir, sf.definition)
        for var_name, file_id in schema.sets_variables.items():
            if not any(f.name == file_id for f in schema.files):
                raise ValueError(
                    f"output_schema {schema.name!r}: sets_variables maps "
                    f"{var_name!r} to file logical-id {file_id!r}, which is not "
                    f"present in this schema's files: "
                    f"{[f.name for f in schema.files]}"
                )
            if var_name not in variables:
                raise ValueError(
                    f"output_schema {schema.name!r}: sets_variables references "
                    f"variable {var_name!r} which is not declared in variables"
                )
        for var_name in schema.required_variables:
            if var_name not in variables:
                raise ValueError(
                    f"output_schema {schema.name!r}: required_variables references "
                    f"variable {var_name!r} which is not declared in variables"
                )

    return Flow(
        graph=graph,
        schemas=schemas,
        variables=variables,
        definitions=definitions,
        flow_dir=flow_dir,
        dot_path=dot_path,
        supervision_instructions=supervision_instructions,
        bypass_variables=bypass_variables,
        merge_strategies=merge_strategies,
    )
