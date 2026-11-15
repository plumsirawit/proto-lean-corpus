import csv
import json
import re
from pathlib import Path
import pyarrow.parquet as pq
from ..common import text
from ..lean import LeanFailure, inspect, replace_span, slice_span, target_info


class Unsupported(ValueError):
    pass


def doc_metadata(shape):
    return (
        isinstance(shape, list)
        and len(shape) == 3
        and shape[0] == "node"
        and shape[1]
        in {"Lean.Parser.Command.docComment", "Lean.Parser.Command.moduleDoc"}
    )


def comparable_shape(shape):
    """Ignore only parsed documentation metadata; source and task IDs are unchanged."""
    if not isinstance(shape, list):
        return shape
    if len(shape) == 3 and shape[0] == "node":
        return [
            shape[0],
            shape[1],
            [comparable_shape(child) for child in shape[2] if not doc_metadata(child)],
        ]
    return [comparable_shape(child) for child in shape if not doc_metadata(child)]


def rows(path):
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        index = 0
        for batch in pq.ParquetFile(path).iter_batches(batch_size=1024):
            for row in batch.to_pylist():
                yield index, row
                index += 1
    elif suffix == ".jsonl":
        with path.open() as stream:
            for i, line in enumerate(stream):
                if line.strip():
                    try:
                        yield i, json.loads(line)
                    except json.JSONDecodeError as e:
                        yield i, {"_malformed": str(e)}
    elif suffix == ".json":
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            # Workbook JSON is also distributed as keyed records / split arrays.
            if all(isinstance(v, list) for v in data.values()):
                for split, values in sorted(data.items()):
                    for i, value in enumerate(values):
                        yield f"{split}/{i}", value
            elif all(isinstance(v, dict) for v in data.values()):
                yield from sorted(data.items())
            else:
                raise Unsupported("unsupported JSON container")
        elif isinstance(data, list):
            yield from enumerate(data)
        else:
            raise Unsupported("unsupported JSON container")
    elif suffix == ".csv":
        with path.open(newline="") as stream:
            yield from enumerate(csv.DictReader(stream))


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def placeholder_targets(source, parsed):
    return [
        target
        for target in parsed["targets"]
        if "body" in target and slice_span(source, target["body"]) == "by sorry"
    ]


def statement(value, env, runtime):
    if not nonempty(value):
        raise Unsupported("missing formal_statement")
    source = text(value)
    transformations = []
    supplied_source = source
    slot = re.search(r"(?P<slot>:=\s*by|:=)(?P<trivia>\s*)$", source)
    inserted_end = None
    if slot:
        addition = " sorry" if slot["slot"].endswith("by") else " by sorry"
        position = slot.end("slot")
        source = source[:position] + addition + source[position:]
        inserted_end = len(source[: position + len(addition)].encode())
    parsed = inspect(source, env, runtime)
    selected_target = None
    if inserted_end is not None:
        # Only complete the target's entire empty proof slot. A trailing comment,
        # binder initializer, or nested let must not become a fabricated proof.
        completed_targets = [
            target
            for target in placeholder_targets(source, parsed)
            if target["body"][0] < inserted_end <= target["body"][1]
        ]
        if len(completed_targets) == 1:
            selected_target = completed_targets[0]
            transformations.append(
                "Completed supplied empty target proof with canonical placeholder"
            )
        else:
            source = supplied_source
            parsed = inspect(source, env, runtime)
    if env.get("resolved") and not parsed["imports_resolved"]:
        raise LeanFailure(
            "imports_unavailable",
            "\n".join(parsed.get("import_errors") or parsed["errors"])
            or "task imports unavailable in the recorded resolved environment",
        )
    if selected_target is None:
        placeholders = placeholder_targets(source, parsed)
        if len(placeholders) == 1 and parsed["admissions"] == 1:
            selected_target = placeholders[0]
        elif len(parsed["targets"]) == 1:
            selected_target = parsed["targets"][0]
        elif parsed["admissions"] > 1:
            raise Unsupported(
                "multiple target placeholders / admitted supporting lemmas; task requires complete supporting context"
            )
        else:
            raise Unsupported("expected exactly one designated theorem target")
    target = selected_target
    if "unsupported" in target:
        raise Unsupported(target["unsupported"])
    original = slice_span(source, target["body"])
    if original == "by sorry" and parsed["admissions"] != 1:
        raise Unsupported(
            "multiple target placeholders / admitted supporting lemmas; task requires complete supporting context"
        )
    if original != "by sorry":
        transformations.append(
            {
                "action": "Separated supplied target proof from fixed task",
                "proof_span_bytes": target["body"],
            }
        )
    task = replace_span(source, target["body"], "by sorry")
    skeleton = None
    if original == "by sorry" and parsed["admissions"] == 1:
        skeleton = parsed.get("task_skeleton")
    else:
        target_info(task, env, runtime, target["name"])
    return (
        task,
        target["name"],
        original,
        transformations,
        skeleton,
        parsed["imports_resolved"],
    )


def code_block(response):
    blocks = re.findall(r"```(?:lean4?|Lean4?)\s*\n?([\s\S]*?)```", response)
    if len(blocks) != 1:
        raise Unsupported(
            f"ambiguous response: expected one Lean block, found {len(blocks)}"
        )
    return text(blocks[0]).strip()


def proof_term(value, task, env, runtime, response=False, task_skeleton=None):
    value = text(value).strip()
    if response:
        value = code_block(value)
    if value.startswith("by") or value.startswith("fun ") or value.startswith("("):
        # Entire replacement term retained. Verification catches malformed terms and
        # command injection; it is not assumed successful here.
        return value
    parsed = inspect(value, env, runtime)
    if not parsed["targets"]:
        raise Unsupported(
            "response is neither replacement term nor single full theorem"
        )
    fixed = None
    if len(parsed["targets"]) == 1:
        target = parsed["targets"][0]
    else:
        fixed = inspect(task, env, runtime)
        placeholders = placeholder_targets(task, fixed)
        if len(placeholders) != 1 or fixed["admissions"] != 1:
            raise Unsupported("task has no unique designated target")
        name = placeholders[0]["name"]
        targets = [t for t in parsed["targets"] if t["name"] == name]
        if len(targets) != 1:
            raise Unsupported(
                "full-theorem response changed target statement or context"
            )
        target = targets[0]

        # The helper's skeleton masks every theorem body. Preserve supporting
        # proofs explicitly so that comparison may mask only the designated target.
        def supporting(source, targets):
            return [
                (t["name"], slice_span(source, t["body"]))
                for t in targets
                if t["name"] != name and "body" in t
            ]

        if supporting(value, parsed["targets"]) != supporting(task, fixed["targets"]):
            raise Unsupported("full-theorem response changed supporting proofs/context")
    if "unsupported" in target:
        raise Unsupported(target["unsupported"])
    body = slice_span(value, target["body"])
    candidate = replace_span(value, target["body"], "by sorry")
    if candidate.strip() != task.strip():
        fixed_skeleton = task_skeleton
        if fixed_skeleton is None:
            fixed_skeleton = (fixed or inspect(task, env, runtime)).get("task_skeleton")
        if parsed.get("task_skeleton") is None or comparable_shape(
            parsed["task_skeleton"]
        ) != comparable_shape(fixed_skeleton):
            raise Unsupported(
                "full-theorem response changed target statement or context"
            )
    return body


def base(row, env, runtime, formal=None, informal=None):
    task, name, original, transforms, skeleton, imports_resolved = statement(
        formal or row.get("formal_statement"), env, runtime
    )
    return {
        "task": task,
        "target": name,
        "informal": informal or row.get("informal_statement") or row.get("problem"),
        "upstream_id": str(row.get("uuid", row.get("problem_id", row.get("id", ""))))
        or None,
        "attempts": [],
        "reports": [],
        "transformations": transforms,
        "_task_skeleton": skeleton,
        "_parser_imports_resolved": imports_resolved,
    }, original


def add(result, value, field, env, runtime, response=False, explanation=None):
    if value is not None and not isinstance(value, str):
        result["reports"].append(
            {
                "field": field,
                "reason": "unsupported proof field shape",
                "kind": "unsupported",
            }
        )
        return
    if not nonempty(value):
        return
    try:
        term = proof_term(
            value, result["task"], env, runtime, response, result.get("_task_skeleton")
        )
        result["attempts"].append(
            {"proof": term, "field": field, "informal": explanation}
        )
        result["transformations"].append(
            {
                "action": "Extracted replacement term from supplied field/response",
                "field": field,
            }
        )
    except LeanFailure as e:
        if e.reason != "unsupported_syntax":
            raise
        result["reports"].append(
            {"field": field, "reason": str(e), "kind": "unsupported"}
        )
    except (Unsupported, ValueError) as e:
        result["reports"].append(
            {"field": field, "reason": str(e), "kind": "unsupported"}
        )


def goedel_pset_v1(row, env, runtime):
    try:
        result, original = base(row, env, runtime)
    except LeanFailure as error:
        if error.reason != "unsupported_syntax":
            raise
        source = text(row["formal_statement"])
        # A malformed generated tactic body can prevent the command parser from
        # returning a proof span. Limit recovery to one top-level theorem with
        # an indented suffix, and have Lean check the isolated declaration.
        declarations = list(re.finditer(r"(?m)^theorem\s+", source))
        if len(declarations) != 1:
            raise
        declaration = declarations[0]
        if re.search(r"(?m)^(?:namespace|section)\b", source[: declaration.start()]):
            raise
        slot = re.search(r":=\s*(?P<proof>by)\b", source[declaration.end() :])
        if slot is None:
            raise
        start = declaration.end() + slot.start("proof")
        original = source[start:]
        if re.search(r"(?m)\n(?!--)\S", original):
            raise
        try:
            result, placeholder = base(
                row, env, runtime, formal=source[:start] + "by sorry"
            )
        except (Unsupported, LeanFailure) as repair_error:
            if isinstance(repair_error, LeanFailure) and (
                repair_error.reason != "unsupported_syntax"
            ):
                raise
            raise error from repair_error
        if placeholder != "by sorry":
            raise
        result["transformations"].append(
            {
                "action": "Recovered the theorem before a malformed tactic body",
                "proof_span_bytes": [
                    len(source[:start].encode()),
                    len(source.encode()),
                ],
                "original_parse_error": error.detail,
            }
        )
    # Pset is a statement source. If the supplied field contains a proof, preserve it.
    if original != "by sorry":
        result["attempts"].append(
            {"proof": original, "field": "formal_statement", "informal": None}
        )
    return result


WORKBOOK_HEADER_SOURCE = (
    "https://huggingface.co/datasets/internlm/Lean-Workbook/discussions/1"
)
WORKBOOK_UPSTREAM_REVISION = "2e066e310b2c6d2c27616927ae131f82901c8f1c"
WORKBOOK_TOOLCHAIN = "leanprover/lean4:v4.8.0-rc1"
# The root also exposes Nat's prime-counting notation. The posted Workbook
# header opens Real without importing PrimeCounting, where π denotes Real.pi.
WORKBOOK_HEADER = (
    "import Mathlib\n"
    "open BigOperators Nat Real Rat\n"
    'local notation (priority := high) "π" => Real.pi\n'
)


def lean_workbook(row, env, runtime, *, upstream_revision=None):
    formal = row.get("formal_statement")
    reconstructed = (
        upstream_revision == WORKBOOK_UPSTREAM_REVISION
        and env.get("resolved")
        and env.get("toolchain") == WORKBOOK_TOOLCHAIN
        and nonempty(formal)
        and not re.search(r"(?m)^\s*import\s", formal)
    )
    if reconstructed:
        formal = WORKBOOK_HEADER + formal
    result, original = base(
        row, env, runtime, formal=formal, informal=row.get("natural_language_statement")
    )
    if reconstructed:
        result["transformations"].insert(
            0,
            {
                "action": "Reconstructed the Lean Workbook header for Lean 4.8.0-rc1",
                "reference": WORKBOOK_HEADER_SOURCE,
                "header": WORKBOOK_HEADER,
            },
        )
    if original != "by sorry":
        result["attempts"].append(
            {"proof": original, "field": "formal_statement", "informal": None}
        )
    add(result, row.get("formal_proof"), "formal_proof", env, runtime)
    if nonempty(row.get("tactic")):
        add(
            result,
            "by\n  " + row["tactic"].replace("\n", "\n  "),
            "tactic",
            env,
            runtime,
        )
    # The JSON representation supplies a list of tactic scripts in `proof`.
    # Every entry is an independent attempt, regardless of upstream success hints.
    supplied = row.get("proof")
    if supplied is not None:
        if not isinstance(supplied, list):
            result["reports"].append(
                {
                    "field": "proof",
                    "reason": "unsupported proof list shape",
                    "kind": "unsupported",
                }
            )
        else:
            for i, value in enumerate(supplied):
                if nonempty(value) and not re.match(r"by(?:\s|$)", value.lstrip()):
                    value = "by\n  " + value.replace("\n", "\n  ")
                add(result, value, f"proof/{i}", env, runtime)
    return result


def numina_math_lean(row, env, runtime):
    result, original = base(row, env, runtime)
    if original != "by sorry":
        result["attempts"].append(
            {"proof": original, "field": "formal_statement", "informal": None}
        )
    for field in ("formal_ground_truth", "formal_proof"):
        add(result, row.get(field), field, env, runtime)
    result["transformations"].append(
        {
            "ground_truth_type_hint": row.get("ground_truth_type"),
            "locally_verified": False,
        }
    )
    return result


def putnam_bench(row, env, runtime):
    # No answer synthesis. Incomplete supporting definitions fail task validation.
    formal = (
        row.get("lean4")
        or row.get("lean")
        or row.get("Lean")
        or row.get("lean_statement")
        or row.get("formal_statement")
    )
    result, original = base(row, env, runtime, formal=formal)
    if original != "by sorry":
        result["attempts"].append(
            {"proof": original, "field": "lean", "informal": None}
        )
    return result


def conversation(result, messages, env, runtime, prefix="messages"):
    if not isinstance(messages, list):
        result["reports"].append(
            {
                "field": prefix,
                "reason": "messages must be a list",
                "kind": "unsupported",
            }
        )
        return
    for i, message in enumerate(messages):
        if isinstance(message, list):
            conversation(result, message, env, runtime, f"{prefix}/{i}")
        elif isinstance(message, dict) and message.get("role") == "assistant":
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                result["reports"].append(
                    {
                        "field": f"{prefix}/{i}/content",
                        "reason": "unsupported assistant content shape",
                        "kind": "unsupported",
                    }
                )
                continue
            add(
                result,
                content,
                f"{prefix}/{i}/content",
                env,
                runtime,
                response=True,
                explanation=content,
            )
        elif not isinstance(message, dict):
            result["reports"].append(
                {
                    "field": f"{prefix}/{i}",
                    "reason": "unsupported message shape",
                    "kind": "unsupported",
                }
            )


def goedel_prover_v2_sft(row, env, runtime):
    messages = row.get("messages")
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        raise Unsupported("missing or malformed messages")
    prompts = [
        (i, m)
        for i, m in enumerate(messages)
        if m.get("role") == "user" and "```" in m.get("content", "")
    ]
    if len(prompts) != 1:
        raise Unsupported("ambiguous conversation: expected one formal task prompt")
    i, prompt = prompts[0]
    result, _ = base(row, env, runtime, formal=code_block(prompt["content"]))
    result["statement_field"] = f"messages/{i}/content"
    conversation(result, messages, env, runtime)
    return result


def nemotron_math_proofs_v1(row, env, runtime):
    formal = row.get("formal_statement")
    if not nonempty(formal):
        raise Unsupported("missing formalization; informal-only record")
    header = row.get("lean_header") or ""
    result, original = base(
        row, env, runtime, formal=header + ("\n" if header else "") + formal
    )
    if original != "by sorry":
        result["attempts"].append(
            {"proof": original, "field": "formal_statement", "informal": None}
        )
    conversation(result, row.get("messages") or [], env, runtime)
    return result


PUTNAM_UPSTREAM_REVISION = "a635813f5edf297d928f9bd5ef8324a26bdde09c"
PUTNAM_ANSWER_CONVENTION = (
    "https://github.com/trishullab/PutnamBench/blob/"
    + PUTNAM_UPSTREAM_REVISION
    + "/lean4/README.md"
)


def putnam_answer(source, problem_name):
    # This pinned benchmark places the supplied answer on the line immediately
    # after a named, admitted answer abbreviation. Leave other admissions alone.
    name = f"{problem_name}_solution"
    pattern = (
        rf"^(?:noncomputable[ \t]+)?abbrev[ \t]+{re.escape(name)}"
        r"[ \t]*:[^\n]+:=[ \t]*(sorry)[ \t]*\n--[ \t]*([^\n]+)"
    )
    answer = re.search(pattern, source, flags=re.MULTILINE)
    if answer is None:
        return source, []
    start, end = answer.span(1)
    value = answer[2].strip()
    if not value:
        return source, []
    transformed = source[:start] + f"({value})" + source[end:]
    return transformed, [
        {
            "action": "Filled the answer abbreviation with its upstream comment",
            "declaration": name,
            "reference": PUTNAM_ANSWER_CONVENTION,
            "answer_span_bytes": [
                len(source[:offset].encode()) for offset in answer.span(2)
            ],
        }
    ]


def library(path, env, runtime, *, putnam=False):
    source = text(Path(path).read_text())
    answer_transforms = []
    if putnam:
        source, answer_transforms = putnam_answer(source, path.stem)
    parsed = inspect(source, env, runtime)
    for target in parsed["targets"]:
        try:
            if "unsupported" in target:
                raise Unsupported(target["unsupported"])
            # All preceding commands remain verbatim, including earlier proofs,
            # definitions, variables, attributes and options. Close open scopes.
            prefix = source.encode()[: target["command"][1]].decode()
            body = slice_span(source, target["body"])
            task = (
                replace_span(prefix, target["body"], "by sorry")
                + "\n"
                + "".join(command + "\n" for command in target["closing_commands"])
            )
            target_info(task, env, runtime, target["name"])
            result = {
                "task": task,
                "target": target["name"],
                "informal": None,
                "upstream_id": target["name"],
                "attempts": []
                if putnam and re.fullmatch(r"(?:by\s+)?sorry", body.strip())
                else [
                    {
                        "proof": body,
                        "field": f"declaration={target['name']}&proof",
                        "informal": None,
                    }
                ],
                "reports": [],
                "transformations": [
                    *answer_transforms,
                    "Retained source prefix; replaced parsed target term; closed open scopes",
                ],
            }
            yield result
        except ValueError as e:
            yield {"error": str(e), "target": target["name"]}
    if not parsed["targets"]:
        yield {
            "error": "no supported theorem/lemma targets (definitions retained as context only)",
            "target": None,
        }


# Library adapters intentionally share Lean's parser, not text declaration splitting.
def mathlib4(path, env, runtime):
    return library(path, env, runtime)


def tau_ceti(path, env, runtime):
    return library(path, env, runtime)


ADAPTERS = {
    f.__name__: f
    for f in (
        goedel_pset_v1,
        lean_workbook,
        numina_math_lean,
        putnam_bench,
        mathlib4,
        tau_ceti,
        goedel_prover_v2_sft,
        nemotron_math_proofs_v1,
    )
}
