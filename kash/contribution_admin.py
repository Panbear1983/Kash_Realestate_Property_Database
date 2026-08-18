"""Small protected admin application layer for the staged contribution workflow.

UI transports must call this layer, not manipulate proposal tables directly.
"""
from __future__ import annotations


class ContributionAdmin:
    def __init__(self, workflow, queue, workspace_sync_audit=None):
        self.workflow = workflow
        self.queue = queue
        self.workspace_sync_audit = workspace_sync_audit

    def list(self, actor_id, *, status=None):
        return self.queue.for_actor(actor_id, status=status)

    def list_workspace_sync_audit(self, actor_id):
        if self.workspace_sync_audit is None:
            return []
        return self.workspace_sync_audit.for_owner(actor_id)

    def approve(self, actor_id, proposal_id, *, reason):
        return self.workflow.approve(actor_id, proposal_id, reason=reason)

    def reject(self, actor_id, proposal_id, *, reason):
        return self.workflow.reject(actor_id, proposal_id, reason=reason)

    def publish(self, actor_id, proposal_id):
        return self.workflow.publish(actor_id, proposal_id)

    def rollback(self, actor_id, proposal_id, *, reason):
        return self.workflow.rollback(actor_id, proposal_id, reason=reason)
