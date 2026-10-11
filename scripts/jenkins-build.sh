#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${JENKINS_URL:?set JENKINS_URL}"
: "${JENKINS_USER:?set JENKINS_USER}"
: "${JENKINS_TOKEN:?set JENKINS_TOKEN}"
: "${JOB_NAME:?set JOB_NAME}"

POLL_SECONDS="${POLL_SECONDS:-10}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-1500}"
JENKINS_URL="${JENKINS_URL%/}"
JOB_URL="${JENKINS_URL}/job/${JOB_NAME}"

work=$(mktemp -d)
trap 'rm -rf "${work}"' EXIT
auth="${work}/auth.conf"
cookies="${work}/cookies"
auth_value=$(printf '%s:%s' "${JENKINS_USER}" "${JENKINS_TOKEN}" | sed 's/\\/\\\\/g; s/"/\\"/g')
printf 'user = "%s"\n' "${auth_value}" > "${auth}"
unset auth_value

jenkins() {
    curl -sS --fail --max-time 30 -K "${auth}" -b "${cookies}" -c "${cookies}" "$@"
}

summary() {
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        printf '%s\n' "$@" >> "${GITHUB_STEP_SUMMARY}"
    fi
}

deadline=$((SECONDS + TIMEOUT_SECONDS))
check_deadline() {
    if [ "${SECONDS}" -ge "${deadline}" ]; then
        echo "gave up after ${TIMEOUT_SECONDS}s: $1"
        exit 1
    fi
}

crumb_json=$(jenkins "${JENKINS_URL}/crumbIssuer/api/json")
crumb=$(jq -r '.crumb' <<< "${crumb_json}")
crumb_field=$(jq -r '.crumbRequestField' <<< "${crumb_json}")

headers=$(jenkins -X POST -H "${crumb_field}: ${crumb}" -D - -o /dev/null "${JOB_URL}/build")
queue_id=$(tr -d '\r' <<< "${headers}" | sed -n 's|^[Ll]ocation:.*/queue/item/\([0-9][0-9]*\)/*$|\1|p')
if [ -z "${queue_id}" ]; then
    echo "Jenkins accepted the request but returned no queue item"
    exit 1
fi
echo "queued as item ${queue_id}"

build_number=""
while [ -z "${build_number}" ]; do
    check_deadline "the build never left the queue"
    item=$(jenkins "${JENKINS_URL}/queue/item/${queue_id}/api/json")
    if [ "$(jq -r '.cancelled // false' <<< "${item}")" = "true" ]; then
        echo "the queued build was cancelled"
        exit 1
    fi
    build_number=$(jq -r '.executable.number // empty' <<< "${item}")
    if [ -z "${build_number}" ]; then
        sleep "${POLL_SECONDS}"
    fi
done

build_url="${JOB_URL}/${build_number}/"
echo "running ${build_url}"

result=""
while [ -z "${result}" ]; do
    check_deadline "build ${build_number} is still running"
    sleep "${POLL_SECONDS}"
    result=$(jenkins "${build_url}api/json?tree=building,result" | jq -r 'if .building then empty else (.result // empty) end')
done

echo "build ${build_number} finished: ${result}"
summary "## Deploy" "" "- Jenkins job: \`${JOB_NAME}\`" "- Build: [#${build_number}](${build_url})" "- Result: **${result}**" "- Commit: \`${GITHUB_SHA:-local}\`"

if [ "${result}" != "SUCCESS" ]; then
    jenkins "${build_url}consoleText" | tail -n 60 || true
    exit 1
fi
