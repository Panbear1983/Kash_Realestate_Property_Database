"""Small protected admin application layer for the staged contribution workflow.

UI transports must call this layer, not manipulate proposal tables directly.
"""
from __future__ import annotations


class ContributionAdmin:
    def __init__(self, workflow, queue, workspace_sync_audit=None, notifier=None):
        self.workflow = workflow
        self.queue = queue
        self.workspace_sync_audit = workspace_sync_audit
        # Optional ProposalNotifier: pings the submitter after a successful decision.
        # Best-effort by contract — the notifier itself never raises — so a push problem
        # can never block or alter a review action.
        self.notifier = notifier

    def list(self, actor_id, *, status=None):
        return self.queue.for_actor(actor_id, status=status)

    def list_workspace_sync_audit(self, actor_id):
        if self.workspace_sync_audit is None:
            return []
        return self.workspace_sync_audit.for_owner(actor_id)

    def _notify(self, proposal_id, decision, *, reason="", actor_id=None):
        if self.notifier is not None:
            self.notifier.decision(proposal_id, decision, reason=reason, actor_id=actor_id)

    def approve(self, actor_id, proposal_id, *, reason):
        result = self.workflow.approve(actor_id, proposal_id, reason=reason)
        self._notify(proposal_id, "approved", reason=reason, actor_id=actor_id)
        return result

    def reject(self, actor_id, proposal_id, *, reason):
        result = self.workflow.reject(actor_id, proposal_id, reason=reason)
        self._notify(proposal_id, "rejected", reason=reason, actor_id=actor_id)
        return result

    def publish(self, actor_id, proposal_id):
        result = self.workflow.publish(actor_id, proposal_id)
        self._notify(proposal_id, "published", actor_id=actor_id)
        return result

    def rollback(self, actor_id, proposal_id, *, reason):
        return self.workflow.rollback(actor_id, proposal_id, reason=reason)
