# CHANGELOG


## v0.3.0 (2026-08-06)

### Bug Fixes

- **backup**: Record the mode of the source directory itself
  ([`dd888ea`](https://github.com/datapointchris/safekeep/commit/dd888ea39531bdefc5678b7309d7c216b072c7b9))

survey_tree skipped the root of every path it walked, so ~/.ssh and ~/.config/gnupg were the two
  directories in the tree whose 0700 nothing wrote down. A rebuild recreated them at the 0755
  default and gpg refuses a homedir anyone can read.

Snapshots already taken have no mode for their source roots and will keep restoring them at the
  default; the next backup records them.

### Build System

- Skip the subprocess bandit checks
  ([`3ede69b`](https://github.com/datapointchris/safekeep/commit/3ede69bb1116367fef7793af2e038f8f72696fe7))

safekeep runs git, rsync and fzf by name. B404, B603 and B607 fire on every one of those calls,
  which is the whole tool, so `task lint` failed on findings that will never be actionable. Skipped
  in config rather than at each call site.

### Documentation

- Stop promising anything about pre-version-2 snapshots
  ([`2415ebc`](https://github.com/datapointchris/safekeep/commit/2415ebc7cd59f778e35e1d5f9ad091fe63ebd6e1))

The file lists are a property of what a backup writes now. Carrying a documented guarantee about the
  snapshots taken before them means every later change has to be weighed against a format nobody is
  going to restore from again.

Nothing is removed from the reading path: file_kinds already reaches for 'paths' with a default,
  which is ordinary dict access rather than a compatibility branch, and a manifest without them
  restores unlabelled because there is nothing to label with.

### Features

- **restore**: Name every file, and speak in sources
  ([`0cf0a73`](https://github.com/datapointchris/safekeep/commit/0cf0a73bf8cfd1f10ab40defa4597cf0b081eb3f))

A restore said "3 files" and then nothing for minutes, and it called the things it restored "groups"
  — a manifest word for a (kind, source) pair, which meant a repo appeared twice in the picker while
  restoring once, and the header and the summary counted in different units under one name.

Restore now walks the snapshot's own subtree for each source. That one list drives everything:

- Every file is named as rsync writes it, marked + for new and ~ for replaced, with the kind beside
  a repo's files so an ignored one is distinguishable from an untracked one. Manifest version 2
  records the file lists on the git groups to make that possible; version 1 snapshots restore
  unlabelled. - The mode pass no longer walks the target. It used to chmod every path under a
  restored directory — for a repo, the entire working tree — and applied the default 0644 to tracked
  files the manifest had never seen, stripping +x off files a clone had just put back. Restoring 2
  untracked files touched 52 paths in a fixture and 11139 on a real machine. It now applies the
  recorded deviations, and the defaults only to paths the restore created. - Conflicts are visible:
  the default policy says which files it replaced and kept a .pre-restore copy of, and --on-conflict
  ask names each existing file and waits for a decision, keeping no copies. - The picker offers one
  row per source, sorted by path, previewing the files that source holds rather than an ls of its
  top level.

--group is now --source everywhere; the old spelling still parses. The progress flag goes: the file
  lines are the progress signal, and interleaving progress2's redraws with them served neither.


## v0.2.0 (2026-08-06)

### Documentation

- Bring the reference page over from dotfiles
  ([`e0a6e51`](https://github.com/datapointchris/safekeep/commit/e0a6e5178e0eeef4285646dd1f2b9dc27fc9326d))

The dotfiles docs carried a 223-line reference for safekeep: the manifest format, the restore
  conflict policies, the schema-change rules, and the reasoning behind each. It moves here intact
  rather than being rewritten, because the reasoning is the valuable part and re-deriving it would
  lose the rejected alternatives — why the config is TOML and the manifest JSON, why a renamed key
  is fatal while a retired one only warns, why backup and restore have opposite defaults for
  selection.

dotfiles keeps a pointer page rather than a copy. Two copies get a chance to disagree, and the one
  shipping beside the code is the one that will be right.

Cross-links to sibling dotfiles pages became absolute URLs into the published docs site, since
  relative paths resolved to nothing once the file moved.

### Features

- Add a version, record it in the manifest, and self-update
  ([`493801f`](https://github.com/datapointchris/safekeep/commit/493801fd75e7c73dcc98f84bbe076454bf2bafaa))

safekeep carried MANIFEST_VERSION = 1 for its on-disk format while the tool writing it had no
  version at all — the sharpest argument that it had outgrown living in dotfiles. A backup whose
  snapshots outlive the machine has to be able to say what wrote them.

The version is read from the installed distribution metadata, not a constant here: semantic-release
  owns pyproject.toml, and a second copy in the module is the one that goes stale. A source checkout
  that was never installed reports 'unknown' rather than inventing a number — release.md is explicit
  that no version string can distinguish a release from a dev build, so nothing tries.

The manifest gains safekeep_version beside version. Additive, so every existing snapshot still
  reads: 'version' says what shape the file is, the new field says which build chose that shape,
  which is what makes a future format change diagnosable instead of mysterious.

Self-update is pyselfupdate, notify-only per release.md — one check per 24h, one line to stderr, and
  `safekeep update` the only thing that writes. The notice is deferred through pyselfupdate's atexit
  hook so it lands after a command's own output. Errors surface only in `update`; the notice path
  swallows them into the state file by design, which is why that command is the one place they
  print.

pyselfupdate resolves from PyPI while pytermstyle needs a git source — the only difference is that
  'pytermstyle' was already taken there. No [typer] extra: safekeep is argparse, and the extra
  exists for the ready-made typer command.

Also fixes a defect the move introduced: the fzf snapshot preview shelled out to Path(__file__),
  which as a package is src/safekeep/__init__.py — running that re-imports the module as __main__
  rather than resolving the installed package. It now invokes `-m safekeep`, the same entry point
  the tests use.


## v0.1.0 (2026-08-06)

### Build System

- Add the generated toolchain config
  ([`b422289`](https://github.com/datapointchris/safekeep/commit/b422289a434b1c42e992c250a2f1a6cf75a37560))

Output of the forge dies from repo-structure.md § Bootstrapping, run with -F safekeep because the
  dies are fleet-wide by default. The standard [tool.*] sections and the [tool.forge] managed list
  were merged into pyproject.toml by the same sync, which is why they were absent when it was
  written.

sync-gitignore reported SKIP: the entries it ensures were already present, which is the idempotence
  it promises rather than an omission.

### Features

- Move safekeep out of dotfiles into its own repo
  ([`d233050`](https://github.com/datapointchris/safekeep/commit/d233050617e14cb7c3735948b426b8b21623c654))

safekeep owns a data format that lives outside the machine: it writes a manifest to a network drive
  that has to be read by a different safekeep, on a different machine, on a different OS, possibly
  years later. MANIFEST_VERSION = 1 is the tell — it already versioned its data and had no version
  of its own. That is why this is the app that left dotfiles, not its size.

The script becomes src/safekeep/__init__.py verbatim, minus the PEP 723 header and the __main__
  guard. Keeping the module name means every safekeep.<name> reference across the 90 tests is
  untouched by the move.

Two entry points, for two callers. [project.scripts] gives a user the console script; __main__.py
  gives the tests `python -m safekeep`, which is the closest thing to how a user runs it without
  depending on PATH. The old tests invoked the file directly with sys.executable, which a package
  cannot offer.

pytermstyle supplies the palette and help grammar, pinned to tag v0.1.1, so screens stay identical
  to the bash and Go CLIs beside it. Everything else is stdlib — tomllib for the config, rsync for
  the copying.
