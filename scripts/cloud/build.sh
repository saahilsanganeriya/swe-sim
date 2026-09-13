#!/bin/bash
# Build the worker image from exactly one commit with Cloud Build and print its digest reference as the LAST line of
# stdout (everything else goes to stderr, so `make` can capture the digest):
#
#   bash scripts/cloud/build.sh <project> <region> [sha]      # sha defaults to HEAD; any rev-parse-able ref works
#
# The source is `git archive <sha>` (that commit and nothing untracked), submitted with cloudbuild.yaml and run as the
# deployer service account (created by Terraform). Tags: <repo>/worker:<sha7> and :latest. The digest is read back from
# Artifact Registry, never parsed from build logs.
set -euo pipefail
cd "$(dirname "$0")/../.."

PROJECT=${1:?usage: build.sh <project> <region> [sha]}
REGION=${2:?usage: build.sh <project> <region> [sha]}
SHA=$(git rev-parse --verify "${3:-HEAD}^{commit}")
SHORT=${SHA:0:7}
REPO="$REGION-docker.pkg.dev/$PROJECT/swesim"
BUILD_SA="projects/$PROJECT/serviceAccounts/swesim-deployer@$PROJECT.iam.gserviceaccount.com"

src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT
git archive --format=tar "$SHA" | tar -x -C "$src"

echo "building $REPO/worker:$SHORT from $SHA" >&2
gcloud builds submit "$src" \
  --project "$PROJECT" \
  --config "$src/cloudbuild.yaml" \
  --service-account "$BUILD_SA" \
  --substitutions "_REPO=$REPO,SHORT_SHA=$SHORT,COMMIT_SHA=$SHA" \
  --quiet 1>&2

digest=$(gcloud artifacts docker images describe "$REPO/worker:$SHORT" --project "$PROJECT" --format 'value(image_summary.digest)')
case $digest in
  sha256:*) ;;
  *) echo "could not resolve the digest of $REPO/worker:$SHORT" >&2; exit 1 ;;
esac
echo "$REPO/worker@$digest"
