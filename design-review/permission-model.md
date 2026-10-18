# Permission model: design review

Design review only: this file is never merged.
The source of truth is [issue 978](https://github.com/dzhioev/bro/issues/978), where the settled model lands;
this copy and its branch are deleted when the review ends.

## Model (draft)

The **host** is the trusted runtime that starts missions, checks every start, and keeps a record of each mission that no mission can change.
Missions and the other terms are defined below.

### Mission types and options

𝒯 is the set of **mission types**.
Each type t has a finite set of **options** Oₜ, and each option o has a **kind** over a set B, its **base set**:

- **scalar over B**: a value of o is one element of B;
- **subset over B**: a value of o is a finite subset of B.

The **domain** dom(o) is the set of o's values: B for a scalar, the finite subsets of B for a subset option.
A **flag** is a scalar over {on}.

> **Example.** The examples share two types.
> T has four options:
>
> - A, a scalar over {a1, a2, a3, a4, a5};
> - B, a subset over {b1, b2, b3, b4};
> - F, a flag;
> - N, a scalar over the integers 1–100.
>
> U has one option, C, a scalar over item names.
> So dom(A) = {a1, a2, a3, a4, a5}, dom(F) = {on}, and dom(B) holds all 16 subsets of {b1, b2, b3, b4}, from ∅ through {b2, b4} to {b1, b2, b3, b4}.

### Value sets

Each option o has a family 𝒮(o) of **value sets**: the sets of values that permissions and patterns (below) can name.
𝒮(o) contains ∅, dom(o) (written `*`), and every singleton.
An option may add more, such as ranges of numbers, "every subset of X", or "every superset of Y".
The host must compute, in finite time:

- the **intersection** S₁ ∩ S₂ and the **difference** S₁ \ S₂, each again a value set in 𝒮(o);
- **emptiness**: whether S = ∅.

**Membership** follows from these: v ∈ S if and only if (iff) {v} ∩ S isn't empty.

> **Example.** A and B have finite domains, so 𝒮(A) and 𝒮(B) can hold every set of their values.
> 𝒮(B) names some of them "every subset of X" and "every superset of Y", and 𝒮(N) includes ranges such as 10–20.
> "Every subset of {b1, b2}" is {∅, {b1}, {b2}, {b1, b2}}, and "every superset of {b3}" is the eight values that contain b3.
>
> - {a1, a2, a3} ∩ {a3, a4, a5} = {a3}, and 10–20 \ 15–30 = 10–14.
> - Every subset of {b1, b2, b3} ∩ every superset of {b3} = {{b3}, {b1, b3}, {b2, b3}, {b1, b2, b3}}.
> - Every subset of {b1, b2} \ every superset of {b2} = {∅, {b1}}, which is every subset of {b1}.
> - {b1, b3} ∈ every superset of {b3}, because {{b1, b3}} ∩ every superset of {b3} isn't empty.

### Descriptors

A **descriptor** d = (t, σ) is a type t and a partial map σ from Oₜ to values, written {o ↦ σ(o), …}, with σ(o) ∈ dom(o).
An option in σ is **set**;
an option outside σ is **unset**.
Unset differs from every value, the empty subset included.

> **Example.** (T, {A ↦ a4, B ↦ {b1, b3}}) sets A to a4 and B to the subset {b1, b3}, and leaves F and N unset.
> (T, {A ↦ a4, B ↦ ∅}) is a different descriptor: it sets B to the empty subset.
> (T, {A ↦ a4}) differs from both: in it, B is unset.

### Atoms and permissions

An **atom** is a unit of permission:

- the **start atom** t permits starting a mission of type t;
- the **option atom** (t, o, v), with v ∈ dom(o), permits a start of type t to set o to v.
  It doesn't permit the start itself.

A **permission** is a set of atoms of one of two forms: {t}, or (t, o, S) = {(t, o, v) : v ∈ S} for a value set S ∈ 𝒮(o).

> **Example.**
>
> - (T, A, {a1, a2, a3}) = {(T, A, a1), (T, A, a2), (T, A, a3)}.
> - (T, B, every subset of {b1, b2}) = {(T, B, ∅), (T, B, {b1}), (T, B, {b2}), (T, B, {b1, b2})};
>   it doesn't contain (T, B, {b1, b3}), since b3 ∉ {b1, b2}.
> - (T, B, every superset of {b3}) holds eight atoms, from (T, B, {b3}) to (T, B, {b1, b2, b3, b4}).
>
> None of these permissions lets anyone start a T mission;
> only the start atom T does.

### Patterns

A **pattern** π = (t, D, R) describes descriptors: D maps some options o of t to value sets D(o) ∈ 𝒮(o), written like σ, and R ⊆ Oₜ lists options that must be set.
A descriptor d **matches** π, written d ⊨ π, iff d has type t, σ(o) ∈ D(o) for every option o in D that d sets, and d sets every option in R.

| o in D | o in R | Requirement on o |
|---|---|---|
| no | no | none |
| yes | no | unset, or set to a value in D(o); D(o) = ∅ means unset |
| no | yes | set |
| yes | yes | set to a value in D(o) |

> **Example.** Let π = (T, {A ↦ {a1, a2, a3}, B ↦ every subset of {b1, b2, b3}}, {F}).
>
> - (T, {A ↦ a2, B ↦ {b1, b3}, F ↦ on}) matches π.
> - So does (T, {B ↦ {b2}, F ↦ on}): π allows A to be unset, since A ∉ R.
> - (T, {A ↦ a2, B ↦ {b1, b4}, F ↦ on}) doesn't match, because {b1, b4} isn't a subset of {b1, b2, b3}.
> - (T, {A ↦ a5, F ↦ on}) doesn't match, because a5 ∉ {a1, a2, a3}.
> - Neither does (T, {A ↦ a2, B ↦ {b1}}), which leaves F unset.
>
> The pattern (T, {B ↦ every superset of {b2}}, {B}) matches the T descriptors whose B is set and contains b2, and (T, {F ↦ ∅}, ∅) matches exactly those that leave F unset.

**Meet.** For patterns of the same type, π₁ ⊓ π₂ = (t, D, R₁ ∪ R₂), where D(o) = D₁(o) ∩ D₂(o) if both name o, and otherwise the value set of whichever names it.
A descriptor matches π₁ ⊓ π₂ iff it matches both.
For sets, G₁ ⊓ G₂ is the set of π₁ ⊓ π₂ over all pairs π₁ ∈ G₁, π₂ ∈ G₂ of the same type.
A descriptor matches G₁ ⊓ G₂ iff it matches a pattern in G₁ and a pattern in G₂.

> **Example.** Take
>
> - πa = (T, {A ↦ {a1, a2, a3}, B ↦ every subset of {b1, b2, b3}}, ∅);
> - πb = (T, {A ↦ {a3, a4}, B ↦ every superset of {b2}}, {F}).
>
> Then πa ⊓ πb = (T, {A ↦ {a3}, B ↦ {{b2}, {b1, b2}, {b2, b3}, {b1, b2, b3}}}, {F}).

**Inclusion.** A set of patterns G₁ is **within** a set G₂ if every descriptor that matches a pattern in G₁ also matches a pattern in G₂.
The host can decide this with the value-set operations, because a pattern constrains each option separately: to a value set, with or without the unset state.

> **Example.**
>
> - {(T, {A ↦ {a1}}, {A, F})} is within {(T, {A ↦ {a1, a2}}, {A})}: a descriptor that sets A to a1 and sets F sets A within {a1, a2}.
> - {(T, {A ↦ {a1, a2}}, ∅)} is within {(T, {A ↦ {a1}}, ∅), (T, {A ↦ {a2}}, ∅)}: together the two patterns allow A unset, a1, and a2.
> - {(T, ∅, ∅)} isn't within {(T, {A ↦ {a1}}, {A})}: (T, {A ↦ a3}) matches the first but not the second.

### Authority

An **authority** H gives every atom x a pair H(x) = (use_H(x), give_H(x)):

- use_H(x) ∈ {0, 1} says whether its holder may use x in a start it makes;
- give_H(x) is a set of patterns, x's **recipient patterns**.
  A mission whose descriptor matches one of them is a **recipient** of x: the holder may give x to it when starting it.

**Join and meet.** For two pairs (u₁, g₁) and (u₂, g₂):

- their **join** is (u₁, g₁) ⊔ (u₂, g₂) = (max(u₁, u₂), g₁ ∪ g₂): use if either pair has it, and the recipients of either;
- their **meet** is (u₁, g₁) ⊓ (u₂, g₂) = (min(u₁, u₂), g₁ ⊓ g₂): use only if both pairs have it, and only the recipients that match both.

Joining with (0, ∅) changes nothing, and neither does meeting with ⊤ = (1, {(t, ∅, ∅) : t ∈ 𝒯}), whose recipients are all missions, since (t, ∅, ∅) matches every descriptor of type t.

> **Example.** Let π₁ = (T, {A ↦ {a1}}, {A}), which matches the T descriptors whose A is a1, and π₂ = (T, {B ↦ every subset of {b1, b2}}, {B}), which matches those whose B is set within {b1, b2}.
>
> - (1, {π₁}) ⊔ (0, {π₂}) = (1, {π₁, π₂}).
> - (1, {π₁}) ⊓ (1, {π₂}) = (1, {π₁ ⊓ π₂}), where π₁ ⊓ π₂ = (T, {A ↦ {a1}, B ↦ every subset of {b1, b2}}, {A, B}).
> - (1, {π₁}) ⊓ (0, ∅) = (0, ∅), and (1, {π₁}) ⊓ ⊤ = (1, {π₁}).

**Authorities and caps.** Both are written as lists of **entries** p ↦ (u, g), with p a permission, and every atom of p gets the pair (u, g).
They differ in how entries add up:

- in an **authority**, an atom in several entries gets the join of their pairs, and an atom in none gets (0, ∅);
- in a **cap**, an atom in several entries gets the meet of their pairs, and an atom in none gets ⊤.

Two authorities combine by joining each atom's pairs, (H₁ ⊔ H₂)(x) = H₁(x) ⊔ H₂(x).
A cap K limits an authority by meeting them, (H ⊓ K)(x) = H(x) ⊓ K(x).
Joining never lowers anything, and meeting never raises anything.

> **Example.** Let H0 be the authority:
>
> - T ↦ (1, ∅)
> - (T, A, {a1, a2, a3}) ↦ (1, ∅)
> - (T, B, every subset of {b1, b2, b3}) ↦ (1, ∅)
> - (T, F, `*`) ↦ (1, ∅)
> - (T, N, `*`) ↦ (1, ∅)
> - (U, C, {c1}) ↦ (0, {π₁})
>
> H0's holder may start T missions with A ∈ {a1, a2, a3}, any F and N, and B within {b1, b2, b3}: ∅, {b2}, or {b1, b3}, say, but not {b4} or {b1, b4}.
> It may neither start U missions nor set C, but it may give (U, C, c1) to T missions whose A is a1.
>
> Combining:
>
> - H0 ⊔ {(T, B, every superset of {b4}) ↦ (1, ∅)} may set B to any of the 16 values: one without b4 lies within {b1, b2, b3}, and one with b4 is a superset of {b4}.
> - H0 ⊔ {(T, B, every superset of {b3}) ↦ (0, {π₁})} keeps H0's use and adds give.
>   The four values in both B entries, {b3}, {b1, b3}, {b2, b3}, and {b1, b2, b3}, get (1, ∅) ⊔ (0, {π₁}) = (1, {π₁});
>   the four with both b3 and b4 get (0, {π₁}), giveable but not usable.
>
> Capping:
>
> - H0 ⊓ {T ↦ (0, ∅)} may not start T missions.
>   The cap gives every other atom ⊤, so H0's option atoms of T stay, but they're useless without the start atom.
> - H0 ⊓ {(T, B, every superset of {b3}) ↦ (0, ∅)} may set B only within {b1, b2}: the cap takes away every value that contains b3.
> - H0 ⊓ {(U, C, {c1}) ↦ (1, ∅)} may no longer give c1;
>   its use of c1 was 0 and stays 0.
> - H0 ⊓ {(U, C, {c1}) ↦ (1, {π₂})} gives c1 the pair (0, {π₁}) ⊓ (1, {π₂}) = (0, {π₁ ⊓ π₂}): it may give c1 only to T missions whose A is a1 and whose B is set within {b1, b2}.

### Base

A **source** is a trusted input, such as a configuration file.
A **reading** is the content of every source at one moment.
Each type t defines its **base**: for a descriptor d of type t and a reading ρ, baseₜ(d, ρ) is the authority the host computes from ρ for d.
d's values may pick which parts of ρ apply, but never what they contain.

> **Example.** Say the reading ρ gives T missions a base picked by the value of A:
>
> - for A = a2, the base is H0;
> - for A = a1, it is {U ↦ (1, ∅), (U, C, {c2}) ↦ (1, ∅)}.
>
> A start chooses A, and with it which base applies, but it can't change what either base contains.

### Missions and principals

A **mission** is one started instance of a type.
The host gives it a unique id and records its descriptor dₘ, the gift and cap it was started with, its operator gift and cap (see Override), its authority Hₘ, and the reading ρₘ it carries.
Two missions can share a descriptor and differ in authority.

A **principal** is a sender of start requests: a mission, or the **operator**, the party that runs the host.
The host sets the operator's authority O;
by default O gives every atom ⊤, which permits every start and every gift.

The principal that starts a mission is its **starter**.
A mission the operator starts is a **root**.
A root starts with a new reading, and every other mission carries its starter's;
a resume (below) replaces it.
A mission's **chain** is the mission, its starter, that starter's starter, and so on up to the operator.

> **Example.** The operator starts a root m0 with descriptor (T, {A ↦ a2, B ↦ {b1, b2}, N ↦ 50}).
> m0 carries a new reading ρ, so its base is H0.
> m0 starts m1, and m1 starts m2;
> both carry ρ, so a change to the sources after m0 started doesn't reach them.
> m2's chain is m2, m1, m0, the operator.

### Starting a mission

A **start request** names a type t and carries values for some of its options, a **gift** Γ (an authority for the new mission), and a cap K.
A missing gift is empty, and a missing cap limits nothing.
A request may also carry content no permission governs, such as a prompt;
anything a permission must be able to restrict has to be an option.

**Normalization.** Each type t supplies two rules:

- which of its descriptors are **valid**, meaning the combinations of values t accepts;
- for some options, a **default**: one value the type computes from the values the request sets and the reading the new mission will carry, never from any principal's authority.

The host applies these rules to turn a request into the new mission's descriptor d:

1. It refuses the request if it names an unknown option or a value outside its option's domain, or if its gift or cap names an unknown type or option, or a value set its option doesn't have.
2. It sets each option the request leaves unset to the option's default, if it has one.
3. It refuses the request if the resulting descriptor isn't valid;
   otherwise that descriptor is d.

An option whose unset state would mean "unrestricted" needs a default that says so as a value, so that the use rule checks it.

> **Example.** T's defaults are a1 for A, {b1, b2} for B, and for N the value ρ gives, 50;
> F has none.
> T's valid descriptors are those in which B contains b4 only if F is set.
>
> - m0, holding H0, requests a T mission and sets nothing.
>   It gets d1 = (T, {A ↦ a1, B ↦ {b1, b2}, N ↦ 50}), with F unset.
> - A request with B ↦ {b3, b4} and F unset is refused as invalid.
> - Had T given B no default and treated an unset B as {b1, b2, b3, b4}, a starter could get all four elements past the use rule by leaving B unset.
>   T would have to default B to {b1, b2, b3, b4} instead, so that the use rule checks it.

**Rules.** A starter with authority H may start d = (t, σ) with gift Γ and cap K iff:

- **use rule:** use_H(t) = 1, and use_H(t, o, σ(o)) = 1 for every option o that d sets, defaults included;
- **gift rule:** for every atom x with Γ(x) ≠ (0, ∅), the new mission is a recipient of x (d ⊨ π for some π ∈ give_H(x)), and give_Γ(x) is within give_H(x).

> **Example.**
>
> - Use rule: m0 may start d1, since H0 gives use of T, (T, A, a1), (T, B, {b1, b2}), and (T, N, 50).
>   - A request with A ↦ a4 fails at (T, A, a4).
>   - A request with B ↦ {b1, b4} and F ↦ on fails at (T, B, {b1, b4}), since b4 lies outside {b1, b2, b3}.
>   - A starter that may use (T, A, a2) but not (T, A, a1) and leaves A unset fails at (T, A, a1), the default;
>     it has to set A ↦ a2 itself.
>   - Every U request fails at the start atom U.
> - Gift rule: m0 may start d1 with the gift Γ = {(U, C, {c1}) ↦ (1, ∅)}, because d1 matches π₁.
>   The same gift fails for a T descriptor with A = a2, which matches no recipient pattern of c1.
>   The gift {(U, C, {c1}) ↦ (1, {(T, {A ↦ {a1}}, {A, F})})} passes, since its pattern is within {π₁}.
>   The gift {(U, C, {c1}) ↦ (1, {(T, ∅, ∅)})} is refused: it would let m1 pass c1 to T missions with any A, which m0 itself can't.

**Admission checks.** The host also runs checks that aren't about authority, such as a limit on chain length.

> **Example.** With at most three missions per chain, m2 may start nothing, whatever its authority.

**The new authority.** Hₘ = (baseₜ(d, ρ) ⊔ Γ) ⊓ K, where ρ is the reading the new mission carries.
The gift rule bounds a gift's give but not its use, so a starter may give use it doesn't have, but it can't extend how far an atom travels.
A gift never takes away what the base supplies;
only the cap lowers anything.
The base needn't lie within the starter's authority.
An override (below) can later add a layer on top.

> **Example.**
>
> - m1's base is the one ρ gives for A = a1, {U ↦ (1, ∅), (U, C, {c2}) ↦ (1, ∅)}.
>   m0 starts d1 with the gift Γ above and the cap K = {(U, C, {c2}) ↦ (0, ∅)}, so m1's authority is {U ↦ (1, ∅), (U, C, {c1}) ↦ (1, ∅)}.
>   m1 may start a U mission with C ↦ c1 but not with c2, while m0 still may not start any U mission.
> - Had m0 given (U, C, {c1}) ↦ (1, {π₁}), m1 could pass c1 on as well, to T missions whose A is a1, just as m0 can.

**What give means.** A non-empty give lets its holder create a usable x in any recipient, including one it can steer through its option values.
use = 0 stops only direct use: if a recipient acts on its starter's instructions, the starter effectively uses what it gave.
The recipient patterns are the real boundary.

> **Example.** m0 can start a T mission whose A is a1 and give it (U, C, c1).
> If T missions follow their starter's instructions, m0 can then have it start a U mission with C ↦ c1 on m0's behalf.
> What limits who can end up using c1 is π₁.

### Joint starts

A **joint start** is one start request completed in two steps: first by a mission, which becomes the starter, then by the operator.

- **prepare:** the mission sends the request with some options unset.
  The host refuses unknown options and out-of-domain values, runs the use rule on the start atom and on the options the mission set, checks that the gift's give is within the mission's own, and records the request.
  Nothing starts.
- **commit:** the operator sets any options, replacing values the starter set if it chooses.
  The host fills the remaining defaults from the values both principals set, and checks validity.
  Then it runs every check again, against the authorities as they stand at commit:
  the use rule checks each option against the authority of the principal whose value it holds, with defaults counting as set by the starter, and the gift rule runs on the final descriptor.
  Any failure refuses the commit.
  The starter's gift and cap can't change.

> **Example.** m0 prepares a T start with the gift Γ and sets nothing.
>
> - Suppose the operator commits A ↦ a3.
>   The use rule passes, because a3 is checked against the operator's authority.
>   But the descriptor no longer matches π₁, so the gift rule fails and the commit is refused.
> - Committing A ↦ a1, or leaving A to its default a1, starts the mission with the gift.
> - The operator may also set B ↦ {b2, b4}, a value m0 couldn't use itself, as long as it sets F ↦ on too.
> - Had m0 prepared the start with B ↦ {b1}, the operator could still commit B ↦ {b2}.
>   The value it ends with is the operator's, so it's checked against the operator's authority.

### Existence

An option's domain never changes.
An option over credential names, say, has every well-formed name as a value, whether or not a credential by that name exists, and `*` covers names created later too.
The host checks that the items a descriptor names exist only when it runs a start that has passed every rule and check, so only a starter allowed to name an item can learn that it's missing.
What a mission receives for a value, such as a credential's material, is outside the model.

> **Example.** C's values are item names, and the store holds c1 and c2.
>
> - A starter that may use only c1 and asks for C ↦ c9 is refused by the use rule at (U, C, c9), whether or not c9 exists.
> - A holder of (U, C, `*`) that starts a U mission with C ↦ c9 passes the rules, and the start fails when it runs, because c9 doesn't exist.
>   Once the store gains c3, the same holder can start one with C ↦ c3, with no change to its authority.

### Override and resume

**Override.** The operator may override any running mission, root or not.
An override sets three things: the mission's **operator gift** Γₒ, its **operator cap** Kₒ, and the values of its **overridable** options, which update its recorded descriptor.
A mission starts with an empty operator gift and an operator cap that limits nothing.
Each type names its overridable options, and each must be **authority-neutral**: the base never depends on it, and no pattern names it.
A gift or cap whose patterns name an overridable option is refused, and a base whose patterns name one is an error.
So changing an option never changes the mission's authority: it can't swap the base or undo the recipient check a gift passed.
The host checks an override as it would a start by the operator: normalization for changed options, then the use rule and the gift rule against the operator's authority O.
If they pass, it recomputes the mission's authority:

Hₘ = (((baseₜ(d, ρ) ⊔ Γ) ⊓ K) ⊔ Γₒ) ⊓ Kₒ,

with ρ the mission's reading and Γ and K the gift and cap it was started with.
So the starter's cap can't cut what the operator grants, and the operator's cap can take away anything.
The base stays as it was: an override doesn't touch the reading, and the options it changes don't feed the base.
If the checks fail, nothing changes.
The missions the overridden mission already started keep what they have.

> **Example.**
>
> - N is overridable: the base is picked by A alone, and no pattern names N.
>   The operator changes m1's N from 50 to 80, and m1 continues with N = 80 and the same authority.
>   Changing N to 120 would be refused, since 120 is outside N's domain.
>   A gift whose recipient pattern is (T, {N ↦ 1–50}, ∅) is refused, because it names N.
> - The store gains c3 while m1 runs.
>   The operator overrides m1 with Γₒ = {(U, C, {c3}) ↦ (1, ∅)}, and m1's authority becomes {U ↦ (1, ∅), (U, C, {c1, c3}) ↦ (1, ∅)}.
>   m2, already running, keeps its authority.
>   The operator could also take c1 away with Kₒ = {(U, C, {c1}) ↦ (0, ∅)}.
> - An override with Γₒ = {(U, C, {c2}) ↦ (1, ∅)} would hand m1 the c2 that m0's cap took away, and one with Kₒ = {U ↦ (0, ∅)} would stop m1 from starting U missions at all.

**Resume.** The operator may resume any mission whose record the host keeps, and a resumed mission is a root.
The host starts it again under the same id, as a start by the operator with a new reading.
That start carries the mission's recorded descriptor, gift, and cap, and its operator gift and cap, any of which the operator may replace, and the host runs every check of a start on them.
The missions the resumed mission had started don't survive a resume.

> **Example.**
>
> - The sources change so that the base for A = a2 no longer gives c1.
>   Resuming m0 starts it again with a new reading, under which its authority no longer gives c1.
>   m1 and m2 don't survive the resume.
> - m1 fails, and the host keeps its record.
>   Resuming m1 starts it as a root with a new reading, its descriptor d1, the gift of c1 from m0, and m0's cap.
>   Its chain is now m1 and the operator, and m2 doesn't survive.

### Visibility and refusals

- A mission can read its own descriptor and authority, including atoms it may give but not use.
- A refusal names the rule or check that failed, and the atom or option involved.

> **Example.** m0's view of its authority lists (U, C, {c1}) ↦ (0, {π₁}), although m0 can't use c1.
> m0's request for a U mission with C ↦ c1 is refused, naming the use rule and the start atom U.

### Limits

- **Options are independent.** The use rule checks each option on its own, so the starts an authority permits are all combinations of per-option values.
  This is also what lets a gift widen one option while the base still bounds the others.
  *Example:* H0 can't permit A = a3 only together with a B that contains b3;
  its starts are every combination of A ∈ {a1, a2, a3}, B within {b1, b2, b3}, and any F and N.
  A recipient pattern can name such a combination, though: (T, {A ↦ {a3}, B ↦ every superset of {b3}}, {A, B}).
- **Subset values don't combine element by element**, so elements that are independent permissions belong in separate options.
  *Example:* every subset of {b1, b2} from the base and every subset of {b3, b4} from a gift permit {b1, b2} and {b3, b4}, but not {b2, b3}.
- **Setting isn't forced.** Limiting an option's values doesn't make starts set it;
  only a default does.
  *Example:* a starter whose only F atom is (T, F, on) may still leave F unset.
- **Recipient patterns cover one hop.** *Example:* for m1 to be able to give c1 to T missions whose A is a2, m0's own give for c1 must cover those missions.
  That also lets m0 give c1 to them directly.

### Putting it together

1. The operator starts the root m0, whose authority is H0.
   m0 may not start U missions, but it may give (U, C, c1) to T missions whose A is a1.
2. m0 starts m1 with descriptor d1 = (T, {A ↦ a1, B ↦ {b1, b2}, N ↦ 50}), the gift {(U, C, {c1}) ↦ (1, ∅)}, and the cap {(U, C, {c2}) ↦ (0, ∅)}.
3. m1 starts m2, a U mission with C ↦ c1.

m0 never holds a usable c1, and m1 can't pass c1 on, because its give for c1 is empty.

