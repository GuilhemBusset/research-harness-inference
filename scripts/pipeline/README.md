# Pipeline scripts

This folder holds the scripts that the pipeline's steps run (system design, section 8.1), one
module per script.

It is not a package: there is no `__init__.py`. The folder is on the import path of pytest, Ruff
and ty (`pyproject.toml`, `ruff.toml`, `ty.toml`), so a test imports a script by its module name:
`scripts/pipeline/leak_check.py` is `import leak_check`.

The scripts of the synth step (`src/rhi/pipeline/synth.py`), which gives each one the folders of
pytest's import path in `PYTHONPATH`:

- `fetch_config.py` reads the configuration from SSM and writes it to the file `RHI_CONFIG_FILE`
  names, outside the source tree. It prints no value.
- `leak_check.py` searches the source tree for the value of every private key of that file, in any
  letter case, and fails with the file and line of each hit, never with the value. It refuses to
  run when that file lies inside the tree it searches.

The script of the verification step (`src/rhi/pipeline/verify.py`), which runs after the stage's
deployment and needs no import path:

- `verify.py` reads the canary's key from the secret `rhi/canary-key` and runs the smoke tests of
  `tests/smoke` against the deployed endpoint, in a process of their own that takes the key from
  its environment. It deselects, by their IDs, the tests that must reach the internal load
  balancer, and exits non-zero when a test fails. While the endpoint has no HTTPS listener, or the
  secret is missing or holds no key, it runs no test, exits 0 and writes its reason, `listener-off`
  or `canary-missing`, to the file `RHI_VERIFY_REASON_FILE` names, which the step's post-build
  phase copies to the artifact bucket. It never prints the key, and what the tests print reaches
  the build's log with `[canary key]` in place of the key and of any piece of it.
