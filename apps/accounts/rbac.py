"""
Permission catalogue and the one place that decides "may this user do X?".

Permissions are ``module.action`` codes, for example ``sales.cancel``.
Three layers feed the decision (spec: Presets -> Positions -> Overrides):

* Presets are code-defined starting templates. Picking one copies its
  grants into a Position; later edits to a preset never touch Positions.
* A Position stores what its holders may do (``PositionPermission``: the four
  classic flags plus an ``actions`` list for finer actions).
* A ``StaffPermissionOverride`` allows or denies one code for one person.

Resolution order, always read from the database (no cache across requests, so
a change applies on the next request):

1. Reserved codes belong to the owner only; nobody else ever holds them.
2. The person's override (allow / deny).
3. The Position's grant.
4. Otherwise deny.

Owners hold every code. A company owner inside a Branch is read-only, so only
the ``view`` / ``export`` style codes survive there.
"""

from .models import PositionPermission, User

# module -> {action: label}. Add new modules or actions here and every layer
# (API validation, presets, UI) picks them up.
CATALOG = {
    "dashboard": {"view": "View dashboard"},
    "inventory": {
        "view": "View stock",
        "create": "Add products and stock",
        "update": "Edit products",
        "delete": "Delete products",
        "adjust": "Adjust stock",
        "write_off": "Write off stock",
        "transfer": "Transfer stock between branches",
        "approve": "Approve stock adjustments",
    },
    "purchases": {
        "view": "View purchases",
        "create": "Create purchases",
        "update": "Edit purchases",
        "delete": "Delete purchases",
        "receive": "Receive goods",
        "pay": "Pay suppliers",
        "return": "Return goods",
        "approve": "Approve high-value purchases",
    },
    "sales": {
        "view": "View sales",
        "create": "Create sales",
        "update": "Edit sales",
        "delete": "Delete sales",
        "cancel": "Cancel sales",
        "discount": "Give discounts",
        "refund": "Refund customers",
        "approve": "Approve exceptional sales",
    },
    "bike_sales": {
        "view": "View bike sales",
        "create": "Create bike sales",
        "update": "Edit bike sales",
        "delete": "Delete bike sales",
    },
    "orders": {
        "view": "View orders",
        "create": "Create orders",
        "update": "Edit orders",
        "delete": "Delete orders",
    },
    "finance": {
        "view": "View income and expenses",
        "create": "Record income and expenses",
        "update": "Edit income and expenses",
        "delete": "Delete income and expenses",
        "void": "Void income and expenses",
        "approve": "Approve high-value expenses",
    },
    "reports": {
        "view": "View reports",
        "export": "Export reports",
    },
    "staff": {
        "view": "View staff",
        "create": "Add staff",
        "update": "Edit staff",
        "delete": "Deactivate staff",
        "assign_position": "Assign positions",
        "manage_permissions": "Manage permissions",
    },
    "reminders": {"view": "View reminders"},
    "tasks": {
        "view": "See everyone's tasks",
        "create": "Assign tasks",
        "update": "Edit and reassign tasks",
        "delete": "Cancel tasks",
    },
    "backup": {
        "view": "See backups",
        "create": "Download backups",
        "restore": "Restore from a backup",
    },
}

# Owner only. A Position or override can never grant these, so an
# Administrator Position never becomes equivalent to ownership.
RESERVED = frozenset({
    "staff.manage_permissions",
})

# Codes that only read. These are the only ones a read-only context keeps.
READ_ACTIONS = frozenset({"view", "export"})

# The four classic flags stored as columns on PositionPermission.
FLAG_ACTIONS = ("view", "create", "update", "delete")


def all_codes():
    return {
        f"{module}.{action}"
        for module, actions in CATALOG.items()
        for action in actions
    }


def delegable_codes():
    return all_codes() - RESERVED


def is_valid_code(code):
    return code in all_codes()


def code_label(code):
    """``sales.cancel`` -> ``Cancel sales``; an unknown code comes back as it is."""
    module, _, action = code.partition(".")

    return CATALOG.get(module, {}).get(action, code)


def catalogue():
    """The catalogue as data for the API / UI."""
    return [
        {
            "module": module,
            "label": dict(PositionPermission.Module.choices).get(
                module, module.replace("_", " ").title()
            ),
            "actions": [
                {
                    "action": action,
                    "code": f"{module}.{action}",
                    "label": label,
                    "reserved": f"{module}.{action}" in RESERVED,
                }
                for action, label in actions.items()
            ],
        }
        for module, actions in CATALOG.items()
    ]


# ---------------------------------------------------------------------------
# Presets: starting templates only. Never roles, never kept in sync.
# ---------------------------------------------------------------------------

def _all(module):
    return list(CATALOG[module])


def _view(*modules):
    return {module: ["view"] for module in modules}


PRESETS = {
    "administrator": {
        "label": "Administrator",
        "department": "Management",
        "grants": {
            module: [
                action
                for action in actions
                if f"{module}.{action}" not in RESERVED
            ]
            for module, actions in CATALOG.items()
        },
    },
    "business_manager": {
        "label": "Business Manager",
        "department": "Management",
        "grants": {
            "dashboard": ["view"],
            "inventory": ["view", "create", "update", "adjust", "transfer"],
            "purchases": ["view", "create", "update", "receive", "return"],
            "sales": ["view", "create", "update", "discount", "cancel"],
            "bike_sales": _all("bike_sales"),
            "orders": _all("orders"),
            "finance": ["view"],
            "reports": ["view", "export"],
            "staff": ["view"],
            "reminders": ["view"],
            "tasks": _all("tasks"),
        },
    },
    "accountant": {
        "label": "Accountant",
        "department": "Finance",
        "grants": {
            "dashboard": ["view"],
            "finance": ["view", "create", "update", "delete", "void"],
            "reports": ["view", "export"],
            **_view("sales", "purchases", "inventory"),
            "reminders": ["view"],
        },
    },
    "sales_manager": {
        "label": "Sales Manager",
        "department": "Sales",
        "grants": {
            "dashboard": ["view"],
            "sales": [
                "view", "create", "update", "cancel", "discount", "refund",
            ],
            "bike_sales": _all("bike_sales"),
            "orders": _all("orders"),
            **_view("inventory", "reminders"),
            "reports": ["view"],
        },
    },
    "sales_staff": {
        "label": "Sales Staff",
        "department": "Sales",
        "grants": {
            "sales": ["view", "create"],
            "bike_sales": ["view", "create"],
            "orders": ["view", "create"],
            **_view("inventory", "reminders"),
        },
    },
    "purchase_manager": {
        "label": "Purchase Manager",
        "department": "Purchasing",
        "grants": {
            "purchases": [
                "view", "create", "update", "receive", "pay", "return",
            ],
            "inventory": ["view", "create"],
            "reports": ["view"],
            "reminders": ["view"],
        },
    },
    "purchase_staff": {
        "label": "Purchase Staff",
        "department": "Purchasing",
        "grants": {
            "purchases": ["view", "create", "receive"],
            **_view("inventory", "reminders"),
        },
    },
    "inventory_manager": {
        "label": "Inventory Manager",
        "department": "Inventory",
        "grants": {
            "inventory": [
                "view", "create", "update", "adjust", "write_off", "transfer",
            ],
            **_view("purchases", "reminders"),
            "reports": ["view"],
        },
    },
    "inventory_staff": {
        "label": "Inventory Staff",
        "department": "Inventory",
        "grants": {
            "inventory": ["view", "create", "adjust"],
            **_view("reminders"),
        },
    },
    "cashier": {
        "label": "Cashier",
        "department": "Sales",
        "grants": {
            "sales": ["view", "create"],
            "finance": ["view", "create"],
            **_view("inventory"),
        },
    },
    # A mechanic. Sees stock and prices to check parts, but nothing else of
    # the business. His own tasks, pay and profile need no permission.
    "technician": {
        "label": "Technician",
        "department": "Workshop",
        "grants": {
            **_view("inventory"),
        },
    },
    "hr_manager": {
        "label": "HR / Staff Manager",
        "department": "HR",
        "grants": {
            "staff": ["view", "create", "update", "delete", "assign_position"],
            "reminders": ["view"],
        },
    },
}


def presets():
    return [
        {
            "key": key,
            "label": preset["label"],
            "department": preset["department"],
            "permissions": sorted(
                f"{module}.{action}"
                for module, actions in preset["grants"].items()
                for action in actions
            ),
        }
        for key, preset in PRESETS.items()
    ]


def preset_codes(key):
    preset = PRESETS[key]

    return {
        f"{module}.{action}"
        for module, actions in preset["grants"].items()
        for action in actions
    }


# ---------------------------------------------------------------------------
# Codes <-> PositionPermission rows
# ---------------------------------------------------------------------------

def position_codes(position):
    """What a Position grants, as a set of codes (valid codes only)."""
    codes = set()

    for row in position.permissions.all():
        for action in FLAG_ACTIONS:
            if getattr(row, f"can_{action}"):
                codes.add(f"{row.module}.{action}")

        for action in row.actions or []:
            codes.add(f"{row.module}.{action}")

    return codes & all_codes()


def write_position_codes(position, codes):
    """Replace a Position's grants with exactly ``codes``."""
    by_module = {}

    for code in codes:
        module, action = code.split(".", 1)
        by_module.setdefault(module, set()).add(action)

    keep = set(by_module)

    position.permissions.exclude(module__in=keep).delete()

    for module, actions in by_module.items():
        PositionPermission.objects.update_or_create(
            position=position,
            module=module,
            defaults={
                **{
                    f"can_{flag}": flag in actions
                    for flag in FLAG_ACTIONS
                },
                "actions": sorted(actions - set(FLAG_ACTIONS)),
            },
        )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def override_map(user):
    return {
        item.permission: item.effect
        for item in user.permission_overrides.all()
    }


def effective_permissions(user, access_mode="full"):
    """Every code ``user`` holds right now, in the business they are in."""
    if (
        not user
        or not user.is_authenticated
        or not user.is_active
        or user.role == User.Role.SUPER_ADMIN
    ):
        return set()

    if user.role == User.Role.OWNER:
        codes = set(all_codes())

    elif user.role == User.Role.STAFF:
        position = user.position if user.position_id else None

        if position is None or not position.is_active:
            return set()

        codes = position_codes(position)

        for code, effect in override_map(user).items():
            if effect == "allow":
                codes.add(code)
            elif effect == "deny":
                codes.discard(code)

        codes &= delegable_codes()

    else:
        return set()

    if access_mode == "read_only":
        codes = {
            code for code in codes
            if code.split(".", 1)[1] in READ_ACTIONS
        }

    return codes


def has_permission(user, code, access_mode="full"):
    return code in effective_permissions(user, access_mode)


def breakdown(user):
    """
    For the Access & Permissions screen: what the Position gives, the
    person's own overrides, and the result.
    """
    position = user.position if user.position_id else None
    inherited = position_codes(position) if position else set()
    overrides = override_map(user)

    return {
        "inherited": sorted(inherited & delegable_codes()),
        "overrides": overrides,
        "effective": sorted(effective_permissions(user)),
    }
