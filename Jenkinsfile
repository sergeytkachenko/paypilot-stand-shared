pipeline {
    agent none

    environment {
        APP = 'paypilot-stand'
        NAMESPACE = 'paypilot-stand'
        RELEASE = 'paypilot-stand'
        CHART = 'install/helm'
        REGISTRY = 'docker-registry.docker-registry.svc.cluster.local:5000'
        IMAGE = "${REGISTRY}/${APP}"
        DOCKER_BUILDKIT = '1'
    }

    options {
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '30'))
        disableConcurrentBuilds()
        skipDefaultCheckout()
        timestamps()
    }

    stages {
        stage('Checkout') {
            agent { label 'jenkins-agent' }
            steps {
                deleteDir()
                checkout scm
                script {
                    env.IMAGE_TAG = sh(script: 'git rev-parse --short HEAD', returnStdout: true).trim()
                    currentBuild.displayName = "#${env.BUILD_NUMBER} ${env.IMAGE_TAG}"
                }
                stash name: 'source', includes: '**', excludes: '.git/**'
            }
        }

        stage('Build the image') {
            agent { label 'docker' }
            steps {
                deleteDir()
                unstash 'source'
                container('docker') {
                    sh '''
                        set -eu

                        attempts=30
                        until docker info >/dev/null 2>&1; do
                            attempts=$((attempts - 1))
                            if [ "$attempts" -eq 0 ]; then
                                echo "the docker daemon never came up"
                                exit 1
                            fi
                            sleep 1
                        done

                        docker build -t "${IMAGE}:${IMAGE_TAG}" -t "${IMAGE}:latest" .
                        docker push "${IMAGE}:${IMAGE_TAG}"
                        docker push "${IMAGE}:latest" || echo "the latest tag did not push, which is not fatal"
                    '''
                }
            }
        }

        stage('Deploy to k3s') {
            agent { label 'helm' }
            steps {
                deleteDir()
                unstash 'source'
                container('helm') {
                    sh '''
                        set -eu

                        kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}"

                        for secret in paypilot-stand-env paypilot-stand-tunnel; do
                            kubectl -n "${NAMESPACE}" get secret "${secret}" >/dev/null \
                                || { echo "secret ${secret} is missing in ${NAMESPACE}; see docs/adr/0004-k3s-deployment.md"; exit 1; }
                        done

                        helm lint "${CHART}" --set image.tag="${IMAGE_TAG}"

                        helm template "${RELEASE}" "${CHART}" \
                            --namespace "${NAMESPACE}" \
                            --set image.tag="${IMAGE_TAG}" \
                            --validate > /dev/null

                        helm upgrade --install "${RELEASE}" "${CHART}" \
                            --namespace "${NAMESPACE}" \
                            --set image.tag="${IMAGE_TAG}" \
                            --wait \
                            --timeout 8m

                        helm status "${RELEASE}" -n "${NAMESPACE}"
                    '''
                }
            }
        }

        stage('Verify') {
            agent { label 'helm' }
            steps {
                container('helm') {
                    sh '''
                        set -eu

                        kubectl -n "${NAMESPACE}" rollout status "deployment/${RELEASE}" --timeout=120s
                        kubectl -n "${NAMESPACE}" rollout status "deployment/${RELEASE}-phoenix" --timeout=120s

                        running=$(kubectl -n "${NAMESPACE}" get deployment "${RELEASE}" \
                            -o jsonpath='{.spec.template.spec.containers[0].image}')
                        if [ "${running}" != "${IMAGE}:${IMAGE_TAG}" ]; then
                            echo "the deployment runs ${running}, not ${IMAGE}:${IMAGE_TAG}"
                            exit 1
                        fi

                        kubectl -n "${NAMESPACE}" exec "deployment/${RELEASE}" -- \
                            python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=10).status == 200 else 1)"
                        echo "the stand answered /health"
                    '''
                }
            }
        }
    }

    post {
        success {
            echo "deployed ${env.IMAGE}:${env.IMAGE_TAG} to ${env.NAMESPACE}"
        }
        failure {
            echo "build ${env.BUILD_NUMBER} failed; the cluster keeps the release it was already running"
        }
    }
}
