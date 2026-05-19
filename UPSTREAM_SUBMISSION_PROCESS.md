# Upstream submission process

Authoritative checklist for every issue, MR, or patch we send to
`gitlab.gnome.org/GNOME/orca`. Born out of two consecutive
misattributed root causes (issues #711 and #712) where I reasoned
from our local perf branch's modified source instead of upstream's
canonical source.

The single rule that prevents every failure mode in those cases:

> **Develop and verify patches against `upstream/main` exclusively.
> Only apply to the perf branch after verification.**

Everything below is the operational expansion of that rule.

## The process

### 1. Reproduce against stock upstream first

Before claiming any bug, reproduce it against a clean checkout of
`upstream/main` — not the perf branch, not the installed distro
Orca with our patches sideloaded, not our dev tree with `sd-piper`
running.

- `git worktree add /tmp/orca-stock upstream/main` gives an
  isolated working copy at the upstream HEAD without touching the
  perf branch.
- Build it with a separate `meson` prefix so the binary doesn't
  conflict with `~/.local/bin/orca` from the perf branch.
- Use **stock speech-dispatcher modules only** (espeak-ng, plus
  whatever ships with the distro). Don't load `sd-piper` or any
  other custom module — those changes belong in the perf branch's
  symptom-finding column, not in upstream bug reports.

If the bug doesn't reproduce on stock upstream, the bug is in our
perf branch (or in `sd-piper`, or in a config drift) — not in
upstream. Stop. Don't file.

### 2. Read upstream source for every named function in the analysis

When the analysis names a function, **open that function in the
upstream tree and read it.** Not in the perf branch. Not from
memory. Not from a `grep` against the working copy.

```sh
git show upstream/main:src/orca/<file>.py | sed -n '<line>,<line>p'
```

This was the proximate cause of both #711 and #712:

- #711: I claimed `Script.activate()` fires on focus moves within
  the prefs dialog. The upstream `set_active_script()` early-returns
  on unchanged scripts, so it doesn't. I never read upstream's
  `set_active_script()` before writing the analysis.
- #712: I claimed `_prune_hung_objects()` iterates `HUNG_OBJECTS`
  directly and races with concurrent writes. Upstream's version
  uses `list(...)` to snapshot the keys, so it doesn't. I read our
  perf branch's modified version (which uses `.items()` and does
  need a lock) and projected its semantics onto upstream.

Every function mentioned in the "Root Cause" section of an issue
must have been read from `upstream/main` before the issue is
filed.

### 3. Develop the patch on a fresh branch from upstream/main

```sh
git checkout -b upstream-fix/<short-name> upstream/main
# ... edits ...
# ... unit tests ...
```

Never edit the perf branch and then `cherry-pick` to upstream. The
perf branch carries state (caches, lock disciplines, refactored
call sites) that the patch may unknowingly depend on. The patch
must apply cleanly to a tree that has *only* upstream's state.

### 4. Verify the patch on the upstream-only tree

- `git apply --check 0001-<subject>.patch` against `upstream/main`
  must exit 0.
- `python3 -m pytest tests/unit_tests/test_<module>.py` must pass
  on the upstream-only tree.
- Manually run the modified Orca through the bug's reproducer
  steps and confirm the fix.

This is the line `48fde5fc0e9` failed — the patch worked on the
perf branch (where it solved a problem only the perf branch had)
but the bug it claimed to fix didn't exist on upstream.

### 5. Write the issue/MR body from the upstream-only tree's vantage

The "Steps to Reproduce" section describes what happens on stock
upstream. The "Root Cause" section quotes upstream code, not our
modified code. The "Proposed Fix" section is a diff against
upstream.

If the issue can't be reproduced on stock upstream, the body needs
to either:
- Be honest that this is a code-inspection finding (state it
  plainly, accept that it may be declined), or
- Not be filed.

### 6. Only after the patch is accepted (or rejected) do we touch perf

If accepted: rebase the perf branch on upstream's new HEAD; drop
any of our parallel commits that the upstream fix supersedes;
update BRANCH_INFO.md.

If rejected: leave the perf branch as-is. Our local fix may still
be the right thing for our setup; upstream's reasons for declining
are documented on the issue.

Never the reverse direction: never write a patch on perf and then
try to retrofit it as an upstream submission. That's how both
#711 and #712 happened.

## Checklist (paste into each submission's prep doc)

- [ ] Bug reproduces against a clean `upstream/main` checkout
- [ ] Bug does NOT reproduce only because of perf-branch or
      `sd-piper` modifications
- [ ] Every function named in the analysis has been read from
      `upstream/main`, not the perf branch
- [ ] Patch developed on a branch forked from `upstream/main`
- [ ] `git apply --check` passes against `upstream/main`
- [ ] Unit tests on the upstream-only tree pass
- [ ] Manual repro confirms the fix on the upstream-only tree
- [ ] Issue body cites upstream code, not perf-branch code

## When to skip the upstream-tree workflow

Never, for upstream submissions. Even "obvious" one-line fixes.
The two issues we got wrong were both ostensibly "obvious" —
that's exactly why they were filed without the workflow.

Local-only changes to the perf branch (caches, refactors, items
we don't intend to upstream) don't need this process. The process
is gated on intent-to-submit, not on the technical content.
