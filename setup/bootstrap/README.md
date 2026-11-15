# The bounded bootstrap

By default `cdk bootstrap` gives CloudFormation's execution role administrator access, so a merged
template could do anything in the account. This folder bootstraps the account with two policies of
the project instead (system design, section 8.4, control 1):

| Policy | Name | What carries it |
| --- | --- | --- |
| The execution policy | `rhi-cfn-execution` | CloudFormation's execution role, in place of `AdministratorAccess` |
| The permissions boundary | `rhi-permissions-boundary` | That role, and every role a deployment creates |

[`policies.yaml`](policies.yaml) is the CloudFormation template that creates both. Each allows
everything else and denies:

- creating an IAM user, an access key or a login profile;
- creating a SAML or an OpenID Connect provider;
- creating a role without the boundary, and removing a role's boundary or replacing it with
  another;
- changing or deleting either policy.

So a role that a deployment creates is refused without the boundary, and with it the role is held
to the same list, whatever policy the template gives it.

**Trust to another account is not in the list.** IAM has no condition key that reads a role's
trust policy, so neither policy can deny a role that another account may assume. The policy tests
hold that invariant, on the synthesized templates before a merge
([`tests/policy/test_trust.py`](../../tests/policy/test_trust.py)). The boundary only bounds what
such a role could do.

This folder is a protected path, and
[`tests/policy/test_bootstrap_policies.py`](../../tests/policy/test_bootstrap_policies.py) fails
when a denial leaves the template.

## The owner's two commands

**The owner runs them, once, with their own credentials** (system design, section 8.6, step 6).
No agent runs them. Run both from the repository root, after `npm ci --ignore-scripts`, with
credentials set to the region the endpoint lives in (system design, section 15, decision 9).

`123456789012` and `us-east-1` are placeholders: write the account's ID and that region in their
place, on the command line only, never in a file of this repository.

1. Deploy the policies:

   ```sh
   aws cloudformation deploy \
     --stack-name rhi-bootstrap-policies \
     --template-file setup/bootstrap/policies.yaml \
     --capabilities CAPABILITY_NAMED_IAM
   ```

2. Bootstrap with the execution policy and the boundary:

   ```sh
   RHI_CONFIG=example node_modules/.bin/cdk bootstrap aws://123456789012/us-east-1 \
     --cloudformation-execution-policies arn:aws:iam::123456789012:policy/rhi-cfn-execution \
     --custom-permissions-boundary rhi-permissions-boundary
   ```

The first command needs `CAPABILITY_NAMED_IAM` because the policies have fixed names. The policies
are global to the account; the bootstrap is per region.

The second command names its environment, and the CDK CLI still synthesizes the app first, because
the repository has a `cdk.json`. `RHI_CONFIG=example` gives that synthesis the example
configuration, as `scripts/check.sh synth` does: the bootstrap deploys nothing of the app, so the
real configuration is not needed yet.

## Checking the result

Only the account can show that the bootstrap took. Both of these read, and change nothing:

```sh
aws iam list-attached-role-policies --role-name cdk-hnb659fds-cfn-exec-role-123456789012-us-east-1
aws iam get-role --role-name cdk-hnb659fds-cfn-exec-role-123456789012-us-east-1 --query Role.PermissionsBoundary
```

The first lists `rhi-cfn-execution` and nothing else, so not `AdministratorAccess`. The second
names `rhi-permissions-boundary`.

## Changing a policy

The denials keep a deployment from changing either policy, so a change is the owner's too: merge
the change to `policies.yaml`, then run the first command again.

From the bootstrap on, a deployment that creates a role without the boundary fails. The app
therefore has to put the boundary on every role it creates, by the name above.
