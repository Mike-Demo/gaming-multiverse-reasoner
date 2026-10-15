# Accessibility audit — Gaming Multiverse Reasoner (2026-10-14)

**Lighthouse Accessibility: 100/100, zero failing audits** (final build `ae0562e`)
Site: https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud
Method note: Lighthouse (axe-core) was run against the identical build served
on the project's VSI and reached through an SSH tunnel. The sandbox Chrome
could not load the public URL directly (TLS interstitial imposed by this
environment's egress proxy — the public site itself is fine: curl, the managed
browser, and Demo's phone all load it). A Lighthouse score is automated
coverage only — it is not a WCAG conformance claim.

## Audit history
- First run (build `8bc290f`): **96/100**, two failing audits:
  - `label-content-name-mismatch` (28 elements) — graph nodes' accessible names didn't contain their visible (truncated) labels; path cards' names didn't match their visible text either
  - `target-size` (24 elements) — the "Cypher" `<summary>` toggles were 18px tall (< 24px WCAG 2.2 AA minimum)
- Fixes in `ae0562e`: node accessible names now begin with the exact visible label; path cards take their name from content (aria-label removed); summaries got min-height 24px + padding; plus a twitter:card and /docs alias from the companion Silicon Friendly audit
- Second run: 100/100 with one audit still listing items (path cards) — fixed by the name-from-content change
- Third run: **100/100, zero failing audits**

## Manual checklist (WCAG 2.2, per the local-accessibility skill)
- Landmarks: header / main / footer present; skip link ("Skip to results") added — PASS
- Form labels: every game input has an associated `<label for>` — PASS
- Combobox pattern: inputs carry role=combobox with aria-expanded, aria-controls, aria-activedescendant; dropdown is role=listbox with role=option items and aria-selected; arrow keys + Enter + Escape verified in code and in the earlier live browser pass — PASS
- Live regions: status line is role=status aria-live=polite (search progress, results, "link copied") — PASS
- Keyboard operability: graph nodes, path cards and claims are focusable (tabindex 0, role=button) and activate with Enter/Space; all controls are native buttons/links/inputs — PASS
- Focus visible: :focus-visible outlines (3px) on interactive elements; SVG nodes get a visible focus ring via the hit circle — PASS
- Target size: buttons and share controls ≥ 44px; Cypher summaries ≥ 24px after the fix — PASS
- Reduced motion: `prefers-reduced-motion` disables edge flow, node pop-in and pulse animations and the slot-machine reel cycling — PASS
- Color contrast: body text #e6edf3 on #0d1117 (~15:1); muted #8b949e on #0d1117 (~5.9:1) — AA PASS for normal text; node colors are decorative (identity is also carried by text labels, never color alone)
- Page language: `<html lang="en">`; title and meta description present — PASS

## Not covered (honest gaps)
- No screen-reader pass (NVDA/VoiceOver) was performed — the ARIA follows the APG combobox pattern, but real AT behavior is unverified
- Mobile (~390px) layout remains visually unverified by automation (the browser tool cannot resize); responsive CSS is in the build — a 10-second check on a real phone is the fastest confirmation
- Lighthouse ran the desktop presentation; the auto-run search completed during the audit window, so dynamically rendered content (graph, path cards) was included — narration claims render only after a click and were assessed from source, not by axe
