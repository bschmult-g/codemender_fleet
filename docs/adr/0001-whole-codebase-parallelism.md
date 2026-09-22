# ADR-0001: Parallelize Across Repositories, Never Subdirectories

**Status:** Accepted  
**Date:** 2026-09-22  **Deciders:** Brian Schmult

## Context

When scanning large codebases or fleets of hundreds of repositories with an LLM-based security scanner like CodeMender, scan duration can grow significantly. There is temptation to parallelize execution by splitting a single repository into subdirectories (e.g. running one worker on `/src/api` and another worker on `/src/db`) to decrease per-repo scan time.

However, modern security vulnerabilities (such as Server-Side Request Forgery, SQL Injection, and privilege escalation) frequently cross architectural boundaries. An HTTP route handler defined in one directory often accepts untrusted parameters that are sanitized or executed in another directory.

## Decision

Enforce whole-codebase analysis for every repository scan pass. Parallelism is strictly horizontal across distinct repositories (1 worker = 1 full repository), never vertical within a repository's directory hierarchy.

## Alternatives Considered

| Option | Why Not |
|---|---|
| **Subdirectory splitting** | Destroys cross-file taint analysis. The scanner misses vulnerabilities where taint source and sink reside in different directories. |
| **Monolithic sequential scans** | Scanning 100+ repositories sequentially takes days and underutilizes GitHub Actions matrix capabilities. |
| **File-by-file chunking** | Causes high LLM token overhead, loses semantic context, and produces high false-positive rates. |

## Consequences

**Good:**
* Complete static AST and data-flow awareness across all modules in the repository.
* Zero false negatives caused by artificial directory isolation.
* Scales linearly with GitHub Actions runner capacity or local thread pool concurrency.

**Bad / Accepted Cost:**
* Repositories with large numbers of files take 5–15 minutes per scan.

**Revisit If:**
* CodeMender CLI introduces native inter-module dependency graph caching with distributed cross-directory taint tracking.
