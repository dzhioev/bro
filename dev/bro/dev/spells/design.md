---
name: design
description:

Develop or revise a standalone software design specification
— “write a specification”, “define the model and its rules”, “write the full design”,
“turn this discussion into a design document”, or “revise the specification”.
Also used by a workflow that explicitly commissions a design phase.
Produces a self-contained, checked document and maintains it through revisions.
Not for ordinary technical questions, brainstorming, or a brief implementation proposal.
Does not authorize implementation.

parameters: {"subject?": "what to design, or the existing design to revise; omitted means the current discussion"}
version: 1.0.0
---

# Design

Produce a specification whose meaning does not depend on remembering the conversation.
A specification defines the proposed system's concepts, behavior, and constraints
well enough to evaluate it and implement its in-scope behavior without inventing missing rules.

The specification is the deliverable.
Research, questions, alternatives, and review support it;
they are not substitutes for it.

## 1. Establish the design boundary

Read the subject and any existing design, requirements, and decisions it references.
Without a subject, use the current discussion.
If several subjects or document versions plausibly apply, resolve which one before proceeding.

Establish:

- the problem and the outcomes the design must enable;
- what is in scope and what is deliberately outside it;
- requirements and decisions already settled;
- choices that remain open, including decisions explicitly assigned to a later phase.

Distinguish decisions this design must settle from choices deliberately delegated to later work.
Specify the constraints or process governing deferred choices.

Do not ask the user to repeat information already available.
Do not treat an earlier agent recommendation as an agreed requirement.

Use the destination named by the request or calling workflow.
Otherwise, present the document in the conversation;
do not create repository files or publish it elsewhere merely to give it a home.

## 2. Ground the proposal

Read the code, documentation, and external contracts needed to establish the design's constraints.
Distinguish existing behavior from requirements for the proposed system.

Existing syntax and structure are evidence, not requirements to preserve,
unless compatibility is an actual constraint.
Work out the clean model before choosing how to fit it into the implementation.
When replacing an existing representation, identify the user capabilities it serves;
do not silently retire a needed capability along with the representation.

When a proposal accumulates substantial machinery, investigate existing mechanisms that might supply it.
Compare the behavior they provide, the constraints they impose, and what the surrounding system would still own.
A working example alone does not establish that a replacement is simpler.

Where a missing fact can be investigated, investigate it.
Where a choice requires the user's judgment, present the recommendation and tradeoff
under the session's interaction rules.
Keep unresolved choices explicit rather than disguising them as established facts.

## 3. Write the specification

Organize the document around the system being designed, not the sequence of discussion.
Use the structure the subject needs rather than filling a fixed template.

The document must establish:

- its goal, scope, and constraints;
- the concepts it introduces and the relationships between them;
- the operations the system supports;
- the rules those operations must obey;
- the boundary cases and failure behavior that affect those rules;
- examples beside concepts and operations that need explanation,
  with enough distinct values to demonstrate their important relationships and boundary cases.

Define each domain-specific term and introduced symbol before using it,
or explicitly point ahead to its definition.
Use one name per concept and one meaning per name.
Prefer descriptive names for concepts and rules; reserve short notation for relationships it makes clearer.
Explain notation where it first appears;
prefer words when notation adds no precision.

For each operation, specify its inputs, preconditions, result or state changes,
rejection conditions, and the component responsible for enforcing its rules,
where those distinctions apply.
Distinguish intended guarantees from implementation limitations or approximations,
and state their observable consequences.

Separate conceptual rules, representations, and implementation decisions where those distinctions matter.
Include only the levels the requested design needs.
Examples must follow the stated rules, not introduce hidden exceptions.
Choose examples at the requested level of abstraction;
use domain-neutral examples when conceptual independence is part of the request.

Show the resulting rules and values, not only relative instructions such as “keep” or “unchanged”.
State each rule once and reference it elsewhere.
Include rationale where a consequential choice would otherwise be unexplained,
not as a transcript of the alternatives discussed.

The document may be a draft with open decisions.
Clearly distinguish proposed choices from settled requirements;
do not call it settled while decisions required for the agreed design scope remain open.

## 4. Check the whole document before presenting it

Read the complete candidate as a reader who has not seen the conversation.
Perform these checks on every presented revision, not only on the changed passage:

- Vocabulary: every introduced term and symbol has a definition;
  names do not collide, and synonyms do not conceal different concepts.
- Rules: the prose, notation, examples, and implementation mapping agree;
  conditions do not accidentally claim more than they permit.
- Coverage: every in-scope requirement has a defined outcome;
  necessary defaults, absence states, invalid inputs, and failures are covered.
- Composition: individually valid operations or rules do not produce an undefined
  or contradictory result when combined.
- Examples: trace representative valid and invalid cases through the actual rules;
  check the boundary cases for distinctions the design introduces.
- Revisions: settled requirements remain satisfied,
  and removed or renamed concepts leave no stale references.
- Necessity: check whether similar mechanisms can share one definition,
  and distinguish primitive operations from operations derived from them.
  Keep separate concepts or variants only where they explain or support a required distinction;
  do not simplify by discarding needed behavior.

Fix defects found by these checks.
If resolving one requires a new design decision, expose that decision
rather than silently changing the agreed behavior.

Use executable checks where they can settle a claim.
Do not describe a manual consistency check as a proof or as runtime verification.

## 5. Maintain the specification through discussion

A question gets an answer, not automatic agreement or a rewrite.
Check whether a challenge exposes a defect or a new requirement;
otherwise explain why the existing distinction is needed.
Revise when the user asks for the redraw;
until then, keep agreed corrections as a short list of pending revisions.

When revising, propagate the change through definitions, rules, notation,
examples, and implementation mapping.
Then run the whole-document checks again.

Maintain one complete, current specification.
Present revisions in the format requested by the user or calling workflow;
do not leave the accepted design scattered across conversational amendments.
Keep any change summary outside the specification itself.

A correction that exposes a class of defect calls for checking that class
throughout the document, not only repairing the cited occurrence.

Do not treat approval of one correction as approval of unrelated proposed choices.

## 6. Deliver and hand back

Deliver the current specification, with remaining decisions and unverified assumptions
identified separately.
A draft remains a draft until the approval required by the session or calling workflow is obtained.

Follow the scope and handoff established by the invoking request or workflow.
Include implementation strategy, migration, rollout, or planning when requested;
do not silently expand design work into implementation execution or task management.
When invoked on its own, stop after delivering the design;
approval of the design alone is not an instruction to implement it.
