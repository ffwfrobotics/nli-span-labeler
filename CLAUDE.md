# E13 Labeler - GoblinCorps Development Guide

## Project

This repo was the NLI span labeler. It is being rebuilt as the **E13 labeler**
(`docs/e13/E13_LABELING_APP_REQUIREMENTS.md`). Work is tracked in `ROADMAP.md`:
tick items off there as they land.

- Code: the `e13_labeler/` package (FastAPI + SQLite + one static page, no build step).
- Run: `uv sync`, then `uv run python -m e13_labeler create-owner`, then `./run.sh`.
- Test: `uv run pytest`. Tests must run offline.
- Each MUST requirement's "Test" in the requirements doc becomes a pytest.
- Never put personal names or emails in code, config or fixtures (NFR-4).

## Team Members & GitHub Usernames

**IMPORTANT**: Discord names differ from GitHub usernames! Use these for @mentions:

| Goblin | Discord | GitHub Username |
|--------|---------|-----------------|
| Frick | Frick | @frick-goblin |
| Frack | Frack | @frack-goblin |
| Contraption | Contraption | @goblin-contraption |

## Team Roles

### PR Workflow

| Role | Goblin | Responsibilities |
|------|--------|------------------|
| **PR Review** | Frick (@frick-goblin) | Reviews PRs, requests changes, resolves merge conflicts, adds fix commits |
| **Merge & Test** | Contraption (@goblin-contraption) | Runs tests, merges approved PRs, rejects if tests fail |
| **Feature Development** | Frack (@frack-goblin) | Implements features, bug fixes, new functionality |

### PR Process

1. **Submission**: Developer opens PR or external contributor submits changes
2. **Review (Frick)**:
   - Reviews code for correctness and adherence to project standards
   - Requests changes or adds fix commits as needed
   - Resolves merge conflicts with main branch
   - Approves PR when ready
3. **Merge (Contraption)**:
   - Runs full test suite on approved PRs
   - If tests pass → Merge PR
   - If tests fail with trivial fixes → Fix and merge
   - If tests fail with non-trivial issues → Reject PR back to reviewer

### Test Maintenance

Contraption (@goblin-contraption) is responsible for:
- Keeping the test suite passing and up to date
- Adding tests for new functionality
- Fixing broken tests after merges
- Maintaining test coverage standards

## Development Standards

### Commit Messages
- Use conventional commit format when appropriate
- Be descriptive about what changed and why

### Testing
- All PRs must pass existing tests before merge
- New features should include corresponding tests
- Test failures block merging

## Getting Help

- **User coordination & issues**: @frick-goblin
- **Feature implementation**: @frack-goblin
- **Tests, docs, infrastructure**: @goblin-contraption
