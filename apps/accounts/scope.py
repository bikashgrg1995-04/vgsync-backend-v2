"""
Which users and Positions the owner of the active business may manage.

* Head Office (company context): the company's own users, whose Positions
  belong to the Head Office.
* Branch or Independent business: the users who belong directly to that
  business, with that business's Positions.

Owners are never part of the managed set, so an owner cannot demote,
deactivate or delete an owner (including themselves) through these APIs.
Owner accounts are created in Django Admin.
"""

from django.db.models import Q

from apps.businesses.models import Business

from .models import Position, User

# Roles an owner can hand out through the API.
MANAGEABLE_ROLES = (
    User.Role.ADMIN,
    User.Role.ACCOUNTANT,
    User.Role.STAFF,
)


def scoped_users(business):
    if business.business_type == Business.BusinessType.HEAD_OFFICE:
        scope = Q(company_id=business.company_id)
    else:
        scope = Q(business_id=business.id)

    return (
        User.objects
        .filter(scope)
        .exclude(role__in=[User.Role.OWNER, User.Role.SUPER_ADMIN])
    )


def scoped_positions(business):
    return Position.objects.filter(business=business)


def username_taken(business, username):
    """
    Usernames are unique within a tenant (a company with its branches, or
    one independent business). The check ignores case so "Ram" and "ram"
    cannot both exist and confuse the people logging in.
    """
    users = User.objects.filter(username__iexact=username)

    if business.company_id:
        users = users.filter(tenant_company_id=business.company_id)
    else:
        users = users.filter(
            business_id=business.id,
            tenant_company__isnull=True,
        )

    return users.exists()
