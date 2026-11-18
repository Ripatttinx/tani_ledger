from decimal import Decimal, ROUND_HALF_UP
from django.db import migrations


HPP_BY_PRODUCT = {
    'PANEN-L1': Decimal('1000.00'),
    'PANEN-L2': Decimal('750.00'),
    'PANEN-L3': Decimal('1100.00'),
    'PANEN-L4': Decimal('3000.00'),
    'PANEN-L5': Decimal('2200.00'),
}


def update_fifo_costs(apps, schema_editor):
    db = schema_editor.connection.alias
    Product = apps.get_model('accounting', 'Product')
    Movement = apps.get_model('accounting', 'InventoryMovement')
    Layer = apps.get_model('accounting', 'InventoryLayer')
    Allocation = apps.get_model('accounting', 'InventoryAllocation')
    Account = apps.get_model('accounting', 'Account')
    Profile = apps.get_model('accounting', 'BusinessProfile')
    JournalLine = apps.get_model('accounting', 'JournalLine')

    old_opening_total = Decimal('0.00')
    new_opening_total = Decimal('0.00')
    for code, unit_cost in HPP_BY_PRODUCT.items():
        product = Product.objects.using(db).filter(code=code).first()
        if not product:
            continue
        # Only the initial harvest layer is repriced. Later purchases retain actual cost.
        opening_movements = Movement.objects.using(db).filter(
            product_id=product.pk, kind='OPENING'
        )
        for movement in opening_movements:
            old_opening_total += Decimal(movement.cost_amount or 0)
            new_amount = (Decimal(movement.quantity) * unit_cost).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            movement.unit_price = unit_cost
            movement.cost_amount = new_amount
            movement.save(using=db, update_fields=['unit_price', 'cost_amount'])
            new_opening_total += new_amount
            Layer.objects.using(db).filter(movement_id=movement.pk).update(unit_cost=unit_cost)

        # Recalculate prior sales that consumed any repriced opening layer.
        for allocation in Allocation.objects.using(db).filter(layer__product_id=product.pk).select_related('layer'):
            amount = (Decimal(allocation.quantity) * Decimal(allocation.layer.unit_cost)).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            if amount != allocation.amount:
                allocation.amount = amount
                allocation.save(using=db, update_fields=['amount'])

        for movement in Movement.objects.using(db).filter(product_id=product.pk, kind='OUT').exclude(entry_id=None):
            total_cost = sum((Decimal(a.amount) for a in Allocation.objects.using(db).filter(outgoing_id=movement.pk)), Decimal('0.00'))
            movement.cost_amount = total_cost
            movement.save(using=db, update_fields=['cost_amount'])
            profile = Profile.objects.using(db).first()
            inventory_code = profile.inventory_account_code if profile else '1150'
            cogs_code = profile.cogs_account_code if profile else '5110'
            inventory = Account.objects.using(db).filter(code=inventory_code).first()
            cogs = Account.objects.using(db).filter(code=cogs_code).first()
            if inventory and cogs:
                JournalLine.objects.using(db).filter(entry_id=movement.entry_id, account_id=cogs.pk).update(debit=total_cost, credit=Decimal('0.00'))
                JournalLine.objects.using(db).filter(entry_id=movement.entry_id, account_id=inventory.pk).update(debit=Decimal('0.00'), credit=total_cost)

    # Opening inventory and its balancing owner's capital must move by the same delta.
    delta = new_opening_total - old_opening_total
    if delta:
        inventory_account = Account.objects.using(db).filter(code='1150').first()
        capital_account = Account.objects.using(db).filter(code='3100').first()
        if inventory_account:
            inventory_account.opening_balance = Decimal(inventory_account.opening_balance) + delta
            inventory_account.save(using=db, update_fields=['opening_balance'])
        if capital_account:
            capital_account.opening_balance = Decimal(capital_account.opening_balance) + delta
            capital_account.save(using=db, update_fields=['opening_balance'])


def reverse_costs(apps, schema_editor):
    # Irreversible business-policy change; retain the revised costs on rollback.
    pass


class Migration(migrations.Migration):
    dependencies = [('accounting', '0003_party_auto_codes_and_sale_prices')]
    operations = [migrations.RunPython(update_fifo_costs, reverse_costs)]
