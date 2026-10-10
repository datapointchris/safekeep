"""Entry point for `python -m safekeep`.

The console script declared in pyproject.toml calls ``main`` directly. This exists for the
callers that run the tool as `python -m safekeep`: the tests, which invoke it as a subprocess
the way a user does, and the fzf picker, whose preview panes call `snapshots show` through it.
"""

from safekeep.main import main

if __name__ == '__main__':
    main()
