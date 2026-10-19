# CodeGuardian — Git Workflow Map

A practical reference for working in this repository. Not a general
git tutorial. Every command here is one you will actually use.

Open this file at the start of every session. Look up the situation
you are in. Run the commands. Close the file.

---

## 1. The decision tree

Start here. Match your situation. Follow the commands.

```
What am I trying to do?
│
├─► Start a new phase of work
│   └─► Section 2.1 — Start a feature branch
│
├─► Save my current work
│   └─► Section 2.2 — Commit and push
│
├─► Finish a phase and merge it
│   └─► Section 2.3 — Finish a branch
│
├─► Mark a version for release
│   └─► Section 2.4 — Tag a release
│
├─► Revert a change that broke something
│   └─► Section 3.1 — Revert a bad commit
│
├─► Find which commit broke a test
│   └─► Section 3.2 — Bisect
│
├─► I lost a commit / messed up history
│   └─► Section 3.3 — Recovery with reflog
│
├─► Pre-commit is failing
│   └─► Section 4 — Pre-commit troubleshooting
│
└─► Something broke and I do not know what
    └─► Section 5 — "I broke something"
```

---

## 2. Everyday workflows

### 2.1 — Start a feature branch

Every phase of work happens on a branch. `main` is always the last
known-good state.

**Why:** if Phase 9 goes badly, you delete the branch. `main` never
saw it. No history rewriting. No fixes.

```bash
cd /mnt/c/Users/arun0/Videos/CodeGuardian

# Make sure you are on main and up to date
git checkout main
git pull

# Create and switch to a new branch
git checkout -b phase-9-plugins
```

**Name convention:** `<phase>-<short-description>`. Examples:
- `phase-9-plugins`
- `phase-10-prompts`
- `fix-cross-file-blast`
- `test-add-language-fixture`

**Verify you are on the right branch:**

```bash
git branch --show-current
# should print: phase-9-plugins
```

Every commit until you explicitly switch back happens on this branch.
`main` is untouched.

### 2.2 — Commit and push

The standard pattern in this repo. Pre-commit is installed and
`.gitattributes` is set. This is what works:

```bash
# Stage everything (this is the only way that avoids the
# stash/restore conflict pre-commit causes with partial staging)
git add -A

# Commit — pre-commit runs automatically
git commit -m "feat(plugins): add plugin loader infrastructure"

# Push — the first time on a new branch, use -u
git push -u origin phase-9-plugins
```

**After the first push of a branch**, later pushes are just:

```bash
git push
```

**Commit message format** (this is the standard the repo uses):

```
<type>(<scope>): <short description>

[optional body]

[optional footer]
```

Types: `feat`, `fix`, `docs`, `test`, `build`, `chore`, `refactor`, `perf`
Scopes for this repo: `plugins`, `kernel`, `pyo3`, `blast`, `sandbox`, `audit`, `providers`, `storage`, `state`

Examples from this repo's history:
- `feat(kernel): add LanguageModule dispatch for 8 languages`
- `fix(errors): map json and tomllib decode errors to CONFIG_PARSE_ERROR`
- `docs(state): record multi-language phase`
- `build: add gitattributes to normalize line endings to LF`

### 2.3 — Finish a branch

When the deep test passes and the phase is complete:

```bash
# Make sure everything is committed
git status
# should print "nothing to commit, working tree clean"

# Push the final state
git push

# Go to GitHub → the repo → Pull Requests → New Pull Request
# Base: main    Compare: phase-9-plugins
# Add a title and description
# Review the "Files changed" tab — this is the diff that will enter main
# Merge when satisfied
```

**What happens after merge:** the `main` branch now has all Phase 9
work. The `phase-9-plugins` branch can be deleted.

```bash
# Locally
git checkout main
git pull
git branch -d phase-9-plugins

# On GitHub: the PR page has a "Delete branch" button after merge
```

### 2.4 — Tag a release

Tags mark a specific commit forever. `v1.0.0` should be tagged on the
commit that was the multi-language phase's final state.

```bash
# See which commit you are on
git log --oneline -3

# Tag the current commit
git tag -a v1.0.0 -m "First working multi-language kernel (8 languages)"

# Push the tag to GitHub
git push origin v1.0.0

# List all tags
git tag -l
```

**When to tag:** after a phase boundary where the deep test passed.
Recommended tag sequence for this project:

| Tag | When | Description |
|---|---|---|
| `v1.0.0` | After multi-language phase (now) | 8-language kernel, deep test green |
| `v1.1.0` | After Phase 9 | Plugin system |
| `v1.2.0` | After cross-file blast fix | Full-language blast radius |
| `v1.3.0` | After Phase 11 | Agents working end to end |
| `v2.0.0` | After Phase 13 + 14 | Orchestrator and CLI |

**Check out an old tag to see how things worked:**

```bash
git checkout v1.0.0    # you are now on the v1.0.0 commit
git checkout main      # back to the tip of main
```

---

## 3. Recovery — when things go wrong

### 3.1 — Revert a bad commit

A commit was made and pushed and now breaks something.

**Option A — if the commit is on `main` and other people may have pulled:**

```bash
# Find the commit
git log --oneline -10

# Revert it — creates a NEW commit that undoes the bad one
git revert <commit-hash>

# Push the revert
git push
```

`revert` does not rewrite history. It adds a new commit. Everyone who
pulled sees the fix. This is the safe choice.

**Option B — if the commit is only on your branch and never pushed:**

```bash
# Undo the last commit, keep the changes staged
git reset --soft HEAD~1

# Or: undo the last commit, unstage the changes
git reset HEAD~1

# Or: nuke the last commit and all its changes (dangerous)
git reset --hard HEAD~1
```

**`--soft`** keeps the changes staged. **No flag** unstages but keeps
the files. **`--hard`** deletes the changes. Use `--hard` only when
you are certain you want the work gone.

**Option C — undo the last 3 commits but keep the code:**

```bash
git reset --soft HEAD~3
# All changes from those 3 commits are now staged, ready for one new commit
git commit -m "combined message"
```

### 3.2 — Bisect: find which commit broke a test

A test used to pass and now fails. You have no idea which commit did it.

```bash
# Make sure everything is committed first
git status
# If anything is not committed, commit it or stash it
# (bisect checks out old commits — it needs a clean working tree)

# Start the bisect
git bisect start

# Tell it the current state is bad
git bisect bad

# Tell it a known-good state — use a tag or commit hash
git bisect good v1.0.0

# Git checks out a commit in the middle
# Now run the test that fails:
PYTHONPATH=python python3 -m pytest tests/unit/rust/test_kernel_multilang.py -q

# If the test PASSES on this commit: this commit is good
git bisect good

# If the test FAILS on this commit: this commit is bad
git bisect bad

# Git keeps checking out middle commits, halving the range each time
# After ~log2(number_of_commits) steps, git prints:
# "<hash> is the first bad commit"

# See the bad commit's diff
git show <hash>

# Return to your normal branch
git bisect reset
```

**Time saved:** 40 commits takes 6 steps. 400 commits takes 9 steps.
Do not read diffs by hand.

### 3.3 — Recovery: I lost a commit

You ran a bad `reset --hard`, a bad `rebase`, or a `filter-branch`.
You think the work is gone. **It is not.** Git keeps everything for
90 days.

```bash
# See every position HEAD has ever been
git reflog

# Output looks like:
# abc1234 HEAD@{0}: reset: moving to HEAD~3
# def5678 HEAD@{1}: commit: feat: my lost work
# 9012abc HEAD@{2}: commit: fix: previous work

# The commit at HEAD@{1} has your work
git reset --hard HEAD@{1}

# You are back to where you were before the mistake
```

**The rule:** when in doubt, `git reflog`. Never panic. Nothing is
ever truly lost within 90 days.

**If even reflog is not enough** — a truly unreachable commit — search
by content:

```bash
git fsck --lost-found
# prints any dangling commits
# each is a commit hash you can inspect with git show <hash>
```

### 3.4 — "I'm on the wrong branch and have uncommitted changes"

You did work while on `main` but meant to be on a branch.

```bash
# Save your changes
git stash

# Create the correct branch
git checkout -b phase-9-plugins

# Restore your changes onto the new branch
git stash pop
```

Your uncommitted work is now on the right branch. `main` is clean.

---

## 4. Pre-commit troubleshooting

Pre-commit runs on every `git commit`. When it fails, it prints a
reason. Here is what each failure means and what to do.

### 4.1 — "files were modified by this hook"

This is not an error. It means the hook fixed something (trailing
whitespace, EOF newline, line endings, formatting) and you need to
re-stage and re-commit.

```bash
# Look at what changed
git diff

# Re-stage the fixed files
git add -A

# Commit again
git commit -m "your message"
```

The second commit will pass because the files are now clean.

### 4.2 — "Rolling back fixes... Restored changes from..."

This is the stash/restore conflict that happened in this session.
It happens when pre-commit tries to fix files but conflicts with
unstaged changes.

**The cause:** some files are staged (`git add`) and some are not.
pre-commit cannot cleanly restore the unstaged ones after fixing
the staged ones.

**The fix:** always commit everything at once.

```bash
git add -A
git commit -m "message"
```

Or, if you must commit a subset:

```bash
git add file1.py file2.py
git stash --keep-index    # hide the unstaged changes
git commit -m "message"
git stash pop             # bring them back
```

### 4.3 — Real errors from ruff

Ruff catches actual Python problems: unused imports, unused variables,
shadowed names, unreachable code. These are not formatting.

**Example from this session:**

```
tests/unit/rust/test_blast_multilang.py:422:9: B007 Loop control
variable `language` not used within loop body
```

That means a `for language, suffix, target in [...]` loop uses `suffix`
and `target` but never uses `language`. Real problem — a variable is
declared and never used.

**Fix:** either use it, or rename with an underscore to signal "I know
this is unused":

```python
for _language, suffix, target in [...]:
    ...
```

Then re-stage and re-commit.

### 4.4 — Bypass pre-commit

Sometimes you need to commit without pre-commit. Legitimate cases:

- The commit is only `.gitattributes` or other config that the hooks
  do not apply to
- Pre-commit is broken on your machine and you need to push work now
- You already ran the hooks manually with `pre-commit run --all-files`
  and everything is clean

```bash
git commit --no-verify -m "message"
```

`--no-verify` skips pre-commit entirely. Use it sparingly. If you skip
it because of a real ruff error, that error enters `main`.

### 4.5 — Run pre-commit manually

Before committing, check whether the hooks will pass:

```bash
pre-commit run --all-files
```

**Expected output when everything is clean:** every hook reports
`Passed` or `Skipped`, with no `files were modified`.

**If it reports modifications:** run it again. It should be idempotent
after one pass. If the second pass still modifies files, something is
wrong (usually `.gitattributes` is missing or misconfigured).

---

## 5. The five situations that break git

### 5.1 — Mixed line endings (what happened this session)

**Symptom:** every commit fails, pre-commit says "mixed line ending"
or "fixed mixed line endings."

**Cause:** WSL2 reading a Windows filesystem. Some files have LF, some
have CRLF. Git sees them as different bytes.

**The fix is permanent** — `.gitattributes` is in the repo:

```
* text=auto eol=lf
```

That rule tells git: normalize every text file to LF in the repository.
From now on, no mixed-line-ending failures.

**Verify it works:**

```bash
cat .gitattributes
# should print the rule
```

**If mixed line endings appear anyway**, they come from somewhere else:

```bash
# Show all files with CRLF line endings
grep -rIl $'\r' --include='*.py' --include='*.rs' --include='*.toml' .

# If any appear, normalize them:
sed -i 's/\r$//' <file>
git add -A
git commit -m "fix: normalize line endings"
```

### 5.2 — Merge conflict

Two branches changed the same line of the same file.

```bash
git checkout main
git pull
git checkout phase-9-plugins
git merge main
# CONFLICT (content): Merge conflict in src/foo.py
```

Open the file. You will see:

```python
<<<<<<< HEAD
your version
=======
their version
>>>>>>> main
```

**Resolve:** edit the file to keep what you want. Delete the `<<<<<<<`,
`=======`, `>>>>>>>` lines. Keep the code you want. Then:

```bash
git add <file>
git commit -m "merge: resolve conflict in foo.py"
```

**Abandon the merge:**

```bash
git merge --abort
```

Back to before you started merging.

### 5.3 — Detached HEAD

You see this message:

```
You are in 'detached HEAD' state
```

It means you checked out a specific commit instead of a branch:

```bash
git checkout abc1234
# "detached HEAD" — you are not on any branch
```

Any commits you make here will be lost. Get back to a branch:

```bash
git checkout main
# or
git checkout phase-9-plugins
```

**If you made commits while in detached HEAD**, save them first:

```bash
git branch my-temp-work    # create a branch pointing at your work
git checkout my-temp-work  # switch to it
```

### 5.4 — Accidentally committed a big file

You committed a 500 MB binary or a `.so` file that should have been
gitignored.

**If not pushed yet:**

```bash
git reset --soft HEAD~1    # undo the commit
git rm --cached <bigfile>  # unstage the file
echo "<bigfile>" >> .gitignore
git commit -m "message"
```

**If already pushed:** this is painful. You need `git filter-repo`.
Do not use `git filter-branch`. Do not use `git rebase` for this. Ask
for help before running anything.

**The permanent prevention:** check `.gitignore` before staging. The
repo's `.gitignore` already excludes `_native.so`, `target/`, `.venv/`,
`__pycache__/`, and `work/` under `sandtest/`.

### 5.5 — Accidentally committed a secret

You committed a `.env` file or an API key.

**Treat the secret as compromised.** Rotate it immediately in the
provider's console. Then:

- If not pushed: `git reset --soft HEAD~1` and remove the secret
- If pushed: the secret is in the history forever. It must be revoked
  at the source. Then consider `git filter-repo` to remove the file
  from history.

**Prevention:** the repo's `.gitignore` should have `.env`. Verify:

```bash
grep -E "^\.env" .gitignore
# should print .env
```

---

## 6. Cheat sheet

Commands you will use most, one line each.

### Daily

```bash
git status                              # what's changed
git diff                                # see unstaged changes
git diff --staged                       # see staged changes
git add -A                              # stage everything
git commit -m "message"                 # commit
git push                                # push current branch
git pull                                # pull from origin
git log --oneline -10                   # last 10 commits
```

### Branches

```bash
git branch                              # list local branches
git branch -a                           # list all branches (including remote)
git checkout -b <name>                  # create and switch to a new branch
git checkout <name>                     # switch to an existing branch
git branch -d <name>                    # delete a merged branch
git branch -D <name>                    # delete a branch, even if unmerged
git branch --show-current               # which branch am I on?
```

### Undo

```bash
git restore <file>                      # discard changes to a file (uncommitted)
git restore --staged <file>             # unstage a file
git reset --soft HEAD~1                 # undo commit, keep changes staged
git reset HEAD~1                        # undo commit, unstage changes
git reset --hard HEAD~1                 # undo commit, delete changes (dangerous)
git revert <hash>                       # safe undo — creates new commit
git reflog                              # every position HEAD has been in
```

### Branches comparison

```bash
git log main..phase-9-plugins --oneline # commits on your branch not on main
git diff main..phase-9-plugins          # changes between the two branches
git show <hash>                         # what a specific commit did
```

### Tags

```bash
git tag -a v1.0.0 -m "message"          # create an annotated tag
git tag -l                              # list all tags
git show v1.0.0                         # see the commit a tag points at
git push origin v1.0.0                  # push one tag to GitHub
git push --tags                         # push all tags
```

### Find things

```bash
git log --oneline --all --graph         # visual history of all branches
git log --author="you" --oneline        # commits by author
git log -S "function_name"              # commits that added/removed a string
git log -- path/to/file.py              # commits touching one file
git blame path/to/file.py               # who last changed each line
```

### Bisect (when a test is broken and you don't know why)

```bash
git bisect start
git bisect bad
git bisect good <known-good-tag-or-hash>
# ... test the middle commit
git bisect good        # or git bisect bad
# repeat until git tells you the first bad commit
git bisect reset
```

---

## 7. The Phase 9 workflow, in full

Concrete sequence for the next phase. Copy these, run them in order.

### Before starting

```bash
cd /mnt/c/Users/arun0/Videos/CodeGuardian

# Make sure main is up to date
git checkout main
git pull

# Tag the last green state
git tag -a v1.0.0 -m "Multi-language kernel complete, deep test green"
git push origin v1.0.0

# Create the Phase 9 branch
git checkout -b phase-9-plugins
```

### During Phase 9

After each block (B-042, B-043, etc.):

```bash
# Check what you have
git status
git diff --stat

# Commit
git add -A
git commit -m "feat(plugins): add loader infrastructure (B-042)"

# Push (only if you want a backup)
git push
```

### If a test fails during Phase 9

```bash
# You are on phase-9-plugins and something broke
# Option 1: revert the bad commit
git log --oneline -5
git revert <bad-commit-hash>

# Option 2: find which commit broke it
git bisect start
git bisect bad
git bisect good v1.0.0
# ... bisect until you find it
git bisect reset

# Option 3: nuke and start over
git checkout main
git branch -D phase-9-plugins
git checkout -b phase-9-plugins
```

### When Phase 9 is complete

```bash
# Deep test must pass first
python sandtest/deep_test.py

# If green, commit anything pending
git add -A
git commit -m "feat(plugins): Phase 9 complete"
git push

# Go to GitHub → Pull Requests → New Pull Request
# Base: main    Compare: phase-9-plugins
# Title: "Phase 9 — Plugins"
# Description: what this phase added, deep test result
# Review the diff on the "Files changed" tab
# Merge

# After merge, back locally:
git checkout main
git pull
git branch -d phase-9-plugins

# Tag the release
git tag -a v1.1.0 -m "Plugin system complete"
git push origin v1.1.0
```

---

## 8. Lessons from this session

Three real mistakes made during Phase 8 and multi-language commits. Save
these. Do not repeat them.

### Lesson 1 — check the environment before the plan

Before giving a commit plan, always check:

```bash
git config core.autocrlf
git config core.eol
ls .gitattributes 2>/dev/null
cat .pre-commit-config.yaml 2>/dev/null
```

Four commands, ten seconds. They would have surfaced the line-ending
problem and the pre-commit presence before we spent three hours
fighting them.

### Lesson 2 — commit everything at once when pre-commit is installed

The pattern `git add file1.py file2.py && git commit` fails when
pre-commit tries to stash the unstaged files. That is the exact bug
that happened this session.

**Always do:**

```bash
git add -A
git commit -m "message"
```

Unless you have a real reason to commit a subset, in which case use
`git stash --keep-index`.

### Lesson 3 — `.gitattributes` goes in first, always

In any new repository, before the first real commit:

```bash
cat > .gitattributes << 'EOF'
* text=auto eol=lf
*.so binary
*.png binary
*.jpg binary
EOF
git add .gitattributes
git commit --no-verify -m "chore: add gitattributes"
```

Prevents the entire class of line-ending failures.

---

## 9. Commands you will not need yet

Listed so you recognize them if they appear in help output or blog
posts. Do not use them until the situation requires them.

| Command | When to use |
|---|---|
| `git rebase -i main` | To clean up commits before pushing a branch (never after pushing) |
| `git cherry-pick <hash>` | To pull one commit from another branch (e.g. a bugfix) |
| `git worktree add <path> <branch>` | To have two branches checked out simultaneously in two terminal windows |
| `git filter-repo` | To rewrite history (removing secrets, huge files) — replace `git filter-branch` |
| `git submodule` | To embed one git repo inside another — avoid, use packages instead |
| `git gc`, `git prune` | Housekeeping — git runs these automatically |
| `git archive` | To create a tarball of a specific commit |
| `git commit -S` | To sign commits with GPG — useful when code must be provably yours |

---

## 10. Emergency contact

If something is broken and you do not know why, run this one block and
paste the output somewhere safe:

```bash
cd /mnt/c/Users/arun0/Videos/CodeGuardian
echo "=== status ==="
git status
echo ""
echo "=== current branch ==="
git branch --show-current
echo ""
echo "=== last 10 commits ==="
git log --oneline -10
echo ""
echo "=== uncommitted diff ==="
git diff --stat
echo ""
echo "=== branches ==="
git branch -a
echo ""
echo "=== tags ==="
git tag -l
```
