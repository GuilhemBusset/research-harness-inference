# Acknowledged findings

Some IAM actions are granted by AWS only on every resource, and cdk-nag raises findings that a
construct cannot always fix. Both are accepted only with a reason. The list of accepted ones is
itself an invariant ([system design](../../../docs/design/system-design.md), section 8.4,
control 2), so this folder is a protected path like the rest of `tests/policy/`.

## One file per construct module

Each construct module of the app has its own TOML file here, so that issues on different modules
never change the same file. A file is named after the module's path under `src/rhi/`, with `/`
replaced by `_` and without `.py`:

| Module | File |
| --- | --- |
| `src/rhi/pools/group.py` | `pools_group.toml` |
| `src/rhi/stage.py` | `stage.toml` |
| `src/rhi/pipeline/stack.py` | `pipeline_stack.toml` |

A module with nothing to acknowledge has no file. A file whose name is not that of a module of
the source tree fails the tests.

## The two tables

A file has at most two tables, and no other key:

```toml
[nag]
"AwsSolutions-S1" = "The bucket holds the access logs themselves, which are not logged again."
"AwsSolutions-IAM5[Resource::*]" = "The Describe calls of EC2 have no resource to name."

[wildcard_actions]
"ec2:DescribeInstances" = "AWS grants the Describe calls of EC2 only on every resource."
```

- `[nag]`: a key is a cdk-nag finding id, exactly as cdk-nag reports it. That is a rule id
  (`AwsSolutions-S1`), or a rule id with its finding in brackets
  (`AwsSolutions-IAM5[Resource::*]`). cdk-nag 3 acknowledges each finding by its own id: there is
  no prefix matching, so a rule id alone does not cover its bracketed findings. One key is not a
  finding id as it stands: cdk-nag names a finding after its resource, so a statement on a pool's
  resource brings a finding that holds the pool's name, and no file here names the pools of a
  deployed configuration. A key that holds `<pool>` in place of the name
  (`...:log-group:/rhi/pools/<pool>:*]`) lists that finding for each pool of the configuration,
  and for no other name. The example's pool keeps an entry under its own name beside it, with the
  same reason: `test_nag_acknowledgements.py` holds the example's templates to the ids as cdk-nag
  writes them.
- `[wildcard_actions]`: a key is one IAM action that AWS grants only on every resource
  (`ec2:DescribeInstances`, `ecr:GetAuthorizationToken`, `cloudwatch:PutMetricData`). The key is
  the action as the policy statement writes it. `*`, every action, is never accepted.

Both kinds of key hold characters that TOML does not allow in a bare key, so a key is quoted.

Every entry maps its key to its reason, a string of at least one sentence: at least three words,
ending with a full stop. An entry with an empty reason, or with a word in place of one, fails the
tests.

An entry covers the resources its module created, and no other: the same statement on a role
that another module created needs its own entry in that module's file.

## Which module created a resource

The module that defines the class of the resource's nearest enclosing construct whose class is
defined under `src/rhi/`.

- A role's default policy is a child of the role, so a policy statement belongs to the module
  that created the role or the policy that holds it.
- Every stack is a class defined under `src/rhi/`, or is created inside one, so every resource
  has such a construct. A resource that has none fails the tests.

## Who reads the files

- `tests/policy/conftest.py` gives every policy test the templates of the app, the module that
  created each resource, and the tables of these files.
- `tests/policy/test_acknowledged.py` holds every file to the format above.
- `tests/policy/test_iam_wildcards.py` reads the `[wildcard_actions]` tables.
- The cdk-nag tests read the `[nag]` tables.

An issue that adds or changes a construct module names that module's file in its deliverables.
