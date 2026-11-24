+++
id = "E3-54"
title = "The VPC spans the configured zones, and each pool's group launches only in its own"
labels = ["size:M", "kind:infra"]
epic = 5
blocked_by = ["E3-53"]
+++

## Context

Issue E3-53 puts the zones in the configuration: zones, the zones the VPC spans in the order of its
subnets, and each pool's own zones, some of those (`src/rhi/config.py`,
`rhi_common.pool_config`). This issue builds the network and the groups from them, so that the
coursework pool's group launches only in zones that offer its instance types, one the VPC does not
span today among them (system design, section 5.4).

**The network.** The VPC spans the configured zones, in their order (`src/rhi/network/stack.py`;
`src/rhi/network/vpc.py`, which today requires exactly three). The CDK names each subnet after its
position and allocates the CIDR blocks in that order, so the first three zones keep their subnets,
PublicSubnet1 to 3 and PrivateSubnet1 to 3, with their logical IDs and CIDR blocks, and a fourth
zone adds PublicSubnet4 (10.0.3.0/24) and PrivateSubnet4 (10.0.64.0/20) with their route tables,
associations and default routes. The NAT gateway stays in the first public subnet, and every
private route table routes to it. A configuration without zones synthesizes the network of today.

The template names no zone in full, so the stacks stay environment-agnostic (`AGENTS.md`), and
cfn-lint, which warns on a zone written as a literal (W3010) where `tests/synth/test_cfn_lint.py`
holds every template to no finding, has nothing to report:

- the first three zones are written as today, `Fn::Select` of positions 0, 1 and 2 over
  `Fn::GetAZs`. E3-53's check holds the configured first three to the region's first three by
  name, which are the zones `Fn::GetAZs` gives, so the template of the deployed subnets does not
  change at all;
- a further zone is written as the deploying region followed by the last letter of its name
  (`Fn::Join` of `AWS::Region` and the letter): a zone's name is its region's followed by one
  letter, and E3-53 refuses two zones that end in the same letter.

**The groups.** Each pool's group spans the private subnets of exactly its own zones; a pool
without its own list spans those of the first three zones, as every group does today
(`src/rhi/pools/group.py`, `src/rhi/pools/stack.py`). Everything else that selects subnets by
their type keeps doing so, and spans every zone: the public load balancer, the internal one, the
gateway's service and Grafana's. That matters for the internal load balancer: a target registered
in a zone the load balancer does not span receives no traffic from it, so it must span every zone
a group launches in.

**What a deployment replaces**, once the owner's configuration gives the zones (E3-53's owner
step) with one zone added after the first three:

- nothing of the network is replaced or modified: the three subnets, their route tables and
  routes, the NAT gateway and the internet gateway keep their template. The added zone's public
  and private subnets, route tables, associations and default routes are created, with exports of
  the new subnets for the stacks that import them, and the S3 gateway endpoint gains the new
  private route table, in place;
- both load balancers gain the added zone's subnet, in place;
- the gateway's and Grafana's services gain the added private subnet, which starts a deployment of
  each: new tasks start before the old ones stop;
- the coursework group's subnets change, in place, to those of its zones. It has no instance,
  running or warm, in a zone it leaves, since that zone does not offer its type;
- the mock group's template does not change.

No export of the network stack disappears: the internal load balancer and the two services still
import every private subnet, so CloudFormation never refuses to delete an export in use. The
invariants under `tests/policy/` hold unchanged; this issue changes none of them.

Suggested branch: `feat/pool-zones-network`

## Deliverables

- [ ] `src/rhi/network/stack.py` and `src/rhi/network/vpc.py` give the VPC the configuration's zones in their order, at least three, the first three written as Fn::Select of positions 0 to 2 over Fn::GetAZs and each further zone as Fn::Join of AWS::Region and the last letter of its name, and the three zones of today when the configuration gives none.
- [ ] `src/rhi/pools/group.py` and `src/rhi/pools/stack.py` give each pool's group the private subnets of exactly the pool's zones, and a pool without its own zones the private subnets of the VPC's first three zones.
- [ ] `docs/design/system-design.md` says, in sections 5.4, 6.5 and 17, that the VPC spans the zones the configuration lists, a zone only ever appended, and each pool's group the zones its pool lists, by default the VPC's first three, with the reason: an instance type is not offered in every zone.
- [ ] `tests/synth/test_vpc.py` and a new `tests/synth/test_pool_zones.py` cover the network and the groups on the example configuration and on one that gives no zones.

## Acceptance criteria

- [ ] `tests/synth/test_vpc.py` holds that the example's network has four public and four private subnets, the fourth of each in the zone Fn::Join of AWS::Region and d, that the network's template holds the text example-zone nowhere, and that every subnet, route table, route, association and NAT gateway of the network synthesized without zones is in the example's network under the same logical ID with the same properties.
- [ ] `tests/synth/test_pool_zones.py` holds that on the example the coursework group's subnets are the private subnets of the first, second and fourth zones and no other, the mock group's those of the first three, and the internal load balancer's every private subnet of the VPC.
- [ ] `uv run --locked pytest tests/synth tests/policy` passes on the example's four zones, cfn-lint's test among them, with no file under `tests/policy/` changed.
- [ ] `scripts/check.sh synth` exits 0.
- [ ] Once the owner's zones are deployed, no launch of the coursework group fails as unsupported in its zone, as the group's scaling activities since that deployment show (aws autoscaling describe-scaling-activities for the group rhi-pool-coursework) (needs aws:deployed-environment).

## References

- `src/rhi/network/stack.py`, `NetworkStack.availability_zones`
- `src/rhi/network/vpc.py`, `Network` and `ZONES`
- `src/rhi/pools/group.py`, `PoolGroup`
- `src/rhi/pools/routing.py`, the internal load balancer
- `tests/synth/test_vpc.py`, `tests/synth/test_pool_group.py` and `tests/synth/test_cfn_lint.py`
- `docs/design/system-design.md`, sections 5.4, 6.5 and 17
