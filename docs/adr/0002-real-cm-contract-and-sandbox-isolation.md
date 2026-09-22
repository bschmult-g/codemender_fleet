# ADR-0002: Real CodeMender CLI Contract and Sandbox Boundary Management

**Status:** Accepted  
**Date:** 2026-09-22  **Deciders:** Brian Schmult

## Context

CodeMender (`cm`) operates as an autonomous agent powered by Gemini on Google Cloud Vertex AI. Earlier iterations encountered two key integration obstacles:
1. Attempting to pass format flags (`cm find -o sarif` or `cm find --format sarif`) failed because `cm find` performs discovery only and does not accept direct report formatting flags.
2. When scanning cloned repositories in temporary directories (`/private/var/folders/...`), CodeMender reported 0 findings because its agent was confined to `project_paths` defined in the local user's `~/.codemender/config.yaml`.
3. Isolating `$HOME` for parallel worker SQLite databases caused Google Cloud Application Default Credentials (ADC) lookups to fail.

## Decision

Standardize the two-stage execution pipeline, disable filesystem sandbox path constraints via `--unrestricted`, and properly project ADC into isolated worker environments.

1. **Two-Stage Execution Pipeline**:
   * Discovery: `cm find -y --bypass-warning --unrestricted .`
   * Report Export: `cm report -f sarif > report.sarif`
2. **Dynamic Project Paths & Unrestricted Mode**:
   * In `local_run.sh`, patch `project_paths` in the isolated worker's `config.yaml` to match the cloned directory, and pass `--unrestricted` to `cm find`.
3. **ADC Symlinking**:
   * When setting `HOME=worker_temp/home`, explicitly symlink `~/.config` and export `GOOGLE_APPLICATION_CREDENTIALS`.

## Alternatives Considered

| Option | Why Not |
|---|---|
| **Mock CLI / Fallback stubs (`fake_cm.sh`)** | Violates core requirement for real vulnerability discovery; generates synthetic findings that don't reflect real security posture. |
| **Running without `$HOME` isolation** | Multiple parallel `cm find` processes compete for a single SQLite database in `~/.codemender`, causing database locks and cross-repo state corruption. |
| **Relying on default sandbox boundary** | Agent is trapped in the user's previously opened project directory and cannot view files in cloned workspaces. |

## Consequences

**Good:**
* Real security findings with complete semantic analysis.
* Full isolation between parallel workers; zero database locking.
* Agent explores target code freely without sandbox barriers.

**Bad / Accepted Cost:**
* Requires valid Google Cloud credentials (ADC or Workload Identity Federation) for both local and CI runs.

**Revisit If:**
* CodeMender CLI adds a `--database-path` flag to decouple SQLite storage from `$HOME`.
