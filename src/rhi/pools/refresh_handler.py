"""The two handlers of a pool's instance refresh, which `rhi.pools.release` gives a function each.

`on_event` starts the refresh of the group a deployment names and `is_complete` says whether it
has ended, so the deployment does not finish before the refresh does. Instances protected from
scale-in are replaced, not waited for. A refresh that does not end well fails the deployment.

This file is the functions' whole code, carried in the template: it imports the runtime's AWS
SDK and nothing of the repository.
"""

import time

import boto3
from botocore.exceptions import ClientError

# A new instance is protected from scale-in, and a refresh waits an hour on a protected instance
# and then fails, unless it is told to replace it.
PREFERENCES = {"ScaleInProtectedInstances": "Refresh"}
SUCCESSFUL = "Successful"
RUNNING = ("Pending", "InProgress", "Baking")
# A group has one refresh at a time. One still running began before this release was written, so
# it is cancelled and a new one started, once the group lets go of the old one.
IN_PROGRESS = "InstanceRefreshInProgress"
NONE_TO_CANCEL = "ActiveInstanceRefreshNotFound"
CANCEL_SECONDS = 600
PAUSE_SECONDS = 15


def _code(error):
    return error.response.get("Error", {}).get("Code")


def on_event(event, _context=None, autoscaling=None, sleep=time.sleep):
    """Start the refresh when the resource is created or its properties change."""
    if event["RequestType"] == "Delete":
        return {}
    autoscaling = autoscaling or boto3.client("autoscaling")
    group = event["ResourceProperties"]["Group"]
    waited = 0
    while True:
        try:
            started = autoscaling.start_instance_refresh(AutoScalingGroupName=group, Preferences=PREFERENCES)
            return {"Data": {"RefreshId": started["InstanceRefreshId"]}}
        except ClientError as error:
            if _code(error) != IN_PROGRESS or waited >= CANCEL_SECONDS:
                raise
        try:
            autoscaling.cancel_instance_refresh(AutoScalingGroupName=group)
        except ClientError as error:
            if _code(error) != NONE_TO_CANCEL:
                raise
        sleep(PAUSE_SECONDS)
        waited += PAUSE_SECONDS


def is_complete(event, _context=None, autoscaling=None):
    """Whether the refresh `on_event` started has ended. One that ended otherwise than well raises."""
    if event["RequestType"] == "Delete":
        return {"IsComplete": True}
    autoscaling = autoscaling or boto3.client("autoscaling")
    found = autoscaling.describe_instance_refreshes(
        AutoScalingGroupName=event["ResourceProperties"]["Group"], InstanceRefreshIds=[event["Data"]["RefreshId"]]
    )["InstanceRefreshes"]
    status = found[0]["Status"] if found else "unknown"
    if status == SUCCESSFUL:
        return {"IsComplete": True}
    if status in RUNNING:
        return {"IsComplete": False}
    # The status is one of Auto Scaling's own words, and goes to the stack's events only.
    raise RuntimeError("the instance refresh did not succeed: " + status)
