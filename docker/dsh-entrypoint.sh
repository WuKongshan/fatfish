#!/bin/sh
set -eu

profile_dir="$DSH_HOME/profiles/web"
if [ ! -e "$profile_dir/node_modules/dsh-pet" ]; then
    dsh plugin --profile web add dsh-pet@0.4.7
fi

bind_ip="$(hostname -i | cut -d ' ' -f 1)"
exec dsh --profile web \
    --no-open \
    --host "$bind_ip" \
    --port 3080 \
    --public-url "$DSH_PUBLIC_URL" \
    --trusted-host "$DSH_TRUSTED_HOST"
