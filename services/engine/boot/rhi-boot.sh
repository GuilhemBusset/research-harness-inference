#!/bin/bash
# The boot program: at every start of an instance it fetches the pool's current release, checks
# it and starts it (system design, "What happens on an instance"). rhi-boot.service runs it.
#
# The instance's tag rhi-release-parameter names the pool's release parameter. The tag and the
# region come from the instance metadata, read with a session token. Everything else comes from
# the parameter: the bucket, the key and the SHA-256 digest of the release's archive, the engine
# image, the settings, which every unit reads from /run/rhi/release.env, and the engine's flags,
# which the engine's unit reads from /run/rhi/engine-flags. The archive holds units/,
# collector/config.yaml and agent/.
#
# The units of units/gpu/ are for an instance that has an NVIDIA GPU, and are installed on no
# other. The PCI bus says which it is, whether or not the driver has finished loading: a device
# whose vendor file under /sys/bus/pci/devices reads 0x10de, NVIDIA's vendor ID.
#
# Nothing of the archive is unpacked, installed or started unless its bytes have the digest the
# parameter names. Whatever fails, the program ends with a failure before it starts a unit.
# The lifecycle agent reads the recorded release from /var/lib/rhi/release.json. The boot
# program publishes that record after installing the checked release and before starting the agent.
#
# It runs before any container, on both machine images, so it uses only bash, coreutils, tar,
# curl, the AWS CLI and the image's python3 with its standard library.
#
# RHI_BOOT_ROOT and RHI_BOOT_METADATA are for the tests, which run the program in a temporary
# root against a stand-in for the instance metadata. The unit sets neither.

set -euo pipefail

root="${RHI_BOOT_ROOT:-}"
metadata="${RHI_BOOT_METADATA:-http://169.254.169.254}"
release="$root/opt/rhi/release"
units="$root/run/systemd/system"
settings="$root/run/rhi/release.env"
flags="$root/run/rhi/engine-flags"
state="$root/var/lib/rhi"

fail() {
  echo "rhi-boot: $1" >&2
  exit 1
}

fetch() {   # fetch <curl arguments>: one answer of the instance metadata, which no proxy serves
  curl --silent --show-error --fail --noproxy '*' --connect-timeout 2 --max-time 10 --retry 3 "$@"
}

# The release of an earlier start goes first: a start that fails leaves none behind. The work
# folder is on the release's own volume, so the checked release moves into place in one step.
mkdir -p "$root/opt/rhi" "$state"
rm -f "$state/release.json"
rm -rf "$release" "$root/opt/rhi"/boot.*
work="$(mktemp -d "$root/opt/rhi/boot.XXXXXX")"
record=""
trap 'rm -rf "$work"; if [ -n "$record" ]; then rm -f "$record"; fi' EXIT
record="$(mktemp "$state/.release.XXXXXX")"

token="$(fetch --request PUT --header 'X-aws-ec2-metadata-token-ttl-seconds: 60' "$metadata/latest/api/token")" \
  || fail "the instance metadata gives no session token"
parameter="$(fetch --header "X-aws-ec2-metadata-token: $token" \
  "$metadata/latest/meta-data/tags/instance/rhi-release-parameter")" \
  || fail "the tag rhi-release-parameter cannot be read"
region="$(fetch --header "X-aws-ec2-metadata-token: $token" "$metadata/latest/meta-data/placement/region")" \
  || fail "the region cannot be read"
# The pool's engine, for a release whose settings name none, as those written before the settings
# had it: the launch template tags every instance of the pool with its engine (rhi.pools.group).
# Without the tag, the release's setting is the only source.
engine_tag="$(fetch --header "X-aws-ec2-metadata-token: $token" \
  "$metadata/latest/meta-data/tags/instance/rhi-engine" 2>/dev/null)" || engine_tag=""

aws ssm get-parameter --region "$region" --name "$parameter" --output json >"$work/parameter.json" \
  || fail "the release parameter cannot be read"

# The parameter's value is JSON (services/engine/lifecycle/rhi_lifecycle/release.py gives its
# keys). This writes the settings, one NAME=value line each, the engine's flags, an argument on
# each line, where the archive is, and the record for the lifecycle agent.
python3 -I - "$work" "$region" "$parameter" "$record" "$engine_tag" <<'PY' || fail "the release parameter does not hold a release"
import json
import os
import sys

work, region, parameter, record, engine_tag = sys.argv[1:]
SETTINGS = (
    "group", "launch_hook", "termination_hook", "log_group", "workspace_url", "heartbeat_table", "secret_id",
)


def text(value, name):
    # A value is one line of release.env, so it is a string with no control character.
    if not isinstance(value, str) or value == "" or not value.isprintable():
        raise ValueError(name)
    return value


try:
    with open(os.path.join(work, "parameter.json"), encoding="utf-8") as stream:
        release = json.loads(json.load(stream)["Parameter"]["Value"])
    files, settings = release["files"], release["settings"]
    image = text(release["engine_image"], "engine_image")
    revision = release["model_revision"]
    variables = [
        ("RHI_POOL", text(settings["pool"], "settings.pool")),
        ("RHI_REGION", text(region, "the region")),
        ("RHI_RELEASE_PARAMETER", text(parameter, "the tag")),
        ("RHI_ENGINE_IMAGE", image),
    ]
    variables += [("RHI_" + name.upper(), text(settings[name], "settings." + name)) for name in SETTINGS]
    # RHI_ENGINE, the engine's row of metric names the lifecycle agent reads the engine under: the
    # release's setting, or the instance's tag for a release that has none.
    engine = settings["engine"] if "engine" in settings else engine_tag
    variables.append(("RHI_ENGINE", text(engine, "settings.engine")))
    measured = release.get("measured_start_seconds")
    if measured is not None:
        if isinstance(measured, bool) or not isinstance(measured, int) or measured < 1:
            raise ValueError("measured_start_seconds")
        variables.append(("RHI_MEASURED_START", str(measured)))
    flags = release["engine_flags"]
    # An argument is one line of its file: a string with no control character. It may be empty.
    if not isinstance(flags, list) or not all(isinstance(flag, str) and flag.isprintable() for flag in flags):
        raise ValueError("engine_flags")
    archive = [text(files[name], "files." + name) for name in ("bucket", "key", "sha256")]
except (KeyError, TypeError, ValueError) as error:
    # The message names a key or a place in the text, never a value.
    sys.exit("rhi-boot: " + type(error).__name__ + ": " + str(error))

with open(os.path.join(work, "release.env"), "w", encoding="utf-8") as stream:
    stream.writelines(name + "=" + value + "\n" for name, value in variables)
with open(os.path.join(work, "engine-flags"), "w", encoding="utf-8") as stream:
    stream.writelines(flag + "\n" for flag in flags)
with open(os.path.join(work, "archive"), "w", encoding="utf-8") as stream:
    stream.writelines(line + "\n" for line in archive)
with open(record, "w", encoding="utf-8") as stream:
    json.dump({"engine_image": image, "model_revision": revision, "engine_flags": flags,
               "files": {"sha256": archive[2]}}, stream)
PY
mapfile -t archive <"$work/archive"

aws s3 cp --region "$region" --only-show-errors "s3://${archive[0]}/${archive[1]}" "$work/release.tar" \
  || fail "the release's archive cannot be fetched"

# The digest is that of the archive's bytes, as they were fetched: nothing is unpacked before.
digest="$(sha256sum <"$work/release.tar")"
[ "${digest%% *}" = "${archive[2]}" ] || fail "the archive does not have the digest the release parameter names"

mkdir "$work/release"
tar --extract --file "$work/release.tar" --directory "$work/release" --no-same-owner \
  || fail "the release's archive cannot be unpacked"
[ -d "$work/release/units" ] || fail "the release's archive holds no folder units"

mv "$work/release" "$release"
mkdir -p "$units" "${settings%/*}"
install -m 0644 "$work/release.env" "$settings"
install -m 0644 "$work/engine-flags" "$flags"
# The unit files at the top of units/. Of its subfolders only gpu/ is installed, and only on an
# instance that has an NVIDIA GPU: each file at its own path under systemd's folder, so a
# drop-in keeps the folder of its unit.
for unit in "$release/units"/*; do
  if [ -f "$unit" ]; then
    install -m 0644 "$unit" "$units/"
  fi
done
for vendor in "$root/sys/bus/pci/devices"/*/vendor; do
  if [ -f "$vendor" ] && [ "$(<"$vendor")" = 0x10de ] && [ -d "$release/units/gpu" ]; then
    cp --recursive "$release/units/gpu/." "$units/"
    break
  fi
done

systemctl daemon-reload
mv -f "$record" "$state/release.json"
# Without waiting for them: a unit ordered after this one would otherwise never start.
systemctl start --no-block rhi-collector.service rhi-lifecycle.service
