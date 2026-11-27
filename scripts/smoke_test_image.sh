#!/usr/bin/env bash
# Smoke test of a built image, as CI runs it on every pull request and the
# release workflow on each platform of the published image:
#
#   scripts/smoke_test_image.sh IMAGE [VERSION] [PLATFORM]
#
# VERSION: the expected src.__version__ (the build's --build-arg VERSION).
# PLATFORM: e.g. linux/arm64, to test one platform of a multi-platform image
# (other architectures need QEMU, e.g. docker/setup-qemu-action).
#
# Checks: the runtime pieces of yt-dlp (deno, curl_cffi, yt-dlp-ejs), ffmpeg,
# the unprivileged user, every entrypoint importing on a read-only root
# filesystem with the environment the Helm chart renders, and the GUI process
# starting and answering /health.
set -euo pipefail

image="${1:?usage: $0 IMAGE [VERSION] [PLATFORM]}"
version="${2:-}"
platform="${3:-}"

run=(docker run --rm)
if [ -n "$platform" ]; then
  run+=(--platform "$platform")
fi
# Like Kubernetes with the chart: read-only root filesystem, a writable /tmp
# and a data volume owned by the app user (an anonymous volume, initialised
# from the image: recent Docker versions mount `--tmpfs /app/data` as root).
read_only=(--read-only --tmpfs /tmp -v /app/data)
# Numbers as plain strings and the DB on the data volume, as the chart does.
app_env=(
  -e BOT_TOKEN=123456:smoke-test-not-a-token
  -e SECRET_KEY=smoke-test-secret-key-0123456789abcdef0123456789
  -e ADMIN_TOKEN=smoke-test-admin-token-0123456789
  -e TELEGRAM_MAX_FILE_SIZE=52428800
  -e REQUESTS_DB=/app/data/requests.db
)

step() { printf '\n== %s (%s%s)\n' "$1" "$image" "${platform:+, $platform}"; }

step "yt-dlp runtime pieces"
"${run[@]}" "$image" python -c "import curl_cffi, yt_dlp, yt_dlp_ejs; print('yt-dlp', yt_dlp.version.__version__)"
"${run[@]}" "$image" python -m yt_dlp --version
"${run[@]}" "${read_only[@]}" "$image" deno eval "console.log('deno', Deno.version.deno)"

step "ffmpeg and ffprobe"
for tool in ffmpeg ffprobe; do
  banner="$("${run[@]}" "$image" "$tool" -hide_banner -version)"
  echo "${banner%%$'\n'*}"
done

step "unprivileged user"
uid="$("${run[@]}" "$image" id -u)"
echo "uid $uid"
test "$uid" = 1000

if [ -n "$version" ]; then
  step "version"
  built="$("${run[@]}" "$image" python -c 'import src; print(src.__version__)')"
  echo "src.__version__ = $built"
  test "$built" = "$version"
fi

step "entrypoints import on a read-only root filesystem"
"${run[@]}" "${read_only[@]}" "${app_env[@]}" "$image" \
  python -c "import src.config, src.db, src.providers, src.downloader, src.media, src.worker, src.bot, src.gui; src.db.init_db(); print('imports ok')"

step "the GUI starts and answers /health"
start=(docker run -d)
if [ -n "$platform" ]; then
  start+=(--platform "$platform")
fi
cid="$("${start[@]}" "${read_only[@]}" "${app_env[@]}" -e HEALTH_DEBUG=true -e PORT=8081 \
  -p 127.0.0.1::8081 "$image" uvicorn src.gui:app --host 0.0.0.0 --port 8081)"
trap 'docker rm -f -v "$cid" > /dev/null 2>&1 || true' EXIT
address="$(docker port "$cid" 8081/tcp)"
port="${address%%$'\n'*}"
port="${port##*:}"
body=""
for _ in $(seq 1 90); do
  if body="$(curl -fsS "http://127.0.0.1:${port}/health" 2> /dev/null)"; then
    break
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$cid")" != "true" ]; then
    docker logs "$cid" || true
    echo "the GUI container exited" >&2
    exit 1
  fi
  sleep 1
done
echo "$body"
if [ -z "$body" ]; then
  docker logs "$cid" || true
  echo "the GUI did not answer /health" >&2
  exit 1
fi
if [ -n "$version" ]; then
  grep -q "\"version\":\"${version}\"" <<< "$body"
fi

echo
echo "smoke test passed: $image${platform:+ ($platform)}"
