# Quality Constraints

A change is a **hack** if any of these apply:

1. **Bypasses existing abstraction** - Calls an internal function that the module's public API was designed to hide.
2. **Hardcodes a value** that should be configurable or derived from data.
3. **Duplicates logic** that already exists elsewhere in the codebase.
4. **Defers cleanup** - Adds a TODO/FIXME for something that could be resolved now.
5. **Ignores error paths** - Assumes happy path without handling failure modes that the surrounding code already handles.
6. **Breaks existing tests** without updating them.
7. **Introduces a new dependency** for something achievable in <20 lines of stdlib code.
8. **Couples modules** that should be independent (e.g., UI directly imports database logic).

## What Counts as Clean

- Uses existing patterns from the codebase (even if you'd choose differently).
- Every new function has a clear single responsibility.
- Every modified interface maintains backward compatibility or updates all callers.
- Test data is explicit and reproducible, not random.
