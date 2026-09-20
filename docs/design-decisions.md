# Design Decisions — auth-settlement-recon

Companion to `docs/debug-log.md` — that log records what went wrong; this
records why things are the way they are. Written as compact ADRs (Architecture
Decision Records): every decision carries its context, the alternatives
considered, and the consequences — including the uncomfortable ones.

**Status legend:** *Accepted* = in force. *Revisit trigger* states what would
legitimately reopen the decision.

---

## Index

| ID | Decision (one line) | Category | Status |
|----|---------------------|----------|--------|
| ADR-001 | Money is integer minor units + explicit currency; floats never enter the money path | Data | Accepted |
| ADR-002 | Ground truth lives in a local ledger; the stream stays detectable-from-data-alone | Data | Accepted |
| ADR-003 | Realism by mechanism: distributions emerge from modeled behavior, not decoration | Data | Accepted |
| ADR-004 | Three anomaly classes, each mirroring a real operational failure mode | Data | Accepted |
| ADR-005 | Card references are tokens; no PAN-shaped values anywhere (PCI out of scope by construction) | Data | Accepted |
| ADR-006 | Egress behind a one-method sink interface (Null / StdOut / EventHub) | Code | Accepted |
| ADR-007 | Installable src-layout package with a console entry point — not scripts, not notebooks | Code | Accepted |
| ADR-008 | Time and rates are injected dependencies (dual clocks, config-driven anomalies) | Code | Accepted |
| ADR-009 | Terraform owns Azure resources; Fabric Git integration owns Fabric items | Infra | Accepted |
| ADR-010 | Remote state backend; one hand-bootstrapped storage account (the documented paradox) | Infra | Accepted |
| ADR-011 | Key Vault uses Azure RBAC and role assignments, not legacy access policies | Infra | Accepted |
| ADR-012 | CI gates from week one: lint, tests, fmt — the badge is a machine-verified claim | Process | Accepted |

---

## Data & domain modeling

### ADR-001 — Money is integer minor units, never floats

**Context:** Amounts flow ingestion → KQL → PySpark aggregation → Power BI.
Binary floats cannot represent 0.10 exactly, and the error compounds across
millions of rows and currency sums.

**Decision:** amounts stored as `amount_minor: int` (cents/centimes/pennies)
with an explicit `currency` code on every event. Conversion to float/decimal
happens only at the presentation edge.

**Alternatives considered:** `float` (rejected — drift); `Decimal` (viable but
heavier when a fixed scale is known); strings (rejected — kills arithmetic).

**Consequences:** exact sums; currency-safe schema. Cost: display layers divide
by 100; Phase 3 tolerance rules operate in minor units (e.g. 1–2 minor units of
FX drift). **Revisit trigger:** never.

### ADR-002 — Ground truth lives in a local ledger, never in the stream

**Context:** the recon engine must be evaluated — did it find the breaks that
actually exist? Production streams never carry "I am a duplicate" flags, and
labels in the stream would make evaluation circular.

**Decision:** every emitted message is also appended to a local JSONL ledger
carrying `ground_truth` tags and cross-reference links (`duplicate_of_seq`,
`reversal_of_seq`). The outbound stream carries the clean domain message only.
The ledger is gitignored and local-only.

**Alternatives considered:** flags in the stream (circular evaluation,
unrealistic); a database for the ledger (operational overhead, no benefit at
this scale).

**Consequences:** enables precision/recall measurement of Phase 3 breaks; the
stream shape equals production shape. Costs: two consumers of one payload had
to be split at the emit boundary (caused D-01 in the debug log), and the ledger
can drift from the stream if code changes — guarded by the
`test_stream_payload_is_clean` / links tests. **Revisit trigger:** a second
consumer of the ledger → promote it to a proper store.

### ADR-003 — Realism by mechanism, not decoration

**Context:** synthetic data that is statistically plausible but behaviorally
fake (uniform amounts, flat traffic) produces a pipeline that looks right and
teaches nothing. Real cardholder data is unavailable by definition (PCI-DSS).

**Decision:** distributions emerge from modeled mechanisms: log-normal ticket
sizes per merchant, hourly diurnal weights, weekend multipliers, Zipf-weighted
repeat customers, and an expiry-driven card pool (~6% already expired) so ISO
response 54 declines *emerge from data* rather than from a coin flip.

**Alternatives considered:** uniform random (visibly fake in any dashboard);
canned datasets (no temporal dynamics, no story).

**Consequences:** Eventhouse dashboards will show real shapes (evening peaks,
long-tail amounts); declines have causes that Phase 3 matching can reason
about. Cost: the generator is more code than a random sketch — accepted,
because the generator *is* the project's data story. **Revisit trigger:**
per-merchant diurnal curves if the catalog grows.

### ADR-004 — Anomaly classes mirror operational failure modes

**Context:** a recon engine with nothing real to find is a demo, not a
project — but injected breaks must correspond to things that genuinely happen
in card operations.

**Decision:** three auth-side anomaly classes, each an operational event with a
downstream consequence: (1) duplicate send — terminal retransmission while
awaiting the issuer response; tests idempotent consumption. (2) late response
(codes 68/91) — issuer timeout makes settlement *uncertain*; the genuine
breaks generator. (3) full reversal (MTI 0410) — approved-then-reversed; must
never settle. Rates are config-driven and elevated in tests.

**Alternatives considered:** random field corruption (unrealistic, teaches
nothing); settlement-side anomalies (Phase 2 scope — planned, not a
replacement).

**Consequences:** each class maps 1:1 to a Phase 3 recon rule and a test; the
interview question "why these three?" has a crisp answer. Cost: the
late-response/reversal interplay needed an explicit ordering rule in
`step()`. **Revisit trigger:** Phase 2 adds settlement-side classes (missing
settlements, duplicates, FX drift, T+5 late arrivals).

### ADR-005 — No PAN-shaped data, ever

**Context:** anything resembling cardholder data drags the project into
PCI-DSS scope and makes public hosting reckless.

**Decision:** card references are `tkn_`-prefixed random hex tokens —
deliberately not Luhn-valid, not 16 digits, no PAN structure. BINs are random
draws in scheme prefix ranges (3/4/5/6); collisions with real BIN ranges are
possible and acceptable (a BIN alone is not sensitive).

**Alternatives considered:** realistic fake PANs (rejected — generates
Luhn-valid numbers indistinguishable from real cardholder data at a glance).

**Consequences:** the dataset can sit in a public repository safely, and the
design demonstrates knowing what *creates* PCI scope. Cost: Luhn cannot be
used as a sanity check downstream — irrelevant here. **Revisit trigger:** none.

---

## Architecture & code

### ADR-006 — Egress behind a one-method sink interface

**Context:** the same generator must run in three contexts: unit tests (no
network), dry-run debugging (human eyeballs), and the live demo (Event Hub).

**Decision:** egress sits behind a minimal `send(messages)` interface with
three implementations — `NullSink`, `StdOutSink`, `EventHubSink` — selected by
CLI flag. The Azure SDK import happens inside `EventHubSink` only.

**Alternatives considered:** environment-driven conditionals scattered through
the main loop; monkeypatching in tests.

**Consequences:** tests run offline and fast; the pattern is directly reusable
by Phase 2's settlement generator. Cost: one extra indirection layer — worth
it. **Revisit trigger:** Phase 2's file sink must keep the same one-method
interface.

### ADR-007 — Installable package, not scripts

**Context:** the default failure mode of data projects is script/notebook
spaghetti that cannot be tested, reviewed, or shipped.

**Decision:** `src/`-layout installable package (`authgen`) defined by
`pyproject.toml`, with a console entry point and a `[dev]` extra group.
Notebooks are reserved for exploration — so far, not needed at all.

**Alternatives considered:** loose scripts plus `requirements.txt` (legacy; no
entry point, no extras, duplicated metadata).

**Consequences:** `pip install -e ".[dev]"` delivers code, CLI, and tooling in
one command; tests import the package directly; CI has one canonical install
step. **Revisit trigger:** when Databricks notebooks arrive in Phase 2, the
same rule applies — logic lives in importable modules, notebooks stay thin
orchestrators.

### ADR-008 — Time and rates are injected dependencies

**Context:** two things make data generators untestable: hidden wall-clock
time and hard-coded rates. Demos additionally need compressed days; tests need
determinism.

**Decision:** time comes from an injected clock — `LiveClock` or
`SimulatedClock` (compresses 24h into N wall-clock minutes using
`time.monotonic()`, which is immune to NTP/DST jumps). All anomaly rates and
pacing come from a config dict that tests override with elevated rates and
fixed seeds.

**Alternatives considered:** inline `datetime.now()` (untestable); global
constants (a code edit for every experiment).

**Consequences:** deterministic, non-flaky tests; small samples are guaranteed
to contain every anomaly class; a 20-minute demo shows a full diurnal wave in
Power BI. No costs observed. **Revisit trigger:** if the simulated clock needs
intra-day event scheduling (reversals minutes later), add a scheduler
abstraction.

---

## Infrastructure

### ADR-009 — Terraform for Azure; Fabric Git integration for Fabric

**Context:** infrastructure-as-code from day one is a seniority requirement —
but the stack spans two control planes: classic Azure resources (Event Hub,
ADLS, Databricks, Key Vault) have a mature Terraform provider, while Fabric
items (Eventstream, Eventhouse, Data Activator) are not fully manageable in
standard Terraform.

**Decision:** split the control plane by what each natively supports. Azure
resources: Terraform with remote state. Fabric items: Fabric's built-in Git
integration (native JSON item definitions). Every resource's home is
documented.

**Alternatives considered:** ClickOps for Fabric "just this once" (the thin
end of the wedge — everything ends up unmanaged); third-party Fabric Terraform
providers (premature and fragile).

**Consequences:** everything is reproducible, each by its native mechanism.
Cost: two IaC workflows to learn — accepted, because that duality is itself
current, interview-valuable knowledge. **Revisit trigger:** Fabric Terraform
support is evolving; re-check before Phase 2.

### ADR-010 — Remote state backend, one hand-bootstrapped account

**Context:** Terraform needs remote state (locking, durability, no state in
git), but the storage account holding the state cannot create itself — the
bootstrap paradox.

**Decision:** one hand-created pair — `rg-tfstate` plus a random-suffixed
storage account with a `tfstate` container — provisioned once via CLI. The
backend block in `main.tf` points at it. Everything else, including future
state containers, goes through Terraform. State files are gitignored.

**Alternatives considered:** local state (no locking, single machine);
Terraform Cloud (external account dependency for a personal project).

**Consequences:** blob-lease locking prevents concurrent-apply corruption; a
clean interview answer for "how do you bootstrap IaC?". Cost: exactly one
manual artifact exists outside Terraform — documented and intentional.
**Revisit trigger:** a second consumer of the backend → add lifecycle rules.

### ADR-011 — Key Vault with RBAC, not access policies

**Context:** Key Vault has two permission models: legacy per-vault access
policies and Azure RBAC (recommended for new deployments, auditable at scope).

**Decision:** `rbac_authorization_enabled = true`; the deployer receives
`Key Vault Secrets Officer` at vault scope via a Terraform role assignment.
Secrets will be referenced by managed identity later — never by keys stored in
code.

**Alternatives considered:** access policies (legacy, per-vault, awkward to
audit, no PIM integration).

**Consequences:** modern and audit-friendly — consistent with how enterprises,
including banks, actually run Azure. Cost: RBAC propagation lag means an
occasional idempotent re-run of `apply` after first creation (watch list
W-01). **Revisit trigger:** when Phase 2 adds workload identities, the
workload gets `Key Vault Secrets User` — not the human.

---

## Process

### ADR-012 — CI gates from week one

**Context:** the project's own lesson N-06 — documentation and reality drift
apart the moment both are maintained by hand. A badge is a claim, and claims
must be machine-verified.

**Decision:** GitHub Actions on every push/PR: a python job (install package,
ruff, pytest) and a terraform job (`fmt -check -recursive`,
`init -backend=false`, `validate`). The credless `init` pattern lets CI
validate HCL without any cloud credentials.

**Alternatives considered:** "add CI when the project matures" (that day never
comes); lint-only CI (a green badge that proves nothing — explicitly rejected
after catching the stub, before it shipped).

**Consequences:** the badge is a true claim from the first week; every lesson
in the debug log is enforced by a gate, not by memory. Cost: first-push red
runs are possible (W-02) — acceptable; the red-to-green flip is part of the
story. **Revisit trigger:** Phase 5 adds CD — plan on PR, apply on merge
behind a manual approval gate.

---

## Open questions (deliberately deferred)

- **Phase 3 matching tolerances:** proposed defaults — 72h unsettled SLA,
  1–2 minor units FX drift tolerance — to be validated against the injected
  anomaly volumes, not chosen arbitrarily.
- **DST handling:** the synthetic catalog uses fixed UTC offsets. Acceptable
  for synthetic data; would be a genuine defect against real merchant data.
- **Phase 3 execution locus:** matching in Databricks over an Eventhouse
  export vs Kusto-native — pending what the current Fabric tier supports.
- **Settlement-side anomaly calibration (Phase 2):** target base rates so
  break proportions stay realistic rather than comically high.

---

*Related docs: `docs/debug-log.md` (failures and fixes) · `docs/failure-drills.md`
(Phase 4, planned) · README (architecture and runbook).*
