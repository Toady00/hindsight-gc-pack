{{define "hindsight-coordination" -}}
## Agent handoffs

For agent-addressed proposals, questions, repair requests and incident reports
that need action, use `gc mail send <target> -s <subject> -m <message> --notify`.
Replies needed to continue a mail conversation notify too. Address a requester
by session ID or alias. Unread mail alone does not wake a session. Notification
waits for idle delivery to a running session; for a stopped session it can
explicitly wake it, cancel waits and clear holds or wake backoff. After a wake
while delegated work is still open, inspect and restore any canceled completion
wait before yielding. Notification is not proof of reading, arbitration or repair.
Watch stderr for `nudge failed` or `no managed wake was requested` and, with
`--json`, for a missing or false `notified` on a new agent-addressed send or reply
requesting notification. Exit 0 alone does not establish a requested turn.
Preserve the message ID and retry attention once with
`gc session nudge <recipient> "Mail <id> needs action." --delivery wait-idle`, not
another mail send. If that fails or only queues without wake, record/report the
undelivered handoff on the work bead, or in the conversation when none exists.

Tracked work returns through persisted results and bead closure. Send supplemental
result mail only if the assignment asks for it, without notification; the requester
uses a completion wait or formula dependencies. Other silent mail names its alternate
wake mechanism, such as a mail-plus-nudge outbox, or explicitly allows deferred
processing. Arbitration verdicts and bounces are deferred calibration mail under
`hindsight-arbitrate`, not a request to resume the proposer. Mail to `human` has no
managed agent to wake. Before yielding, check routed work and unread mail without
overlapping foreground writes. Agent mail is not approval, a work claim or a
ship/retain decision; memory proposals go through `hindsight-arbitrate`, and repair
requests needing execution go through a tracked work bead and the normal write path.
Blocking questions may notify the requesting session while that work is open;
after answering, the requester checks its wait and re-registers if canceled.

Use a slung work bead for substantive repair with artifacts and completion
criteria. Gas City's controller and core route notifier handle pickup. Do not
inspect the recipient's queue to decide whether to wake it; that observation
races its final check. Use `--nudge` for immediate pickup or a stalled route,
and inspect claims and completion evidence rather than treating dispatch as success.
Inside a claimed formula step, follow its blocker/report/close protocol and native
routing. Required incident mail uses notification; it does not launch extra work.
{{- end}}
