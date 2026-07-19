# Output Format

Every blueprint plan must follow this structure.

## Header

```
# [Project Name] Code Modification Plan

Generated: [date]
Scope: [one-sentence summary]
```

## Phase Template

Repeat for each phase:

```
## Phase [N]: [Goal]

**Depends on**: Phase [X] (or "None")
**Deliverable**: [what exists after this phase that didn't before]

### Changes

| File | Action | Description |
|------|--------|-------------|
| path/to/file | CREATE | [what it does] |
| path/to/file | MODIFY | [what changes and why] |
| path/to/file | DELETE | [why it's removed] |

### Interfaces

[Describe data contracts between new/modified modules. Include function signatures, data shapes, API contracts.]

### Validation

[How to verify this phase works. Include specific commands, expected outputs, test data.]
```

## Final Section

```
## Documentation & Conflict Resolution

### Docs to Update
- [ ] [doc path]: [what changes]
- [ ] [doc path]: [what changes]

### Conflict Resolution
[How to handle merge conflicts, breaking changes, or dependency version conflicts]
```
