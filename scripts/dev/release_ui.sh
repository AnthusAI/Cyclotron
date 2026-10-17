#!/usr/bin/env bash
# Release the web package (the npm package "cyclotron") as a GitHub tarball.
#
#   scripts/dev/release_ui.sh            # test, build, pack, tag ui-v<version>, create the release
#   scripts/dev/release_ui.sh --dry-run  # test, build, pack and print the pin block; no tag, no release
#
# The version comes from the root package.json. Run it from a clean checkout of
# the commit to release (normally main). It needs node, npm, openssl and an
# authenticated gh. Nothing runs in GitHub Actions: the gate is local.
set -euo pipefail
dry_run=0; [ "${1:-}" = "--dry-run" ] && dry_run=1
repository_root=$(git rev-parse --show-toplevel)
cd "$repository_root"
package_version=$(node -p "require('./package.json').version")
release_tag="ui-v${package_version}"
tarball_path="dist/cyclotron-${package_version}.tgz"
release_commit=$(git rev-parse HEAD)

[ -z "$(git status --porcelain)" ] || { echo "refusing: working tree is not clean"; exit 1; }
if git ls-remote --exit-code --tags origin "refs/tags/${release_tag}" >/dev/null 2>&1; then
  echo "refusing: ${release_tag} already exists on origin"; exit 1
fi
if [ "$dry_run" = 0 ]; then
  gh auth status >/dev/null 2>&1 || { echo "refusing: gh is not authenticated"; exit 1; }
  git fetch -q origin
  git merge-base --is-ancestor HEAD origin/main || { echo "refusing: HEAD is not on origin/main; push it first"; exit 1; }
fi

(cd trace-ui && npm ci && npm test && npm run build)
git diff --exit-code -- src/decision_flywheel/vendor/trace-ui
rm -f "$tarball_path"
make pack-ui

sha256_hex=$(openssl dgst -sha256 -r "$tarball_path" | cut -d' ' -f1)
sha512_integrity="sha512-$(openssl dgst -sha512 -binary "$tarball_path" | openssl base64 -A)"
download_url="https://github.com/AnthusAI/Cyclotron/releases/download/${release_tag}/cyclotron-${package_version}.tgz"

if [ "$dry_run" = 0 ]; then
  git tag "$release_tag" "$release_commit"
  git push origin "$release_tag"
  gh release create "$release_tag" "$tarball_path" --title "$release_tag" \
    --notes "Web package ${package_version} from ${release_commit}. See CHANGELOG.md."
  heading="Pin block for docs/shared-priming-release.md:"
else
  heading="Pin block for docs/shared-priming-release.md (dry run: nothing was tagged or published):"
fi

cat <<PIN

${heading}

| UI package | [${release_tag}](https://github.com/AnthusAI/Cyclotron/releases/tag/${release_tag}) | \`${download_url}\` with lock integrity \`${sha512_integrity}\` (SHA-256 \`${sha256_hex}\`) |

Commit: ${release_commit}
PIN
