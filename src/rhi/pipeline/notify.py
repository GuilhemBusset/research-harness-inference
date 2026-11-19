"""The owner's topic of the pipeline stack: what a public commit status may not carry (system
design, section 8.4).

A commit status is public, so it holds a state, a fixed context and allow-listed parts only. The
rest of a failure, the failed action's error message and the status reason of each failed
resource, is text from AWS that a person needs to diagnose it: "Anything beyond that goes to the
owner by SNS." The reporter of `rhi.pipeline.reporter` publishes it to this topic.

The pipeline exists from phase 0, before the stage's alarms and their topic, so the pipeline stack
has a topic of its own:

- encrypted at rest with the key AWS manages for SNS. That key's policy lets the account's
  principals use it through SNS, so a publisher needs no grant on it, and the topic adds no key
  of its own to pay for and keep;
- refusing a publication that does not use TLS: its topic policy denies every request whose
  `aws:SecureTransport` is false;
- with one subscription, by e-mail, to the admin address of the configuration (section 8.6,
  step 4). The address comes from the configuration at synthesis, never from the repository. The
  owner confirms the subscription once, from the e-mail it sends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_cdk import aws_sns as sns
from constructs import Construct

if TYPE_CHECKING:
    from rhi.config import Config

# The key AWS manages for SNS, in the deploying account.
SNS_KEY_ALIAS = "alias/aws/sns"


class OwnerTopic(Construct):
    """The topic the owner receives a failure's details on, by e-mail."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config) -> None:
        super().__init__(scope, construct_id)
        self.topic = sns.Topic(self, "Topic", enforce_ssl=True)
        # By the alias SNS documents for its own key: an alias's ARN would be built from the account.
        topic = self.topic.node.default_child
        assert isinstance(topic, sns.CfnTopic)
        topic.kms_master_key_id = SNS_KEY_ALIAS
        # A construct of a fixed name: CDK names an e-mail subscription, and its logical ID, after
        # the address, which is private.
        # ty reads the CDK's generated classes as unlike the protocols they implement where a method
        # names a parameter otherwise: jsii passes it by position.
        sns.Subscription(
            self,
            "Subscription",
            topic=self.topic,  # ty: ignore[invalid-argument-type]
            protocol=sns.SubscriptionProtocol.EMAIL,
            endpoint=config.admin_email,
        )
