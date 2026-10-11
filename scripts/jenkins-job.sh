#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
    cat <<'TEXT'
Creates or updates the Jenkins pipeline job that builds and deploys a service.

    ./scripts/jenkins-job.sh <job-name> <owner/repo>

The job checks out main from https://github.com/<owner/repo>.git with the
github-credentials credential and runs the Jenkinsfile at the repository root.
It has no trigger of its own: .github/workflows/deploy.yml starts it.

Reads the Jenkins address, user and API token from op://<vault>/jenkins-admin
(vault k3s-infrastructure unless VAULT= is set). JENKINS_URL= overrides the address.
TEXT
}

if [ "$#" -ne 2 ]; then
    usage
    exit 1
fi

JOB="$1"
REPO="$2"
VAULT="${VAULT:-k3s-infrastructure}"

if ! [[ "${JOB}" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "job name must be lowercase letters, digits and dashes: ${JOB}"
    exit 1
fi
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    echo "repository must look like owner/name: ${REPO}"
    exit 1
fi

for tool in op curl; do
    command -v "${tool}" >/dev/null 2>&1 || { echo "missing: ${tool}"; exit 1; }
done

if [ -z "${JENKINS_URL:-}" ]; then
    JENKINS_URL=$(op read "op://${VAULT}/jenkins-admin/website")
fi
JENKINS_URL="${JENKINS_URL%/}"
jenkins_user=$(op read "op://${VAULT}/jenkins-admin/username")
jenkins_token=$(op read "op://${VAULT}/jenkins-admin/token")

work=$(mktemp -d)
trap 'rm -rf "${work}"' EXIT
auth="${work}/auth.conf"
cookies="${work}/cookies"
auth_value=$(printf '%s:%s' "${jenkins_user}" "${jenkins_token}" | sed 's/\\/\\\\/g; s/"/\\"/g')
printf 'user = "%s"\n' "${auth_value}" > "${auth}"
unset auth_value jenkins_token

status_of() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 20 -K "${auth}" -b "${cookies}" -c "${cookies}" "$@" || echo 000
}

crumb_json=$(curl -sS --fail --max-time 20 -K "${auth}" -b "${cookies}" -c "${cookies}" "${JENKINS_URL}/crumbIssuer/api/json")
crumb=$(sed -n 's/.*"crumb":"\([^"]*\)".*/\1/p' <<< "${crumb_json}")
crumb_field=$(sed -n 's/.*"crumbRequestField":"\([^"]*\)".*/\1/p' <<< "${crumb_json}")

config="${work}/config.xml"
cat > "${config}" <<XML
<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <description>Builds ${REPO} and deploys it to k3s. Started by .github/workflows/deploy.yml.</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty/>
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition" plugin="workflow-cps">
    <scm class="hudson.plugins.git.GitSCM" plugin="git">
      <configVersion>2</configVersion>
      <userRemoteConfigs>
        <hudson.plugins.git.UserRemoteConfig>
          <url>https://github.com/${REPO}.git</url>
          <credentialsId>github-credentials</credentialsId>
        </hudson.plugins.git.UserRemoteConfig>
      </userRemoteConfigs>
      <branches>
        <hudson.plugins.git.BranchSpec>
          <name>*/main</name>
        </hudson.plugins.git.BranchSpec>
      </branches>
      <doGenerateSubmoduleConfigurations>false</doGenerateSubmoduleConfigurations>
      <submoduleCfg class="empty-list"/>
      <extensions/>
    </scm>
    <scriptPath>Jenkinsfile</scriptPath>
    <lightweight>true</lightweight>
  </definition>
  <triggers/>
  <disabled>false</disabled>
</flow-definition>
XML

if [ "$(status_of "${JENKINS_URL}/job/${JOB}/api/json")" = "200" ]; then
    target="${JENKINS_URL}/job/${JOB}/config.xml"
    verb="updated"
else
    target="${JENKINS_URL}/createItem?name=${JOB}"
    verb="created"
fi

code=$(status_of -X POST \
    -H "${crumb_field}: ${crumb}" \
    -H "Content-Type: application/xml" \
    --data-binary "@${config}" \
    "${target}")
if [ "${code}" != "200" ]; then
    echo "Jenkins answered ${code} to ${target}"
    exit 1
fi

if [ "$(status_of "${JENKINS_URL}/job/${JOB}/api/json")" != "200" ]; then
    echo "Jenkins accepted the job but ${JENKINS_URL}/job/${JOB}/ does not answer"
    exit 1
fi

echo "${verb} ${JENKINS_URL}/job/${JOB}/"
