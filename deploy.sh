#!/usr/bin/env bash
# Deploy the terrain viewer to the DigitalOcean droplet over Tailscale.
#
#   ./deploy.sh            # push site/ and keep it PUBLIC on :8443 via Funnel
#   ./deploy.sh --private  # push, but restrict it to the tailnet
#
# The default is public on purpose. `tailscale serve` and `tailscale funnel` both
# rewrite the config for a port, so a tailnet-only deploy silently tears down an
# existing Funnel -- a routine "push the new build" would quietly take the site
# off the internet. Defaulting to Funnel makes the published state survive a deploy,
# and taking it down is then explicit.
#
# Port 8443 is used deliberately: port 443 on this host serves Gitea, and
# Tailscale Funnel is enabled per-PORT, not per-path. Funnelling 443 would
# publish Gitea too.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST=/var/www/terrain
PORT=8443
SRC="$HERE/site"

# The target host is deliberately not in the repo. Put it in deploy.env next to this
# script (gitignored), or set it in the environment:
#
#     TERRAIN_HOST=root@100.x.y.z
#
if [ -f "$HERE/deploy.env" ]; then . "$HERE/deploy.env"; fi
HOST="${TERRAIN_HOST:?not set -- put TERRAIN_HOST=user@host in deploy.env or the environment}"

# Parsed up front so a typo fails before the 44 MB upload, and so an unrecognised
# flag can never fall through to the public default.
case "${1:-}" in
  ""|--public)               PUBLIC=1 ;;
  --private|--tailnet-only)  PUBLIC=0 ;;
  *) echo "usage: $0 [--public|--private]" >&2; exit 2 ;;
esac

# Keep the pre-gzipped copies in step with the sources; the static file server
# does not negotiate content-encoding, so the browser inflates them itself.
[ -f "$SRC/models/terrain.bin" ] && gzip -9 -kf "$SRC/models/terrain.bin"
[ -f "$SRC/models/points.bin" ]  && gzip -9 -kf "$SRC/models/points.bin"

echo "==> pushing $SRC -> $HOST:$DEST"
COPYFILE_DISABLE=1 tar czf - -C "$SRC" . \
  | ssh "$HOST" "rm -rf $DEST && mkdir -p $DEST && tar xzf - -C $DEST && du -sh $DEST"

if [ "$PUBLIC" = 1 ]; then
  echo "==> PUBLIC on :$PORT via Funnel"
  echo "    (requires the 'funnel' nodeAttr in your tailnet ACL policy)"
  ssh "$HOST" "tailscale funnel --bg --https=$PORT $DEST"
else
  echo "==> tailnet-only on :$PORT -- this REMOVES any existing Funnel"
  ssh "$HOST" "tailscale serve --bg --https=$PORT $DEST"
fi

ssh "$HOST" "tailscale serve status"

# Assert we actually landed in the requested state. `tailscale funnel` can no-op
# silently when the tailnet lacks the funnel nodeAttr, so "it printed no error" is
# not evidence the site is reachable. Also fails loudly if 443 ever gets funnelled,
# since that would publish Gitea.
echo "==> verifying exposure"
ssh "$HOST" "tailscale serve status --json" | PORT="$PORT" PUBLIC="$PUBLIC" python3 -c '
import json, os, sys
port, want = os.environ["PORT"], os.environ["PUBLIC"] == "1"
allow = json.load(sys.stdin).get("AllowFunnel") or {}
public = sorted(hp for hp, on in allow.items() if on)
got = any(hp.endswith(":" + port) for hp in public)
label = lambda b: "public" if b else "tailnet-only"
print("    public endpoints: " + (", ".join(public) or "(none)"))
if any(hp.endswith(":443") for hp in public):
    sys.exit("    !! port 443 is funnelled -- that publishes Gitea. Fix now.")
if got != want:
    sys.exit("    !! :%s is %s, expected %s" % (port, label(got), label(want)))
print("    :%s is %s, as requested" % (port, label(got)))
'
