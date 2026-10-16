# VPS_1 public source security decision

The repository owner explicitly requests **PUBLIC** visibility and confirms authorship of the imported application source. Neither GitHub repository visibility nor public availability grants other people a license to reuse the code unless a separate LICENSE or agreement explicitly grants one.

## Before PR #7 can merge to main
- GitHub Actions Gitleaks scans all fetched public refs and the source tree; reviewed synthetic historical fixtures are restricted by exact hashes.
- Public source inventory policy scans the checked-out files for committed private artifacts, credentials and sensitive manifest/backup data. It prints category counts only, not sensitive values.
- Phase 1 GitHub-to-VPS SSH and Tailscale job guards remain unconditional false. No GitHub job is authorized for VPS deployment.
- Required CI, main ruleset, and the exact migration SHA must pass a final recheck.
- External provider scopes (GitHub secrets access and Tailscale OAuth/ACL) cannot be verified using this GitHub repository connector alone. They must remain a separate *Phase 2* gate before enabling any remote workflow, and should never be assumed safe based on CI success.

No .env, access tokens, user media, private archives, live data, database dumps, or SSH key material may be committed. Source manifests record infrastructure paths, not database or upload contents; anyone can read them in a public repo. Do not change Git history or rotate credentials without impact review.
