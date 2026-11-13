# Source adapters

All adapters retain supported attempts as `unverified`, including `sorry` attempts and upstream claimed successes. Empty fields do not create proofs. Unknown schema/Lean forms are reported with source locations. Fixtures are synthetic schema examples, not copied corpus rows; online inspection used only small previews, cards and parser definitions.

| Source | Supported forms and boundaries |
| --- | --- |
| Goedel Pset v1 | `problem_id`, `formal_statement`, `informal_statement`; statement-only records remain problems. Any actual supplied target proof is retained. |
| Lean Workbook | Both JSON containers and Parquet are inspected, including keyed records and split arrays. `formal_statement`, `natural_language_statement`, optional `formal_proof`, and supplied `tactic` are supported. A tactic is wrapped in `by` and checked later; status/state-after strings never establish validity. Overlapping representations merge only through exact fingerprints. No Mathlib import is invented for fragments lacking context. |
| NuminaMath LEAN | `uuid`, `problem`, `formal_statement`, human `formal_ground_truth`, and generated `formal_proof` remain distinct. A supplied empty terminal `:= by` is completed with a placeholder and the change is recorded. `ground_truth_type` is a hint, never verification. Empty fields are skipped, admitted attempts retained, and statement-only rows remain usable tasks. |
| PutnamBench | Prefer a pinned official `lean4/` project. CSV imports accept the Lean-specific fields `lean4`, `lean`, `Lean`, `lean_statement`, or `formal_statement`; Coq/Isabelle fields are never extracted. Answer definitions containing admissions are unsupported. No answer-placeholder resolution is attempted without a separate explicit transformation/reference implementation; there is no answer guessing. The selected representation is recorded by original file path and acquisition route. |
| Mathlib4 | Lean command parsing extracts theorem targets and the standard Mathlib/Batteries lemma group form, including multiple declarations per file. Each task retains its entire source prefix and closes the recorded open scopes. Definitions stay context. `.lake/packages` is never a source. Non-simple/equational theorem bodies and unsupported parser extensions are reported. Private/mangled target names or unusual scope behavior can fail target inspection rather than yield a valid result. |
| Tau Ceti | Same library extractor, with its own toolchain and dependency lock, preserved file notices and explicit AI-authorship acquisition metadata. No independently selected latest Mathlib. |
| Goedel Prover v2 SFT | Exactly one user task block; each assistant turn supplies a separate possible attempt. Assistant responses require exactly one Lean/Lean4 fence. Multiple blocks (including theorem restatements in reasoning), changed full theorems and ambiguous multiple task prompts are reported. Tool/user messages never become proofs. Explanations are retained without NLP. |
| Nemotron Math Proofs v1 | `formal_statement` plus `lean_header`, `uuid`, `problem`, and assistant messages, including nested conversation lists. Empty/missing formalizations are reported; empty messages produce problem-only records. Same conservative response extraction as SFT. Upstream verification claims remain hints. |
| AceReason Math | Explicitly deferred in the registry; acquisition refuses it. No fabricated Lean statements. |

Library context reconstruction uses preceding source, not imports of the target's own module. This deliberately retains earlier proofs and may be larger than a minimal context. A preceding incomplete theorem makes later tasks unsupported rather than introducing hidden admissions. Syntax beyond the imported parser's capabilities, ambiguous mutual/anonymous declarations, nonstandard generated theorem commands and unsupported versioned helper APIs require a later adapter extension. Processing never silently rewrites a project to a new Lean version.

A full-theorem response must currently match the original task's surrounding source exactly (apart from outer transport whitespace). This rejects some otherwise equivalent source renderings, extra comments and reordered imports. Such rejections are visible, and the originals remain acquired; no relaxed semantic-equivalence procedure is claimed.

Official references inspected during implementation:

* [Goedel Pset](https://huggingface.co/datasets/Goedel-LM/Goedel-Pset-v1)
* [Workbook card](https://huggingface.co/datasets/internlm/Lean-Workbook/blob/main/README.md) and JSON/Parquet inventory
* [Numina card/schema](https://huggingface.co/datasets/AI-MO/NuminaMath-LEAN)
* [Putnam project](https://github.com/trishullab/PutnamBench)
* [Mathlib lemma parser v4.19](https://github.com/leanprover-community/mathlib4/blob/v4.19.0/Mathlib/Tactic/Lemma.lean)
* [Tau Ceti toolchain](https://github.com/TauCetiProject/TauCeti/blob/main/lean-toolchain)
* [Goedel SFT preview](https://huggingface.co/datasets/Goedel-LM/SFT_dataset_v2)
* [Nemotron field documentation](https://huggingface.co/datasets/nvidia/Nemotron-Math-Proofs-v1/blob/main/README.md)

These observations are not locks. Exact later acquisitions are hashed and recorded independently.
