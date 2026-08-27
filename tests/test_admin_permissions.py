from app.core.admin_permissions import (
    ADMIN_ROLE_BILLING,
    ADMIN_ROLE_PRODUCT,
    ADMIN_ROLE_SUPERADMIN,
    ADMIN_ROLE_SUPPORT,
    PERMISSION_ACCOUNTS_READ,
    PERMISSION_ACCOUNTS_WRITE,
    PERMISSION_AUDIT_READ,
    PERMISSION_BILLING_READ,
    PERMISSION_BILLING_WRITE,
    PERMISSION_PRODUCT_READ,
    PERMISSION_PRODUCT_WRITE,
    permissions_for_role,
    resolve_admin_role,
)


def test_legacy_staff_is_resolved_as_superadmin() -> None:
    assert resolve_admin_role(admin_role=None, is_staff=True) == ADMIN_ROLE_SUPERADMIN


def test_invalid_explicit_role_fails_closed_even_for_legacy_staff_flag() -> None:
    assert resolve_admin_role(admin_role="owner", is_staff=True) is None


def test_support_is_strictly_read_only() -> None:
    permissions = permissions_for_role(ADMIN_ROLE_SUPPORT)

    assert PERMISSION_ACCOUNTS_READ in permissions
    assert PERMISSION_AUDIT_READ in permissions
    assert PERMISSION_ACCOUNTS_WRITE not in permissions
    assert PERMISSION_BILLING_WRITE not in permissions
    assert PERMISSION_PRODUCT_WRITE not in permissions


def test_billing_and_product_roles_do_not_cross_privileged_boundaries() -> None:
    billing = permissions_for_role(ADMIN_ROLE_BILLING)
    product = permissions_for_role(ADMIN_ROLE_PRODUCT)

    assert PERMISSION_BILLING_READ in billing
    assert PERMISSION_BILLING_WRITE in billing
    assert PERMISSION_PRODUCT_WRITE not in billing
    assert PERMISSION_PRODUCT_READ in product
    assert PERMISSION_PRODUCT_WRITE in product
    assert PERMISSION_BILLING_WRITE not in product
