---
name: release
description: Cut a new release of ai-code-cognitive-stress — bump the version, stamp the CHANGELOG, commit, create an annotated git tag, and publish a GitHub release. Use when the maintainer asks to release a version, cut a release, bump the version for release, tag a version, or publish a GitHub release. Maintainer-only; the publish step (push + GitHub release) always pauses for explicit confirmation first.
user-invocable: true
---

# release — cut a versioned release

This is the **maintainer release workflow**: it turns the current `main` into a
tagged, published release with consistent version / changelog / tag / GitHub
notes. It is the one sanctioned developer process that touches the network
(`git push`, `gh release`). It does **not** change the tool's runtime
no-network guarantee — the package still makes no network calls; only this
human-driven dev step does.

Releasing is gated by the repo rule in `AGENTS.md`: never publish without an
explicit prompt. Invoking this skill IS that prompt for the local steps, but the
**publish step (Phase 5) still stops and shows exactly what will be pushed and
published, and waits for an explicit "go"** before any remote action. Nothing
reaches origin or GitHub until then.

## Conventions (the single source of truth)

| Thing | Where / format |
|---|---|
| Version | `ai_code_cognitive_stress/__init__.py` → `__version__ = "X.Y.Z"`. **Only file to edit** — `pyproject.toml` reads it dynamically via hatch. |
| SemVer | `MAJOR.MINOR.PATCH`. Breaking → major; new feature/flag/config key → minor; fix/internal-only → patch. Pre-1.0 (`0.y.z`): breaking changes bump MINOR, everything else PATCH. |
| Changelog | `CHANGELOG.md`, [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Sections: `Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`. |
| Tag | Annotated, `vX.Y.Z`, message `vX.Y.Z — <short title>` (en-dash spaced; the no-em-dash rule is paper-only). |
| GitHub release | Title `vX.Y.Z — <short title>` (matches the tag), body = that version's CHANGELOG section. |

## Procedure

Run the phases in order. Stop and report if any pre-flight check fails — do not
"fix forward" past a red check.

### Phase 0 — Pre-flight

1. **Branch**: must be on `main` (`git branch --show-current`). Releases are cut
   from `main`.
2. **Tree state**: `git status --short`. The tree should contain only the
   release changes you are about to make (or be clean if a prior session already
   bumped + committed). Unrelated uncommitted work → stop and surface it.
3. **Tests green**: `python3 -m pytest` must pass. A release never ships red.
4. **Paper currency**: if `paper/` changed since the last tag
   (`git diff --stat $(git describe --tags --abbrev=0)..HEAD -- paper/`), the
   tracked PDF must be rebuilt — defer to the `paper-current-state` skill and
   stop if it is stale. (No `paper/` changes → skip.)
5. **Pick the version**. If the maintainer named one, use it. Otherwise propose
   one from the `[Unreleased]` changelog content (a `Removed`/breaking entry →
   minor pre-1.0 or major post-1.0; any `Added` → minor; only `Fixed`/`Changed`
   → patch) and confirm before proceeding.
6. **Not already released**: `git tag -l vX.Y.Z` and `gh release view vX.Y.Z`
   must both be empty. A taken version means pick the next one — never re-tag or
   force-replace a published version.

### Phase 1 — Bump the version

Edit `ai_code_cognitive_stress/__init__.py` so `__version__ == "X.Y.Z"`. Nothing
else references the literal.

### Phase 2 — Stamp the CHANGELOG

Move the `[Unreleased]` items into a new dated section and refresh the links:

- Insert `## [X.Y.Z] - YYYY-MM-DD` (today, local date) below `## [Unreleased]`,
  carrying the accumulated `Added`/`Changed`/`Fixed`/… subsections.
- Leave `## [Unreleased]` in place but empty (ready for the next cycle).
- Update the link refs at the bottom:
  - `[Unreleased]: …/compare/vX.Y.Z...HEAD`
  - add `[X.Y.Z]: …/compare/<prev-tag>...vX.Y.Z`

If a prior session already wrote the entry as a dated `## [X.Y.Z]` section (bump
and release done in one go), keep it — just verify the date and links.

### Phase 3 — Commit

Consult the `git-commit` skill for message style. One commit for the release;
tight subject with the `[area]` prefix, body bullets for the substance. **No
AI-attribution trailer.** If the version bump and changes already landed in an
earlier commit this session, fold the changelog stamp into it with
`git commit --amend` rather than adding a bookkeeping commit (only if unpushed).

```bash
git add ai_code_cognitive_stress/__init__.py CHANGELOG.md
git commit -F - <<'EOF'
[release] vX.Y.Z

- <one line per headline change, mirroring the changelog>
EOF
```

### Phase 4 — Annotated tag

```bash
git tag -a vX.Y.Z -m "vX.Y.Z — <short title>"
```

The `<short title>` is the release's one-line theme (e.g. `CODL graded
capacity-dose`), reused verbatim as the GitHub release title.

### Phase 5 — Publish (explicit confirmation required)

Extract the release notes (this version's changelog section, header excluded):

```bash
awk '/^## \[X\.Y\.Z\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md > /tmp/relnotes-X.Y.Z.md
```

Then **present the maintainer a summary and wait for an explicit "go"**:

- version, tag name, release title;
- the commit subject and SHA;
- the extracted notes;
- the exact remote commands below.

Only after confirmation:

```bash
git push origin main
git push origin vX.Y.Z
gh release create vX.Y.Z \
  --title "vX.Y.Z — <short title>" \
  --notes-file /tmp/relnotes-X.Y.Z.md \
  --verify-tag
```

Mark `--latest` (the default for the highest semver) is automatic; pass
`--prerelease` for an `-rc`/`-beta` tag.

### Phase 6 — Verify

```bash
gh release view vX.Y.Z --web   # or without --web to print
git ls-remote --tags origin | grep vX.Y.Z
```

Confirm the release page renders, the notes match the changelog, and the tag is
on origin. Report the release URL.

## Rollback

- **Before Phase 5** (nothing pushed): delete the local tag and reset/amend
  freely — `git tag -d vX.Y.Z`, `git reset --soft HEAD~1` (or `--amend`).
- **After publishing**: do not delete or force-replace a published tag/release —
  others may have fetched it. Cut a new patch (`X.Y.Z+1`) with the correction
  and note it in the changelog instead.

## Notes

- This skill is auto-discovered by Claude Code from `.claude/skills/`. After
  adding or editing it, re-run `python install.py --codex` so Codex CLI picks up
  the same definition.
- `gh` must be authenticated (`gh auth status`). The origin remote is the
  GitHub repo in the changelog links.
