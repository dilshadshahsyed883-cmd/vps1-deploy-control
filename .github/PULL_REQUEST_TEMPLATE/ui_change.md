# VPS1 UI/UX Change — Required Checklist

Use for every shell, navigation, theme, responsive, and application UI PR. Canonical contract: docs/UI_UX_MASTER_RULES_V1.md.

## Scope and ownership
- [ ] List affected app(s) and identify **shared-shell** versus **app-owned** UI changes.
- [ ] Confirm independent backend, workers, queues, media, OAuth and databases preserved.
- [ ] No Auto Script enablement, no broad live deployment implied by UI-only PR.

## Experience
- [ ] One stable sidebar, correct login/role controls, no duplicate global navigation.
- [ ] Same top tab x/y baseline across Studio apps at equivalent viewport widths.
- [ ] Test 1440, 1024, 768, 390 and 360 CSS px; no overlapping or horizontal page overflow.
- [ ] Check tab/Back/F5/logout, active indicators, loading/error/empty/form states, keyboard focus and reduced motion.
- [ ] No avoidable full-page reload on local subtab click, no unexpected user-facing API/iframe URL.

## Proof
- [ ] Required source contract: python3 -m unittest discover -s tests -p 'test_ui_design_governance.py' -v
- [ ] All relevant app tests pass; screenshot/geometry comparison PASS or explicitly **NOT VERIFIED**.
- [ ] New screenshots in protected artifacts only, free of real user data and credentials.
- [ ] Guarded, app-scoped deployment with rollback and HTTPS authenticated route check when UI ships.
- [ ] Update Master Google Doc application tab with issue, actual fix, CI, PR, deployment and UAT status.

**Important:** passing source-contract tests does not certify actual pixel alignment. The browser audit is not yet a mandatory CI check without a provisioned protected authenticated environment.
