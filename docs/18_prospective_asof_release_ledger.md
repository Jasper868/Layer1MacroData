# Stage14.5A - Prospective As-Of Release Ledger

## Purpose

The Data repository already validates and publishes a stable Data-to-Research handoff.
Stage14.5A adds one controlled, append-only receipt per computer-local date. Each receipt
identifies the clean Git commit that already contains the exact handoff files and records
their hashes. It does not create indicators, features, scores, signals, backtests, or
portfolio actions.

## Why it exists

Several sources can revise history and the Data repository does not have a conventional
vendor vintage panel. A later copy of an old date cannot be assumed to equal the information
that existed on that old date. The receipt makes Git history usable as the cross-computer
vintage store:

```text
already committed Data release
-> clean HEAD verified
-> receipt records that HEAD and handoff hashes
-> receipt is committed and pushed
-> future Research reads exact files from the recorded Data commit
```

A local copy is also written beneath `data/archive/` as redundancy only. The version-controlled
receipt ledger plus Git history remains the cross-computer source of truth.

## Controls

1. Run only on `main` with a clean worktree.
2. The command uses `date.today()` only. It cannot backdate a receipt.
3. It validates the current manifest-verified Data release before writing.
4. One receipt is allowed per local date; no receipt or archive is overwritten.
5. Commit and push the updated ledger immediately.
6. `OK` and `WARN` releases can be recorded. A `WARN` receipt preserves a degraded data state;
   it is not permission to use stale data as a fresh signal.
7. Every output remains `NO_PORTFOLIO_ACTION`.

## Daily workflow on the Data computer

After the normal data update has been validated and committed:

```powershell
python scripts\record_prospective_asof_release.py --dry-run
python scripts\record_prospective_asof_release.py

git add data\meta\prospective_asof_release_ledger.csv
git diff --cached --check
git commit -m "Record prospective as-of Data receipt YYYY-MM-DD"
git push origin main
```

Do not record a second receipt on the same local date.

## Research implication

Stage16.9B must not treat a later live dataset as proof of what was known on T-20 or
T-5. A later Research integration must select the latest receipt on or before a target
session and use the recorded Data Git commit.
