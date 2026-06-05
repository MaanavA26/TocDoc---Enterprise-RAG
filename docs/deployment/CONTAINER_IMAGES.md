# Prebuilt Container Images (GitHub Container Registry)

Self-hosters do not have to build the TocDoc service images locally. Each
tagged release publishes prebuilt, versioned images to
[GitHub Container Registry (GHCR)](https://ghcr.io) via the
[`release` workflow](../../.github/workflows/release.yml).

## Published images

| Service   | Image                                              |
|-----------|----------------------------------------------------|
| Q&A       | `ghcr.io/<owner>/tocdoc-qna`                        |
| Ingestion | `ghcr.io/<owner>/tocdoc-ingestion`                  |

Replace `<owner>` with the GitHub user or organization that owns the
repository (the GHCR owner is always lowercase).

Each push of a `v*` git tag (e.g. `v1.2.3`) produces:

- a version tag matching the release, e.g. `:1.2.3`, and
- a floating `:latest` tag pointing at the most recent release.

**Platform:** images are built for `linux/amd64`. `arm64` is not currently
published — the Q&A image installs the Microsoft ODBC driver, which is slow to
build under emulation. (See the workflow comments for how to enable `arm64`.)

## Pulling the images

```bash
# Pin to a specific release (recommended for production)
docker pull ghcr.io/<owner>/tocdoc-qna:1.2.3
docker pull ghcr.io/<owner>/tocdoc-ingestion:1.2.3

# Or track the latest published release
docker pull ghcr.io/<owner>/tocdoc-qna:latest
docker pull ghcr.io/<owner>/tocdoc-ingestion:latest
```

If the repository's GHCR packages are private, authenticate first with a
GitHub token that has the `read:packages` scope:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <github-username> --password-stdin
```

Public packages need no login to pull.

## Using the published images in a deployment

The
[Installation Guide](INSTALLATION.md) Step 3 builds and pushes images to your
own registry. To use the prebuilt GHCR images instead, skip the `docker build`
/ `docker push` steps and point the Container App `--image` at the GHCR
reference (pin to a version tag):

```bash
az containerapp update \
  --name tocdoc-ingestion-prod \
  --resource-group rg-tocdoc-<client-name> \
  --image ghcr.io/<owner>/tocdoc-ingestion:1.2.3

az containerapp update \
  --name tocdoc-qna-prod \
  --resource-group rg-tocdoc-<client-name> \
  --image ghcr.io/<owner>/tocdoc-qna:1.2.3
```

If the GHCR packages are private, configure registry credentials on the
Container App (`az containerapp registry set`) so the platform can pull them.

## Cutting a release

Maintainers publish a new set of images by pushing a `v*` tag:

```bash
git tag v1.2.3
git push origin v1.2.3
```

The `release` workflow then builds both images, pushes them to GHCR, and
creates a GitHub Release for the tag with auto-generated notes. The workflow
can also be run manually from the Actions tab (`workflow_dispatch`) to exercise
the build wiring without cutting a release — in that case no version tags and no
GitHub Release are produced.
