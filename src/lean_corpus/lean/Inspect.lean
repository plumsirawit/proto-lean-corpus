import Lean
open Lean Lean.Elab

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
unsafe def main (args : List String) : IO UInt32 := do
  let path := args[0]!
  let mode := args[1]!
  initSearchPath (← findSysroot)
  enableInitializersExecution
  let input ← IO.FS.readFile path
  let ictx := Parser.mkInputContext input path
  let (header, ps, msgs) ← Parser.parseHeader ictx
  let (env0, importMsgs) ← processHeader header {} msgs ictx (leakEnv := true)
  let env ← if importMsgs.hasErrors && mode == "parse" then importModules #[{module := `Lean}] {} else pure env0
  if importMsgs.hasErrors && mode != "parse" then
    IO.println (Json.compress (Json.mkObj [("error", toJson "imports unavailable")]))
    return 2
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
      Frontend.elabCommandAtFrontend commandForElab
      modify fun s => {s with commands := s.commands.push cmd0}
      done_ := Parser.isTerminalCommand cmd0
      let st ← get
      let cmd := st.commands.back!
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
  let (rows, st) ← (action {inputCtx := ictx}).run {commandState := Command.mkState env msgs {}, parserState := ps, cmdPos := ps.pos}
  let errors ← st.commandState.messages.toList.filterMapM fun m => do
    if m.severity == .error then return some (← m.data.toString) else return none
  let mut result := [("targets", Json.arr rows), ("errors", toJson errors),
    ("admissions", toJson (st.commands.foldl (fun n c => n + admissions c) 0)),
    ("imports_resolved", toJson (!importMsgs.hasErrors)), ("parse_failed", toJson st.parserState.recovering)]
  if mode == "parse" then
    let bodies := st.commands.foldl (fun acc cmd => acc ++ proofBodies cmd) #[]
    result := result ++ [("task_skeleton", Json.arr #[syntaxShape header #[], Json.arr (st.commands.map (syntaxShape · bodies))])]
  if mode == "verify" then
    let name := args[2]!.toName
    let localNames := st.commandState.env.constants.foldStage2 (fun names n _ => names.push n) (#[] : Array Name)
    let mut supportingAdmissions : Array String := #[]
    for n in localNames do
      if n != name && !env.contains n then
        let (deps, _) ← (Lean.collectAxioms n : CoreM (Array Name)).toIO
          {fileName := path, fileMap := ictx.fileMap} {env := st.commandState.env}
        if deps.contains `sorryAx then
          supportingAdmissions := supportingAdmissions.push n.toString
    result := result ++ [("supporting_admissions", toJson supportingAdmissions)]
    match st.commandState.env.find? name with
    | none => result := result ++ [("target_missing", toJson true)]
    | some ci =>
      let (axs, _) ← (Lean.collectAxioms name : CoreM (Array Name)).toIO
        {fileName := path, fileMap := ictx.fileMap} {env := st.commandState.env}
      result := result ++ [("axioms", toJson (axs.map Name.toString)),
        ("type", toJson (toString ci.type)), ("is_theorem", toJson (match ci with | .thmInfo _ => true | _ => false))]
  IO.println ("CORPUS_JSON:" ++ Json.compress (Json.mkObj result))
  return 0
