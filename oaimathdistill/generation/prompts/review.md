Review the candidate independently in this fresh request. Treat its answer,
basis, uncertainty, and construction notes as claims to inspect, not authority.
Check the actual student messages as well as the supporting source. Do not edit
the candidate; return passed, rejected, or needs_review with concrete reasons.
Use needs_review for substantial unresolved questions, not automatic acceptance.

For local_proof and proof_completion: check sufficient premises, the required
inference, correct constants/quantifiers, and lack of answer leakage.
For proof_repair: verify the alleged error really is invalid, its diagnosis is
accurate, and the repair resolves it with the original conditions and conclusion.
For reading (both forms): check fidelity, notation, concepts, and logic.
For reflection: check specific supporting reasons, logical coherence, and the
scope of conclusions; do not reward a paraphrase as deep analysis.
For exploration: check relevance, motivation, a plausible starting approach,
and explicit uncertainty. An open idea need not already be proved to pass.
Do not reward unsupported novelty or portray a conjecture as established fact.

Your assessment is model review, not an independent proof certificate. State
the scope and limitations of your assessment in evidence_scope. Return JSON
with status, reasons (a nonempty list), and evidence_scope.
