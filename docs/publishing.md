# Publishing `whyslow-db`

The Python distribution is named `whyslow-db`; its import package and command
remain `whyslow`. The benchmark is included in the wheel, so users do not need
a repository checkout.

## One-time PyPI setup

Create a pending Trusted Publisher for project `whyslow-db` in PyPI with:

- Owner: `kraftaa`
- Repository: `whyslow`
- Workflow: `release.yml`
- Environment: `pypi`

In the GitHub repository, create an environment named `pypi`. Add required
reviewers if releases should need manual approval. No PyPI API token is needed.

## Release

1. Ensure `whyslow.__version__` contains the intended version.
2. Merge the change and confirm the full `test` workflow is green on `main`.
3. Create and push the matching tag, for example `v0.5.0`.
4. The release workflow builds and validates the wheel and source archive,
   audits dependencies, creates an SBOM and checksums, publishes with GitHub
   OIDC, and attaches the same artifacts to a GitHub Release.

The workflow refuses a tag that does not match the package version or a commit
without a successful full test run.
