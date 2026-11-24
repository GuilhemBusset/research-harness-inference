+++
id = "E3-53"
title = "A zone list per pool in the configuration, checked against EC2's offerings before the owner writes it"
labels = ["size:L", "kind:infra"]
epic = 5
+++

## Context

Every pool's Auto Scaling group spans the VPC's three zones (`src/rhi/pools/group.py`): the
region's first three by name, which `Fn::GetAZs` gives when the stack is deployed, with no lookup
(`src/rhi/network/stack.py`; `ZONES` in `src/rhi/network/vpc.py`). The coursework pool's instance
type is not offered in every one of those zones, and is offered in a zone the VPC does not span.
So the group's launches in a zone without the type fail at once as unsupported, each costing
minutes of retry, and the zone outside the VPC that offers it is never tried, though GPU capacity
is scarce where the type is offered. The owner chose a zone list per pool: each pool launches only
in the zones the configuration lists for it.

This issue puts the lists in the configuration and checks them where a configuration enters SSM.
Issue E3-54 builds the network and the groups from them. Until E3-54 is merged the synthesis reads
neither list, so a configuration that gives them deploys as before. The owner writes the lists only
once this issue is merged: the loader of an earlier commit refuses a key it does not know.

**Two lists.**

- zones, a new optional top-level key: the zones the VPC spans, by name, in the order of its
  subnets. At least three, none named twice, and no two ending in the same letter: a zone's name
  is its region's followed by one letter, and E3-54 writes a zone beyond the third as the deploying
  region followed by that letter, so that no template names a region. The first three are the
  zones the VPC spans today, and a zone is only ever appended: the CDK names each subnet after its
  position, so a zone moved or removed would replace subnets of the deployed VPC. Left out, it is
  None, and the VPC spans the region's first three zones, as today.
- zones, a new optional field of a pool's entry: the zones the pool's group spans, each one of the
  top-level list, at least one, none named twice. Left out, it is None, and the group spans the
  first three zones of the VPC, which is what every group spans today: a pool that gives no list
  is unchanged. A pool may give the field only when the configuration gives the top-level key,
  since without it a name could not be matched to a subnet at synthesis.

A zone name is lower-case letters, digits and hyphens, and ends in a lower-case letter.
`rhi_common.pool_config` reads the field's shape (the module knows no other key), and
`src/rhi/config.py` reads each pool's list against the top-level one, as it reads
`allowed_instance_types` against the pools. Both are optional, so a merge needs no value the
deployed configuration lacks (each module's "Adding a key" or "Adding a field").

`config.example.toml` holds placeholders only: example-zone-a to example-zone-d for the VPC, and
example-zone-a, example-zone-b and example-zone-d for the coursework pool, so that the synthesis
and the tests of E3-54 see a pool that leaves out one of the first three zones and takes a fourth.
The mock pool gives no list.

**Where the check runs.** Synthesis has no AWS access, so whether a zone offers an instance type
can only be asked with credentials. The owner's configuration tool asks it, not the pipeline's
synth step: `python -m rhi.owner_config write` is where a configuration enters SSM (system design,
section 8.6, step 4), so a wrong list is refused before anything is written. The synth step would
fail only after a merge, and its role would need EC2's Describe calls, which AWS grants only on
every resource: a new entry acknowledged under `tests/policy/` and a change to the protected
pipeline (`src/rhi/pipeline/`). `check` stays offline, as the walkthrough's dry run requires
(`setup/owner-setup.sh`).

Before `write` writes any parameter, it asks EC2, with the caller's credentials and region:

- DescribeAvailabilityZones, filtered to the zone type availability-zone: the region's zones,
  sorted by name;
- DescribeInstanceTypeOfferings, with the location type availability-zone, filtered to the
  instance types the pools list, every page: which zone offers which type.

It refuses, with exit status 1 and nothing written, a configuration in which:

1. a zone of the top-level list is not one of the region's zones;
2. the top-level list does not begin with the region's first three zones by name, which are the
   zones `Fn::GetAZs` gave the deployed VPC;
3. a zone of a pool does not offer one of the pool's instance types. A pool's zones are its own
   list; without one, the first three of the top-level list; without that, the region's first
   three zones by name. A pool that lists fallback types is held to every one of them in every
   one of its zones.

The coursework pool's present entry is refused by the third until it lists its zones, which is
the point.

The tool prints no value of the file (its docstring): a refusal names the pool, and the zone and
the instance type by their positions in the configuration (the pool's third zone, its second
instance type), never a zone's name, an instance type or AWS's text; an error of the SDK is
refused naming the call that failed. The EC2 client is made like the SSM client of
`src/rhi/config.py` (`_ssm`), and a caller may give its own, so the tests give a stand-in and need
no AWS and no network. boto3 is already a dependency, and is imported only when the check runs:
the loader and the file writer still load without the SDK (`tests/unit/test_config_ssm.py`).

**What a deployment of this issue replaces.** Nothing of the network or of the groups. The
gateway's image and the scaler's bundle carry `rhi_common`, so the merge rolls the gateway's tasks
and updates the scaler's function, as any change to that package does. Once the owner writes the
coursework pool's list, the pool's entry in the gateway's environment and in the scaler's settings
holds it, which rolls the gateway's tasks once more at the next deployment.

Suggested branch: `feat/pool-zones-config`

## Deliverables

- [ ] `services/common/rhi_common/pool_config.py` reads a pool's optional zones field, a list of at least one zone name with none named twice, held on the pool as a tuple and as None when left out, and refuses any other value with a PoolConfigError that names the pool and the field.
- [ ] `src/rhi/config.py` reads the optional top-level key zones, at least three zone names with none named twice and no two ending in the same letter, held as a tuple and as None when left out, and named in OPTIONAL.
- [ ] `src/rhi/config.py` refuses a pool whose zones name a zone the top-level key does not list, and a pool that gives zones when the top-level key is left out, with a ConfigError that names the key and the pool and never a value.
- [ ] Both lists are written by write_ssm and write_file and read back by the ssm and file sources as the same configuration.
- [ ] `config.example.toml` gives zones as example-zone-a, example-zone-b, example-zone-c and example-zone-d, and the coursework pool's zones as example-zone-a, example-zone-b and example-zone-d, with comments that say what each list is, its default, and that a zone is only ever appended to the top-level list.
- [ ] `src/rhi/owner_config.py` write runs the zone check of the Context, with the two EC2 calls it names, before it writes any parameter, and refuses with exit status 1 each of the three cases the Context numbers, naming no zone, instance type or text of AWS.
- [ ] `tests/unit/common/test_pool_config.py`, `tests/unit/test_config.py`, `tests/unit/test_config_ssm.py` and a new `tests/unit/test_owner_zones.py` cover the two lists and the check, and the write tests of `tests/unit/test_owner_setup.py` give the check a stand-in EC2 client.

## Acceptance criteria

- [ ] `tests/unit/test_owner_zones.py` holds, with stand-in EC2 and SSM clients, that write puts no parameter and exits 1 for a pool zone that does not offer one of the pool's instance types, for a zone that is not the region's, and for a top-level list whose first three are not the region's first three by name, and that it writes the example when every type is offered in every zone.
- [ ] `tests/unit/test_owner_zones.py` holds that a pool without its own list is checked against the first three zones of the top-level list, and against the region's first three by name when the configuration gives no list, and that no refusal's text holds a zone's name or an instance type.
- [ ] `tests/unit/test_config.py` holds that the example loads with its four zones and the coursework pool's three, that a configuration that gives neither list loads with both None, and that each refusal of the Deliverables names the key and holds no value.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py tests/unit/test_config.py tests/unit/test_config_ssm.py tests/unit/test_owner_setup.py tests/unit/test_owner_zones.py` passes, the round trip of both lists through SSM and through a file among them.
- [ ] The owner adds the top-level zones and the coursework pool's zones to the configuration file and writes it with python -m rhi.owner_config write, which the check accepts (needs aws:account).

## References

- `src/rhi/config.py`, the module's docstring ("The pools", "Adding a key"), `_read_against_the_pools` and `_ssm`
- `services/common/rhi_common/pool_config.py`, the module's docstring ("Adding a field")
- `src/rhi/owner_config.py`
- `src/rhi/network/stack.py`, `src/rhi/network/vpc.py` and `src/rhi/pools/group.py`
- `setup/owner-setup.sh`, the configuration step
- `docs/design/system-design.md`, sections 5.4, 8.6 and 10
