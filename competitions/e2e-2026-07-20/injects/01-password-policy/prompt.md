# INJECT 01 — Enterprise Password Policy

**From:** CISO, Aperture Regional Health
**To:** Blue Team Lead
**Priority:** High
**Time to complete:** 45 minutes

## Background

A recent audit found that several service and administrator accounts across our Linux
estate use weak or shared passwords. Leadership has asked IT Security to define and begin
enforcing a formal password policy before the next audit window.

## Tasks

1. Draft a one-page **password policy** covering: minimum length, complexity, maximum age,
   reuse history, and lockout thresholds. Justify each value briefly.
2. Describe the **technical controls** you would use to enforce it on the in-scope Ubuntu
   hosts (name the specific PAM modules / config files and the settings you would change).
3. List the concrete **commands** you ran (or would run) to apply the lockout and complexity
   rules on the `webmail` and `core` hosts.

## Deliverable

Submit a single PDF or Markdown document answering all three tasks. Do **not** change the
passwords of the scored service-check accounts — doing so will cause service checks to fail
and cost you availability points.
