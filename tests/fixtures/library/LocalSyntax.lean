/- Synthetic local syntax/context fixture. Apache-2.0. -/
import Lean
open Lean Parser

syntax (name := fixtureLemma) declModifiers
  group("lemma " declId ppIndent(declSig) declVal) : command

@[macro fixtureLemma] def expandFixtureLemma : Macro := fun stx => do
  let stx := stx.modifyArg 1 fun stx =>
    let stx := stx.modifyArg 0 (mkAtomFrom · "theorem" (canonical := true))
    stx.setKind ``Parser.Command.theorem
  pure <| stx.setKind ``Parser.Command.declaration

section Named
namespace Nested.Inner
local notation "fixtureZero" => (0 : Nat)
lemma local_syntax : fixtureZero = 0 := by rfl
end Inner
end Nested
end Named
