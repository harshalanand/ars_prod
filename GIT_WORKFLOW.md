# Git Workflow — Push & Pull (ARS_PROD)

How to push code **to** GitHub and pull code **from** GitHub for this project.

- **Remote:** `origin` → https://github.com/harshalanand/ars_prod.git
- **Main branch:** `main` (production / PR target)
- **Working branches:** `dev`, `ARS_NEW`, `santosh_dev`
- **Shell:** Windows PowerShell (commands below are PowerShell-safe)

> Rule of thumb: never commit straight to `main`. Work on a branch, push it, open a PR.

---

## 0. One-time setup

```powershell
# Confirm identity
git config user.name
git config user.email

# Set them if blank
git config user.name  "santosh kumar"
git config user.email "santosh@v2kart.com"

# Confirm the remote is correct
git remote -v
```

---

## 1. Check where you are (do this first, every time)

```powershell
git status            # what's changed / which branch
git branch            # * marks current branch
git fetch origin      # refresh remote info (no file changes)
```

---

## 2. PULL — get the latest code FROM git

Always pull before you start working so you build on the latest code.

```powershell
# Make sure you have no uncommitted changes (commit or stash first)
git status

# Pull the current branch
git pull origin <branch>          # e.g. git pull origin ARS_NEW
```

If you have local edits you are not ready to commit:

```powershell
git stash                 # park local changes
git pull origin <branch>  # get latest
git stash pop             # bring your changes back on top
```

**Pull a branch you don't have locally yet:**

```powershell
git fetch origin
git checkout <branch>     # e.g. git checkout dev — auto-tracks origin/<branch>
```

---

## 3. PUSH — send your code TO git

### 3a. Work on a branch (recommended)

```powershell
# Start from up-to-date main
git checkout main
git pull origin main

# Create a feature branch
git checkout -b feature/from-hold-qty-reflect
```

### 3b. Stage, commit, push

```powershell
git status                         # review what changed
git add <file1> <file2>            # stage specific files
# or stage everything:
git add -A

git commit -m "Reflect FROM_HOLD_QTY to ARS_LISTING_WORKING"

# First push of a new branch (sets upstream):
git push -u origin feature/from-hold-qty-reflect

# Later pushes on the same branch:
git push
```

### 3c. Multi-line commit message (PowerShell)

Use a single-quoted here-string. The closing `'@` must be at column 0:

```powershell
git commit -m @'
Reflect FROM_HOLD_QTY to ARS_LISTING_WORKING

Adds OPT-grain rollup so MSA vs hold split is queryable.
'@
```

---

## 4. Open a Pull Request (merge into main/dev)

After pushing your branch:

```powershell
# Using GitHub CLI
gh pr create --base main --head feature/from-hold-qty-reflect `
  --title "Reflect FROM_HOLD_QTY to ARS_LISTING_WORKING" `
  --body  "Adds OPT-grain rollup of warehouse-hold draw."
```

Or open the PR link printed by `git push`, or on GitHub: **Compare & pull request**.

---

## 5. Keep your branch up to date with main

While your PR is open and `main` moves ahead:

```powershell
git checkout main
git pull origin main

git checkout feature/from-hold-qty-reflect
git merge main            # merge latest main into your branch
# resolve conflicts if any, then:
git add -A
git commit                # completes the merge
git push
```

---

## 6. Common situations

### See what you're about to push
```powershell
git log origin/<branch>..HEAD --oneline   # local commits not yet pushed
git diff origin/<branch>..HEAD            # the actual code diff
```

### Undo staging (keep file changes)
```powershell
git restore --staged <file>
```

### Discard local changes to a file (cannot undo)
```powershell
git restore <file>
```

### Pull was rejected ("non-fast-forward")
Someone pushed before you. Pull, resolve, push again:
```powershell
git pull origin <branch>     # merge remote changes in
# resolve conflicts if shown
git push
```

### Resolve a merge conflict
1. `git status` lists conflicted files.
2. Open each file, fix the `<<<<<<< ======= >>>>>>>` markers.
3. `git add <file>` for each resolved file.
4. `git commit` (finishes the merge), then `git push`.

---

## 7. Quick reference

| Goal | Command |
|------|---------|
| See status | `git status` |
| Refresh remote info | `git fetch origin` |
| Pull latest | `git pull origin <branch>` |
| New branch | `git checkout -b <name>` |
| Switch branch | `git checkout <name>` |
| Stage all | `git add -A` |
| Commit | `git commit -m "msg"` |
| Push new branch | `git push -u origin <name>` |
| Push existing | `git push` |
| Open PR | `gh pr create --base main --head <name>` |

---

## 8. Deploy `main` to the running server

You don't edit on the server — you **pull `main`** (or zip-deploy it) and restart.

### A. Local / on-prem Windows server (runs via `START_SMART.bat`)

The server runs `uvicorn main:app --port 8000` from `backend\`.

```powershell
# 1. STOP the running ARS server
#    In the server's console window press  Ctrl + C  (then it prints "Server stopped").
#    ⚠️ Do NOT kill port 8000 blindly — on store machines that port is the
#       Semnox Parafait POS. Stop the ARS uvicorn window/process specifically.

# 2. Go to the repo and make sure the working tree is clean
git -C d:\ARS_PROD\ars_prod status
git -C d:\ARS_PROD\ars_prod stash       # only if there are local edits to keep

# 3. Switch to main and pull the latest
git -C d:\ARS_PROD\ars_prod checkout main
git -C d:\ARS_PROD\ars_prod pull origin main

# 4. If backend dependencies changed (requirements.txt)
d:\ARS_PROD\ars_prod\backend\venv\Scripts\python.exe -m pip install -r d:\ARS_PROD\ars_prod\backend\requirements.txt

# 5. If the frontend changed, rebuild it
#    (from frontend\) npm install ; npm run build

# 6. RESTART the server
#    Double-click START_SMART.bat  — or:
d:\ARS_PROD\ars_prod\backend\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000

# 7. Verify
#    Open http://localhost:8000/health  (or /docs)
```

> Run from `main` only if this server is meant to track production. To deploy a
> different branch, `git checkout <branch>` instead of `main` in step 3.

### B. Azure App Service (cloud production)

Azure does **not** `git pull`. You build a zip from `main` and push it with
`zipdeploy` (full command in `CLAUDE.md` → "HOW TO DEPLOY"). Short form:

```powershell
git checkout main
git pull origin main
# then run the zipdeploy block from CLAUDE.md (gets Azure token, zips backend\, posts to scm)
# Azure restarts the app automatically; verify:
#   https://ars-v2retail-api.azurewebsites.net/health
```

### Quick checklist before any deploy
- [ ] `git status` clean (committed or stashed)
- [ ] On the intended branch (`main` for prod)
- [ ] `git pull` succeeded with no conflicts
- [ ] Deps installed if `requirements.txt` / `package.json` changed
- [ ] Server restarted and `/health` returns OK

---

## 9. Don'ts

- ❌ Don't commit/push directly to `main`.
- ❌ Don't `git push --force` to shared branches (`main`, `dev`).
- ❌ Don't commit secrets (`.env`, passwords, tokens) — check `.gitignore`.
- ❌ Don't pull with uncommitted changes you care about — commit or `git stash` first.
