from django.db import migrations

# (old question to replace or None, question, answer). Matched by the old
# question, or the new one, so running it twice changes nothing.
FAQS = [
    ("How to add a new product?", "How do I add a new product?",
     "Open Inventory and choose + Add Stock, fill in the stock number, name, prices and "
     "opening stock, then save. To add many products at once use Import Products with an "
     "Excel file. Only a Head Office or an independent business keeps the product list; "
     "branches use their Head Office's products."),
    ("How to update stock quantity?", "How do I change a product's stock quantity?",
     "For one product, open Inventory, use the ⋯ menu on the product and choose Adjust stock, "
     "then give a reason so the change shows in the stock history. For many products, use "
     "Import Stock with a count sheet. Quantities also change on their own when you confirm "
     "a purchase, a sale or a return."),
    (None, "How do I import my current stock from Excel?",
     "Open Inventory and choose Import Stock. Download the template, fill in Stock No and "
     "Quantity (the quantity you have now), then upload it and press Check file. Fix any "
     "row it lists, then Save. Each product is brought to that quantity and the change is "
     "recorded in the stock history. The products must already exist."),
    (None, "How do I update MRP and prices from a new price list?",
     "Open Inventory and choose Update MRP (Head Office or independent business only; "
     "branches get the new prices from Head Office). Upload an Excel file with Stock No, "
     "Stock Name, Purchase Price and Selling Price. Products are matched by Stock No, so the "
     "name may differ: the name and prices in your inventory are changed to the file's. "
     "Stock quantity is not changed."),
    ("How to process a sale?", "How do I make a sale?",
     "Open Sales and choose New Sale. Pick the customer, add the items, record any payment "
     "taken, then confirm the sale. Confirming takes the items out of stock and adds the "
     "sale to the customer's account. A sale left partly unpaid creates a follow-up."),
    ("Can I get a refund?", "How do I record a sales return?",
     "Open Sales Returns, find the sale and record the return. Only stock sales can be "
     "returned; servicing and bike sales cannot. The credit shows on the customer's account "
     "and can be refunded or used on a later sale. For your VGSync subscription, raise a "
     "support ticket instead."),
    (None, "How do I record a purchase and a purchase return?",
     "Open Purchase to add the supplier's bill and its items, then record the goods as "
     "received so stock goes up. Damaged goods can be recorded as damaged on receipt. For "
     "goods you send back, use Purchase Returns."),
    (None, "What are Follow-ups?",
     "Follow-ups are calls to make: a check three days after a service, a 90-day service "
     "reminder, EMI instalments, credit still to receive and free services on a bike. They "
     "are created automatically. Mark one done or skipped with a note; it closes by itself "
     "when the payment it waited for is received."),
    (None, "How do I add staff and decide what they can do?",
     "Open Staff Management. Position/Role is where you create positions (for example "
     "Technician, Store Keeper, Telecaller, EMI Officer) and tick what each may do. Staffs "
     "is where you add the person, choose their position and set their login. Tasks lets "
     "the owner assign work to staff and follow its status."),
    (None, "How does a technician ask for parts or extra work on a job?",
     "The technician opens My Jobs, picks the job and requests the parts or work needed. "
     "The office sees the request on the sale and approves or rejects it. Purchase cost is "
     "hidden from technicians unless their position is allowed to see it."),
    (None, "How do I bring in data from the old VGSync system?",
     "The owner opens Settings, then Backup, and uses Import old VGSync data. Upload the old "
     "data.json file, press Check file to see what will come in, then type your business "
     "code to confirm. It works only for a Head Office or independent business with no sales "
     "or purchases yet, so nothing is entered twice. A safety copy is kept first so it can "
     "be undone."),
    (None, "How do I back up or restore my data?",
     "Open Settings, then Backup. Download backup gives a ZIP of your records (passwords are "
     "never included). Restore puts the records back from such a file after you confirm with "
     "your business code, and a safety copy from just before is kept."),
    ("How to generate sales report?", "How do I get a sales report?",
     "Open Reports, choose the sales report, set the date range and branch, and the figures "
     "load on the page. Use the Excel or PDF buttons to download a copy."),
    (None, "What is the difference between Finance statements and Reports?",
     "Finance holds the money records you enter and reconcile: income, expenses, cash and "
     "bank. Reports are read-only summaries of sales, purchases, stock and staff for a date "
     "range, with downloads."),
]


def refresh(apps, schema_editor):
    Faq = apps.get_model("support", "Faq")

    for order, (old, question, answer) in enumerate(FAQS, start=1):
        row = (
            Faq.objects.filter(question=question).first()
            or (Faq.objects.filter(question=old).first() if old else None)
        )

        if row is None:
            row = Faq(question=question)

        row.question, row.answer, row.sort_order, row.is_published = question, answer, order, True
        row.save()


class Migration(migrations.Migration):
    dependencies = [
        ("support", "0002_seed_faqs"),
    ]

    operations = [
        migrations.RunPython(refresh, migrations.RunPython.noop),
    ]
