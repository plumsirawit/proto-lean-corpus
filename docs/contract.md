# Data contract and implementation decisions

Schema version: `1`, embedded in Parquet schema metadata. `schema.py` is the authoritative Arrow declaration. Required strings are nonempty; IDs are unique after deduplication. Foreign keys and source coverage are checked at writes. Arrow types are explicit; nested references are actual lists of structs.

| Table | Columns |
| --- | --- |
| problems | id: string; formal_statement: string; informal_statement: nullable string; target_name: string; environment_id: string; source_refs: list&lt;struct&gt; |
| proofs | id: string; problem_id: string; formal_proof: string; informal_proof: nullable string; verification_status: string; source_refs: list&lt;struct&gt; |
| provenance | id: string; dataset_name: string; revision: string; acquired_at: timestamp[us, UTC]; manifest_hash: string; license_refs: list&lt;string&gt; |

A reference has required `source_id: string`, nullable `upstream_id: string`, and required `location: string`. Locations retain original paths plus row/declaration and field/message selectors. Two attempts from separate message turns have separate locations. There is no `proof_ids` column; join `proofs.problem_id = problems.id`.

## Canonical serialization and identities

JSON canonical serialization uses sorted keys, UTF-8 with unescaped Unicode, no insignificant separators, and no NaN/Infinity. Hashes use SHA-256. Source text normalizes CRLF and CR to LF. No layout whitespace, arbitrary comments, binders, context order, names, or imports are heuristically rewritten. Adapters trim surrounding transport whitespace from response blocks/terms; target/context comparisons otherwise stay conservative.

* Problem ID: `problem-v1-<hash>` of `{"source": complete_normalized_source, "target": target_name, "environment": environment_id}`.
* Proof ID: `proof-v1-<hash>` of `{"problem": internal_problem_id, "term": normalized_replacement_term}`.
* Source ID: `source-v1-<hash>` of the complete acquisition manifest, excluding the separately stored acquisition receipt timestamp.
* Environment ID: `environment-v1-<hash>` of the complete environment manifest. Fully resolved manifests record exact toolchain, build configuration, dependency commits and project source hashes. Unresolved manifests include source/input scope so unknown environments do not accidentally merge.

Source and proof locations never determine mathematical identity. Same upstream IDs across sources cannot merge different source text. Changed assumptions, context, target identity or environment remain distinct. Acquisition order and timestamps are absent from problem/proof fingerprints.

Normalization stores occurrence rows. Deduplication groups by exact content IDs; the identity already redirects all duplicate problem occurrences to the same internal foreign key. Proofs group only within the same retained problem. Reference unions deduplicate exact structs and order by canonical JSON. Nullable informal metadata takes the first present value ordered by canonical JSON serialization of each occurrence’s source-reference list. Object keys in that serialization are lexicographic, so location sorts before source ID and upstream ID. This deterministic occurrence precedence does not imply a preference for any source’s proof quality; conflicts are reported with all distinct texts. Acquired originals retain the full unselected metadata. No approximate matching or fuzzy merging is enabled.

## Artifact layout and resume

`work/snapshots/<source-id>/manifest.json` inventories `original/` with file sizes and SHA-256. `receipt.json` stores UTC acquisition time. `locks/` identifies resolved source revisions. `environments/` contains checking manifests. `runtimes/` maps IDs to prepared local projects and compiled search paths; these machine-specific paths never enter releases. `resolutions/` records explicit environment changes. `environment-projects/` preserves supplied resolved project inputs while omitting generated dependency/build folders.

`normalized/` contains occurrence Parquet tables; `canonical/` contains unique tables. `reports/extraction.json` distinguishes unsupported representations and malformed records and counts input/problem/proof occurrences. `occurrences.json` maps source locations to retained content IDs; `transformations.json` records extraction changes. `deduplication.json` records merges/conflicts and before/after counts. `verification.json` and `verification-cache/` contain checks, reasons, logs and policy. `quality.json` in an export counts unique rows and statuses, and source membership counts that can overlap after merging. Rejection reports identify every skipped data record or unsupported file; documentation/config files are not data records. The central tables and proof-discovery view contain extracted candidates, including contexts that later fail elaboration. Use the per-problem results in `reports/verification.json` to select elaborated tasks; the verified-proof view contains only locally valid proofs.

Parquet writes batch rows with Zstandard compression. Stage memory currently scales with the selected corpus. Run bounded inputs before attempting large sources. No per-row database transactions or extra central tables are used. Logical rows sort by content ID. Acquisition receipts are stable per local snapshot; no claim of identical bytes across independently timestamped acquisitions is made.

## Verification policy v1

Extraction parses supplied proof bodies but does not execute their tactics; context elaboration uses temporary theorem placeholders. Original terms and admission counts remain intact. Verification executes the supplied terms with normal imported-package initialization enabled.

A usable task has exactly one designated proof term represented by the parser span of `by sorry`. Supporting declarations can include complete earlier theorems; supporting admissions are rejected. Comments and strings do not count as admissions. The entire proof term replaces the target span, preserving suffixes such as namespace closures. Full-theorem responses must reproduce the parsed task skeleton (including imports and all commands), ignoring parser trivia and masking only the target proof body; changed statements/context and ambiguous blocks are unsupported.

The task is elaborated first, with its placeholder. This establishes neither truth nor provability. A proof is `valid` only when Lean elaborates the replacement, the same theorem and elaborated target type remain, the replacement occupies exactly the target term span, no admissions remain, and the target's **transitive** axiom dependencies are inspected. Allowed foundations are `propext`, `Classical.choice`, and `Quot.sound`; `sorryAx` and all other axioms are rejected. This policy can conservatively reject legitimate projects using additional declared axioms; such policy changes must be explicit and invalidate the cache. `unverified` means untouched; `invalid` means a demonstrated structural/elaboration/target/admission failure; `inconclusive` means environment/tooling/import/sandbox/timeout/resource/protocol failure.

Linux execution uses fresh user, mount, PID and network namespaces, a temporary root, read-only binds of installed tools and the supplied project, and a writable temporary directory. No credentials or arbitrary home directories are mounted. Lean sees a clean environment, no network interfaces, finite CPU/wall time, 4 GiB address-space default, 16 MiB output-file limits and no core dumps. Unsupported isolation fails closed. Helper compilation is part of each isolated call and currently adds overhead. Build dependencies separately; the runner never mutates snapshots or fetches packages.

The runner assumes the pinned Lean toolchain and its imported metaprogramming extensions behave according to Lean's normal trusted implementation. It is not an independent kernel implementation or an audit of deliberately malicious toolchains/plugins. Formal consistency checks do not audit informal faithfulness, satisfiability, benchmark exposure or legal permissions.

Cache identities include task/proof source and IDs, target, complete environment, policy, package implementation version, helper and Python runner/interface/sandbox hashes. Infrastructure failures are not cached as terminal results. Runtime configuration/source/dependency checks precede cache reuse. Environment resolution requires renormalization and changes all affected task/proof IDs.

`configs/pipeline.yaml` records the shipped policy/defaults for release documentation; the current CLI exposes `--timeout`, while foundational policy changes require editing/versioning the implementation. It is not a general workflow configuration language.
