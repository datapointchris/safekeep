"""Entry point for `python -m safekeep`.

The console script declared in pyproject.toml calls ``main`` directly; this
exists so the tests can invoke the tool as a subprocess the way a user does,
without depending on the console script being installed on PATH.
"""

from safekeep import main

if __name__ == '__main__':
    main()
