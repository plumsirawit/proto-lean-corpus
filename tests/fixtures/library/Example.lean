/- Synthetic fixture, Apache-2.0. Author: proto-lean-corpus contributors. -/
namespace Fixture
variable (n : Nat)
def label := "sorry is just text"
-- sorry in comments is not an admission.
theorem first : n = n := by rfl
theorem second : True := by trivial
end Fixture
