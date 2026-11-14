+++
id = "E1-17"
title = "Read the configuration from SSM and write it back, with the AWS SDK pinned"
labels = ["size:S", "kind:infra"]
epic = "E1"
blocked_by = ["E1-03"]
+++

## Context

The loader of issue E1-03 reads the committed example and an untracked file. Two callers need SSM
(section 8.1): the pipeline's synth step, which reads the real configuration from SSM into a file
outside its checkout that RHI_CONFIG_FILE names (issue E1-10, section 8.2, step 2), and the owner, who writes the configuration to SSM
once through the walkthrough (issue E1-13, section 8.6, step 4) and deploys the pipeline stack once
from a laptop with the source ssm (section 8.6, step 6). Both use this module, so that they read and
write the same keys the same way. Nothing else needs it, so it is apart from the loader.

**How the configuration is laid out in SSM.** One String parameter per key under the path prefix
/rhi/config/, a constant of the module, and one per pool entry under /rhi/config/pools/, so that the
owner adds a pool with one write and no entry approaches the size limit of a standard parameter
(/rhi/config/pools/mock for the mock pool). The prefix holds the configuration and nothing else:
other parameters of the system live beside it, never below it, such as each pool's release
parameter at /rhi/pools/<pool>/release (issue E2-42), so that the reader, which refuses a parameter
under the prefix that names no key, never meets one, and the synth role, which may read the prefix
only, never reads one.
None of the values is a credential (section 10), so none needs a SecureString or a key to decrypt
it, and the synth role reads the prefix and nothing else (issue E1-10). The region is the caller's
own: the module names none.

The AWS SDK for Python is pinned here, in the project's dependencies, for the app's own use. The
services' functions carry the same version in their dependency groups (issue E1-16); one lock holds
one version.

The loader refuses a pool entry until issue E2-69 gives entries their shape (issue E1-03), so this
issue writes and reads an empty pools table only; issue E2-69 keeps the layout, one parameter per
entry, when it accepts entries.

Tests never reach AWS: they use a stand-in for the SSM client, and the gate scripts run with no AWS
credentials at all.

## Deliverables

- [ ] `pyproject.toml` and `uv.lock` add the AWS SDK for Python at an exact version to the project's dependencies, used by the configuration module's ssm source and writer only.
- [ ] `src/rhi/config.py` reads the source ssm, which RHI_CONFIG may now name: one String parameter per key under the prefix /rhi/config/, a constant of the module, the pools table as one parameter per entry under /rhi/config/pools/ (none while the table is empty), read with the caller's credentials and region; a parameter under the prefix that names no key is refused like an unknown key, and nothing outside the prefix is read.
- [ ] `src/rhi/config.py` writes a configuration back out, to those parameters or to a TOML file, in the form each source reads, and its errors name the key or the parameter, never a value.
- [ ] `tests/unit/test_config_ssm.py` covers the source and the writer with a stand-in for SSM.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_config_ssm.py` passes: the ssm source gives the same typed object as the example for the same values; a configuration written to the stand-in or to a file and read back is the same object; a missing parameter, an unknown one and a value of the wrong type each raise and name the key without its value.
- [ ] `tests/unit/test_config_ssm.py::test_the_layout` shows the prefix being /rhi/config/, the writer putting one String parameter per key under it, none under /rhi/config/pools/ for an empty pools table, and nothing outside the prefix, and the ssm source loading the same configuration from a stand-in that also holds /rhi/pools/mock/release.

## References

- `docs/design/system-design.md`, sections 8.1, 8.2, 8.6, 10
