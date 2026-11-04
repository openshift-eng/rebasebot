# Rebase Bot

Rebase Bot synchronizes downstream repositories with their upstream sources through pull requests. This glossary defines the domain vocabulary shared by maintainers and operators.

## Language

### Synchronization

**Synchronization**:
The incorporation of selected upstream history into a downstream repository through rebase mode or merge mode.

**Rebase mode**:
A synchronization method that starts from the selected upstream content and reapplies selected downstream carries, preserving earlier downstream history.

**Merge mode**:
A synchronization method that incorporates the selected upstream history through a merge into the destination, retaining downstream commits without reapplying them.

**Automation pause**:
A maintainer-requested suspension of automated synchronization until the pause is lifted.
_Avoid_: Manual override, when referring to the pause.

**Manual override**:
An explicit instruction to proceed with synchronization despite an automation pause for the current run.

**Synchronization PR**:
The pull request proposing integration of the publication branch into the destination branch.

### Repository roles

**Source**:
The repository and selected Git reference supplying upstream history for synchronization.

**Source reference**:
The selected branch or tag identifying the upstream history for synchronization.

**Destination**:
The GitHub repository and branch receiving synchronization changes through a pull request.

**Destination branch**:
The downstream branch receiving the synchronization changes.

**Publication repository**:
The GitHub repository hosting the branch proposed for integration into the destination. Source, destination, and publication are roles and may share a repository.

**Publication branch**:
The branch in the publication repository containing the changes proposed for the destination.

### Downstream changes

**Carry**:
A downstream change maintained across synchronization with upstream. A carry does not require an explicit `UPSTREAM: <carry>` tag.

**Included upstream**:
A change is included upstream when it is present in the selected source reference.
_Avoid_: Upstreamed, when describing whether a change still needs carrying.

**UPSTREAM tag**:
A commit-message annotation expressing that a downstream commit should be carried, dropped, or associated with an upstream pull request. Its interpretation depends on the tag policy.

**Dropped commit**:
A downstream commit deliberately omitted when selecting commits for rebase mode.

**Content-loss warning**:
A warning that conflict resolution while reapplying a carry may have removed upstream content.

### Rebase policies

**Tag policy**:
The rules for interpreting `UPSTREAM` commit tags when selecting downstream commits to carry, including how to treat untagged commits. Tag policy applies to rebase mode.

**Conflict policy**:
The rules for accepting, warning about, or rejecting potential loss of upstream content when resolving conflicts while reapplying carries. Conflict policy applies to rebase mode.

### Lifecycle

**Lifecycle**:
The ordered stages of a synchronization run, from workspace preparation through publication and pull request creation or update.

**Lifecycle hook**:
A custom action attached to a designated stage of the rebase-mode lifecycle.

**Source reference hook**:
A custom action that selects the source branch or tag before workspace preparation. It is available in both rebase mode and merge mode.
