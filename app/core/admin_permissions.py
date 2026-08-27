from collections.abc import Iterable

ADMIN_ROLE_SUPPORT = "support"
ADMIN_ROLE_BILLING = "billing"
ADMIN_ROLE_PRODUCT = "product"
ADMIN_ROLE_SUPERADMIN = "superadmin"

ADMIN_ROLES = frozenset(
    {
        ADMIN_ROLE_SUPPORT,
        ADMIN_ROLE_BILLING,
        ADMIN_ROLE_PRODUCT,
        ADMIN_ROLE_SUPERADMIN,
    }
)

PERMISSION_ADMIN_ACCESS = "admin:access"
PERMISSION_ACCOUNTS_READ = "accounts:read"
PERMISSION_ACCOUNTS_WRITE = "accounts:write"
PERMISSION_ADMIN_ROLES_WRITE = "admin_roles:write"
PERMISSION_AUDIT_READ = "audit:read"
PERMISSION_ATTENTION_READ = "attention:read"
PERMISSION_BILLING_READ = "billing:read"
PERMISSION_BILLING_WRITE = "billing:write"
PERMISSION_PRODUCT_READ = "product:read"
PERMISSION_PRODUCT_WRITE = "product:write"
PERMISSION_MESSAGING_READ = "messaging:read"
PERMISSION_MESSAGING_WRITE = "messaging:write"

ALL_ADMIN_PERMISSIONS = frozenset(
    {
        PERMISSION_ADMIN_ACCESS,
        PERMISSION_ACCOUNTS_READ,
        PERMISSION_ACCOUNTS_WRITE,
        PERMISSION_ADMIN_ROLES_WRITE,
        PERMISSION_AUDIT_READ,
        PERMISSION_ATTENTION_READ,
        PERMISSION_BILLING_READ,
        PERMISSION_BILLING_WRITE,
        PERMISSION_PRODUCT_READ,
        PERMISSION_PRODUCT_WRITE,
        PERMISSION_MESSAGING_READ,
        PERMISSION_MESSAGING_WRITE,
    }
)

_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ADMIN_ROLE_SUPPORT: frozenset(
        {
            PERMISSION_ADMIN_ACCESS,
            PERMISSION_ACCOUNTS_READ,
            PERMISSION_AUDIT_READ,
            PERMISSION_ATTENTION_READ,
            PERMISSION_BILLING_READ,
            PERMISSION_PRODUCT_READ,
            PERMISSION_MESSAGING_READ,
        }
    ),
    ADMIN_ROLE_BILLING: frozenset(
        {
            PERMISSION_ADMIN_ACCESS,
            PERMISSION_ACCOUNTS_READ,
            PERMISSION_AUDIT_READ,
            PERMISSION_ATTENTION_READ,
            PERMISSION_BILLING_READ,
            PERMISSION_BILLING_WRITE,
        }
    ),
    ADMIN_ROLE_PRODUCT: frozenset(
        {
            PERMISSION_ADMIN_ACCESS,
            PERMISSION_AUDIT_READ,
            PERMISSION_ATTENTION_READ,
            PERMISSION_PRODUCT_READ,
            PERMISSION_PRODUCT_WRITE,
            PERMISSION_MESSAGING_READ,
            PERMISSION_MESSAGING_WRITE,
        }
    ),
    ADMIN_ROLE_SUPERADMIN: ALL_ADMIN_PERMISSIONS,
}


def resolve_admin_role(*, admin_role: str | None, is_staff: bool) -> str | None:
    if admin_role in ADMIN_ROLES:
        return admin_role
    if admin_role is None and is_staff:
        return ADMIN_ROLE_SUPERADMIN
    return None


def permissions_for_role(role: str | None) -> frozenset[str]:
    return _ROLE_PERMISSIONS.get(role or "", frozenset())


def has_admin_permission(role: str | None, permission: str) -> bool:
    return permission in permissions_for_role(role)


def has_any_admin_permission(role: str | None, permissions: Iterable[str]) -> bool:
    granted = permissions_for_role(role)
    return any(permission in granted for permission in permissions)
