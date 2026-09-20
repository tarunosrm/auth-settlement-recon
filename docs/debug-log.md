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
| T-01 | A test that could not fail (vacuous invariant check) | Test | 1 | 🔴 |
| T-02 | Self-contradictory duplicate test — could never pass | Test | 1 | 🔴 |
| E-01 | `az login` OTP loop / email-code catch-22 | Env | 0 | 🟡 |
| E-02 | venv not found after repo folder was recreated | Env | 1 | 🟡 |
| N-01 | Manual folder creation → nested clone risk | Near-miss | 0 | ⚪ |
| N-02 | PowerShell `>>` writes UTF-16 into `.gitignore` | Near-miss | 0 | 🟠 |
| N-03 | Prose line pasted into `.gitignore` snippet | Near-miss | 0 | ⚪ |
| N-04 | Rendered markdown copied instead of raw — doc structure lost | Doc | 1 | ⚪ |
| N-05 | Verification gate flagged the doc's own fingerprint example — false positive traced | Doc | 1 | ⚪ |
| N-06 | Documented fixes (T-01, D-04) never applied to code — linter caught both | Process | 1 | 🟠 |
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

**Impact if unfixed:** the Eventhouse table would contain anomaly labels, so the
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


### N-05 — Gate flagged the doc's own fingerprint example: false positive traced ⚪
**Phase**: 1 · **Found by**: the generator script's destination-verification gate

**Symptom**: verification FAIL — one &#x20; found in the generated doc, whilethe utf-8 round-trip check passed (so the entity was in the embedded source atcopy time, not introduced by writing).

**Investigation**: the failure locator printed the offending source line —which turned out to be this log's own N-04 entry, where the entity appearsdeliberately as an example of a corruption fingerprint. The chat-to-editorcopy of the Python block had contained zero injected entities; the transportwas clean. My initial hypothesis (whitespace-run entity injection by the chatclient) was plausible and wrong — the evidence overturned it.

**Fix**: an html.unescape() repair step plus an explicit "no entities ondisk" check; the self-referential example was reworded to prose ("HTML spaceentities") so the doc no longer carries a literal fingerprint that filters andgates will keep flagging.

**Lesson**: a verification gate reports a pattern match, not a cause — everyhit needs individual tracing before repair, or you "fix" your own exampletext. And content that describes its own failure signatures will trip thechecks built to catch those signatures; keep such examples out ofmachine-verified artifacts.

### N-06 — Fixes documented in this log but never applied to the code 🟠
**Phase: 1** · Found by: ruff check (SIM222 flagged T-01's or True;F841 flagged D-04's walrus)

**Root cause**: I wrote the fixes into this log during a documentationsession and never returned to the source files. The log recorded intent asif it were fact — the same failure mode as T-02's premature "11 passed"claim. A linter — a tool that never gets tired and never reads the log —caught both.

**Fix**: applied both edits, re-ran the full gate sequence(ruff check clean → pytest -q 11 passed) before committing.

**Lesson**: documentation drifts from code the moment both are maintained byhand. The only claims a log (or README, or dashboard) should make are ones amachine has verified on the current commit. This is precisely why CI gatesexist.
---


## Watch list (anticipated, not yet hit)

| ID | Risk | Planned response |
|----|------|------------------|
| W-01 | `terraform apply` fails on the Key Vault secret with an authorization error — Azure RBAC propagation lag (role assignment created seconds earlier) | Wait 2–3 min, re-run `apply` (idempotent — it resumes where it left off) |
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
5. **A gate hit is a hypothesis, not a verdict** (N-05): verification gatesflag patterns,
   not causes — trace each hit to its source before repairing,or you corrupt 
   the artifact while "fixing" it.
---

*Related doc: `docs/failure-drills.md` (Phase 4) — deliberate chaos experiments.
This log records accidental bugs; that one records intentional breakage. Both
feed interview stories.*
