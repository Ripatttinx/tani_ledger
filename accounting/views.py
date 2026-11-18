from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from .forms import (AccountForm, AutoJournalForm, JournalEntryForm, JournalPairFormSet,
                    MovementForm, PartyForm, ProductForm, ProfileForm)
from .models import (Account, BusinessProfile, InventoryLayer, InventoryMovement,
                     JournalEntry, JournalLine, Party, Product, ZERO)
from .journal_rules import TERM_SUGGESTIONS, build_general_journal
from .vouchers import next_voucher_reference, voucher_prefix
from .services import (create_opening_stock, create_stock_movement, delete_journal_entry, ledger,
                       period_totals, report_balances, stock_card, subsidiary, worksheet)


def cutoff(request, key='to'):
    try:
        return date.fromisoformat(request.GET[key])
    except (KeyError, ValueError):
        return timezone.localdate()


def optional_date(request, key='from'):
    try:
        return date.fromisoformat(request.GET[key])
    except (KeyError, ValueError):
        return None


def field_errors(form):
    return '; '.join(str(e) for e in form.non_field_errors())


@login_required
def dashboard(request):
    as_of = cutoff(request)
    totals = period_totals(as_of)
    products = Product.objects.count()
    stock = InventoryLayer.objects.filter(remaining_quantity__gt=0).aggregate(q=Sum('remaining_quantity'))['q'] or 0
    recent = JournalEntry.objects.filter(date__lte=as_of).prefetch_related('lines').order_by('-date', '-id')[:6]
    journals = JournalEntry.objects.filter(date__lte=as_of).count()
    # Last six months: amount of transactions during each month, not accumulated balances.
    months = []
    current = as_of.year * 12 + as_of.month - 1
    for offset in range(5, -1, -1):
        x = current - offset
        y, m = divmod(x, 12)
        months.append((date(y, m + 1, 1), y, m + 1))
    chart = {'labels': [], 'revenue': [], 'costs': []}
    for start, y, m in months:
        q = JournalLine.objects.filter(entry__date__year=y, entry__date__month=m,
             entry__date__lte=as_of).values('account__category').annotate(d=Sum('debit'), c=Sum('credit'))
        by_type = {r['account__category']: (r['d'] or ZERO, r['c'] or ZERO) for r in q}
        rev = sum((by_type.get(k, (ZERO, ZERO))[1] - by_type.get(k, (ZERO, ZERO))[0]
                   for k in ('REVENUE', 'OTHER_INCOME')), ZERO)
        cost = sum((by_type.get(k, (ZERO, ZERO))[0] - by_type.get(k, (ZERO, ZERO))[1]
                    for k in ('COGS', 'EXPENSE', 'OTHER_EXPENSE')), ZERO)
        chart['labels'].append(start.strftime('%b %Y'))
        chart['revenue'].append(float(rev))
        chart['costs'].append(float(cost))
    return render(request, 'accounting/dashboard.html', {
        'title': 'Dashboard', 'as_of': as_of, 'totals': totals,
        'product_count': products, 'stock_count': stock, 'journal_count': journals,
        'recent': recent, 'chart': chart,
    })


@login_required
def accounts(request):
    return render(request, 'accounting/accounts.html', {
        'title': 'Daftar Akun', 'rows': Account.objects.all(),
    })


@login_required
@require_http_methods(['GET', 'POST'])
def account_form(request, pk=None):
    obj = get_object_or_404(Account, pk=pk) if pk else None
    form = AccountForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Daftar akun berhasil disimpan.')
        return redirect('accounts')
    return render(request, 'accounting/form_page.html', {
        'title': 'Ubah Akun' if obj else 'Tambah Akun', 'form': form, 'back_url': reverse('accounts'),
        'helper': 'Saldo awal bertanda positif mengikuti saldo normal. Gunakan negatif untuk akun kontra.'})


@login_required
def parties(request):
    return render(request, 'accounting/parties.html', {'title': 'Kode Pembantu', 'rows': Party.objects.all()})


@login_required
@require_http_methods(['GET', 'POST'])
def party_form(request, pk=None):
    obj = get_object_or_404(Party, pk=pk) if pk else None
    form = PartyForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Data relasi berhasil disimpan.')
        return redirect('parties')
    return render(request, 'accounting/form_page.html', {
        'title': 'Ubah Relasi' if obj else 'Tambah Relasi', 'form': form, 'back_url': reverse('parties'),
        'helper': 'Pelanggan untuk piutang (1140), pemasok untuk utang (2110).'})


@login_required
def journal_list(request):
    qs = JournalEntry.objects.prefetch_related('lines').exclude(kind=JournalEntry.Kind.ADJUSTMENT)
    start = optional_date(request)
    end = cutoff(request)
    if start:
        qs = qs.filter(date__gte=start)
    qs = qs.filter(date__lte=end)
    return render(request, 'accounting/journal_list.html', {
        'title': 'Jurnal Umum', 'rows': qs[:1000], 'start': start, 'end': end,
    })


def save_journal_pairs(formset, entry):
    """Ubah setiap input pasangan menjadi dua baris JournalLine."""
    for pair in formset.cleaned_data:
        if not pair or pair.get('DELETE'):
            continue
        amount = pair['amount']
        description = pair.get('description') or ''
        JournalLine.objects.create(
            entry=entry, account=pair['debit_account'], party=pair.get('debit_party'),
            description=description, debit=amount, credit=ZERO,
        )
        JournalLine.objects.create(
            entry=entry, account=pair['credit_account'], party=pair.get('credit_party'),
            description=description, debit=ZERO, credit=amount,
        )


def _plan_from_params(data, *, default_amount=None):
    """Bangun rencana jurnal dari data form/query (dipakai simpan dan pratinjau)."""
    def dec(key):
        raw = (data.get(key) or '').strip()
        try:
            return Decimal(raw) if raw else None
        except InvalidOperation:
            raise ValidationError('Angka tidak valid.')
    try:
        entry_date = date.fromisoformat(data.get('date') or '')
    except ValueError:
        entry_date = timezone.localdate()
    return entry_date, build_general_journal(
        data.get('description') or '', dec('amount') or default_amount, entry_date,
        term_text=data.get('term_text') or '', discount_percent=dec('discount_percent'),
        discount_rp=None,
        use_term_discount=str(data.get('use_term_discount', '')).lower() in ('1', 'true', 'on'))


@login_required
def journal_preview(request):
    """Pratinjau live akun debet/kredit; sumber kebenarannya sama dengan saat menyimpan."""
    if not (request.GET.get('description') or '').strip():
        return JsonResponse({'ok': False, 'hint': True, 'message': 'Ketik keterangan transaksi untuk melihat akun otomatis.'})
    # Pratinjau transaksi persediaan yang ditulis langsung sebagai keterangan jurnal.
    description = (request.GET.get('description') or '').casefold()
    stock_kind = ('OUT' if any(w in description for w in ('penjualan', 'jual ', 'menjual')) else
                  'IN' if any(w in description for w in ('pembelian', 'beli ', 'membeli')) else '')
    if stock_kind:
        qty_match = re.search(r'(\d+(?:[.,]\d+)?)\s*kg\b', description)
        products = list(Product.objects.filter(is_active=True))
        matches = []
        for product in products:
            aliases = [product.name.casefold(), product.code.casefold()]
            m = re.search(r'lahan\s*(\d+)', product.name.casefold())
            if m:
                aliases.append('lahan ' + m.group(1))
            if any(re.search(r'(?<![a-z0-9])' + re.escape(a) + r'(?![a-z0-9])', description)
                   for a in aliases):
                matches.append(product)
        if len(matches) == 1 and qty_match:
            product = matches[0]
            quantity = Decimal(qty_match.group(1).replace(',', '.'))
            credit = any(w in description for w in ('kredit', 'tempo', 'utang', 'hutang'))
            bank = any(w in description for w in ('transfer', 'bank', 'rekening'))
            cash = any(w in description for w in ('tunai', 'cash', 'kas', 'kontan'))
            settle_code = ('1140' if stock_kind == 'OUT' else '2110') if credit else ('1110' if bank else '1120' if cash else None)
            if settle_code:
                settlement = Account.objects.filter(code=settle_code, is_active=True, is_header=False).first()
                profile = BusinessProfile.current()
                accounts = {a.code: a for a in Account.objects.filter(
                    code__in=[profile.inventory_account_code, profile.cogs_account_code,
                              profile.sales_account_code, settle_code], is_active=True, is_header=False)}
                if settlement and all(c in accounts for c in [profile.inventory_account_code,
                        profile.cogs_account_code, profile.sales_account_code]):
                    if stock_kind == 'OUT':
                        sales = money(quantity * product.sale_price)
                        remaining = quantity
                        cost = ZERO
                        for layer in InventoryLayer.objects.filter(product=product, remaining_quantity__gt=0).select_related('movement').order_by('movement__date','movement__id','id'):
                            taken = min(remaining, layer.remaining_quantity)
                            cost += money(taken * layer.unit_cost)
                            remaining -= taken
                            if remaining <= 0: break
                        if remaining <= 0:
                            lines = [
                                (settle_code, settlement.name, sales, ZERO),
                                (profile.sales_account_code, accounts[profile.sales_account_code].name, ZERO, sales),
                                (profile.cogs_account_code, accounts[profile.cogs_account_code].name, cost, ZERO),
                                (profile.inventory_account_code, accounts[profile.inventory_account_code].name, ZERO, cost),
                            ]
                            return JsonResponse({'ok': True, 'lines': [
                                {'code': c, 'name': n, 'party': '', 'debit': str(d), 'credit': str(cr)}
                                for c,n,d,cr in lines], 'total_debit': str(sales+cost),
                                'total_credit': str(sales+cost), 'discount': '0'})
                        return JsonResponse({'ok': False, 'message': 'Stok tidak cukup untuk kuantitas penjualan tersebut.'})
                    total = Decimal((request.GET.get('amount') or '0').replace(',', '.'))
                    if total > 0:
                        unit = money(total / quantity)
                        lines = [(profile.inventory_account_code, accounts[profile.inventory_account_code].name, total, ZERO),
                                 (settle_code, settlement.name, ZERO, total)]
                        return JsonResponse({'ok': True, 'lines': [
                            {'code': c, 'name': n, 'party': '', 'debit': str(d), 'credit': str(cr)}
                            for c,n,d,cr in lines], 'total_debit': str(total), 'total_credit': str(total), 'discount': '0'})
    try:
        _, plan = _plan_from_params(request.GET)
    except (ValidationError, InvalidOperation) as exc:
        return JsonResponse({'ok': False, 'message': ' '.join(exc.messages) if hasattr(exc, 'messages') else str(exc)})
    return JsonResponse({
        'ok': True,
        'lines': [{'code': x['account'].code, 'name': x['account'].name,
                   'party': str(x['party']) if x['party'] else '',
                   'debit': str(x['debit']), 'credit': str(x['credit'])} for x in plan.lines],
        'total_debit': str(plan.total_debit), 'total_credit': str(plan.total_credit),
        'discount': str(plan.discount),
        'term': plan.term.text if plan.term else '',
        'due_date': plan.due_date.isoformat() if plan.due_date else '',
    })


@login_required
@require_http_methods(['GET', 'POST'])
def journal_new(request):
    instance = JournalEntry(kind=JournalEntry.Kind.GENERAL)
    form = AutoJournalForm(request.POST or None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        try:
            cleaned = form.cleaned_data
            if cleaned.get('inventory_kind'):
                stock_kind = cleaned['inventory_kind']
                account = cleaned['settlement_account']
                prefix = voucher_prefix([], movement_kind=stock_kind, settlement_code=account.code)
                reference = next_voucher_reference(prefix)
                effective_price = (cleaned['inventory_product'].sale_price if stock_kind == 'OUT'
                                   else cleaned['inventory_unit_price'])
                movement = create_stock_movement(
                    product=cleaned['inventory_product'], date_value=cleaned['date'],
                    reference=reference, kind=stock_kind, quantity=cleaned['inventory_quantity'],
                    unit_price=effective_price, settlement_account=account,
                    description=cleaned['description'], party=cleaned.get('inventory_party'),
                    user=request.user)
                messages.success(request, f'{movement.reference}: transaksi persediaan dan jurnal perpetual berhasil dicatat.')
                return redirect('journal_detail', pk=movement.entry_id)
            _, plan = _plan_from_params(request.POST)
            with transaction.atomic():
                obj = form.save(commit=False)
                obj.kind = JournalEntry.Kind.GENERAL
                obj.created_by = request.user
                # Pada metode perpetual, pembelian/penjualan barang dagang wajib melalui
                # Transaksi Persediaan agar kuantitas dan HPP FIFO ikut diperbarui.
                if (plan.type == 'purchase' and any(x['account'].code == '1150' for x in plan.lines)) or (
                    plan.type == 'sale' and any(x['account'].code == '4110' for x in plan.lines)
                ):
                    raise ValidationError(
                        'Untuk transaksi persediaan, pilih jenis transaksi persediaan dan isi produk serta kuantitas pada bagian khusus di atas agar stok dan HPP tercatat otomatis.'
                    )
                prefix = voucher_prefix(plan.lines)
                obj.reference = next_voucher_reference(prefix)
                obj.term = plan.term.text if plan.term else ''
                obj.due_date = plan.due_date
                obj.discount_amount = plan.discount
                obj.save()
                for item in plan.lines:
                    row = JournalLine(entry=obj, account=item['account'], party=item['party'],
                                      description=obj.description, debit=item['debit'], credit=item['credit'])
                    row.full_clean()
                    row.save()
            messages.success(request, 'Jurnal umum berhasil dicatat. Akun dan nominal debet/kredit dibuat otomatis.')
            return redirect('journal_detail', pk=obj.pk)
        except (IntegrityError, ValidationError) as exc:
            form.add_error(None, ' '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))
    return render(request, 'accounting/journal_form.html', {
        'title': 'Tambah Jurnal Umum', 'form': form,
        'back_url': reverse('journal_list'), 'journal_type': 'umum',
        'term_suggestions': TERM_SUGGESTIONS,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def journal_delete(request, pk):
    obj = get_object_or_404(JournalEntry.objects.prefetch_related('lines__account', 'lines__party'), pk=pk)
    is_adjustment = obj.kind == JournalEntry.Kind.ADJUSTMENT
    back = reverse('adjustment_list' if is_adjustment else 'journal_list')
    if request.method == 'POST':
        try:
            reference = obj.reference
            delete_journal_entry(obj)
        except (ValidationError, IntegrityError) as exc:
            messages.error(request, ' '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))
            return redirect('journal_detail', pk=obj.pk)
        messages.success(request, f'Jurnal {reference} berhasil dihapus. Buku besar dan seluruh laporan otomatis diperbarui.')
        return redirect(back)
    return render(request, 'accounting/journal_delete.html', {
        'title': f'Hapus {obj.reference}', 'obj': obj, 'back_url': back,
        'is_stock': obj.kind == JournalEntry.Kind.INVENTORY,
    })


@login_required
def adjustment_list(request):
    qs = JournalEntry.objects.prefetch_related('lines').filter(kind=JournalEntry.Kind.ADJUSTMENT)
    start, end = optional_date(request), cutoff(request)
    if start:
        qs = qs.filter(date__gte=start)
    qs = qs.filter(date__lte=end)
    return render(request, 'accounting/journal_list.html', {
        'title': 'Jurnal Penyesuaian', 'rows': qs[:1000], 'start': start,
        'end': end, 'is_adjustment': True,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def adjustment_new(request):
    instance = JournalEntry(kind=JournalEntry.Kind.ADJUSTMENT)
    form = JournalEntryForm(request.POST or None, instance=instance)
    form.fields.pop('kind', None)
    formset = JournalPairFormSet(request.POST or None, prefix='pairs')
    if request.method == 'POST' and form.is_valid() and formset.is_valid():
        try:
            with transaction.atomic():
                obj = form.save(commit=False)
                # Forced server-side: a POST payload cannot switch journal types.
                obj.kind = JournalEntry.Kind.ADJUSTMENT
                obj.created_by = request.user
                obj.reference = next_voucher_reference('JP')
                obj.save()
                save_journal_pairs(formset, obj)
            messages.success(request, 'Penyesuaian berhasil diposting ke neraca lajur dan laporan.')
            return redirect('journal_detail', pk=obj.pk)
        except (IntegrityError, ValidationError) as exc:
            form.add_error(None, str(exc))
    return render(request, 'accounting/journal_form.html', {
        'title': 'Tambah Jurnal Penyesuaian', 'form': form, 'formset': formset,
        'back_url': reverse('adjustment_list'), 'journal_type': 'penyesuaian',
    })


@login_required
def journal_detail(request, pk):
    obj = get_object_or_404(JournalEntry.objects.prefetch_related('lines__account', 'lines__party'), pk=pk)
    return render(request, 'accounting/journal_detail.html', {'title': obj.reference, 'obj': obj,
        'back_url': reverse('adjustment_list' if obj.kind == JournalEntry.Kind.ADJUSTMENT else 'journal_list')})


@login_required
def ledger_view(request):
    account = Account.objects.filter(pk=request.GET.get('account'), is_header=False).first() if request.GET.get('account', '').isdigit() else None
    start, end = optional_date(request), cutoff(request)
    data = ledger(account, start, end) if account else None
    return render(request, 'accounting/ledger.html', {
        'title': 'Buku Besar', 'account': account, 'accounts': Account.objects.filter(is_header=False),
        'data': data, 'start': start, 'end': end,
    })


@login_required
def subsidiary_view(request):
    party = Party.objects.filter(pk=request.GET.get('party')).first() if request.GET.get('party', '').isdigit() else None
    start, end = optional_date(request), cutoff(request)
    data = subsidiary(party, start, end) if party else None
    return render(request, 'accounting/subsidiary.html', {
        'title': 'Buku Besar Pembantu', 'party': party, 'parties': Party.objects.all(),
        'data': data, 'start': start, 'end': end,
    })


@login_required
def trial_balance(request):
    as_of = cutoff(request)
    rows = report_balances(as_of)
    totals = {
        'debit': sum((x['debit'] for x in rows), ZERO),
        'credit': sum((x['credit'] for x in rows), ZERO),
    }
    totals['difference'] = totals['debit'] - totals['credit']
    return render(request, 'accounting/trial_balance.html', {
        'title': 'Neraca Saldo', 'as_of': as_of, 'rows': rows, 'totals': totals,
    })


@login_required
def worksheet_view(request):
    as_of = cutoff(request)
    return render(request, 'accounting/worksheet.html', {
        'title': 'Neraca Lajur', 'as_of': as_of, 'data': worksheet(as_of),
    })


@login_required
def income_statement(request):
    as_of = cutoff(request)
    data = period_totals(as_of)
    groups = []
    for code, title in [('REVENUE', 'Pendapatan Usaha'), ('COGS', 'Harga Pokok Penjualan'),
                        ('EXPENSE', 'Beban Operasional'), ('OTHER_INCOME', 'Pendapatan Lain-lain'),
                        ('OTHER_EXPENSE', 'Beban Lain-lain')]:
        rows = data['groups'].get(code, [])
        items = [{'account': r['account'], 'amount': -r['net'] if code in ('REVENUE', 'OTHER_INCOME') else r['net']} for r in rows]
        groups.append({'title': title, 'items': items, 'total': sum((r['amount'] for r in items), ZERO)})
    return render(request, 'accounting/income.html', {
        'title': 'Laba Rugi', 'as_of': as_of, 'data': data, 'groups': groups,
    })


@login_required
def balance_sheet(request):
    as_of = cutoff(request)
    data = period_totals(as_of)
    groups = []
    for code, title in [('ASSET', 'Aset'), ('LIABILITY', 'Kewajiban'), ('EQUITY', 'Ekuitas')]:
        rows = data['groups'].get(code, [])
        items = [{'account': r['account'], 'amount': r['net'] if code == 'ASSET' else -r['net']} for r in rows]
        groups.append({'title': title, 'items': items, 'total': sum((r['amount'] for r in items), ZERO)})
    return render(request, 'accounting/balance.html', {
        'title': 'Neraca', 'as_of': as_of, 'data': data, 'groups': groups,
    })


@login_required
def products(request):
    rows = []
    for product in Product.objects.all():
        card = stock_card(product)
        # Display the unit cost of the next FIFO layer so users can see HPP/kg.
        next_layer = (InventoryLayer.objects.filter(
            product=product, remaining_quantity__gt=0
        ).select_related('movement').order_by('movement__date', 'movement__id', 'id').first())
        hpp_unit = next_layer.unit_cost if next_layer else None
        rows.append({
            'product': product, 'qty': card['qty'], 'value': card['value'],
            'hpp_unit': hpp_unit,
        })
    return render(request, 'accounting/products.html', {'title': 'Master Persediaan', 'rows': rows})


@login_required
@require_http_methods(['GET', 'POST'])
def product_form(request, pk=None):
    obj = get_object_or_404(Product, pk=pk) if pk else None
    form = ProductForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                product = form.save()
                if not obj and form.cleaned_data.get('opening_quantity', 0) > 0:
                    create_opening_stock(product, form.cleaned_data['opening_quantity'],
                        form.cleaned_data['opening_unit_cost'], form.cleaned_data['opening_date'])
            messages.success(request, 'Data produk berhasil disimpan.')
            return redirect('products')
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'accounting/form_page.html', {
        'title': 'Ubah Produk' if obj else 'Tambah Produk', 'form': form, 'back_url': reverse('products'),
        'helper': 'Stok awal hanya dicatat sekali ketika produk dibuat. Pastikan nilainya tercermin dalam saldo awal akun Persediaan.'})


@login_required
def inventory_card(request):
    product = Product.objects.filter(pk=request.GET.get('product')).first() if request.GET.get('product', '').isdigit() else None
    end = cutoff(request)
    data = stock_card(product, end=end) if product else None
    next_layer = (InventoryLayer.objects.filter(
        product=product, remaining_quantity__gt=0
    ).select_related('movement').order_by('movement__date', 'movement__id', 'id').first()) if product else None
    return render(request, 'accounting/inventory.html', {
        'title': 'Inventory Card · FIFO', 'product': product, 'products': Product.objects.all(),
        'data': data, 'end': end,
        'hpp_unit': next_layer.unit_cost if next_layer else None,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def movement_new(request):
    form = MovementForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            prefix = voucher_prefix([], movement_kind=d['kind'],
                settlement_code=d['settlement_account'].code)
            reference = next_voucher_reference(prefix)
            if d['kind'] == 'IN' and not d.get('unit_price'):
                raise ValidationError('Harga beli per unit wajib diisi untuk barang masuk.')
            effective_unit_price = d['product'].sale_price if d['kind'] == 'OUT' else d['unit_price']
            if d['kind'] == 'OUT' and effective_unit_price <= 0:
                raise ValidationError('Harga jual produk belum diatur pada Master Produk & Persediaan.')
            movement = create_stock_movement(product=d['product'], date_value=d['date'],
                reference=reference, kind=d['kind'], quantity=d['quantity'],
                unit_price=effective_unit_price, settlement_account=d['settlement_account'],
                description=d['description'], party=d.get('party'), user=request.user)
            messages.success(request, f'Mutasi stok {movement.reference} dan jurnal otomatis berhasil disimpan.')
            return redirect(f"{reverse('inventory')}?product={movement.product_id}")
        except (ValidationError, IntegrityError) as exc:
            form.add_error(None, str(exc))
    return render(request, 'accounting/form_page.html', {
        'title': 'Transaksi Persediaan', 'form': form, 'back_url': reverse('inventory'),
        'helper': 'METODE PERPETUAL (FIFO): barang masuk langsung menambah Persediaan; barang keluar otomatis mencatat Penjualan sekaligus HPP dan pengurangan Persediaan. Nomor bukti dibuat otomatis (BKK/BKM/FP/FJ). Transaksi stok tidak dapat dibackdate.'})


@login_required
@require_http_methods(['GET', 'POST'])
def settings_view(request):
    obj = BusinessProfile.current()
    form = ProfileForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Pengaturan nama usaha berhasil disimpan.')
        return redirect('settings')
    return render(request, 'accounting/form_page.html', {
        'title': 'Pengaturan Usaha', 'form': form, 'back_url': reverse('dashboard'),
        'helper': 'Nama usaha muncul pada header seluruh halaman dan file Excel. Kode akun Persediaan/HPP/Penjualan wajib ada sebelum mencatat stok.'})


@login_required
def export_view(request, kind):
    from .exports import export_download
    return export_download(request, kind)


# ---------------------------------------------------------------------------
# Jurnal khusus dan jurnal penutup (fitur tambahan; tidak mengubah alur jurnal lama)
# ---------------------------------------------------------------------------
from django.db.models import Q
from django.utils.dateparse import parse_date


def _period_from_request(request):
    start = parse_date(request.GET.get('from', '') or '') or date(date.today().year, 1, 1)
    end = parse_date(request.GET.get('to', '') or '') or timezone.localdate()
    return start, end


def _special_journal_type(entry):
    """Klasifikasi jurnal khusus berdasarkan akun yang benar-benar diposting."""
    lines = list(entry.lines.select_related('account').all())
    if not lines:
        return 'GENERAL'
    cash_codes = {'1110', '1120', '1130'}
    ar_codes, ap_codes = {'1140'}, {'2110'}
    revenue_codes = {'4110', '4120'}
    inventory_codes = {'1150', '1160'}
    debit_codes = {x.account.code for x in lines if x.debit > 0}
    credit_codes = {x.account.code for x in lines if x.credit > 0}
    # Penjualan kredit: piutang didebit dan pendapatan penjualan dikredit.
    if debit_codes & ar_codes and credit_codes & revenue_codes:
        return 'SALES'
    # Pembelian kredit: persediaan/beban terkait didebit dan utang dikredit.
    if debit_codes & inventory_codes and credit_codes & ap_codes:
        return 'PURCHASES'
    # Penerimaan/pengeluaran kas/bank.
    if debit_codes & cash_codes:
        return 'CASH_RECEIPTS'
    if credit_codes & cash_codes:
        return 'CASH_DISBURSEMENTS'
    return 'GENERAL'


@login_required
def special_journals(request):
    start, end = _period_from_request(request)
    entries = (JournalEntry.objects.filter(date__gte=start, date__lte=end)
               .exclude(kind__in=[JournalEntry.Kind.ADJUSTMENT, JournalEntry.Kind.CLOSING])
               .prefetch_related('lines__account').order_by('date', 'id'))
    groups = {
        'SALES': {'title': 'Jurnal Penjualan', 'rows': []},
        'PURCHASES': {'title': 'Jurnal Pembelian', 'rows': []},
        'CASH_RECEIPTS': {'title': 'Jurnal Penerimaan Kas', 'rows': []},
        'CASH_DISBURSEMENTS': {'title': 'Jurnal Pengeluaran Kas', 'rows': []},
        'GENERAL': {'title': 'Jurnal Umum (transaksi lain)', 'rows': []},
    }
    for entry in entries:
        groups[_special_journal_type(entry)]['rows'].append(entry)
    return render(request, 'accounting/special_journals.html', {
        'title': 'Jurnal Khusus', 'groups': groups, 'start': start, 'end': end,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def closing_journal(request):
    start, end = _period_from_request(request)
    preview = None
    error = None
    if request.method == 'POST':
        start = parse_date(request.POST.get('from', '') or '') or date(end.year, 1, 1)
        end = parse_date(request.POST.get('to', '') or '') or timezone.localdate()
        if start > end:
            error = 'Tanggal awal periode tidak boleh melewati tanggal akhir.'
        else:
            ref = f'TUTUP-{end:%Y%m%d}'
            if JournalEntry.objects.filter(reference=ref).exists():
                error = f'Jurnal penutup {ref} sudah pernah dibuat. Hapus jurnal tersebut terlebih dahulu jika perlu koreksi.'
            else:
                # Hanya akun nominal (pendapatan dan beban) untuk aktivitas periode berjalan.
                nominal = Account.objects.filter(
                    is_header=False, is_active=True,
                    category__in=['REVENUE', 'COGS', 'EXPENSE', 'OTHER_INCOME', 'OTHER_EXPENSE']
                ).order_by('code')
                totals = {r['account_id']: (r['d'] or ZERO, r['c'] or ZERO)
                    for r in JournalLine.objects.filter(
                        entry__date__gte=start, entry__date__lte=end
                    ).values('account_id').annotate(d=Sum('debit'), c=Sum('credit'))}
                closing_lines = []
                net_income_summary = ZERO  # debit minus credit pada ikhtisar
                for account in nominal:
                    debit, credit = totals.get(account.id, (ZERO, ZERO))
                    net = debit - credit
                    if net > 0:
                        closing_lines.append((account, ZERO, net))
                        net_income_summary -= net
                    elif net < 0:
                        closing_lines.append((account, -net, ZERO))
                        net_income_summary += -net
                if not closing_lines:
                    error = 'Tidak ada saldo pendapatan atau beban pada periode yang dipilih.'
                else:
                    # Ikhtisar Laba Rugi dipakai sebagai akun perantara penutupan.
                    summary, _ = Account.objects.get_or_create(
                        code='3900',
                        defaults={'name': 'Ikhtisar Laba Rugi', 'category':'EQUITY',
                                  'normal_balance':'C', 'opening_balance':ZERO,
                                  'is_active':True, 'is_header':False}
                    )
                    retained = Account.objects.filter(code='3110', is_header=False, is_active=True).first()
                    if not retained:
                        error = 'Akun 3110 Retained earnings belum tersedia. Tambahkan akun ekuitas tersebut sebelum menutup buku.'
                    else:
                        # Baris penutup akun nominal menuju Ikhtisar Laba Rugi.
                        # Net summary kredit = laba; net summary debit = rugi.
                        if net_income_summary > 0:
                            # Ikhtisar bersaldo kredit (laba): tutup ke saldo laba.
                            closing_lines.append((summary, ZERO, net_income_summary))
                            closing_lines.append((summary, net_income_summary, ZERO))
                            closing_lines.append((retained, ZERO, net_income_summary))
                        elif net_income_summary < 0:
                            # Ikhtisar bersaldo debet (rugi): tutup ke saldo laba.
                            loss = -net_income_summary
                            closing_lines.append((summary, loss, ZERO))
                            closing_lines.append((summary, ZERO, loss))
                            closing_lines.append((retained, loss, ZERO))
                        with transaction.atomic():
                            entry = JournalEntry.objects.create(
                                date=end, reference=ref,
                                description=f'Jurnal penutup periode {start:%d-%m-%Y} s.d. {end:%d-%m-%Y}',
                                kind='CLOSING', created_by=request.user
                            )
                            for account, debit, credit in closing_lines:
                                JournalLine.objects.create(
                                    entry=entry, account=account,
                                    description='Penutupan saldo akun nominal',
                                    debit=debit, credit=credit
                                )
                        messages.success(request, f'Jurnal penutup {ref} berhasil dibuat.')
                        return redirect('closing_journal')
    # Preview perhitungan periode (tanpa menyimpan jurnal).
    nominal = Account.objects.filter(
        is_header=False, is_active=True,
        category__in=['REVENUE', 'COGS', 'EXPENSE', 'OTHER_INCOME', 'OTHER_EXPENSE']
    ).order_by('code')
    totals = {r['account_id']: (r['d'] or ZERO, r['c'] or ZERO)
        for r in JournalLine.objects.filter(entry__date__gte=start, entry__date__lte=end)
        .values('account_id').annotate(d=Sum('debit'), c=Sum('credit'))}
    preview_rows = []
    period_net = ZERO
    for account in nominal:
        debit, credit = totals.get(account.id, (ZERO, ZERO))
        net = debit - credit
        if net:
            preview_rows.append({'account':account, 'debit':debit, 'credit':credit,
                                 'closing_debit':max(-net, ZERO), 'closing_credit':max(net, ZERO)})
            period_net += net
    preview = {'rows':preview_rows, 'profit':-period_net}
    return render(request, 'accounting/closing_journal.html', {
        'title':'Jurnal Penutup', 'start':start, 'end':end, 'preview':preview, 'error':error,
        'already_closed': JournalEntry.objects.filter(reference=f'TUTUP-{end:%Y%m%d}').exists(),
    })
