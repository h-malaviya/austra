# Case review workflow

Each incoming request becomes a case. New information reopens review. Analysis records a plan for manager review. This definition controls case status; business commitments require separate tool permissions and policy checks.

```json
{
  "id": "case_review",
  "version": 1,
  "initial_state": "new",
  "transitions": [
    {"event": "message_added", "from_states": ["new", "review"], "to_state": "new"},
    {"event": "plan_proposed", "from_states": ["new", "review"], "to_state": "review"}
  ]
}
```
