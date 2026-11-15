import Lean
open Lean Lean.Elab

-- Lean 4.8 predates Lean.collectAxioms. This compatibility traversal follows
-- Lean's Apache-2.0 CollectAxioms implementation (Microsoft, Leonardo de Moura).
-- Copyright (c) 2020 Microsoft Corporation. All rights reserved.
-- Released under Apache 2.0, as described in LICENSE. Author: Leonardo de Moura.
-- Later versions use the standard collector, including its checked kernel env.
structure CorpusAxiomState where
  visited : NameSet := {}
  axioms : Array Name := #[]

partial def corpusCollectLegacy (name : Name) : ReaderT Environment (StateM CorpusAxiomState) Unit := do
  unless (← get).visited.contains name do
    modify fun s => {s with visited := s.visited.insert name}
    let collectExpr (e : Expr) := e.getUsedConstants.forM corpusCollectLegacy
    match (← read).find? name with
    | some (.axiomInfo _) => modify fun s => {s with axioms := s.axioms.push name}
    | some (.defnInfo v) => collectExpr v.type *> collectExpr v.value
    | some (.thmInfo v) => collectExpr v.type *> collectExpr v.value
    | some (.opaqueInfo v) => collectExpr v.type *> collectExpr v.value
    | some (.ctorInfo v) => collectExpr v.type
    | some (.recInfo v) => collectExpr v.type
    | some (.inductInfo v) => collectExpr v.type *> v.ctors.forM corpusCollectLegacy
    | _ => pure ()

def corpusLegacyAxioms (name : Name) : CoreM (Array Name) := do
  let (_, state) := ((corpusCollectLegacy name).run (← getEnv)).run {}
  return state.axioms

elab "corpusAxiomCollector" : term => do
  let name := if (← getEnv).contains `Lean.collectAxioms then
    `Lean.collectAxioms else `corpusLegacyAxioms
  Lean.Elab.Term.elabTerm (mkIdent name) none

partial def nodes (s : Syntax) (kind : Name) : Array Syntax :=
  (if s.getKind == kind then #[s] else #[]) ++ s.getArgs.foldl (fun a x => a ++ nodes x kind) #[]

partial def admissions (s : Syntax) : Nat :=
  (if s.isAtom && (s.getAtomVal == "sorry" || s.getAtomVal == "admit") then 1 else 0) +
  s.getArgs.foldl (fun n x => n + admissions x) 0

partial def theoremNodes (s : Syntax) : Array Syntax :=
  if s.getKind == ``Parser.Command.theorem then #[s]
  else if s.getArgs.size == 4 && s[0].isAtom && s[0].getAtomVal == "lemma" then #[s]
  else s.getArgs.foldl (fun a x => a ++ theoremNodes x) #[]

def proofBodies (cmd : Syntax) : Array Syntax :=
  (theoremNodes cmd).foldl (fun acc thm =>
    let vals := nodes thm ``Parser.Command.declValSimple
    if vals.size == 1 then acc.push vals[0]![1] else acc) #[]

partial def maskBodies (s : Syntax) (bodies : Array Syntax) (placeholder : Syntax) : Syntax :=
  if bodies.any (fun b => s.getKind == b.getKind && s.getPos? == b.getPos? && s.getTailPos? == b.getTailPos?) then placeholder
  else match s with
    | .node info kind args => .node info kind (args.map (maskBodies · bodies placeholder))
    | other => other

def span (s : Syntax) : Json := Json.arr #[toJson (s.getPos?.getD 0).byteIdx, toJson (s.getTailPos?.getD 0).byteIdx]

-- Compare parsed structure, preserving names, command order and layout-derived
-- syntax. Source positions/trivia are absent; the canonical task text is unchanged.
partial def syntaxShape (s : Syntax) (bodies : Array Syntax) : Json :=
  if bodies.any (fun b => s.getKind == b.getKind && s.getPos? == b.getPos? && s.getTailPos? == b.getTailPos?) then
    Json.str "$TARGET_PROOF"
  else match s with
    | .missing => Json.str "$MISSING"
    | .atom _ value => Json.arr #[Json.str "atom", Json.str value]
    | .ident _ _ value _ => Json.arr #[Json.str "ident", Json.str value.toString]
    | .node _ kind args => Json.arr #[Json.str "node", Json.str kind.toString, Json.arr (args.map (syntaxShape · bodies))]

-- Use the recorded project's imports and command elaboration to extend its parser.
-- This supports local notation/macros; commands with errors are never hidden.
partial def requiresProcess (s : Syntax) : Bool :=
  (s.isAtom && ["initialize", "builtin_initialize", "run_cmd", "#eval", "#reduce", "unsafe", "run_tac", "run_elab", "native_decide", "elab", "elab_rules", "macro", "macro_rules", "syntax", "attribute", "init", "implemented_by"].contains s.getAtomVal) ||
  s.getArgs.any requiresProcess

abbrev ImportCache := IO.Ref (Option (String × Environment))

unsafe def inspectInput (input path mode target : String) (cache : ImportCache) (worker : Bool) : IO Json := do
  let ictx := Parser.mkInputContext input path
  let (header, ps, msgs) ← Parser.parseHeader ictx
  let importKey := (syntaxShape header #[]).compress
  let cached ← cache.get
  if worker then
    if let some (key, _) := cached then
      if key != importKey then throw (IO.Error.userError "CORPUS_RESTART:import_header_changed")
  let (env0, importMsgs) ← match cached with
    | some (key, env) =>
      if key == importKey then pure (env, msgs)
      else do
        enableInitializersExecution
        processHeader header {} msgs ictx (leakEnv := true)
    | none => do
      enableInitializersExecution
      processHeader header {} msgs ictx (leakEnv := true)
  if !importMsgs.hasErrors then cache.set (some (importKey, env0))
  if worker && importMsgs.hasErrors then
    throw (IO.Error.userError "CORPUS_FALLBACK:imports_unavailable")
  let env ← if importMsgs.hasErrors && mode == "parse" then do
    enableInitializersExecution
    importModules (leakEnv := true) #[{module := `Lean}] {}
    else pure env0
  if importMsgs.hasErrors && mode != "parse" then
    let details ← importMsgs.toList.mapM fun message => message.data.toString
    throw (IO.Error.userError ("imports unavailable:\n" ++ String.intercalate "\n" details))
  let action : Frontend.FrontendM (Array Json) := do
    let mut rows := #[]
    let mut done_ := false
    while !done_ do
      let before ← Frontend.getCommandState
      let ictx ← Frontend.getInputContext
      let ps ← Frontend.getParserState
      let scope := before.scopes.head!
      let (cmd0, ps0, parseMsgs) := Parser.parseCommand ictx {env := before.env, options := scope.opts, currNamespace := scope.currNamespace, openDecls := scope.openDecls} ps {}
      if parseMsgs.hasErrors then
        Frontend.setMessages (before.messages ++ parseMsgs)
        Frontend.setParserState { ps0 with recovering := true }
        return rows
      Frontend.setParserState ps0
      let mut commandForElab := cmd0
      if mode == "parse" && !(proofBodies cmd0).isEmpty then
        let .ok placeholder := Parser.runParserCategory before.env `term "by sorry"
          | throw (IO.Error.userError "cannot parse internal target placeholder")
        -- Extraction must retain supplied attempts even when their tactics fail
        -- or exhaust resources. Only verify mode executes original proof bodies.
        commandForElab := maskBodies cmd0 (proofBodies cmd0) placeholder
      if worker && requiresProcess commandForElab then
        throw (IO.Error.userError "CORPUS_FALLBACK:metaprogramming_or_explicit_initialization")
      Frontend.elabCommandAtFrontend commandForElab
      modify fun s => {s with commands := s.commands.push cmd0}
      done_ := Parser.isTerminalCommand cmd0
      let st ← get
      let cmd := st.commands[st.commands.size - 1]!
      for thm in theoremNodes cmd do
        let vals := nodes thm ``Parser.Command.declValSimple
        if vals.size == 1 then
          let body := vals[0]![1]
          let name := before.scopes.head!.currNamespace ++ thm[1][0].getId
          rows := rows.push (Json.mkObj [("name", toJson name.toString), ("command", span cmd),
            ("body", span body), ("admissions", toJson (admissions cmd)),
            ("scopes", toJson (before.scopes.length - 1)),
            ("closing_commands", toJson ((before.scopes.take (before.scopes.length - 1)).map fun s => if s.header.isEmpty then "end" else "end " ++ (Name.str .anonymous s.header).toString)),
            ("namespace", toJson before.scopes.head!.currNamespace.toString)])
        else
          rows := rows.push (Json.mkObj [("unsupported", toJson "non-simple theorem proof"), ("name", toJson thm[1][0].getId.toString)])
    return rows
  let (rows, st) ← (action {inputCtx := ictx}).run {commandState := Command.mkState env importMsgs {}, parserState := ps, cmdPos := ps.pos}
  let errors ← st.commandState.messages.toList.filterMapM fun m => do
    if m.severity == .error then return some (← m.data.toString) else return none
  let mut result := [("targets", Json.arr rows), ("errors", toJson errors),
    ("admissions", toJson (st.commands.foldl (fun n c => n + admissions c) 0)),
    ("imports_resolved", toJson (!importMsgs.hasErrors)), ("parse_failed", toJson st.parserState.recovering)]
  if mode == "parse" then
    let bodies := st.commands.foldl (fun acc cmd => acc ++ proofBodies cmd) #[]
    result := result ++ [("task_skeleton", Json.arr #[syntaxShape header #[], Json.arr (st.commands.map (syntaxShape · bodies))])]
  if mode == "verify" then
    let name := target.toName
    let localNames := st.commandState.env.constants.foldStage2 (fun names n _ => names.push n) (#[] : Array Name)
    let mut supportingAdmissions : Array String := #[]
    for n in localNames do
      if n != name && !env.contains n then
        let (deps, _) ← (corpusAxiomCollector n : CoreM (Array Name)).toIO
          {fileName := path, fileMap := ictx.fileMap} {env := st.commandState.env}
        if deps.contains `sorryAx then
          supportingAdmissions := supportingAdmissions.push n.toString
    result := result ++ [("supporting_admissions", toJson supportingAdmissions)]
    match st.commandState.env.find? name with
    | none => result := result ++ [("target_missing", toJson true)]
    | some ci =>
      let (axs, _) ← (corpusAxiomCollector name : CoreM (Array Name)).toIO
        {fileName := path, fileMap := ictx.fileMap} {env := st.commandState.env}
      result := result ++ [("axioms", toJson (axs.map Name.toString)),
        ("type", toJson (toString ci.type)), ("is_theorem", toJson (match ci with | .thmInfo _ => true | _ => false))]
  return Json.mkObj result


def emitWorker (value : Json) : IO Unit := do
  let stdout ← IO.getStdout
  stdout.putStrLn ("CORPUS_WORKER:" ++ value.compress)
  stdout.flush

def memoryKiB (field : String) : IO Nat := do
  try
    let status ← IO.FS.readFile "/proc/self/status"
    for line in status.splitOn "\n" do
      if line.startsWith (field ++ ":") then
        let fields : List String := ((line.replace "\t" " ").splitOn " ").filter (fun s => !s.isEmpty)
        return ((fields.drop 1).head?.getD "").toNat?.getD 0
    return 0
  catch _ => return 0

unsafe def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  let cache : ImportCache ← IO.mkRef none
  if args.head? == some "corpus-worker" then
    emitWorker (Json.mkObj [("event", toJson "ready")])
    let stdin ← IO.getStdin
    repeat
      let line ← stdin.getLine
      if line.isEmpty then break
      let .ok request := Json.parse line | throw (IO.Error.userError "invalid worker JSON")
      let .ok requestId := request.getObjValAs? Nat "id" | throw (IO.Error.userError "missing worker id")
      let .ok input := request.getObjValAs? String "source" | throw (IO.Error.userError "missing source")
      let .ok mode := request.getObjValAs? String "mode" | throw (IO.Error.userError "missing mode")
      let .ok target := request.getObjValAs? String "target" | throw (IO.Error.userError "missing target")
      emitWorker (Json.mkObj [("event", toJson "started"), ("id", toJson requestId)])
      try
        let result ← inspectInput input "/tmp/Task.lean" mode target cache true
        emitWorker (Json.mkObj [("event", toJson "result"), ("id", toJson requestId), ("result", result), ("rss_kib", toJson (← memoryKiB "VmRSS")), ("vm_size_kib", toJson (← memoryKiB "VmSize"))])
      catch error =>
        let message := error.toString
        let event := if message.startsWith "CORPUS_RESTART:" then "restart" else if message.startsWith "CORPUS_FALLBACK:" then "fallback" else "error"
        emitWorker (Json.mkObj [("event", toJson event), ("id", toJson requestId), ("message", toJson message)])
        if event == "error" then return (1 : UInt32)
    return 0
  else
    let input ← IO.FS.readFile args[0]!
    let result ← inspectInput input args[0]! args[1]! args[2]! cache false
    IO.println ("CORPUS_JSON:" ++ result.compress)
    return 0
