import csv
import json
import re
from pathlib import Path
import pyarrow.parquet as pq
from ..common import text
from ..lean import LeanFailure, inspect, replace_span, slice_span, target_info


class Unsupported(ValueError):
    pass


def comparable_shape(shape):
    """Ignore only parsed declaration doc metadata, never source text or code."""
    if not isinstance(shape, list):
        return shape
    if len(shape) == 3 and shape[0] == "node":
        return [
            shape[0],
            shape[1],
            [
                comparable_shape(child)
                for child in shape[2]
                if not (
                    isinstance(child, list)
                    and len(child) == 3
                    and child[:2] == ["node", "Lean.Parser.Command.docComment"]
                )
            ],
        ]
    return [comparable_shape(child) for child in shape]


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


def statement(value, env, runtime):
    if not nonempty(value):
        raise Unsupported("missing formal_statement")
    source = text(value)
    transformations = []
    if re.search(r":=\s*by\s*$", source):
        source += " sorry"
        transformations.append(
            "Completed supplied empty target proof with canonical placeholder"
        )
    parsed = inspect(source, env, runtime)
    if env.get("resolved") and not parsed["imports_resolved"]:
        raise LeanFailure(
            "imports_unavailable",
            "task imports unavailable in the recorded resolved environment",
        )
    if len(parsed["targets"]) != 1:
        if len(parsed["targets"]) > 1 and parsed["admissions"] > 1:
            raise Unsupported(
                "multiple target placeholders / admitted supporting lemmas; supporting context must be completed explicitly"
            )
        raise Unsupported("expected exactly one theorem target")
    target = parsed["targets"][0]
    if "unsupported" in target:
        raise Unsupported(target["unsupported"])
    original = slice_span(source, target["body"])
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
        target_info(task, env, runtime)
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
    if len(parsed["targets"]) != 1:
        raise Unsupported(
            "response is neither replacement term nor single full theorem"
        )
    target = parsed["targets"][0]
    if "unsupported" in target:
        raise Unsupported(target["unsupported"])
    body = slice_span(value, target["body"])
    candidate = replace_span(value, target["body"], "by sorry")
    if candidate.strip() != task.strip():
        fixed_skeleton = task_skeleton
        if fixed_skeleton is None:
            fixed_skeleton = inspect(task, env, runtime).get("task_skeleton")
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
    result, original = base(row, env, runtime)
    # Pset is a statement source. If the supplied field contains a proof, preserve it.
    if original != "by sorry":
        result["attempts"].append(
            {"proof": original, "field": "formal_statement", "informal": None}
        )
    return result


def lean_workbook(row, env, runtime):
    result, original = base(
        row, env, runtime, informal=row.get("natural_language_statement")
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


def library(path, env, runtime):
    source = text(Path(path).read_text())
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
                "attempts": [
                    {
                        "proof": body,
                        "field": f"declaration={target['name']}&proof",
                        "informal": None,
                    }
                ],
                "reports": [],
                "transformations": [
                    "Retained source prefix; replaced parsed target term; closed open scopes"
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
