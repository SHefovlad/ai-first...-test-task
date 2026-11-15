#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отчёт об оплате работ по договорам с подрядчиками.

По файлам contracts.csv / works.csv / payments.csv и дате отчёта программа
считает по каждой работе цену, выплаченное и остаток, и итоги по каждому
договору.

Запуск (Windows, из папки проекта):

    py -3 advance_report.py --data data --date 2026-11-10 --out-file report.txt
    py -3 advance_report.py --data data --date 2026-11-10 --format all --out out
    py -3 advance_report.py --data data --date 2026-11-10 --format json
    py -3 advance_report.py --data data --date 2026-11-10 --allocation spread

Только стандартная библиотека Python 3.10+. Деньги считаются в Decimal,
до копейки, без float. Код возврата: 0 - всё сошлось, 1 - есть вопросы к
данным (см. блок «ЧТО ТРЕБУЕТ ВНИМАНИЯ»), 2 - не удалось прочитать входные
файлы.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
NBSP = "\u00a0"

STATUS_RU = {
    "done": "выполнена",
    "planned": "запланирована",
    "cancelled": "отменена",
}

# статус работы уже на дату отчёта, а не на момент выгрузки
STATUS_AT_RU = {
    "done": "выполнена",
    "done_later": "принята позже даты",
    "planned": "не закрыта",
    "cancelled": "отменена",
}

PAY_STATUS_RU = {
    "paid": "выплачен",
    "queued": "в очереди",
    "refunded": "возвращён",
}


# --------------------------------------------------------------------------
# деньги и даты
# --------------------------------------------------------------------------

def money(value) -> Decimal:
    """Привести сумму к Decimal с точностью до копейки."""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_money(raw: Optional[str]) -> Optional[Decimal]:
    """Разобрать сумму: 100000.00, «100 000,00», «100000», «100 000 руб.»."""
    s = (raw or "").strip()
    for junk in (NBSP, "\u202f", " ", "₽", "руб.", "руб", "р."):
        s = s.replace(junk, "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return money(Decimal(s))
    except InvalidOperation:
        raise ValueError("не удалось разобрать сумму: {!r}".format(raw))


def parse_date(raw: Optional[str]) -> Optional[date]:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError("не удалось разобрать дату: {!r}".format(raw))


def fmt_money(value: Optional[Decimal]) -> str:
    """Сумма для человека: 100 000,00."""
    if value is None:
        return ""
    value = money(value)
    sign = "-" if value < ZERO else ""
    whole, _, frac = "{:.2f}".format(abs(value)).partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return "{}{},{}".format(sign, NBSP.join(groups), frac)


def fmt_date(value: Optional[date]) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


# --------------------------------------------------------------------------
# данные
# --------------------------------------------------------------------------

@dataclass
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: Optional[date]


@dataclass
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: str
    completed_at: Optional[date]
    cancelled_at: Optional[date]


@dataclass
class Payment:
    payment_id: str
    contract_id: str
    work_id: Optional[str]
    purpose: str
    amount: Decimal
    status: str
    created_at: Optional[date]
    paid_at: Optional[date]
    refunded_at: Optional[date]


@dataclass
class Dataset:
    contracts: dict
    works: list
    payments: list
    warnings: list = field(default_factory=list)


def _read_csv(path: Path) -> list:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        text = fh.read()
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items()})
    return rows


def load_dataset(data_dir: Path) -> Dataset:
    warnings = []
    contracts = {}
    works = []
    payments = []

    for row in _read_csv(data_dir / "contracts.csv"):
        cid = row.get("contract_id", "")
        if not cid:
            warnings.append("contracts.csv: строка без contract_id пропущена")
            continue
        if cid in contracts:
            warnings.append("contracts.csv: договор {} встречается дважды".format(cid))
        contracts[cid] = Contract(
            contract_id=cid,
            contractor=row.get("contractor", ""),
            amount=parse_money(row.get("amount")) or ZERO,
            signed_at=parse_date(row.get("signed_at")),
        )

    seen_works = set()
    for row in _read_csv(data_dir / "works.csv"):
        wid = row.get("work_id", "")
        if not wid:
            warnings.append("works.csv: строка без work_id пропущена")
            continue
        if wid in seen_works:
            warnings.append("works.csv: работа {} встречается дважды".format(wid))
        seen_works.add(wid)
        status = (row.get("status") or "").strip().lower()
        if status not in STATUS_RU:
            warnings.append("works.csv: у работы {} неизвестный статус {!r}".format(wid, status))
        works.append(Work(
            work_id=wid,
            contract_id=row.get("contract_id", ""),
            title=row.get("title", ""),
            price=parse_money(row.get("price")) or ZERO,
            status=status,
            completed_at=parse_date(row.get("completed_at")),
            cancelled_at=parse_date(row.get("cancelled_at")),
        ))

    work_contract = {w.work_id: w.contract_id for w in works}
    seen_payments = set()
    for row in _read_csv(data_dir / "payments.csv"):
        pid = row.get("payment_id", "")
        if not pid:
            warnings.append("payments.csv: строка без payment_id пропущена")
            continue
        if pid in seen_payments:
            warnings.append("payments.csv: платёж {} встречается дважды".format(pid))
        seen_payments.add(pid)
        cid = row.get("contract_id", "")
        wid = row.get("work_id", "") or None
        if wid and wid not in work_contract:
            warnings.append("payments.csv: платёж {} ссылается на неизвестную работу {}".format(pid, wid))
            wid = None
        elif wid and work_contract.get(wid) != cid:
            warnings.append("payments.csv: платёж {} привязан к работе {} чужого договора".format(pid, wid))
            wid = None
        status = (row.get("status") or "").strip().lower()
        if status not in PAY_STATUS_RU:
            warnings.append("payments.csv: у платежа {} неизвестный статус {!r}".format(pid, status))
        payments.append(Payment(
            payment_id=pid,
            contract_id=cid,
            work_id=wid,
            purpose=row.get("purpose", ""),
            amount=parse_money(row.get("amount")) or ZERO,
            status=status,
            created_at=parse_date(row.get("created_at")),
            paid_at=parse_date(row.get("paid_at")),
            refunded_at=parse_date(row.get("refunded_at")),
        ))

    known = set(contracts)
    for w in works:
        if w.contract_id not in known:
            warnings.append("works.csv: работа {} ссылается на неизвестный договор {}".format(
                w.work_id, w.contract_id))
    for p in payments:
        if p.contract_id not in known:
            warnings.append("payments.csv: платёж {} ссылается на неизвестный договор {}".format(
                p.payment_id, p.contract_id))

    return Dataset(contracts=contracts, works=works, payments=payments, warnings=warnings)


# --------------------------------------------------------------------------
# расчёт
# --------------------------------------------------------------------------

@dataclass
class WorkLine:
    work: Work
    status_at: str            # cancelled | done | done_later | planned
    price: Decimal
    paid: Decimal = ZERO
    queued: Decimal = ZERO    # заведено в очередь на оплату на дату
    attached: Decimal = ZERO  # из paid пришло платежом, привязанным к работе
    source: list = field(default_factory=list)

    @property
    def remaining(self) -> Decimal:
        if self.status_at == "cancelled":
            return ZERO
        return money(self.price - self.paid)

    @property
    def accepted_at_date(self) -> bool:
        """Принята ли работа на дату отчёта (не позже)."""
        return self.status_at == "done"


@dataclass
class ContractResult:
    contract: Contract
    lines: list
    active_total: Decimal      # цена работ, действующих на дату
    cancelled_total: Decimal   # цена работ, отменённых к дате
    all_works_total: Decimal
    paid_total: Decimal        # все действующие платежи договора на дату
    paid_on_cancelled: Decimal
    queued_total: Decimal
    advance_excess: Decimal    # деньги, которым не нашлось работы
    allocation: str

    @property
    def works_paid(self) -> Decimal:
        return money(sum((ln.paid for ln in self.lines if ln.status_at != "cancelled"), ZERO))

    @property
    def works_remaining(self) -> Decimal:
        return money(sum((ln.remaining for ln in self.lines), ZERO))

    @property
    def remaining(self) -> Decimal:
        return money(self.active_total - self.paid_total)

    @property
    def remaining_accepted(self) -> Decimal:
        return money(sum((ln.remaining for ln in self.lines if ln.accepted_at_date), ZERO))

    @property
    def remaining_not_accepted(self) -> Decimal:
        return money(sum((ln.remaining for ln in self.lines if not ln.accepted_at_date), ZERO))


def work_status_at(work, as_of, warnings):
    """Статус работы на дату отчёта, а не на момент выгрузки.

    Записи в works.csv описывают состояние на момент выгрузки, поэтому статус
    и даты сверяются с датой отчёта: работа, принятая позже даты, на дату ещё
    не принята, а отменённая позже - на дату ещё действует.
    """
    if work.cancelled_at and work.cancelled_at <= as_of:
        return "cancelled"
    if work.completed_at and work.completed_at <= as_of:
        return "done"
    if work.status == "done":
        if work.completed_at is None:
            warnings.append("работа {}: статус done без completed_at - считаем принятой к {}".format(
                work.work_id, fmt_date(as_of)))
            return "done"
        return "done_later"
    if work.status == "cancelled":
        if work.cancelled_at is None:
            warnings.append(
                "работа {}: статус cancelled без cancelled_at - к {} считаем её действующей".format(
                    work.work_id, fmt_date(as_of)))
        else:
            return "planned"
    return "planned"


def payment_is_effective(payment, as_of, warnings):
    """Действует ли платёж на дату: деньги ушли не позже даты и к дате не вернулись."""
    paid_at = payment.paid_at
    if paid_at is None and payment.status == "paid":
        paid_at = payment.created_at
        if paid_at is not None:
            warnings.append(
                "платёж {}: статус paid без paid_at - дату оплаты взяли из created_at".format(
                    payment.payment_id))
    if paid_at is None or paid_at > as_of:
        return False
    if payment.refunded_at is not None and payment.refunded_at <= as_of:
        return False
    return True


def payment_is_queued(payment, as_of):
    """Платёж в очереди на дату: заведён, но денег ещё не ушло."""
    if payment.status != "queued":
        return False
    anchor = payment.created_at or payment.paid_at
    return anchor is not None and anchor <= as_of


def allocation_order(lines, mode):
    """В каком порядке платежи без привязки к работе закрывают работы.

    accepted-first (по умолчанию): сначала принятые на дату работы в порядке
    приёмки, затем принятые позже даты, затем ещё не закрытые - по номеру.
    Так видно, за что уже рассчитались, а аванс ложится на будущие работы.

    spread: пропорционально ценам всех действующих работ.
    """
    if mode == "spread":
        order = [ln for ln in lines if ln.status_at != "cancelled"]
        order.sort(key=lambda ln: ln.work.work_id)
        return order
    buckets = [
        [ln for ln in lines if ln.status_at == "done"],
        [ln for ln in lines if ln.status_at == "done_later"],
        [ln for ln in lines if ln.status_at == "planned"],
    ]
    buckets[0].sort(key=lambda ln: (ln.work.completed_at or date.max, ln.work.work_id))
    buckets[1].sort(key=lambda ln: (ln.work.completed_at or date.max, ln.work.work_id))
    buckets[2].sort(key=lambda ln: ln.work.work_id)
    return buckets[0] + buckets[1] + buckets[2]


def spread(amount, lines):
    """Разложить сумму по работам пропорционально цене, до копейки;
    остаток от округления добираем по крупнейшим остаткам."""
    if amount == ZERO or not lines:
        return [ZERO for _ in lines]
    weights = [ln.price for ln in lines]
    total = sum(weights, ZERO)
    if total <= ZERO:
        return [ZERO for _ in lines]
    raw = [(amount * w / total) for w in weights]
    parts = [money(p) for p in raw]
    diff = money(amount - sum(parts, ZERO))
    if diff != ZERO:
        order = sorted(range(len(lines)), key=lambda i: (-(raw[i] - parts[i]), lines[i].work.work_id))
        step = CENT if diff > ZERO else -CENT
        i = 0
        while diff != ZERO and i < 100000:
            idx = order[i % len(order)]
            if step > ZERO or parts[idx] > ZERO:
                parts[idx] = money(parts[idx] + step)
                diff = money(diff - step)
            i += 1
    return parts


def compute(dataset, as_of, allocation="accepted-first"):
    """Построить отчёт на дату. Возвращает (список ContractResult, предупреждения)."""
    warnings = list(dataset.warnings)
    by_contract = {}
    for work in dataset.works:
        by_contract.setdefault(work.contract_id, []).append(work)

    results = []
    for contract_id in sorted(dataset.contracts):
        contract = dataset.contracts[contract_id]
        works = sorted(by_contract.get(contract_id, []), key=lambda w: w.work_id)
        lines = [WorkLine(work=w, status_at=work_status_at(w, as_of, warnings), price=w.price)
                 for w in works]
        line_by_work = {ln.work.work_id: ln for ln in lines}

        active_total = money(sum((ln.price for ln in lines if ln.status_at != "cancelled"), ZERO))
        cancelled_total = money(sum((ln.price for ln in lines if ln.status_at == "cancelled"), ZERO))
        all_works_total = money(sum((ln.price for ln in lines), ZERO))

        contract_payments = [p for p in dataset.payments if p.contract_id == contract_id]
        effective = [p for p in contract_payments if payment_is_effective(p, as_of, warnings)]
        effective.sort(key=lambda p: (p.paid_at or date.min, p.payment_id))

        queued_total = ZERO
        for payment in contract_payments:
            if payment_is_queued(payment, as_of):
                queued_total = money(queued_total + payment.amount)
                line = line_by_work.get(payment.work_id) if payment.work_id else None
                if line is not None and line.status_at != "cancelled":
                    line.queued = money(line.queued + payment.amount)

        pool = ZERO
        paid_total = ZERO
        paid_on_cancelled = ZERO

        # 1. привязанные платежи идут прямо на свою работу, но не больше её цены
        for payment in effective:
            paid_total = money(paid_total + payment.amount)
            line = line_by_work.get(payment.work_id) if payment.work_id else None
            if line is None:
                pool = money(pool + payment.amount)
                continue
            if line.status_at == "cancelled":
                line.paid = money(line.paid + payment.amount)
                line.attached = money(line.attached + payment.amount)
                line.source.append(payment.payment_id)
                paid_on_cancelled = money(paid_on_cancelled + payment.amount)
                warnings.append(
                    "платёж {} оплатил отменённую работу {} - деньги у подрядчика, "
                    "нужен возврат или зачёт".format(payment.payment_id, payment.work_id))
                continue
            capacity = money(line.price - line.paid)
            take = payment.amount if payment.amount <= capacity else capacity
            if take > ZERO:
                line.paid = money(line.paid + take)
                line.attached = money(line.attached + take)
                line.source.append(payment.payment_id)
            rest = money(payment.amount - take)
            if rest > ZERO:
                pool = money(pool + rest)
                warnings.append(
                    "платёж {} больше цены работы {} на {} - излишек отнесён к общему авансу "
                    "договора".format(payment.payment_id, payment.work_id, fmt_money(rest)))

        # 2. платежи без привязки закрывают работы в выбранном порядке
        open_lines = allocation_order(lines, allocation)
        if allocation == "spread":
            for line, part in zip(open_lines, spread(pool, open_lines)):
                if part != ZERO:
                    line.paid = money(line.paid + part)
                    line.source.append("аванс, разложен по цене")
            pool = ZERO
        else:
            for line in open_lines:
                if pool <= ZERO:
                    break
                capacity = money(line.price - line.paid)
                if capacity <= ZERO:
                    continue
                take = pool if pool <= capacity else capacity
                line.paid = money(line.paid + take)
                line.source.append("аванс")
                pool = money(pool - take)

        results.append(ContractResult(
            contract=contract,
            lines=lines,
            active_total=active_total,
            cancelled_total=cancelled_total,
            all_works_total=all_works_total,
            paid_total=money(paid_total),
            paid_on_cancelled=paid_on_cancelled,
            queued_total=queued_total,
            advance_excess=money(pool),
            allocation=allocation,
        ))

    known = set(dataset.contracts)
    for work in dataset.works:
        if work.contract_id not in known:
            warnings.append("работа {} не в отчёте: договор {} отсутствует в contracts.csv".format(
                work.work_id, work.contract_id))
    for payment in dataset.payments:
        if payment.contract_id not in known:
            warnings.append("платёж {} не в отчёте: договор {} отсутствует в contracts.csv".format(
                payment.payment_id, payment.contract_id))
    return results, warnings


def check(results):
    """Контроль сходимости и расхождения, которые бухгалтеру надо видеть."""
    problems = []
    for res in results:
        cid = res.contract.contract_id
        if money(res.works_paid + res.works_remaining) != money(res.active_total):
            problems.append(
                "договор {}: выплачено по работам {} плюс осталось {} не равно цене работ {}".format(
                    cid, fmt_money(res.works_paid), fmt_money(res.works_remaining),
                    fmt_money(res.active_total)))
        if money(res.works_paid + res.paid_on_cancelled + res.advance_excess) != money(res.paid_total):
            problems.append(
                "договор {}: выплачено {} не разложилось по работам (по работам {}, "
                "по отменённым {}, свободный аванс {})".format(
                    cid, fmt_money(res.paid_total), fmt_money(res.works_paid),
                    fmt_money(res.paid_on_cancelled), fmt_money(res.advance_excess)))
        if res.advance_excess != ZERO:
            problems.append(
                "договор {}: {} оплачено сверх стоимости работ - аванс или переплата".format(
                    cid, fmt_money(res.advance_excess)))
        if res.paid_on_cancelled != ZERO:
            problems.append("договор {}: {} числится за отменёнными работами".format(
                cid, fmt_money(res.paid_on_cancelled)))
        if money(res.paid_total + res.queued_total) > money(res.active_total):
            problems.append(
                "договор {}: оплачено {} плюс в очереди {} больше, чем стоят работы {}".format(
                    cid, fmt_money(res.paid_total), fmt_money(res.queued_total),
                    fmt_money(res.active_total)))
        if res.all_works_total != money(res.contract.amount):
            problems.append(
                "договор {}: сумма работ {} не равна сумме договора {}".format(
                    cid, fmt_money(res.all_works_total), fmt_money(res.contract.amount)))
    return problems


# --------------------------------------------------------------------------
# вывод
# --------------------------------------------------------------------------

def _pad(text, width, right=False):
    text = "" if text is None else str(text)
    gap = width - len(text)
    gap = gap if gap > 0 else 0
    return (" " * gap + text) if right else (text + " " * gap)


def _table(headers, rows, aligns):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    out = [" | ".join(_pad(h, widths[i]) for i, h in enumerate(headers))]
    out.append("-+-".join("-" * w for w in widths))
    for row in rows:
        out.append(" | ".join(_pad(c, widths[i], aligns[i] == "r") for i, c in enumerate(row)))
    return out


def render_text(results, as_of, problems, warnings):
    out = []
    out.append("ОТЧЁТ ОБ ОПЛАТЕ РАБОТ ПО ДОГОВОРАМ")
    out.append("На дату: {}".format(fmt_date(as_of)))
    out.append("Суммы в рублях с копейками. Возвращённые подрядчиком платежи вычтены,")
    out.append("платежи в очереди в «выплачено» не входят.")
    out.append("")

    for res in results:
        c = res.contract
        out.append("Договор {} - {} (от {}, сумма договора {})".format(
            c.contract_id,
            c.contractor or "подрядчик не указан",
            fmt_date(c.signed_at) or "дата не указана",
            fmt_money(c.amount)))
        rows = []
        for ln in res.lines:
            w = ln.work
            note = []
            if ln.status_at == "cancelled":
                note.append("оплате не подлежит")
            if ln.status_at == "done_later":
                note.append("принята {}".format(fmt_date(w.completed_at)))
            if ln.queued != ZERO:
                note.append("в очереди {}".format(fmt_money(ln.queued)))
            rows.append([
                w.work_id,
                w.title,
                STATUS_AT_RU.get(ln.status_at, ln.status_at),
                fmt_money(ln.price),
                fmt_money(ln.paid),
                fmt_money(ln.remaining),
                "; ".join(note),
            ])
        out.extend(_table(
            ["Работа", "Название", "Статус", "Цена", "Выплачено", "Осталось", "Примечание"],
            rows, ["l", "l", "l", "r", "r", "r", "l"]))
        out.append("")
        out.append("  Цена работ (без отменённых):    {}".format(fmt_money(res.active_total)))
        if res.cancelled_total != ZERO:
            out.append("  Отменено работ на:              {}".format(fmt_money(res.cancelled_total)))
        out.append("  Оплачено по договору всего:     {}".format(fmt_money(res.paid_total)))
        out.append("    в т.ч. отнесено на работы:    {}".format(fmt_money(res.works_paid)))
        if res.paid_on_cancelled != ZERO:
            out.append("    оплачено по отменённым:       {}".format(fmt_money(res.paid_on_cancelled)))
        if res.advance_excess != ZERO:
            out.append("    свободный аванс (нет работ):  {}".format(fmt_money(res.advance_excess)))
        if res.queued_total != ZERO:
            out.append("  В очереди на оплату:            {}".format(fmt_money(res.queued_total)))
        out.append("  ОСТАЛОСЬ ОПЛАТИТЬ:              {}".format(fmt_money(res.remaining)))
        out.append("    по принятым на дату работам:  {}".format(fmt_money(res.remaining_accepted)))
        out.append("    по работам, ещё не принятым:  {}".format(
            fmt_money(res.remaining_not_accepted)))
        if res.remaining < ZERO:
            out.append("  ВНИМАНИЕ: выплачено больше стоимости работ на {}".format(
                fmt_money(-res.remaining)))
        out.append("")

    rows = []
    sums = {k: ZERO for k in ("active", "paid", "adv", "remaining", "queued", "accepted")}
    for res in results:
        rows.append([
            res.contract.contract_id,
            res.contract.contractor,
            fmt_money(res.active_total),
            fmt_money(res.paid_total),
            fmt_money(res.advance_excess) if res.advance_excess != ZERO else "—",
            fmt_money(res.remaining),
            fmt_money(res.remaining_accepted),
            fmt_money(res.queued_total) if res.queued_total != ZERO else "—",
        ])
        sums["active"] = money(sums["active"] + res.active_total)
        sums["paid"] = money(sums["paid"] + res.paid_total)
        sums["adv"] = money(sums["adv"] + res.advance_excess)
        sums["remaining"] = money(sums["remaining"] + res.remaining)
        sums["accepted"] = money(sums["accepted"] + res.remaining_accepted)
        sums["queued"] = money(sums["queued"] + res.queued_total)
    rows.append([
        "ИТОГО", "",
        fmt_money(sums["active"]),
        fmt_money(sums["paid"]),
        fmt_money(sums["adv"]) if sums["adv"] != ZERO else "—",
        fmt_money(sums["remaining"]),
        fmt_money(sums["accepted"]),
        fmt_money(sums["queued"]) if sums["queued"] != ZERO else "—",
    ])
    out.append("ИТОГИ ПО ДОГОВОРАМ")
    out.extend(_table(
        ["Договор", "Подрядчик", "Цена работ", "Оплачено", "Аванс",
         "Осталось", "Ост. по принятым", "В очереди"],
        rows, ["l", "l", "r", "r", "r", "r", "r", "r"]))

    if problems:
        out.append("")
        out.append("ЧТО ТРЕБУЕТ ВНИМАНИЯ")
        for p in problems:
            out.append("  ! " + p)
    if warnings:
        out.append("")
        out.append("ДОПУЩЕНИЯ И ПРЕДУПРЕЖДЕНИЯ")
        for w in warnings:
            out.append("  - " + w)
    out.append("")
    return "\n".join(out)


def report_dict(results, as_of, problems, warnings, allocation):
    contracts = []
    for res in results:
        contracts.append({
            "contract_id": res.contract.contract_id,
            "contractor": res.contract.contractor,
            "contract_amount": str(money(res.contract.amount)),
            "signed_at": fmt_date(res.contract.signed_at),
            "works_active_total": str(money(res.active_total)),
            "works_cancelled_total": str(money(res.cancelled_total)),
            "works_paid": str(money(res.works_paid)),
            "unallocated_advance": str(money(res.advance_excess)),
            "paid_on_cancelled": str(money(res.paid_on_cancelled)),
            "paid_total": str(money(res.paid_total)),
            "remaining": str(money(res.remaining)),
            "remaining_accepted": str(money(res.remaining_accepted)),
            "remaining_not_accepted": str(money(res.remaining_not_accepted)),
            "queued": str(money(res.queued_total)),
            "works": [{
                "work_id": ln.work.work_id,
                "title": ln.work.title,
                "status_export": ln.work.status,
                "status_at_date": ln.status_at,
                "completed_at": fmt_date(ln.work.completed_at),
                "cancelled_at": fmt_date(ln.work.cancelled_at),
                "price": str(money(ln.price)),
                "paid": str(money(ln.paid)),
                "remaining": str(money(ln.remaining)),
                "queued": str(money(ln.queued)),
                "paid_by": list(ln.source),
            } for ln in res.lines],
        })
    totals = {
        "works_active_total": money(sum((r.active_total for r in results), ZERO)),
        "paid_total": money(sum((r.paid_total for r in results), ZERO)),
        "unallocated_advance": money(sum((r.advance_excess for r in results), ZERO)),
        "remaining": money(sum((r.remaining for r in results), ZERO)),
        "remaining_accepted": money(sum((r.remaining_accepted for r in results), ZERO)),
        "queued": money(sum((r.queued_total for r in results), ZERO)),
    }
    return {
        "as_of": as_of.isoformat(),
        "as_of_human": fmt_date(as_of),
        "allocation": allocation,
        "currency": "RUB",
        "totals": {k: str(v) for k, v in totals.items()},
        "contracts": contracts,
        "problems": problems,
        "warnings": warnings,
    }


def write_csv(outdir, results, as_of):
    outdir.mkdir(parents=True, exist_ok=True)
    works_path = outdir / "report_works.csv"
    contracts_path = outdir / "report_contracts.csv"

    with works_path.open("w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.writer(fh, delimiter=";")
        wr.writerow(["contract_id", "work_id", "title", "status_export", "status_at_date",
                     "completed_at", "cancelled_at", "price", "paid", "remaining",
                     "queued", "paid_by"])
        for res in results:
            for ln in res.lines:
                wr.writerow([
                    res.contract.contract_id, ln.work.work_id, ln.work.title,
                    ln.work.status, ln.status_at,
                    fmt_date(ln.work.completed_at), fmt_date(ln.work.cancelled_at),
                    str(money(ln.price)), str(money(ln.paid)), str(money(ln.remaining)),
                    str(money(ln.queued)), " ".join(ln.source),
                ])

    with contracts_path.open("w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.writer(fh, delimiter=";")
        wr.writerow(["contract_id", "contractor", "contract_amount", "signed_at",
                     "works_active_total", "works_cancelled_total", "works_paid",
                     "unallocated_advance", "paid_on_cancelled", "paid_total",
                     "remaining", "remaining_accepted", "remaining_not_accepted",
                     "queued", "as_of"])
        for res in results:
            wr.writerow([
                res.contract.contract_id, res.contract.contractor,
                str(money(res.contract.amount)), fmt_date(res.contract.signed_at),
                str(money(res.active_total)), str(money(res.cancelled_total)),
                str(money(res.works_paid)), str(money(res.advance_excess)),
                str(money(res.paid_on_cancelled)), str(money(res.paid_total)),
                str(money(res.remaining)), str(money(res.remaining_accepted)),
                str(money(res.remaining_not_accepted)), str(money(res.queued_total)),
                as_of.isoformat(),
            ])
    return works_path, contracts_path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_report(data_dir, as_of, allocation="accepted-first"):
    """Посчитать отчёт без вывода: удобно для тестов и импорта."""
    dataset = load_dataset(Path(data_dir))
    results, warnings = compute(dataset, as_of, allocation)
    problems = check(results)
    return results, warnings, problems


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Отчёт об оплате работ по договорам на дату: выплачено и осталось.")
    ap.add_argument("--data", default="data",
                    help="папка с contracts.csv, works.csv, payments.csv (по умолчанию data)")
    ap.add_argument("--date", default=None,
                    help="дата отчёта, ГГГГ-ММ-ДД (по умолчанию сегодня)")
    ap.add_argument("--format", default="text", choices=["text", "csv", "json", "all"],
                    help="text (по умолчанию), csv, json или all")
    ap.add_argument("--out", default="out",
                    help="папка для CSV при --format csv/all (по умолчанию out)")
    ap.add_argument("--out-file", default=None,
                    help="дополнительно записать текстовый отчёт в этот файл (UTF-8), "
                         "удобно на Windows, где консоль ломает кириллицу")
    ap.add_argument("--allocation", default="accepted-first",
                    choices=["accepted-first", "spread"],
                    help="как распределять платежи без привязки к работе")
    ap.add_argument("--strict", action="store_true",
                    help="код возврата 1, если есть предупреждения по данным")
    args = ap.parse_args(argv)

    data_dir = Path(args.data)

    def flush(text):
        # Windows-консоль может не уметь UTF-8: не падаем, а пишем, что сможем
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or "cp1251"
            sys.stdout.write(text.encode(enc, "replace").decode(enc, "replace"))
        sys.stdout.flush()

    for name in ("contracts.csv", "works.csv", "payments.csv"):
        if not (data_dir / name).exists():
            sys.stderr.write("нет файла: {}\n".format(data_dir / name))
            return 2

    try:
        as_of = parse_date(args.date) if args.date else date.today()
        results, warnings, problems = build_report(data_dir, as_of, args.allocation)
    except ValueError as exc:
        sys.stderr.write("{}\n".format(exc))
        return 2

    text_output = None
    if args.format in ("text", "all"):
        text_output = render_text(results, as_of, problems, warnings)
        flush(text_output if text_output.endswith("\n") else text_output + "\n")
        if args.out_file:
            Path(args.out_file).write_text(text_output, encoding="utf-8")
            flush("записано: {}\n".format(args.out_file))
    if args.format in ("csv", "all"):
        works_path, contracts_path = write_csv(Path(args.out), results, as_of)
        if args.format == "all":
            flush("\n")
        flush("записано: {}\n          {}\n".format(works_path, contracts_path))
    if args.format == "json":
        payload = report_dict(results, as_of, problems, warnings, args.allocation)
        flush(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    if warnings:
        sys.stderr.write("предупреждений: {}\n".format(len(warnings)))
    if problems:
        sys.stderr.write("вопросов к данным: {}\n".format(len(problems)))
        return 1
    if args.strict and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
