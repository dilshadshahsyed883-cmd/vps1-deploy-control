# Guarded HeyGen Studio heading release

- Scope: Scene Builder HeyGen Studio duplicate heading only, from PR #748.
- Tested source commit: `cb322678a0acbec93a255d12f9f8df2759e822ad`.
- Activation: protected main push with `[vps1-heygen-studio-release]` in the merge commit message.
- Release implementation: existing `.github/workflows/vps1-heygen-studio-release.yml` with server-side rollback and live asset/authentication smoke checks.
- No change to HTTP 500, SaladCloud settings, worker images, authentication, or other studios.
- This is a deploy-request audit record; it changes no runtime code.
