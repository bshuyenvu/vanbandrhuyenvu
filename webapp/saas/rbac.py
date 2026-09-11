from __future__ import annotations

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "platform_super_admin": {"*"},
    "organization_owner": {
        "organization.manage", "department.manage", "member.manage", "billing.manage",
        "model.view", "model.manage_org", "document.*", "audit.view", "policy.manage"
    },
    "organization_admin": {
        "department.manage", "member.manage", "model.view", "document.*", "audit.view", "policy.manage"
    },
    "records_clerk": {
        "document.read", "document.create", "document.register", "document.assign",
        "document.issue", "document.archive", "document.version", "model.view"
    },
    "leader": {
        "document.read", "document.assign", "document.approve", "document.reject",
        "document.comment", "document.version", "model.view", "audit.view_department"
    },
    "department_head": {
        "document.read_department", "document.assign_department", "document.create",
        "document.comment", "document.version", "model.view"
    },
    "member": {
        "document.read_assigned", "document.create", "document.edit_assigned",
        "document.comment", "document.version", "model.view"
    },
    "billing_admin": {"billing.manage", "wallet.view", "usage.view", "model.view"},
    "auditor": {"document.read", "audit.view", "usage.view", "model.view"},
    "viewer": {"document.read_shared", "model.view"},
}


def has_permission(role: str, permission: str) -> bool:
    perms = ROLE_PERMISSIONS.get(role, set())
    if "*" in perms or permission in perms:
        return True
    namespace = permission.split(".", 1)[0] + ".*"
    return namespace in perms


def permissions_for(role: str) -> list[str]:
    return sorted(ROLE_PERMISSIONS.get(role, set()))
