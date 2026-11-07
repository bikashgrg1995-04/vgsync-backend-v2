from django.db import migrations

FAQS = [
    (
        "How to add a new product?",
        "Open Inventory, choose New product, fill in the name, price and "
        "opening stock, then save. The product is available for sales and "
        "purchases in the active branch straight away.",
    ),
    (
        "How to update stock quantity?",
        "Open Inventory, select the product and use Adjust stock. Enter the "
        "new quantity and a reason so the change shows in the stock history. "
        "Quantities also change on their own when you confirm a purchase, a "
        "sale or a return.",
    ),
    (
        "How to process a sale?",
        "Open Sales and choose New sale. Pick the customer, add the items, "
        "record any payment taken, then confirm the sale. Confirming reduces "
        "the stock and adds the sale to the customer's statement.",
    ),
    (
        "Can I get a refund?",
        "For a sale made in VGSync, open Returns, find the sale and record "
        "the return. The credit then shows on the customer's account and can "
        "be refunded or used on a later sale. For your VGSync subscription, "
        "please raise a support ticket.",
    ),
    (
        "How to generate sales report?",
        "Open Reports, choose the sales report, set the date range and "
        "branch, and the figures load on the page. Use the Excel or PDF "
        "buttons to download a copy.",
    ),
]


def seed(apps, schema_editor):
    Faq = apps.get_model("support", "Faq")

    if Faq.objects.exists():
        return

    Faq.objects.bulk_create(
        [
            Faq(question=question, answer=answer, sort_order=index)
            for index, (question, answer) in enumerate(FAQS, start=1)
        ]
    )


class Migration(migrations.Migration):
    dependencies = [
        ("support", "0001_initial"),
    ]

    operations = [
        # Nothing to undo: dropping the table removes the rows.
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
