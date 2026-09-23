# Debug Log — auth-settlement-recon

A running record of every bug, latent defect, and environment battle hit during
development. Each entry: what I saw, why it happened, the fix (with before/after
code), how I verified the fix, and the transferable lesson.

**Why keep this:** bugs are where the real engineering lessons live. This log is
my evidence of debugging discipline — symptom → hypothesis → fix → verification
→ prevention.

**Severity legend:**
🔴 shipped bug (caused wrong behavior) · 🟠 latent (would have bitten later) ·
🟡 environment/tooling · ⚪ near-miss (caught before impact)

---

## Index

| ID   | Title | Category | Phase | Sev |
|------|-------|----------|-------|-----|
| D-01 | Ground-truth labels leaked into the outbound event stream | Code | 1 | 🔴 |
| D-02 | First fix dropped `_emit`'s return contract | Code | 1 | ⚪ |
| D-03 | `authgen stats` ignores configured ledger path | Code | 1 | 🟠 |
| D-04 | Pointless walrus expression in distribution test | Code | 1 | ⚪ |
| D-05 | Malformed ci.yml edit broke CI entirely — no local gate validated the workflow file | Code | 1 | 🔴 |
| D-06 | Consumer crashed on the SDK's empty-wait heartbeat (`event=None`) | Code | 1 | 🔴 |
| D-07 | Sync `receive()` blocks forever — timeout was dead code, tool froze | Code | 1 | 🔴 |
| D-08 | Consumer handed the send-only credential — least-privilege worked, runbook didn't | Infra | 1 | 🔴 |
| D-09 | Workspace `identity` block: failed on locked provider AND unnecessary for KV-backed scopes | Infra | 1 | 🟠 |
| D-10 | Databricks Standard SKU deprecated — 400 mid-apply, partial apply absorbed by state | Infra | 1 | 🟠 |
| D-11 | RBAC-backed Key Vault checks every caller — including the Databricks control plane | Infra | 1 | 🔴 |
| T-01 | A test that could not fail (vacuous invariant check) | Test | 1 | 🔴 |
| T-02 | Self-contradictory duplicate test — could never pass | Test | 1 | 🔴 |
| E-01 | `az login` OTP loop / email-code catch-22 | Env | 0 | 🟡 |
| E-02 | venv not found after repo folder was recreated | Env | 1 | 🟡 |
| N-01 | Manual folder creation → nested clone risk | Near-miss | 0 | ⚪ |
| N-02 | PowerShell `>>` writes UTF-16 into `.gitignore` | Near-miss | 0 | 🟠 |
| N-03 | Prose line pasted into `.gitignore` snippet | Near-miss | 0 | ⚪ |
| N-04 | Rendered markdown copied instead of raw — doc structure lost | Doc | 1 | ⚪ |
| N-05 | Gate flagged the doc's own fingerprint example — false positive traced | Doc | 1 | ⚪ |
| N-06 | Documented fixes (T-01, D-04) never applied to code — linter caught both | Process | 1 | 🟠 |
| N-07 | Almost adopted the namespace Root key — a reviewer called it "standard practice" | Near-miss | 1 | ⚪ |
| N-08 | Ambiguous provider snippet placement — pasted at wrong nesting level | Near-miss | 1 | ⚪ |
| N-09 | Deprecation migration kept a now-invalid sibling argument — plan aborted | Near-miss | 1 | ⚪ |

---

## Code bugs

### D-01 — Ground-truth labels leaked into the outbound event stream 🔴

**Phase:** 1 · **File:** `src/authgen/generator.py` (`_emit`)

**Symptom (what I saw):** During a dry-run, the pretty-printed JSON — exactly
what would be sent to Event Hub — contained:

~~~json
"ground_truth": ["APPROVED"],
"seq": 1
~~~

Anomaly labels and internal sequence numbers must exist **only** in the local
ledger. The stream has to be detectable-from-data-alone, like production.

**Root cause:** `_emit` built **one** enriched dict and passed the same object
to both consumers — the ledger and the sink. Two consumers with different
confidentiality requirements, one object. Classic single-source-two-consumers
mistake.

**Impact if unfixed:** the analytics table would contain anomaly labels, so the
Phase 3 recon engine would "find" breaks by reading them — a circular, worthless
evaluation. Also, internal metadata bloat on every message in the stream.

**How caught:** eyeballing the dry-run output before wiring up Event Hub. This
is exactly why the dry-run sink exists.

**Fix (before):**

~~~python
def _emit(self, msg: AuthMessage, tags: list[str], **links) -> int:
    rec = msg.to_dict()
    rec["ground_truth"] = tags
    rec.update(links)
    seq = self.ledger.append(rec)
    self.sink.send([rec])          # ← same enriched object sent downstream
    return seq
~~~

**Fix (after):**

~~~python
def _emit(self, msg: AuthMessage, tags: list[str], **links) -> int:
    # Ledger: full-fidelity record with ground truth (oracle — never leaves this machine)
    seq = self.ledger.append({**msg.to_dict(), "ground_truth": tags, **links})
    # Stream: clean domain message only
    self.sink.send([msg.to_dict()])
    return seq
~~~

**Verification:** re-ran `authgen run --dry-run --max-events 200` — printed JSON
no longer contains `ground_truth` or `seq`; `authgen stats` still shows all tags
(ledger intact); intercepting test `test_stream_payload_is_clean` passes.

**Lesson:** when one object feeds two consumers with different contracts,
split at the boundary. And run a visual dry-run *before* connecting real
infrastructure — the leak was invisible in the ledger but obvious on the wire.

---

### D-02 — First fix dropped `_emit`'s return contract ⚪

**Phase:** 1 · **File:** `src/authgen/generator.py` (`_emit`)

**Symptom:** none — caught during self-review of the D-01 fix, before running.
Later independently confirmed by a second code review, which validated the entry.

**Root cause:** the first cut of the D-01 fix ended without `return seq`. But
callers use that return value to link derived events:

~~~python
seq = self._emit(msg, tags)
if approved and rng.random() < self.p_dup:
    self._emit(msg, tags + [...], duplicate_of_seq=seq)   # seq would be None
~~~

**Impact if unfixed:** no crash — worse. `duplicate_of_seq` /
`reversal_of_seq` would silently become `None`, corrupting the very
ground-truth links the ledger exists to provide. Silent link corruption in an
evaluation oracle is harder to spot than a crash.

**Fix:** re-traced the call graph and restored `seq = self.ledger.append(...)`
plus the `return seq` (shown in D-01 "after"). Added
`test_duplicate_and_reversal_links_are_intact` to guard the contract, since no
existing test covered the return value.

**Lesson:** a fix that changes a function's I/O behavior must be checked
against *every* caller, not just the buggy path. The most dangerous fixes fail
silently, not loudly. Note: Python does not enforce `-> int` at runtime — only
a test or static type-checker catches the violation, which is why the test
matters more than the annotation.

---

### D-03 — `authgen stats` ignores the configured ledger path 🟠

**Phase:** 1 · **File:** `src/authgen/cli.py` (`cmd_stats`) · **Status: open, fix planned**

**Symptom:** none yet — latent. Found during a documentation review pass.

**Root cause:** `cmd_run` reads `ledger_path` from `config.yaml`;
`cmd_stats` hardcodes the same file. Two code paths reading one logical file
through different mechanisms = drift waiting to happen. If I ever point config
at a different ledger, stats silently reports on the wrong (or an empty) file.

**Fix (planned):**

~~~python
# before
def cmd_stats(_args) -> None:
    ledger = Ledger("data/auth_ledger.jsonl")

# after
def cmd_stats(args) -> None:
    ledger = Ledger(args.ledger)

# and in main():
st.add_argument("--ledger", default="data/auth_ledger.jsonl")
~~~

**Lesson:** single source of truth for paths. Any constant appearing in two
places is a future bug with a delay timer.

---

### D-04 — Pointless walrus expression in a test ⚪

**Phase:** 1 · **File:** `tests/test_distributions.py`

**Before:**

~~~python
assert all(draw_code := decline_code(rng) in {"05", "51", "91", "57", "65"}
           for _ in range(50))
~~~

**After:**

~~~python
for _ in range(50):
    assert decline_code(rng) in {"05", "51", "91", "57", "65"}
~~~

**Root cause:** a walrus operator that assigns a boolean result is pure noise —
the name is never used. Cosmetic, but readability compounds.

**Lesson:** clever syntax with no payoff is negative value. Write tests for the
reader who debugs them at 2 a.m.

---

### D-05 — Workflow yaml syntax error disabled CI (caught by Actions, not locally) 🔴

**Phase:** 1 · **File:** `.github/workflows/ci.yml` (line 18, the pytest step edit)

**Symptom:** GitHub Actions run #2 failed with `Invalid workflow file ... yaml
syntax on line 18`; no jobs ran at all. Run #1 (Phase 0's file) had been green,
isolating the defect to the edit.

**Root cause:** the python job was edited by hand to add the install/pytest
steps. The malformed line passed every local check I ran — because **no local
check validates the workflow file**. Ruff lints Python, `terraform validate`
checks HCL, but the file that gates everything else had no gate. CI config is
code; it shipped unlinted.

**Investigation:** reproduced the parse error locally with PyYAML (already a
project dependency):

~~~powershell
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml', encoding='utf-8')); print('YAML OK')"
~~~

Same error, same line, zero cloud round-trips.

**Fix:** wholesale replacement with a known-good workflow, written to disk by a
verification script that parsed the yaml before commit; local parse green
before push; Actions green after (verified on run #3 before this entry was
committed — the log itself got gated).

**Lesson:** every artifact that gates the pipeline needs a gate of its own —
and "it parses" is a machine-checkable claim, so check it on the machine
before pushing. (Follow-up: consider `actionlint`, the standard GitHub
workflow linter, as a local pre-push step.)

---

### D-06 — Consumer crashed on the SDK's empty-wait heartbeat 🔴

**Phase:** 1 · **File:** `scripts/consume_check.py` (`on_event`)

**Symptom:** repeated `AttributeError("'NoneType' object has no attribute
'body_as_str'")` from both partitions; no events summarized.

**Root cause:** the azure-eventhub `receive(..., max_wait_time=N)` contract
calls `on_event(partition_context, None)` whenever the wait expires with
nothing to read — a heartbeat, not an error. My callback called
`event.body_as_str()` unguarded. Decoding the spam revealed two stacked
facts: the consumer had connected and authenticated cleanly (both partitions
claimed — the infrastructure worked), and the hub was empty because the
producer had not been run yet. My code crashed on the heartbeat instead of
hearing the message. Mental model: the callback is an assistant checking an
empty mailbox every 2 seconds; `None` is its "still nothing here" check-in,
proving it is awake.

**Fix:**

~~~python
def on_event(pc, event):
    if event is None:              # heartbeat: wait expired, nothing to read
        stats["idle_waits"] += 1
        return
    ...
~~~

plus a dedicated `on_error` handler and an `idle_waits` counter so "hub is
empty" is visible as a number.

**Verification:** superseded by D-08 — the first `read=200` run had not
actually been achieved when this was first logged (N-06 pattern recurring in
my own scaffolding). Confirmed only after D-08's credential fix:
`read=203, rejects=0`.

**Lesson:** read the callback contract before assuming inputs are always
non-None — "timeout delivered as a None argument" is a common SDK idiom. And
repeating error spam is often *signal*: this one was simultaneously a real
bug (unguarded callback) and a status report (healthy listener, empty hub).

---

### D-07 — Sync `receive()` blocks forever: the timeout was dead code 🔴

**Phase:** 1 · **File:** `scripts/consume_check.py`

**Symptom:** the tool connected, then sat silent past its 60-second "timeout"
— the summary line at the bottom of the script never ran.

**Root cause:** two SDK contracts, one honored and one ignored. The heartbeat
contract (D-06) was handled; the blocking contract was not:
`client.receive()` (sync SDK) does not return until the client is closed —
my deadline loop lived *after* that call, i.e. it was dead code by
construction. I had also conflated `max_wait_time` (callback cadence) with
total runtime. Caught by independent AI review; my D-06 "verification" claim
had been written before any successful run existed.

**Fix:** a watchdog thread owns the stop decision — it waits for the event
target or the timeout, then `client.close()`, which unblocks `receive()`;
`try/finally` guarantees the summary on every exit path (target, timeout,
Ctrl+C); a `threading.Lock` guards the shared counters, since partition
callbacks run on separate threads.

**Verification:** consumer prints its summary at target and at timeout;
empty hub self-diagnoses after 60 s instead of freezing.

**Lesson:** a blocking call needs an explicit stop story — timeout-as-code
(watchdog + close + finally), not timeout-as-wish (a loop placed after a
call that never returns). Dead code after a blocking call is worse than no
code: it documents an intent the runtime never honors.

---

### D-08 — Consumer handed the send-only credential 🔴

**Phase:** 1 · **Files:** runbook/env setup vs `eventhub.tf`

**Symptom:** consumer ran clean (no errors after D-06/D-07 fixes) but
`read=0` — then timed out. The hub had data (portal metrics showed ~200
incoming).

**Root cause:** the producer credential was created send-only
(`send=true, listen=false`) — correct least-privilege design — but the
runbook loaded that same secret (`eventhub-send-conn`) into
`EVENTHUB_CONNECTION_STRING` for the *consumer*. A consumer with a send-only
key cannot open a receive link. Credentials are capabilities; one secret
name had been carried across two operations with incompatible grants. The
security design was right; the operational instructions bypassed it.

**Fix:** a matching `listen-only` authorization rule in Terraform, its key
stored as `eventhub-listen-conn`, and a two-credential runbook: Send →
`eventhub-send-conn`, Listen → `eventhub-listen-conn`. A reviewer's quicker
fix — the namespace RootManageSharedAccessKey — was rejected (see N-07).

**Verification:** with the listen credential:
`read=203 rejects=0 idle_waits=0`; kinds `{AUTH: 200, REVERSAL: 3}`; codes
led by `00` (178) with `54`=8, `51`=5, `05`=4, `57`=4, `68`=2, `91`=2 —
every distribution matching the generator's design (89% approval, ~4%
expiry-driven declines).

**Lesson:** when a tool "sees nothing," audit the grant before the data
path — permission failures masquerade as empty queues. And least-privilege
is a system property: every new operation needs its own capability, or the
runbook silently widens the grant.

---

## Test bugs

### T-01 — A test that could not fail 🔴

**Phase:** 1 · **File:** `tests/test_generator.py`

**Symptom:** `test_stream_never_carries_ground_truth` passed — while D-01's
leak was live. The test was supposed to guard exactly the thing that was broken.

**Root cause (two compounding mistakes):**

1. It inspected the **ledger record** instead of the **sink payload** — testing
   the wrong artifact entirely.
2. The assertion was a tautology — it stripped the key, then asserted the key
   was absent, with an `or True` escape hatch guaranteeing a pass:

~~~python
visible = {k: v for k, v in r.items() if k != "ground_truth"}
assert "ground_truth" not in visible or True
~~~

A test that cannot fail is worse than no test: it manufactures false
confidence.

**Fix:** intercept the stream at the port boundary with a recording sink —
exactly where a downstream consumer would read:

~~~python
class RecordingSink:
    def __init__(self):
        self.messages = []
    def send(self, messages):
        self.messages.extend(messages)


def test_stream_payload_is_clean(tmp_path):
    """THE invariant: what a consumer receives must carry NO ground truth."""
    sink = RecordingSink()
    ledger = Ledger(tmp_path / "l.jsonl")
    gen = AuthGenerator(CFG, ledger, sink, rng=random.Random(7), clock=LiveClock())
    for _ in range(300):
        gen.step()
    assert sink.messages
    for m in sink.messages:              # stream side: clean
        assert "ground_truth" not in m
        assert "seq" not in m
    assert any("ground_truth" in r for r in ledger.iter_records())  # oracle intact
~~~

**Verification:** with the D-01 fix reverted, this test fails; with the fix in
place, it passes. Proved the test can fail — the meta-requirement T-01 was missing.

**Lesson:** every test should be proven fallible at least once (break the code
deliberately, watch the test go red). Assertions of the form `x or True` are a
code smell for "I wasn't sure what to assert."

---

### T-02 — Self-contradictory duplicate test: could never pass 🔴

**Phase:** 1 · **File:** `tests/test_generator.py` (`test_duplicates_share_auth_id`)

**Symptom:** first-ever run of the test suite: `1 failed, 10 passed` — the
failure was `assert all("DUPLICATE_SEND" in p["ground_truth"] for p in pair)`
→ False.

**Root cause:** two assertions in the same loop encoded incompatible specs:

~~~python
# before — two contradictory demands
assert all("DUPLICATE_SEND" in p["ground_truth"] for p in pair)                   # EVERY record tagged
assert sum("DUPLICATE_SEND" in p["ground_truth"] for p in pair) == len(pair) - 1  # EXACTLY ONE tagged
~~~

`all(...)` demands every record in a duplicate pair carry the tag; the next
line demands exactly one. Since an earlier assertion guarantees the loop always
executes when duplicates exist, the test could never pass. The generator was
correct — the tag deliberately marks only the *retransmission*, leaving the
original looking like a normal approval, which is what makes the ground-truth
ledger a meaningful oracle.

**Fix (after):**

~~~python
for pair in dup_pairs:
    assert len(pair) == 2, "each dup event emits exactly one retransmission"
    assert len({p["rrn"] for p in pair}) == 1          # identical retransmission
    tagged = [p for p in pair if "DUPLICATE_SEND" in p["ground_truth"]]
    assert len(tagged) == 1, "tag marks the retransmission only"
    dup_rec = tagged[0]
    original = next(p for p in pair if "DUPLICATE_SEND" not in p["ground_truth"])
    assert dup_rec["duplicate_of_seq"] == original["seq"]   # link points at the original
~~~

**Verification:** `pytest -q` → 11 passed. Mutation check: tagging both copies
in the generator made the test fail — proven it guards the contract.

**Lesson:** a test encodes a *specification*, and my spec contradicted itself.
When a test fails, the first question is "is the code wrong or my spec wrong?" —
here the code was right. Deeper lesson: I shipped a test suite I had never
executed. Together with T-01, this brackets the two failure modes of test
quality: a test must be *able to fail* and *able to pass* — only between those
poles does it carry information.

---

## Infrastructure & cloud bugs

### D-09 — Workspace `identity` block: wrong for the locked provider AND the auth model 🟠

**Phase:** 1 · **File:** `databricks.tf` · **Found by:** `terraform plan`

**Symptom:** `Blocks of type "identity" are not expected here` on
`azurerm_databricks_workspace`.

**Root cause:** two stacked layers. (1) Version lock: the snippet was valid
against a newer azurerm than the one my lock file pins — provider versions
are part of the code's contract, so snippets are only portable when the
version is. (2) The deeper error, mine: I assumed workload-identity-to-vault
RBAC and removed the planned role assignment, reasoning that runtime reads
bypass vault authorization entirely (see the correction below — that
reasoning was wrong for RBAC-backed vaults).

**Fix:** canonical pattern per Databricks' official Terraform examples — no
`identity` block, no workspace-identity role assignment; workspace + KV-backed
scope only. Caught by `plan` before any state change: zero cleanup, zero drift.

**Verification:** plan → clean; scope created; listed in workspace settings
as Azure Key Vault-backed.

**Lesson:** the plan gate converts config bugs from state surgery into
seconds. Verify synthesized resource blocks against `terraform providers`
and canonical vendor examples — especially anything touching auth.

> **CORRECTION (see D-11):** the claim that vault RBAC "isn't consulted
> per-read" was wrong. Scope ACLs complement vault authorization at a
> different layer; they do not replace it. Every fetch by the control-plane
> app IS RBAC-checked. Left as written for chronology; corrected in D-11.

---

### D-10 — Databricks Standard SKU deprecated: 400 mid-apply, partial apply absorbed 🟠

**Phase:** 1 · **File:** `databricks.tf` (`sku`)

**Symptom:** `terraform apply` created the consumer group, then failed on the
workspace: `DatabricksStandardSkuNotSupported ... Please use Premium SKU`.

**Root cause:** snippet skew — Standard was the long-standing default when
most guides were written; Microsoft deprecated it for new workspaces. Third
instance of guide-age vs cloud-current-state divergence (after D-09's
version lock and the ci.yml edit).

**Fix:** `sku = "premium"`. The partial apply needed no surgery: the
consumer group was already in state, and the next plan reconciled from
actual state — exactly what the remote-state backend (ADR-010) is for.

**Verification:** plan → 1 add; apply green; `workspace_url` output resolves.

**Lesson:** partial applies are the normal case — each resource commits or
fails independently, and the next plan reconciles. Cost note logged: Premium
DBUs ~2× Standard; at this project's usage (small cluster, minutes-long
demos) the delta is cents per session.

---

### D-11 — RBAC-backed Key Vault checks every caller, including the Databricks control plane 🔴

**Phase:** 1 · **Files:** Databricks notebook; `databricks.tf` · **Found by:** runtime error after D-09's fix

**Symptom:** `dbutils.secrets.get("eventhub", ...)` failed with
`PERMISSION_DENIED ... ForbiddenByRbac`. The error named the caller:
`name=AzureDatabricks;appid=2ff814a6-3304-4ab8-85cb-cd0e6f879c1d`.

**Root cause:** D-09's runtime model was wrong (see its correction notice).
On an RBAC-enabled vault, **every** caller is RBAC-checked — including the
Databricks control plane, which fetches secrets on behalf of notebooks using
its first-party Enterprise App (`AzureDatabricks`). That app had no role on
the vault, so the fetch was refused. What D-09 got right: the workspace
managed identity is not on this path, and scope *creation* is authorized
against the creator's vault permissions (which is why creation succeeded).
What it got wrong: presenting Databricks scope ACLs as a replacement for
vault authorization — ACLs gate which Databricks users may use a scope at
the Databricks API layer; they complement vault RBAC and never replace it.
Why the confusion is industry-wide: on legacy access-policy vaults, the
scope-creation flow historically granted the AzureDatabricks app Get/List
automatically, so the identity model stayed invisible. My vault was
RBAC-backed from day one (ADR-011) — the modern choice that exposed it.

**Fix:** `Key Vault Secrets User` granted to the AzureDatabricks app at
vault scope. Immediate unblock: Azure CLI role assignment. Codified in
Terraform via the azuread provider — role assignments take the service
principal's **object ID**, not the app ID, so a data lookup is required:

~~~hcl
data "azuread_service_principal" "databricks" {
  client_id = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
}

resource "azurerm_role_assignment" "databricks_app_kv_read" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = data.azuread_service_principal.databricks.object_id
}
~~~

**Verification:** error evidence confirmed (caller line + ForbiddenByRbac).
Secret-read re-verification **PENDING** — to be amended when
`dbutils.secrets.get` returns the secret after RBAC propagation
(N-06 discipline: no green claim before the green run).

**Lesson:** security boundaries are absolute, and error messages name the
caller — read the caller line before theorizing. Two AIs proposed two
architectures; one line of Azure's output adjudicated. Also: a reviewer's
correct diagnosis can still arrive with a fix worth rejecting (the suggested
quick test used the namespace Root key — rejected per N-07; the
least-privilege shape held).

---

## Environment & tooling

### E-01 — `az login` OTP loop / email-code catch-22 🟡

**Phase:** 0 · **Symptom window:** ~1 hour lost

**Symptom:** `az login` triggered an emailed one-time-code flow. Codes appeared
"13–14 minutes old" seconds after clicking Send. Worse: opening the account's
Security page (to fix the sign-in method) demanded the *same* code flow — a
catch-22.

**Root cause (three compounding factors):**

1. **Gmail conversation threading** — all code emails collapse into one thread;
   opening it showed the *oldest* code, not the newest (which sits at the bottom).
2. **Re-clicking "Send code" invalidates the previous code** — retrying in
   frustration actively sabotaged each attempt.
3. The account was **passwordless**, so *every* sensitive page demanded an
   emailed code, and delayed mail delivery made short-lived codes arrive
   nearly expired.

**Fix:**

- Immediate: opened the login URL in an **incognito tab** and used the freshest
  code (bottom of the Gmail thread) — succeeded.
- Permanent: added a **password** to the Microsoft account
  (account.microsoft.com → Security → Change how I sign in) so `az login` no
  longer depends on emailed codes. CLI now caches the credential after first login.
- Discipline for any future code flow: delete old code emails first, click Send
  **exactly once**, read the **newest** message in the thread.

**Verification:** `az account show` returns the subscription; subsequent
`az login` runs are silent.

**Lesson:** when auth tooling fights you, change the *flow*, not the retry
count. Reuse an existing session, then permanently remove the broken dependency.

---

### E-02 — venv not found after repo folder was recreated 🟡

**Phase:** 1 · **Paths:** `D:\Projects\card-auth-settlement-recon\.venv`

**Symptom:** `.\.venv\Scripts\Activate.ps1` → "not recognized as a cmdlet..."

**Root cause:** the virtual environment had been created before the repo folder
situation was finalized — a venv is just a folder of scripts bound to its
**absolute path**. Recreate or rename the folder and the old venv is orphaned.

**Fix:**

~~~powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"     # reinstall — the editable install is also path-bound
~~~

**Lesson:** a venv (and an editable install) is tied to its path — cheap to
recreate (2 minutes), never worth debugging. Recreate both *together* whenever
the project directory changes.

---

## Near-misses (caught before impact)

### N-01 — Manual folder creation → nested clone risk ⚪

**Phase:** 0

**What almost happened:** created `D:\Projects\card-auth-settlement-recon`
manually, then planned to `cd` inside and run `git clone <url>`. Clone creates
its own folder — this would have produced
`card-auth-settlement-recon\auth-settlement-recon`, with a confusing name
mismatch against the repo (`auth-settlement-recon`).

**Fix:** ran the clone from the parent directory and let git create the folder
(or `git clone <url> <target-name>` to choose the name explicitly).

**Lesson:** know which commands *create* things (`git clone`, `python -m venv`,
`terraform apply`) versus which *assume* they exist. Zero cost when caught
pre-execution.

---

### N-02 — PowerShell `>>` writes UTF-16 into `.gitignore` 🟠

**Phase:** 0 · **File:** `.gitignore`

**What would have happened:** `echo "data/" >> .gitignore` in Windows
PowerShell 5.1 writes **UTF-16 LE** (null bytes between characters). The file
looks fine in any editor, but git's gitignore parser reads UTF-8 and silently
fails to match the pattern — so `data/auth_ledger.jsonl` would eventually show
up in `git status` as untracked despite being "ignored."

**Fix:** `Add-Content .gitignore "data/"` (writes plain text).

**Verification:**

~~~powershell
git check-ignore -v data/auth_ledger.jsonl
# prints the matching rule line if the pattern actually works; silence = broken
~~~

**Lesson:** silent encoding mismatches between shell and tool are the worst
kind of bug — nothing errors, everything just quietly doesn't work. Verify
ignore rules with `git check-ignore`, not with hope.

---

### N-03 — Prose line nearly pasted into `.gitignore` ⚪

**Phase:** 0

**What almost happened:** an instruction snippet for the Terraform ignore rules
contained an explanatory prose line ("the lock file should be committed...").
Pasted verbatim, git would treat it as a literal never-matching pattern —
harmless but sloppy, and confusing to any future repo reader.

**Fix:** only the four real patterns entered the file (`.terraform/`,
`*.tfstate`, `*.tfstate.*`, `*.tfvars`); the explanation stayed in prose, and a
note was added that `.terraform.lock.hcl` must **not** be ignored.

**Lesson:** never paste instruction text blind. Read every line that lands in
version control as if a reviewer will read it — because one day, one will.

---

### N-04 — Rendered markdown copied instead of raw — document structure lost ⚪

**Phase:** 1 · **File:** `docs/debug-log.md`

**Symptom:** this log's index rendered as plain lines instead of a table; code
blocks displayed as flattened prose; underscores appeared escaped
(`\_emit`, `\_\_init\_\_`).

**Root cause:** the document was copied from a *rendered* view rather than the
raw markdown source. Rendering consumes structural syntax (table pipes, heading
hashes, code fences) and escapes characters like underscores. Markdown is a
source format — only the raw text is the artifact. The corruption also survived
one "clean" copy because it entered upstream of that copy: every intermediate
app in a copy chain is a potential lossy transformer, and a copy button only
guarantees the last hop. Final resolution: transport the content inside a
Python script (a format that survives the channel) and write the file to disk
programmatically — removing clipboard rendering from the pipeline entirely.

**Fix:** replaced the file wholesale from generated raw source and verified at
the destination — markdown preview (Ctrl+Shift+V in VS Code), plus automated
signature checks for corruption fingerprints (HTML space entities, escaped
underscores, missing pipes).

**Lesson:** documents are artifacts too — verify them the way you verify code,
at the destination, not at the source. When a transport channel corrupts a
format, stop retrying the channel and change the encoding instead.

---

### N-05 — Gate flagged the doc's own fingerprint example: false positive traced ⚪

**Phase:** 1 · **Found by:** the generator script's destination-verification gate

**Symptom:** verification FAIL — one HTML space entity found in the generated
doc, while the utf-8 round-trip check passed (so the entity was in the
embedded source at copy time, not introduced by writing).

**Investigation:** the failure locator printed the offending source line —
which turned out to be this log's own N-04 entry, where the entity appears
*deliberately* as an example of a corruption fingerprint. The chat-to-editor
copy of the Python block had contained zero injected entities; the transport
was clean. My initial hypothesis (whitespace-run entity injection by the chat
client) was plausible and wrong — the evidence overturned it.

**Fix:** an `html.unescape()` repair step plus an explicit "no entities on
disk" check; the self-referential example was reworded to prose ("HTML space
entities") so the doc no longer carries a literal fingerprint that filters
and gates will keep flagging.

**Lesson:** a verification gate reports a pattern match, not a cause — every
hit needs individual tracing before repair, or you "fix" your own example
text. And content that describes its own failure signatures will trip the
checks built to catch those signatures; keep such examples out of
machine-verified artifacts. (This entry itself briefly carried the literal
fingerprint again during editing and had to be reworded — the lesson is
load-bearing.)

---

### N-06 — Fixes documented in this log but never applied to the code 🟠

**Phase:** 1 · **Found by:** `ruff check` (SIM222 flagged T-01's `or True`;
F841 flagged D-04's walrus)

**Root cause:** I wrote the fixes into this log during a documentation
session and never returned to the source files. The log recorded intent as
if it were fact — the same failure mode as T-02's premature "11 passed"
claim. A linter — a tool that never gets tired and never reads the log —
caught both.

**Fix:** applied both edits, re-ran the full gate sequence (`ruff check`
clean → `pytest -q` 11 passed) before committing.

**Lesson:** documentation drifts from code the moment both are maintained by
hand. The only claims a log (or README, or dashboard) should make are ones a
machine has verified on the current commit. This is precisely why CI gates
exist.

---

### N-07 — Almost adopted the namespace Root key ⚪

**Phase:** 1

**What almost happened:** after D-08, a reviewer's quick fix proposed using
the namespace `RootManageSharedAccessKey` for the consumer, calling it
"standard practice" for testing. Root grants Manage+Send+Listen over the
entire namespace; "temporarily root" is the seed of "permanently root."

**Fix:** rejected. The principled fix cost eight lines of Terraform and
ninety seconds (D-08's `listen-only` rule).

**Lesson:** convenience fixes that erode a security invariant aren't
shortcuts — they're the incident, pre-enacted. Reviewers catch bugs; you
still own the security review of their fixes.

---

### N-08 — Ambiguous provider snippet placement ⚪

**Phase:** 1 · **File:** `main.tf` · **Found by:** `terraform plan`

**What almost happened:** a provider-requirements snippet was given without
stating it must nest inside `required_providers {}`; the natural reading
placed it at the wrong level (`Unsupported argument "databricks"`).
Recurred same session as N-09 — incomplete snippet context is a recurring
guide-failure mode, now tracked as a pattern.

**Fix:** moved the block inside `required_providers`; ran `terraform init`
to fetch the new provider before planning.

**Lesson:** instructions that omit "where" are half instructions. When
applying a snippet, ask what structural context it assumes — nesting,
imports, ordering — before pasting.

---

### N-09 — Deprecation migration left a now-invalid sibling argument ⚪

**Phase:** 1 · **File:** `eventhub.tf` · **Found by:** `terraform plan`

**Symptom:** plan aborted with `Invalid combination of arguments` —
`namespace_id` and `resource_group_name` cannot coexist.

**Root cause:** migrating `namespace_name` → `namespace_id` replaced the
argument but left `resource_group_name` in place. The migration instruction
showed the replacement without stating the required deletion (N-08's
family, recurring); deprecation messages name the successor, never the
siblings that must go. The deeper shape: an ARM resource ID *contains* the
resource group and subscription — a separate `resource_group_name` isn't
redundant but contradictory if the two ever disagree, so the provider
encodes that as an ExactlyOneOf validation.

**Fix:** deleted `resource_group_name` from every block using
`namespace_id` (hub, both consumer groups, both auth rules).

**Verification:** plan → 2 add, 0 destroy, no replacement of existing
resources; apply green (typed `yes` manually — no `-auto-approve`).

**Lesson:** treat a deprecation as a schema migration: the replacement
argument changes which other arguments are legal. Provider validation
errors are the schema teaching you the resource's real shape.

---

## Watch list (anticipated, not yet hit)

| ID | Risk | Planned response |
|----|------|------------------|
| W-01 | `terraform apply` fails on the Key Vault secret with an authorization error — Azure RBAC propagation lag (role assignment created seconds earlier) | Wait 2–3 min, re-run `apply` (idempotent — it resumes where it left off). A variant materialized as D-11: propagation-window failure on a role assignment — for a principal nobody predicted |
| W-02 | CI fails `terraform fmt -check -recursive` on first push | Run `terraform fmt` locally, commit — the loop itself teaches the gate |
| W-03 | Bare `authgen run` without `EVENTHUB_CONNECTION_STRING` raises a raw `KeyError` | Hardening: catch and print a friendly message pointing to env vars / `--dry-run` |

---

## Patterns across my bugs (the meta-lesson)

1. **One object, two consumers** (D-01): whenever a single payload serves
   audiences with different contracts, split it at the boundary.
2. **Tests that mirror the bug instead of guarding against it** (T-01):
   circular assertions manufacture confidence without providing any. Prove
   every test can fail.
3. **Windows tooling silent defaults** (N-02, E-02): encoding and path-binding
   behaviors fail silently. Verify with tools (`git check-ignore`), not with
   assumptions.
4. **Contradictory specs inside tests** (T-02): assertions are requirements
   documents — two conflicting requirements means at least one is wrong, and
   the test run is where that gets discovered.
5. **A gate hit is a hypothesis, not a verdict** (N-05): verification gates
   flag patterns, not causes — trace each hit to its source before
   repairing, or you corrupt the artifact while "fixing" it.
6. **Recurring errors are status messages in disguise** (D-06): before
   muting spam, decode what it is repeating — it may be reporting the very
   condition you are hunting.
7. **Timeouts are mechanisms, not wishes** (D-07): bound blocking APIs with
   code that can actually stop them (watchdog + close + finally); never
   place intended logic after a call that never returns.
8. **AI-scaffolded code gets the same gates as human code** (D-06 through
   D-11): six defects shipped in AI-generated scaffolding — including one
   wrong architectural explanation that entered this log itself; all were
   caught by independent AI review or machine gates, not by hope. No
   "Verification:" claim is written before the run has actually happened —
   including claims made by the assistant.
9. **Every credential is a capability** (D-08, N-07): each operation needs
   its own grant; treat any suggestion to widen a grant as a bug in the
   suggestion.
10. **Plan is a gate, not a formality** (D-09, N-08, N-09): config errors
    caught by `plan` cost seconds; `apply` errors cost state. Read the plan
    as a review artifact — and never `-auto-approve` past your own review.
11. **Cloud services have expiry dates, and guides don't** (D-09, D-10):
    provider versions and SKU lifecycles move faster than documentation.
    Treat every guide — official, AI, or blog — as written for a moment
    that may have passed; check against the lock file and the API's current
    answer.
12. **Error messages name the caller** (D-11): authorization failures
    identify the exact principal that was refused — read that line before
    any architecture theory. Two confident explanations disagreed; one line
    of Azure output settled it.
13. **When patch count and state uncertainty rise together, regenerate**
    (N-04, and this log's own update tooling): a patch script needs every
    anchor to match a document seen only through lossy transports; a
    regeneration needs none. The second failure of the same class is the
    signal to switch strategies, not to sharpen the anchor.

---

*Related doc: `docs/failure-drills.md` (Phase 4) — deliberate chaos experiments.
This log records accidental bugs; that one records intentional breakage. Both
feed interview stories.*
