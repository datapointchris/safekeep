# CHANGELOG


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
