# Object–paper annotation instructions, version object-paper-2

Follow [object-paper-1](object-paper-1.md) for identity, scope, inspected coverage,
roles and exact quotations. Use `ontology_version="object-paper-2"` and record
this instruction version in `extractor.prompt_version` when adding `results`.

The first structured result is an attributed **membership judgement**:

- `association` identifies the physical association (use `TWA` for this pilot).
  `population` optionally retains a named subgroup. A geometric field selection
  is not evidence of physical membership.
- `status` is `member`, `candidate`, `non_member` or `uncertain`. Keep the literal
  published code/phrase in `reported_label` and explain all qualifications.
  The label must occur in a cited exact slice; a normalized status alone is
  insufficient. Never upgrade a possible member to a confirmed member.
- `origin` is `current_analysis` or `prior_work`. State the author/work in
  `attribution`, the basis in `method`, and limitations in `qualification`.
  A table copied from an earlier study remains a prior claim even if a later
  section of the same paper re-evaluates the object.
- Cite identity, classification, code definitions and contextual qualifications
  together when they are separated. Current results require `reports_result`;
  prior results require `cites_prior_result`. Identity must be resolved as an
  annotator assertion, with `assessment="evidence_found"`.

The same object-paper record may hold both an adopted prior judgement and the
paper's own judgement. Do not reconcile their disagreement automatically.
Results inherit the record's source/component/system scope. They never replace
the original member table or alter its probabilities and flags.

Use a new batch ID for a structured revision. Existing v1 history is retained.
The server checks schema, offsets and quotes; it does not verify scientific
interpretation. Every record remains pending. This workflow does not implement
human review or establish an independently labelled evaluation benchmark.
