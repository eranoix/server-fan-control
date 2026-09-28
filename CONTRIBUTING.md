# Contributing

## Git

### Commit messages

Conventional Commits, in English:

```
<type>(<scope>): <summary> (TICKET-12)
```

- `type` is one of `feat`, `fix`, `perf`, `refactor`, `style`, `docs`,
  `test`, `build`, `ci`, `chore`, `revert`.
- `scope` is optional and is one word from the list below.
- `summary` is imperative ("add", not "added"), starts lowercase and has no
  final period.
- The whole subject fits in 72 characters of plain ASCII, with no accented
  letters.
- The ticket key at the end is only there when there is a ticket.
- The body is optional, in English, after a blank line, wrapped at 72
  columns.
- A version commit starts its body with `Release: X.Y.Z`.
- No `Co-Authored-By` trailers and no tool attribution.

Scopes for this repository (kept in `.github/commit-scopes.txt`):

- `demo`
- `deploy`
- `deps`
- `fancurve`

### Branches and pull requests

- One branch per ticket, named `<ticket>-<slug>` in lowercase
  (`abc-92-read-form`), cut from `main` and deleted after the merge.
- One pull request per ticket, merged with squash. The PR title follows the
  commit format because it becomes the commit on `main`.

### Releases

Every release gets an annotated tag `vX.Y.Z` and a GitHub Release with the
notes.

### Enforcement

- `scripts/commitlint.mjs` checks a message (`--file`, `--text`) or a range
  of commits (`--range a..b`). It has no dependencies.
- `.githooks/commit-msg` runs it on every commit. Enable it once per clone:

  ```
  git config core.hooksPath .githooks
  ```

- CI checks every pushed commit and every pull request title.
